"""Backup/restore driver for Home Assistant (#155), over its own backup API
(homeassistant/components/backup, checked against 2026.11.1). Nothing is
mounted: Home Assistant makes the backup, Backuparr downloads it and then
deletes the copy it made there.

Every backup command is admin-only, so the card takes a long-lived access
token of an administrator (Profile > Security). The WebSocket API
(/api/websocket) does the work:

- backup/agents/info names the local backup location: hassio.local on HA OS
  and Supervised installs, backup.local on Container and Core. Which one
  answers is the install type Test connection reports.
- backup/generate to that location makes the backup with the card's options
  and no password (Backuparr encrypts it itself). It returns at once;
  backup/subscribe_events says when it's finished or failed. A Supervisor
  backup's id isn't known until then, so Backuparr names its backup uniquely
  and finds it in backup/info by that name.
- GET /api/backup/download/<id>?agent_id=... downloads it (a plain tar
  holding backup.json and homeassistant.tar.gz, plus one tar per app and
  folder on HA OS), and backup/delete removes Backuparr's copy. Backups the
  user made are never touched, and Home Assistant's own "keep N" count only
  counts its automatic backups, so Backuparr's never pushes one out.

The options mirror Home Assistant's own backup page: Settings (always;
with the ssl folder on HA OS), History (the recorder database, on by
default), and on HA OS and Supervised only, Media, Share folder, Local apps
folder and Apps (none, all or chosen ones, listed by the Supervisor's
/addons over the WebSocket's supervisor/api).

RESTORE: POST /api/backup/upload?agent_id=... (multipart), then
backup/restore with whatever the backup holds. Container and Core restore
settings and history only, so apps and folders are left out there.
Home Assistant restarts to finish: on Core it unpacks the backup before it
starts again and reports the result in backup/info's last_action_event; on
HA OS the Supervisor restores and reports it the same way. The restore
replaces .storage/auth, so a token made after the backup stops working - a
refused token after the restart means it was restored. The uploaded copy
stays in Home Assistant's backups folder, so it's deleted afterwards.

A new install that hasn't been onboarded has no token to use. Its
onboarding API takes a backup without one (/api/onboarding/backup/upload,
/restore and /info, refused once onboarding has started), so a restore onto
it works too - like Home Assistant's own "Restore from backup" first screen.

The version recorded with a backup is the one Home Assistant names in its
WebSocket greeting (auth_required's ha_version), before any login, so it
needs no permission at all.

A self-signed HTTPS certificate is trusted on the card and pinned
(apps/cert_pin.py). The WebSocket runs over a socket that check made, so
nothing - not a request header - reaches a host with another certificate.
"""
import json
import logging
import os
import re
import secrets
import socket
import ssl
import tarfile
import time
from urllib.parse import urlsplit

import requests
import websocket

from apps import cert_pin
from apps.base import AppError, poll_until, save_stream
from apps.multipart import MultipartFile
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

BACKUP_FILE = "homeassistant.tar"
CORE_AGENT = "backup.local"
SUPERVISOR_AGENT = "hassio.local"
INSTALL_CORE = "core"
INSTALL_SUPERVISOR = "supervisor"
INSTALL_TYPES = (INSTALL_CORE, INSTALL_SUPERVISOR)
# The card's folder options and the Home Assistant folder each one backs up.
FOLDER_OPTIONS = {"media": "media", "share": "share", "local_apps": "addons/local"}
FOLDERS = ("ssl", *FOLDER_OPTIONS.values())
APPS_CHOICES = ("none", "all", "chosen")
MAX_CHOSEN_APPS = 100
_SLUG = re.compile(r"[a-z0-9_]{1,64}")
_TOKEN_WHERE = "make a long-lived access token on your profile's Security tab in Home Assistant"
# Generous on purpose: Home Assistant often runs on a Raspberry Pi's SD card
# or a hard disk, many times slower than the NVMe these were measured on
# (a 1 GB history database: backup 23 s, restore 22 s).
_CONNECT_S = 30
_COMMAND_S = 120
_BACKUP_TIMEOUT_S = 60 * 60  # media and apps can be many GB
_UPLOAD_TIMEOUT_S = 60 * 60
# A restore: Home Assistant goes down once it's ready to (at once on Core;
# after the apps and folders on HA OS), and is back once it's unpacked.
_RESTART_S = {INSTALL_CORE: 20 * 60, INSTALL_SUPERVISOR: 60 * 60}
_BACK_S = 60 * 60  # unpacking a large history database on a slow disk, then starting
_RESULT_S = 20 * 60  # back, but its backup integration not loaded yet
_HTTP_S = 120
_DATA_DB = re.compile(r"(?:\./)?data/[^/]+\.db")
_MIB = 1024 * 1024


class HomeAssistantError(AppError):
    pass


class _CommandRefused(HomeAssistantError):
    """A WebSocket command Home Assistant refused: code is its error code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(f"homeassistant: {message}")


class _TokenRefused(HomeAssistantError):
    """Home Assistant refused the card's token."""


class _StartedWithoutBackup(HomeAssistantError):
    """Home Assistant is running again, but its backup integration isn't."""


class _Closed(Exception):
    """The WebSocket closed - Home Assistant stopped or restarted."""


def _members(tar):
    """A tar's files by their name without a leading "./" (Home Assistant
    writes both)."""
    return {os.path.normpath(m.name): m for m in tar.getmembers() if m.isfile()}


def read_backup_json(path):
    """The backup.json of a Home Assistant backup at path, or raises
    HomeAssistantError."""
    try:
        with tarfile.open(path, "r:") as tar:
            member = _members(tar).get("backup.json")
            if member is None:
                raise HomeAssistantError("homeassistant: this isn't a Home Assistant backup (no backup.json in it)")
            meta = json.load(tar.extractfile(member))
    except (OSError, tarfile.TarError, EOFError):
        raise HomeAssistantError("homeassistant: the backup can't be opened as a Home Assistant backup") from None
    except ValueError:
        raise HomeAssistantError("homeassistant: the backup's backup.json isn't valid JSON") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("homeassistant"), dict):
        raise HomeAssistantError("homeassistant: this backup doesn't hold Home Assistant's settings")
    return meta


def backup_version(meta):
    version = meta["homeassistant"].get("version")
    return version if isinstance(version, str) and version else None


# backup.json's folder names that Home Assistant's API calls differently: the
# Supervisor (2026.11) writes "apps/local" for the folder its API takes as
# "addons/local".
_FOLDER_ALIASES = {"apps/local": "addons/local"}


def backup_contents(meta):
    """What a backup holds: {"database": bool, "folders": [...], "addons": [slugs]},
    the folders as Home Assistant's restore takes them."""
    folders = [_FOLDER_ALIASES.get(f, f) for f in meta.get("folders") or []]
    folders = [f for f in dict.fromkeys(folders) if f in FOLDERS]
    addons = [a.get("slug") for a in meta.get("addons") or [] if isinstance(a, dict)]
    return {"database": not meta["homeassistant"].get("exclude_database", False), "folders": folders,
            "addons": [slug for slug in addons if isinstance(slug, str) and _SLUG.fullmatch(slug)],
            "from_supervisor": "supervisor_version" in meta}


def _check_inner(fileobj, gzip, on_database=None):
    """Reads every file in an inner tar to its end, so a damaged one (a bad
    gzip CRC, a cut-short stream) raises. on_database(fileobj, name), if
    given, reads each SQLite database in Home Assistant's config folder
    instead and returns a problem or None."""
    with tarfile.open(fileobj=fileobj, mode="r|gz" if gzip else "r|") as inner:
        for member in inner:
            if not member.isfile():
                continue
            f = inner.extractfile(member)
            if on_database and _DATA_DB.fullmatch(member.name):
                problem = on_database(f, member.name)
                if problem:
                    raise HomeAssistantError(f"homeassistant: {member.name} failed SQLite's quick check ({problem})")
            while f.read(_MIB):
                pass


def check_backup(path, quick_check=None):
    """Raises HomeAssistantError unless path is a whole, unprotected Home
    Assistant backup: backup.json says what it holds, Home Assistant's
    settings are in it, and every inner tar reads to its end.
    quick_check(fileobj, name), if given, checks each SQLite database in the
    settings (the history database among them) and returns a problem or None."""
    meta = read_backup_json(path)
    if meta.get("protected"):
        raise HomeAssistantError("homeassistant: this backup has a password - Backuparr can't check or restore it")
    if not backup_version(meta):
        raise HomeAssistantError("homeassistant: this backup doesn't say which Home Assistant made it")
    compressed = meta.get("compressed", True) is not False
    settings = f"homeassistant.tar{'.gz' if compressed else ''}"
    try:
        with tarfile.open(path, "r:") as tar:
            members = _members(tar)
            if settings not in members:
                raise HomeAssistantError(f"homeassistant: {settings} is missing from the backup")
            for name, member in members.items():
                if name.endswith((".tar", ".tar.gz")):
                    _check_inner(tar.extractfile(member), name.endswith(".gz"),
                                 quick_check if name == settings else None)
    except HomeAssistantError:
        raise
    except (OSError, tarfile.TarError, EOFError, ValueError) as exc:
        raise HomeAssistantError(f"homeassistant: the backup is damaged ({exc or type(exc).__name__})") from None


class _Connection:
    """One WebSocket connection to Home Assistant: commands by id, and the
    events that arrive between their results."""

    def __init__(self, ws, version):
        self.ws = ws
        self.version = version
        self.events = []
        self._next = 0

    def send(self, kind, **fields):
        self._next += 1
        self.ws.send(json.dumps({"id": self._next, "type": kind, **fields}))
        return self._next

    def recv(self, timeout):
        """The next message, None after timeout seconds of nothing. Raises
        _Closed once the connection is closed."""
        self.ws.settimeout(timeout)
        try:
            text = self.ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except (websocket.WebSocketConnectionClosedException, ConnectionError, ssl.SSLError, OSError):
            raise _Closed() from None
        if not text:
            raise _Closed()
        try:
            message = json.loads(text)
        except ValueError:
            raise HomeAssistantError("homeassistant: the WebSocket answered with something other than JSON") from None
        if not isinstance(message, dict):
            raise HomeAssistantError("homeassistant: the WebSocket answered unexpectedly")
        return message

    def result(self, msg_id, timeout=_COMMAND_S):
        deadline = time.monotonic() + timeout
        while True:
            message = self.recv(max(0.1, deadline - time.monotonic()))
            if message is None:
                if time.monotonic() >= deadline:
                    raise HomeAssistantError("homeassistant: Home Assistant didn't answer in time")
                continue
            if message.get("type") == "event":
                self.events.append(message)
                continue
            if message.get("id") != msg_id or message.get("type") != "result":
                continue
            if message.get("success"):
                return message.get("result")
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            raise _CommandRefused(str(error.get("code") or "unknown_error"), str(error.get("message") or "the command failed"))

    def call(self, kind, timeout=_COMMAND_S, **fields):
        return self.result(self.send(kind, **fields), timeout)

    def next_event(self, msg_id, timeout):
        """The next event of subscription msg_id, or None after timeout."""
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self.events):
                if message.get("id") == msg_id:
                    del self.events[index]
                    event = message.get("event")
                    return event if isinstance(event, dict) else {}
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            message = self.recv(left)
            if message is not None and message.get("type") == "event":
                self.events.append(message)

    def close(self):
        try:
            self.ws.close(timeout=1)
        except (websocket.WebSocketException, OSError):
            pass


class HomeAssistantApp:
    def __init__(self, url, api_key, history=None, media=None, share=None, local_apps=None, apps=None,
                 chosen_apps=None, install_type=None, trusted_cert=None, timeout=_HTTP_S):
        self.url = url.rstrip("/")
        self.token = api_key
        self.timeout = timeout
        self.history = history is not False  # on unless turned off
        self.folders = [folder for option, folder in FOLDER_OPTIONS.items()
                        if {"media": media, "share": share, "local_apps": local_apps}[option] is True]
        self.apps = apps if apps in APPS_CHOICES else "none"
        self.chosen_apps = [a["slug"] for a in chosen_apps or [] if isinstance(a, dict) and _SLUG.fullmatch(str(a.get("slug")))]
        self.trusted_cert = trusted_cert or None
        self.session = SameHostSession(self.url, strict=True)
        self.session.headers.update({"Accept": "application/json"})
        cert_pin.pin(self.session, "homeassistant", self.trusted_cert)
        self.request_headers = {}  # configure_app_headers' rows, for the WebSocket too
        self.card_info = None  # what Test connection found, for the card

    def set_request_headers(self, headers):
        """The global request headers the session sends (apps/http_headers);
        the WebSocket's upgrade request gets them too."""
        self.request_headers = dict(headers)

    # ---------------------------------------------------------- websocket --
    def _ws_url(self):
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HomeAssistantError("homeassistant: that doesn't look like a valid URL - it should start with http:// or https://")
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}/api/websocket"

    def _where(self):
        parts = urlsplit(self.url)
        return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)

    def _unreachable(self, exc):
        if isinstance(exc, socket.gaierror):
            return HomeAssistantError(f"homeassistant: couldn't resolve hostname '{urlsplit(self.url).hostname}' - "
                                      "check the URL and that this container can look it up")
        if isinstance(exc, ConnectionRefusedError):
            return HomeAssistantError(f"homeassistant: couldn't connect - connection refused by {self._where()}; "
                                      "is Home Assistant running, and is the port right?")
        if isinstance(exc, (socket.timeout, TimeoutError, websocket.WebSocketTimeoutException)):
            return HomeAssistantError(f"homeassistant: couldn't connect - timed out waiting for {self._where()}")
        if isinstance(exc, ssl.SSLError):
            return HomeAssistantError(f"homeassistant: TLS/SSL error talking to {self._where()} - check http:// vs https://")
        return HomeAssistantError("homeassistant: couldn't connect - check the URL and that it's reachable from this container")

    def _open(self):
        """A WebSocket to Home Assistant that has greeted us, before any login.
        Returns (ws, version)."""
        url = self._ws_url()
        try:
            sock = cert_pin.connect_socket(self.url, "homeassistant", self.trusted_cert, _CONNECT_S)
        except cert_pin.CertificateUntrusted:
            raise
        except (OSError, ssl.SSLError) as exc:
            raise self._unreachable(exc) from None
        try:
            ws = websocket.create_connection(
                url, socket=sock, timeout=_CONNECT_S, redirect_limit=0, suppress_origin=True,
                header=[f"{name}: {value}" for name, value in self.request_headers.items()])
        except websocket.WebSocketBadStatusException as exc:
            sock.close()
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                raise HomeAssistantError(f"homeassistant: {self._where()} refused the WebSocket (HTTP {status}) - "
                                         "check any proxy login in front of Home Assistant") from None
            raise HomeAssistantError("homeassistant: no Home Assistant WebSocket API at this URL - check it "
                                     "(e.g. http://homeassistant:8123)") from None
        except websocket.WebSocketException as exc:
            sock.close()
            if "Redirect" in str(exc):
                raise HomeAssistantError("homeassistant: the URL redirects - use Home Assistant's final address") from None
            raise HomeAssistantError("homeassistant: no Home Assistant WebSocket API at this URL - check it "
                                     "(e.g. http://homeassistant:8123)") from None
        except (OSError, ssl.SSLError) as exc:
            sock.close()
            raise self._unreachable(exc) from None
        connection = _Connection(ws, None)
        try:
            greeting = connection.recv(_CONNECT_S)
        except _Closed:
            greeting = None
        if not greeting or greeting.get("type") != "auth_required":
            connection.close()
            raise HomeAssistantError("homeassistant: no Home Assistant WebSocket API at this URL - check it "
                                     "(e.g. http://homeassistant:8123)")
        connection.version = str(greeting.get("ha_version") or "") or None
        return connection

    def _connect(self):
        """A logged-in WebSocket connection. Raises HomeAssistantError for a
        refused token."""
        connection = self._open()
        try:
            connection.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            answer = connection.recv(_CONNECT_S)
        except _Closed:
            answer = None
        except BaseException:
            connection.close()
            raise
        if not answer or answer.get("type") != "auth_ok":
            connection.close()
            if answer and answer.get("type") == "auth_invalid":
                raise _TokenRefused(f"homeassistant: the token was refused - {_TOKEN_WHERE}")
            raise HomeAssistantError("homeassistant: Home Assistant closed the connection while logging in")
        return connection

    def _admin(self, connection, kind, **fields):
        try:
            return connection.call(kind, **fields)
        except _CommandRefused as exc:
            if exc.code == "unauthorized":
                raise HomeAssistantError("homeassistant: the token must belong to an administrator") from None
            if exc.code == "unknown_command":
                raise HomeAssistantError("homeassistant: this Home Assistant has no backup API yet - update it to "
                                         "2025.1 or newer (or wait until it has finished starting)") from None
            raise

    def _subscribe(self, connection):
        """Subscribes to backup events; returns the subscription's id."""
        subscription = connection.send("backup/subscribe_events")
        try:
            connection.result(subscription)
        except _CommandRefused as exc:
            if exc.code == "unauthorized":
                raise HomeAssistantError("homeassistant: the token must belong to an administrator") from None
            raise
        return subscription

    @staticmethod
    def _install(agents):
        ids = {a.get("agent_id") for a in agents if isinstance(a, dict)}
        if SUPERVISOR_AGENT in ids:
            return INSTALL_SUPERVISOR, SUPERVISOR_AGENT
        if CORE_AGENT in ids:
            return INSTALL_CORE, CORE_AGENT
        raise HomeAssistantError("homeassistant: Home Assistant has no local backup location (backup.local or hassio.local)")

    def _agents(self, connection):
        body = self._admin(connection, "backup/agents/info")
        agents = body.get("agents") if isinstance(body, dict) else None
        if not isinstance(agents, list):
            raise HomeAssistantError("homeassistant: the list of backup locations didn't come back as expected")
        return self._install(agents)

    def _installed_apps(self, connection):
        """The Supervisor's installed apps, [{"slug", "name"}], by name."""
        body = self._admin(connection, "supervisor/api", endpoint="/addons", method="get")
        addons = body.get("addons") if isinstance(body, dict) else None
        apps = [{"slug": a["slug"], "name": str(a.get("name") or a["slug"])[:100]}
                for a in addons or [] if isinstance(a, dict) and _SLUG.fullmatch(str(a.get("slug")))]
        return sorted(apps, key=lambda a: a["name"].lower())

    # ------------------------------------------------------------ version --
    def app_version(self):
        """Home Assistant's version, e.g. "2026.11.1", from its WebSocket
        greeting - no login needed."""
        connection = self._open()
        connection.close()
        if not connection.version:
            raise HomeAssistantError("homeassistant: Home Assistant didn't say its version")
        return connection.version

    def test_connection(self):
        connection = self._connect()
        try:
            install, _agent = self._agents(connection)
            apps = self._installed_apps(connection) if install == INSTALL_SUPERVISOR else []
            backups = self._admin(connection, "backup/info").get("backups") or []
        finally:
            connection.close()
        self.card_info = {"install": install, "apps": apps}
        where = "HA OS or Supervised" if install == INSTALL_SUPERVISOR else "Container or Core"
        count = len(backups)
        return (f"Home Assistant {connection.version or '?'} reachable ({where}), the token can back up and restore, "
                f"{count} backup{'' if count == 1 else 's'} in Home Assistant")

    # ------------------------------------------------------------- backup --
    def _generate_options(self, connection, install, agent):
        options = {"agent_ids": [agent], "include_homeassistant": True, "include_database": self.history}
        if install != INSTALL_SUPERVISOR:
            if self.folders or self.apps != "none":
                logger.warning("homeassistant: Media, Share folder, Local apps folder and Apps are only on HA OS and "
                               "Supervised installs - this one backs up settings%s only",
                               " and history" if self.history else "")
            return options
        options["include_folders"] = ["ssl", *self.folders]  # Settings holds the ssl folder on HA OS
        if self.apps == "all":
            options["include_all_addons"] = True
        elif self.apps == "chosen":
            installed = {a["slug"] for a in self._installed_apps(connection)}
            missing = [slug for slug in self.chosen_apps if slug not in installed]
            if missing:
                logger.warning("homeassistant: %s %s no longer installed - left out", ", ".join(missing),
                               "is" if len(missing) == 1 else "are")
            chosen = [slug for slug in self.chosen_apps if slug in installed]
            if chosen:
                options["include_addons"] = chosen
        return options

    def _wait_for_backup(self, connection, subscription):
        """Follows backup/subscribe_events until the backup completes. The
        generate call was sent after the subscription, so only events after
        an in_progress one are ours."""
        started, last_stage, last_log = False, None, time.monotonic()
        deadline = time.monotonic() + _BACKUP_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                event = connection.next_event(subscription, 10)
            except _Closed:
                raise HomeAssistantError("homeassistant: Home Assistant closed the connection during the backup - "
                                         "it may have restarted") from None
            if event is None:
                if time.monotonic() - last_log >= 30:
                    last_log = time.monotonic()
                    logger.info("homeassistant: waiting for Home Assistant's backup%s...",
                                f" ({last_stage.replace('_', ' ')})" if last_stage else "")
                continue
            if event.get("manager_state") != "create_backup":
                continue
            state = event.get("state")
            if state == "in_progress":
                started = True
                if event.get("stage") and event["stage"] != last_stage:
                    last_stage = event["stage"]
                    logger.info("homeassistant: backing up - %s", last_stage.replace("_", " "))
            elif started and state == "completed":
                return
            elif started and state == "failed":
                reason = str(event.get("reason") or "unknown_error").replace("_", " ")
                raise HomeAssistantError(f"homeassistant: Home Assistant's backup failed ({reason}) - its log says why")
        raise HomeAssistantError("homeassistant: Home Assistant's backup didn't finish within an hour")

    def _find_backup(self, connection, name, agent):
        for backup in self._admin(connection, "backup/info").get("backups") or []:
            if isinstance(backup, dict) and backup.get("name") == name:
                if agent not in (backup.get("agents") or {}):
                    raise HomeAssistantError("homeassistant: Home Assistant made the backup but didn't keep it in its "
                                             "local backup location - its log may say why")
                for kind, label in (("failed_addons", "apps"), ("failed_folders", "folders")):
                    failed = backup.get(kind) or []
                    if failed:
                        logger.warning("homeassistant: Home Assistant couldn't back up these %s: %s", label,
                                       ", ".join(str(f.get("slug") if isinstance(f, dict) else f) for f in failed))
                return backup["backup_id"]
        raise HomeAssistantError("homeassistant: Home Assistant finished the backup but it isn't in its backup list")

    def _get(self, path, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})}
        kwargs.setdefault("timeout", self.timeout)
        res = self.session.get(f"{self.url}{path}", headers=headers, allow_redirects=False, **kwargs)
        if res.status_code == 401:
            raise HomeAssistantError(f"homeassistant: the token was refused - {_TOKEN_WHERE}")
        if 300 <= res.status_code < 400:
            raise HomeAssistantError("homeassistant: the URL redirects - use Home Assistant's final address")
        res.raise_for_status()
        return res

    def _listed(self, connection, backup_id):
        backups = self._admin(connection, "backup/info").get("backups") or []
        return any(isinstance(b, dict) and b.get("backup_id") == backup_id for b in backups)

    def _delete(self, backup_id, after_restart=False):
        """Deletes Backuparr's copy in Home Assistant, and checks it's gone; a
        failure is logged, as the backup or restore itself worked. Returns
        whether it went. After a restart Home Assistant lists its backups a
        little later than it answers, and deleting one it hasn't listed yet
        does nothing, so it waits for the copy to be listed first."""
        try:
            connection = self._connect()
            try:
                if after_restart:
                    listed = poll_until(lambda: True if self._listed(connection, backup_id) else None, _RESULT_S, 2)
                    if not listed:
                        raise HomeAssistantError("homeassistant: Home Assistant didn't list the uploaded copy")
                body = self._admin(connection, "backup/delete", backup_id=backup_id)
                errors = (body or {}).get("agent_errors") or {}
                if errors:
                    raise HomeAssistantError(f"homeassistant: {'; '.join(str(e) for e in errors.values())[:200]}")
                if self._listed(connection, backup_id):
                    raise HomeAssistantError("homeassistant: it's still listed after the delete")
            finally:
                connection.close()
            logger.info("homeassistant: deleted Backuparr's copy (%s) from Home Assistant", backup_id)
            return True
        except (HomeAssistantError, OSError) as exc:
            logger.warning("homeassistant: couldn't delete Backuparr's copy (%s) from Home Assistant (%s) - delete it "
                           "under Settings > System > Backups", backup_id, str(exc).removeprefix("homeassistant: "))
            return False

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        name = f"Backuparr {secrets.token_hex(4)}"
        connection = self._connect()
        backup_id = None
        try:
            install, agent = self._agents(connection)
            options = self._generate_options(connection, install, agent)
            subscription = self._subscribe(connection)
            logger.info("homeassistant: starting Home Assistant's backup...")
            try:
                connection.call("backup/generate", name=name, **options)
            except _CommandRefused as exc:
                if "busy" in str(exc).lower():
                    raise HomeAssistantError("homeassistant: Home Assistant is already busy with a backup or restore - "
                                             "try again when it's done") from None
                raise HomeAssistantError(f"homeassistant: Home Assistant refused to start the backup "
                                         f"({str(exc).removeprefix('homeassistant: ')})") from None
            self._wait_for_backup(connection, subscription)
            backup_id = self._find_backup(connection, name, agent)
        finally:
            connection.close()
        try:
            path = os.path.join(dest_dir, BACKUP_FILE)
            with self._get(f"/api/backup/download/{backup_id}", params={"agent_id": agent}, stream=True) as res:
                save_stream(res, path)
            logger.info("homeassistant: downloaded the backup (%d bytes)", os.path.getsize(path))
        finally:
            self._delete(backup_id)
        return path

    # ------------------------------------------------------------ restore --
    def _onboarding(self):
        """True for a new install nobody has onboarded yet (no step done).
        Once onboarded, Home Assistant answers 404 here after its next start,
        and the onboarding list until then."""
        try:
            res = self.session.get(f"{self.url}/api/onboarding", timeout=self.timeout, allow_redirects=False)
            steps = res.json() if res.status_code == 200 else None
        except ValueError:
            steps = None
        return isinstance(steps, list) and bool(steps) and not any(
            isinstance(step, dict) and step.get("done") for step in steps)

    def _onboarding_install(self):
        res = self.session.get(f"{self.url}/api/onboarding/installation_type", timeout=_COMMAND_S, allow_redirects=False)
        try:
            kind = res.json().get("installation_type") if res.ok else None
        except (ValueError, AttributeError):
            kind = None
        if not isinstance(kind, str):
            raise HomeAssistantError("homeassistant: Home Assistant didn't say its install type")
        return INSTALL_SUPERVISOR if kind in ("Home Assistant OS", "Home Assistant Supervised") else INSTALL_CORE

    def _upload(self, path, agent, fresh):
        body = MultipartFile(path, "file", BACKUP_FILE, "application/x-tar")
        where = "/api/onboarding/backup/upload" if fresh else "/api/backup/upload"
        headers = {"Content-Type": body.content_type}
        if not fresh:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            res = self.session.post(f"{self.url}{where}", params={"agent_id": agent}, data=body, headers=headers,
                                    timeout=_UPLOAD_TIMEOUT_S, allow_redirects=False)
        finally:
            body.close()
        if res.status_code == 401:
            raise HomeAssistantError(f"homeassistant: the token was refused - {_TOKEN_WHERE}")
        if res.status_code >= 400:
            detail = res.text.strip()[:200] if "text/plain" in res.headers.get("Content-Type", "") else ""
            raise HomeAssistantError(f"homeassistant: Home Assistant refused the backup upload "
                                     f"({detail or f'HTTP {res.status_code}'})")
        try:
            backup_id = res.json()["backup_id"]
        except (ValueError, KeyError, TypeError):
            raise HomeAssistantError("homeassistant: the upload didn't come back as expected") from None
        if not isinstance(backup_id, str) or not backup_id:
            raise HomeAssistantError("homeassistant: the upload didn't come back as expected")
        return backup_id

    @staticmethod
    def _refuse_onto_core(contents, install):
        """A backup from HA OS or Supervised doesn't restore onto Container or
        Core: Home Assistant starts on the backup's settings, on HA OS's port
        (80) and with its Supervisor integration, which fails there and takes
        the backup integration down with it. (Container to HA OS is the move
        Home Assistant supports.)"""
        if contents["from_supervisor"] and install == INSTALL_CORE:
            raise HomeAssistantError("homeassistant: this backup is from HA OS or Supervised, and Home Assistant can't "
                                     "restore it onto a Container or Core install - it would start on HA OS's settings "
                                     "and port. Restore it onto HA OS")

    @staticmethod
    def _restore_options(contents, install):
        options = {"restore_database": contents["database"], "restore_homeassistant": True}
        if install == INSTALL_SUPERVISOR:  # Container and Core restore settings and history only
            options["restore_folders"] = contents["folders"]
            options["restore_addons"] = contents["addons"]
        return options

    def _wait_for_restart(self, connection, subscription, install):
        """Waits for the connection to close - Home Assistant stopping to
        finish the restore - logging the restore's stages until then."""
        deadline = time.monotonic() + _RESTART_S[install]
        last_log = time.monotonic()
        while time.monotonic() < deadline:
            try:
                event = connection.next_event(subscription, 10)
            except _Closed:
                return
            if event is None:
                if time.monotonic() - last_log >= 30:
                    last_log = time.monotonic()
                    logger.info("homeassistant: waiting for Home Assistant to restart...")
                continue
            if event.get("manager_state") != "restore_backup":
                continue
            if event.get("state") == "failed":
                reason = str(event.get("reason") or "unknown_error").replace("_", " ")
                raise HomeAssistantError(f"homeassistant: Home Assistant couldn't restore the backup ({reason}) - "
                                         "its log says why")
            if event.get("stage"):
                logger.info("homeassistant: restoring - %s", str(event["stage"]).replace("_", " "))
        raise HomeAssistantError("homeassistant: Home Assistant didn't restart to finish the restore - check its log")

    def _restore_event(self, connection):
        """backup/info's last_action_event once it's a finished restore, else None."""
        try:
            body = connection.call("backup/info")
        except _CommandRefused as exc:
            if exc.code not in ("unknown_command", "home_assistant_error"):
                raise
            # Still starting - unless it has started without its backup integration.
            config = connection.call("get_config")
            if isinstance(config, dict) and config.get("state") == "RUNNING" \
                    and "backup" not in (config.get("components") or []):
                raise _StartedWithoutBackup("homeassistant: Home Assistant restarted after the restore, but without its "
                                         "backup integration, so it can't say how the restore went - its log says why")
            return None
        event = (body or {}).get("last_action_event")
        if isinstance(event, dict) and event.get("manager_state") == "restore_backup" \
                and event.get("state") in ("completed", "failed"):
            return event
        return None

    def _result_after_restart(self):
        """After the restart: {"token_ok": bool}, or raises for a failed
        restore. A refused token means .storage/auth was restored."""
        back_by = time.monotonic() + _BACK_S
        result_by = None
        last_log = time.monotonic()
        while True:
            try:
                connection = self._connect()
            except _TokenRefused:
                return {"token_ok": False}
            except HomeAssistantError:
                connection = None
            if connection is not None:
                try:
                    result_by = result_by or time.monotonic() + _RESULT_S
                    event = self._restore_event(connection)
                except _StartedWithoutBackup:
                    raise
                except (_Closed, HomeAssistantError):
                    event = None
                finally:
                    connection.close()
                if event is not None:
                    if event.get("state") == "failed":
                        reason = str(event.get("reason") or "unknown_error").replace("_", " ")
                        raise HomeAssistantError(f"homeassistant: Home Assistant couldn't restore the backup ({reason}) "
                                                 "and started with its settings as they were - its log says why")
                    return {"token_ok": True}
                if time.monotonic() > result_by:
                    raise HomeAssistantError("homeassistant: Home Assistant restarted but didn't report the restore's "
                                             "result - check its log")
            elif time.monotonic() > back_by:
                raise HomeAssistantError("homeassistant: Home Assistant didn't come back within an hour of the "
                                         "restore - check it's running")
            if time.monotonic() - last_log >= 30:
                last_log = time.monotonic()
                logger.info("homeassistant: waiting for Home Assistant to start again...")
            time.sleep(2)

    def _fresh_status(self):
        """A new install's onboarding backup info, "done" once onboarding
        has been restored, or None while Home Assistant is down or starting.
        An onboarded Home Assistant refuses the onboarding API (401) until it
        restarts, and has none at all (404) after; a new one adds it a few
        seconds after it starts, so a 404 is "done" only if the onboarding
        list is gone too."""
        try:
            res = self.session.get(f"{self.url}/api/onboarding/backup/info", timeout=_HTTP_S, allow_redirects=False)
            if res.status_code == 404:
                listed = self.session.get(f"{self.url}/api/onboarding", timeout=_HTTP_S, allow_redirects=False)
                return "done" if listed.status_code == 404 else None
        except requests.RequestException:
            return None
        if res.status_code == 401:
            return "done"
        try:
            body = res.json() if res.ok else None
        except ValueError:
            body = None
        return body if isinstance(body, dict) else None

    def _onboarding_backup_ready(self):
        """Waits for a new install's onboarding backup API: Home Assistant adds
        it a few seconds after its onboarding list, once its backup
        integration has loaded."""

        def ready():
            status = self._fresh_status()
            if status == "done":
                raise HomeAssistantError("homeassistant: Home Assistant's setup has started, so it no longer takes a "
                                         "backup without a token - restore it from its own Restore from backup")
            return True if status is not None else None

        if poll_until(ready, _RESULT_S, 2) is None:
            raise HomeAssistantError("homeassistant: Home Assistant's new-install restore didn't become available - "
                                     "check it has finished starting")

    def _fresh_restore(self, path, contents):
        self._onboarding_backup_ready()
        install = self._onboarding_install()
        self._refuse_onto_core(contents, install)
        agent = SUPERVISOR_AGENT if install == INSTALL_SUPERVISOR else CORE_AGENT
        logger.info("homeassistant: Home Assistant isn't set up yet - restoring through its onboarding")
        logger.info("homeassistant: uploading the backup to Home Assistant...")
        backup_id = self._upload(path, agent, fresh=True)
        try:
            res = self.session.post(f"{self.url}/api/onboarding/backup/restore", timeout=_COMMAND_S, allow_redirects=False,
                                    json={"backup_id": backup_id, "agent_id": agent,
                                          **{k: v for k, v in self._restore_options(contents, install).items()
                                             if k != "restore_homeassistant"}})
            if res.status_code >= 400:
                try:
                    detail = res.json().get("message") or res.json().get("code")
                except (ValueError, AttributeError):
                    detail = None
                raise HomeAssistantError(f"homeassistant: Home Assistant refused the restore "
                                         f"({detail or f'HTTP {res.status_code}'})")
        except requests.RequestException:
            pass  # it may restart before it answers

        def finished():
            status = self._fresh_status()
            if status == "done":
                return {"token_ok": None}
            event = (status or {}).get("last_action_event")
            if isinstance(event, dict) and event.get("manager_state") == "restore_backup":
                if event.get("state") == "failed":
                    reason = str(event.get("reason") or "unknown_error").replace("_", " ")
                    raise HomeAssistantError(f"homeassistant: Home Assistant couldn't restore the backup ({reason}) - "
                                             "its log says why")
                if event.get("state") == "completed":
                    return {"token_ok": None}
            return None

        result = poll_until(finished, _BACK_S, 2,
                            lambda waited: logger.info("homeassistant: waiting for Home Assistant to restore (%ds)...", waited))
        if result is None:
            raise HomeAssistantError("homeassistant: Home Assistant didn't finish the restore in time - check it's running")
        return backup_id, result

    def _token_works(self):
        """Whether the card's token logs in, once Home Assistant answers."""

        def check():
            try:
                self._connect().close()
                return True
            except _TokenRefused:
                return False
            except HomeAssistantError:
                return None  # still starting

        return poll_until(check, _RESULT_S, 2) is True

    def restore_from_file(self, path):
        """Restores a backup. Returns {"fresh": restored onto a new install,
        "token_ok": the card's token works on the restored Home Assistant,
        "cleaned": the uploaded copy was deleted}."""
        check_backup(path)
        contents = backup_contents(read_backup_json(path))
        if self._onboarding():
            backup_id, result = self._fresh_restore(path, contents)
            fresh = True
        else:
            fresh = False
            connection = self._connect()
            try:
                install, agent = self._agents(connection)
            finally:
                connection.close()
            self._refuse_onto_core(contents, install)
            # No WebSocket stays open over the upload: unanswered, Home
            # Assistant's heartbeat would close it.
            logger.info("homeassistant: uploading the backup to Home Assistant...")
            backup_id = self._upload(path, agent, fresh=False)
            options = self._restore_options(contents, install)
            connection = self._connect()
            try:
                subscription = self._subscribe(connection)
                logger.info("homeassistant: starting the restore - Home Assistant restarts to finish it")
                restore_id = connection.send("backup/restore", backup_id=backup_id, agent_id=agent, **options)
                try:
                    connection.result(restore_id, timeout=_RESTART_S[install])
                except _Closed:
                    pass  # it restarted before it answered
                except _CommandRefused as exc:
                    self._delete(backup_id)
                    raise HomeAssistantError(f"homeassistant: Home Assistant couldn't restore the backup "
                                             f"({str(exc).removeprefix('homeassistant: ')})") from None
                else:
                    self._wait_for_restart(connection, subscription, install)
            finally:
                connection.close()
            result = self._result_after_restart()
        token_ok = result["token_ok"]
        if token_ok is None:  # a new install: the restore brought its users and tokens back
            token_ok = self._token_works()
        cleaned = self._delete(backup_id, after_restart=True) if token_ok else False
        if not token_ok:
            logger.warning("homeassistant: the token in Settings no longer works, so Backuparr's uploaded copy (%s) was "
                           "left in Home Assistant - delete it under Settings > System > Backups", backup_id)
        return {"fresh": fresh, "token_ok": token_ok, "cleaned": cleaned}

