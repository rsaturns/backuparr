"""Backup driver for a Synology NAS's settings (#168): DSM's own
Configuration Backup, the file Control Panel > Update & Restore >
Configuration Backup > Export makes. It holds DSM's settings - users,
groups, shared folder settings and permissions, file services, network,
Task Scheduler - never the data in the shared folders.

Synology doesn't document the export, so this makes the Control Panel's own
calls (read from its admin_center.js on DSM 6.2 and 7.2, the same on both),
over DSM's Web API:

1. SYNO.API.Info query: where each API lives (SYNO.API.Auth is auth.cgi on
   DSM 6, entry.cgi on 7) - no sign-in needed.
2. SYNO.API.Auth login (version 6, documented in Synology's DSM Login Web API
   Guide, as every sign-in step here): the account and password, session
   "webui" (DSM refuses a non-admin's sign-in to a session name it doesn't
   know, 402, so a wrong account would get a misleading error with any
   other), format "sid". A refused sign-in is never retried: DSM's Auto Block
   counts failures per address.
   2-factor sign-in: DSM answers 403 when the account needs a code. The
   card's code goes in once, as otp_code with enable_device_token=yes and a
   device name; DSM then answers a device id (did) that signs in without a
   code from then on (device_id + device_name). Test connection hands it to
   the card (card_info), which keeps it encrypted like other secrets. DSM
   answers 403 again once the device is no longer trusted (the user removed
   it in DSM), and the run asks for a new code.
3. SYNO.DSM.Info getinfo: the version ("DSM 7.2.2-72806 Update 1"). Its
   answer holds the serial number too, so only the version is kept.
4. SYNO.Backup.Config.Backup (version 1): "start" makes the file and answers
   a task id; "status" says when it's finished (about a second); "download"
   sends it, once - a second download answers a JSON error. DSM lets only
   administrators run these (105 otherwise); Test connection runs "list",
   which reads the list of what the export holds and changes nothing.
5. SYNO.API.Auth logout.

Every call is a POST, so the session id never sits in a URL. The file
(.dss) is an xz-compressed tar: ConfigBkp/config_info (DSM's version) and
ConfigBkp/_Syno_ConfBkp.db (SQLite), plus a few more files on DSM 7. It holds
the users and their settings, so Backuparr always encrypts it (#147).
Restores are by hand in DSM (manual_restore, #151): a restore stops DSM's
services while it runs. A self-signed certificate (DSM's default) is trusted
on the card and pinned (apps/cert_pin.py).
"""
import json
import logging
import lzma
import os
import re
import shlex
import tarfile

from apps import cert_pin
from apps.base import AppError, poll_until, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

BACKUP_FILE = "synology-configuration.dss"
DEVICE_NAME = "Backuparr"
_SESSION = "webui"
_AUTH_VERSION = "6"
_BACKUP_API = "SYNO.Backup.Config.Backup"
_TIMEOUT = 60
# The export takes about a second; padded for a busy NAS, at most 1 h as for every app.
_EXPORT_TIMEOUT = 3600
_POLL_INTERVAL = 2
# config_info's lines are short; a .dss is a few hundred KB at most.
_INFO_LIMIT = 64 * 1024
_DB_LIMIT = 256 * 1024 * 1024
INFO_MEMBER = "ConfigBkp/config_info"
DB_MEMBER = "ConfigBkp/_Syno_ConfBkp.db"
_VERSION = re.compile(r"DSM\s+(\d+(?:\.\d+)+-\d+(?:\s+Update\s+\d+)?)")
_DID = re.compile(r"[A-Za-z0-9_.\-]{8,512}")
_DIGITS = re.compile(r"\d{1,9}")

NEW_CODE = "Synology asked for a new 2FA code. Run Test connection on its card."
# DSM 6 answers a non-admin's sign-in the same way as a wrong password (400).
_WRONG_LOGIN = ("DSM refused the username or password - check them, and on DSM 6 that the account is in the "
                "administrators group (DSM blocks an address after repeated failed sign-ins, so check before "
                "testing again)")
_NOT_ADMIN = ("the account isn't in the administrators group - DSM lets only administrators export its "
              "configuration")


class SynologyError(AppError):
    pass


class CodeRequiredError(SynologyError):
    """DSM wants a 2-factor code. Test connection then shows the card's
    2FA code field."""
    needs_login = True


def _not_a_backup(why):
    return SynologyError(f"synology: the file isn't a DSM configuration backup ({why})")


def _read_member(tar, name, limit):
    member = tar.getmember(name)
    if not member.isfile() or member.size > limit:
        raise _not_a_backup(f"{name} isn't a file of the expected size")
    with tar.extractfile(member) as f:
        return f.read(limit + 1)


def parse_config_info(text):
    """config_info's key="value" lines as a dict."""
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().isidentifier():
            try:
                parts = shlex.split(value)
            except ValueError:
                continue
            values[key.strip()] = parts[0] if parts else ""
    return values


def _version_from_info(info):
    """"7.2-72806" from config_info: DSM writes its major and minor version
    and build there, not the patch level ("7.2.2") or update."""
    major, minor, build = (info.get(k, "") for k in ("dsm_majorversion", "dsm_minorversion", "dsm_buildnumber"))
    if not all(_DIGITS.fullmatch(v) for v in (major, minor, build)):
        return None
    return f"{major}.{minor}-{build}"


def read_backup(path, check_db=None):
    """The DSM version a configuration backup records (config_info), after
    checking it's whole: an xz tar holding DSM's config_info and settings
    database. check_db(fileobj), if given, checks the database and returns a
    problem or None (archive_check runs SQLite's quick check). Raises
    SynologyError otherwise."""
    try:
        with tarfile.open(path, "r:xz") as tar:
            names = set(tar.getnames())  # reads the whole stream: a damaged file fails here
            for required in (INFO_MEMBER, DB_MEMBER):
                if required not in names:
                    raise _not_a_backup(f"no {required}")
            info = parse_config_info(_read_member(tar, INFO_MEMBER, _INFO_LIMIT).decode("utf-8", "replace"))
            if info.get("os_name") != "DSM":
                raise _not_a_backup("config_info doesn't name DSM")
            if check_db is not None:
                member = tar.getmember(DB_MEMBER)
                if not member.isfile() or member.size > _DB_LIMIT:
                    raise _not_a_backup(f"{DB_MEMBER} isn't a file of the expected size")
                with tar.extractfile(member) as f:
                    problem = check_db(f)
                if problem:
                    raise SynologyError(f"synology: {DB_MEMBER} failed SQLite's quick check ({problem})")
            return _version_from_info(info)
    except (tarfile.TarError, lzma.LZMAError, EOFError, OSError) as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise _not_a_backup("it isn't a whole xz tar") from None
    except KeyError as exc:
        raise _not_a_backup(f"no {exc.args[0] if exc.args else 'expected file'}") from None


def backup_version(path):
    """Just the version a backup records, for the manifest backfill; None
    if it has none or can't be read."""
    try:
        return read_backup(path)
    except SynologyError:
        return None


def check_backup(path, check_db=None):
    """archive_check's check: raises SynologyError unless the backup is whole."""
    read_backup(path, check_db)


def _login_error(code, types, had_token):
    """The message for a refused sign-in, by DSM's error code (Synology's DSM
    Login Web API Guide)."""
    if code == 400:
        return SynologyError(f"synology: {_WRONG_LOGIN}")
    if code == 401:
        return SynologyError("synology: the account is disabled in DSM")
    if code == 402:
        return SynologyError("synology: DSM doesn't let this account sign in - use an account in the "
                             "administrators group")
    if code == 403:
        if types and "otp" not in types:
            # Approve sign-in, a security key or Adaptive MFA's check (web sign-ins only, Synology says).
            return SynologyError("synology: DSM asked for a second sign-in step Backuparr can't give (Approve sign-in, "
                                 "a security key or Adaptive MFA) - set up 2FA with a verification code (OTP) for "
                                 "the account, then test again")
        if had_token:
            return CodeRequiredError(f"synology: {NEW_CODE}")
        return CodeRequiredError("synology: DSM asks for a 2FA code - enter one from the account's "
                                 "authenticator app and test again")
    if code == 404:
        return CodeRequiredError("synology: the 2FA code is wrong or expired - enter the current one and test again")
    if code == 406:
        return SynologyError("synology: DSM requires 2-factor sign-in for this account but it isn't set up - "
                             "sign in to DSM with it once to set it up")
    if code == 407:
        return SynologyError("synology: DSM blocked Backuparr's address after failed sign-ins - remove it from the Auto Block "
                             "list in DSM's Control Panel > Security, then check the login")
    if code in (408, 409, 410):
        return SynologyError("synology: the account's password has expired - sign in to DSM with it to change it")
    return SynologyError(f"synology: DSM refused the sign-in (error {code})")


def _api_error(code, doing):
    if code == 105:
        return SynologyError(f"synology: {_NOT_ADMIN}")
    if code in (106, 107, 119):
        return SynologyError("synology: DSM ended Backuparr's session - try again")
    return SynologyError(f"synology: DSM couldn't {doing} (error {code})")


class SynologyApp:
    def __init__(self, url, username=None, password=None, otp_code=None, device_token=None, trusted_cert=None,
                 timeout=_TIMEOUT):
        self.url = url.rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.otp_code = re.sub(r"\s+", "", otp_code or "")
        self.device_token = device_token or ""
        self.timeout = timeout
        self.session = SameHostSession(self.url, strict=True)
        cert_pin.pin(self.session, "synology", trusted_cert)
        self.card_info = None  # a new device token from Test connection, for the card
        self._paths = None
        self._sid = None
        self._version = None

    # ---------------------------------------------------------- requests ----
    def _post(self, path, data, timeout=None, stream=False):
        res = self.session.post(f"{self.url}/webapi/{path}", data=data, timeout=timeout or self.timeout,
                                allow_redirects=False, stream=stream)
        if 300 <= res.status_code < 400:
            res.close()
            raise SynologyError("synology: the URL redirects - use DSM's address as it ends up, with its port "
                                "(https://...:5001, or http://...:5000 if DSM doesn't redirect it)")
        return res

    def _json(self, res):
        with res:
            if res.status_code != 200:
                raise SynologyError(f"synology: no DSM answered at this URL (HTTP {res.status_code}) - check it, "
                                    "with its port (5001 for https, 5000 for http)")
            try:
                body = res.json()
            except ValueError:
                raise SynologyError("synology: the URL didn't answer like DSM - check it, with its port "
                                    "(5001 for https, 5000 for http)") from None
        if not isinstance(body, dict):
            raise SynologyError("synology: the URL didn't answer like DSM - check it")
        return body

    @staticmethod
    def _error_code(body):
        error = body.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, int) else 0

    @staticmethod
    def _second_steps(body):
        """The second sign-in steps a 403 lists, e.g. ["otp"]."""
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        errors = error.get("errors") if isinstance(error.get("errors"), dict) else {}
        types = errors.get("types") if isinstance(errors.get("types"), list) else []
        return [t.get("type") for t in types if isinstance(t, dict)]

    def _api_paths(self):
        if self._paths is None:
            body = self._json(self._post("query.cgi", {
                "api": "SYNO.API.Info", "version": "1", "method": "query",
                "query": f"SYNO.API.Auth,SYNO.DSM.Info,{_BACKUP_API}"}))
            data = body.get("data") if body.get("success") else None
            if not isinstance(data, dict) or not isinstance(data.get("SYNO.API.Auth"), dict):
                raise SynologyError("synology: the URL didn't answer like DSM - check it, with its port "
                                    "(5001 for https, 5000 for http)")
            if not isinstance(data.get(_BACKUP_API), dict):
                raise SynologyError("synology: this DSM has no configuration backup to export - DSM 6.2 or "
                                    "later is needed")
            self._paths = {name: str(info.get("path", "entry.cgi")) for name, info in data.items()
                           if isinstance(info, dict)}
            for name, path in self._paths.items():
                if not re.fullmatch(r"[a-z_]+\.cgi", path):
                    raise SynologyError("synology: the URL didn't answer like DSM - check it")
        return self._paths

    def _call(self, api, method, version, doing, timeout=None, **params):
        data = {"api": api, "version": str(version), "method": method, "_sid": self._sid}
        data.update({key: json.dumps(value) for key, value in params.items()})
        body = self._json(self._post(self._api_paths()[api], data, timeout=timeout))
        if not body.get("success"):
            raise _api_error(self._error_code(body), doing)
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ sign-in ----
    def _sign_in(self):
        if not self.username or not self.password:
            raise SynologyError("synology: enter the username and password of an account in the administrators group")
        auth = self._api_paths()["SYNO.API.Auth"]
        form = {"api": "SYNO.API.Auth", "version": _AUTH_VERSION, "method": "login", "account": self.username,
                "passwd": self.password, "session": _SESSION, "format": "sid"}
        using_code = bool(self.otp_code)
        if using_code:
            form.update(otp_code=self.otp_code, enable_device_token="yes", device_name=DEVICE_NAME)
        elif self.device_token:
            form.update(device_id=self.device_token, device_name=DEVICE_NAME)
        body = self._json(self._post(auth, form))
        if not body.get("success"):
            raise _login_error(self._error_code(body), self._second_steps(body), bool(self.device_token) and not using_code)
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        sid = data.get("sid")
        if not isinstance(sid, str) or not sid:
            raise SynologyError("synology: DSM didn't start a session - check the URL is DSM's")
        self._sid = sid
        did = data.get("did")
        if using_code:
            if not isinstance(did, str) or not _DID.fullmatch(did):
                self._sign_out()
                raise SynologyError("synology: DSM accepted the code but didn't remember this device - test again")
            self.device_token = did
            self.otp_code = ""  # one use only
            self.card_info = {"device_token": did}

    def _sign_out(self):
        if not self._sid:
            return
        try:
            self._post(self._api_paths()["SYNO.API.Auth"], {
                "api": "SYNO.API.Auth", "version": _AUTH_VERSION, "method": "logout", "session": _SESSION,
                "_sid": self._sid}).close()
        except Exception as exc:  # noqa: BLE001 - DSM ends an idle session on its own
            logger.debug("synology: sign-out failed (%s)", type(exc).__name__)
        self._sid = None

    def _read_version(self):
        data = self._call("SYNO.DSM.Info", "getinfo", 2, "say its version")
        match = _VERSION.search(str(data.get("version_string") or ""))
        # Only the version: the answer holds the NAS's serial number too.
        self._version = re.sub(r"\s+", " ", match.group(1)) if match else None
        return self._version

    # -------------------------------------------------------------- driver ----
    def app_version(self):
        if self._version:
            return self._version
        self._sign_in()
        try:
            return self._read_version()
        finally:
            self._sign_out()

    def test_connection(self):
        self._sign_in()
        try:
            version = self._read_version()
            self._call(_BACKUP_API, "list", 1, "list what its configuration backup holds")
        finally:
            self._sign_out()
        message = f"synology {version or ''} reachable, signed in as {self.username}, who can export its configuration"
        if self.card_info:
            message += ". 2FA: this device is remembered - Save to keep it"
        return message.replace("  ", " ")

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, BACKUP_FILE)
        self._sign_in()
        try:
            version = self._read_version()
            logger.info("synology: exporting the configuration (DSM %s)...", version or "version unknown")
            task = self._call(_BACKUP_API, "start", 1, "start the configuration export").get("task_id")
            if not isinstance(task, str) or not task:
                raise SynologyError("synology: DSM didn't start the configuration export")
            self._wait(task)
            self._download(task, path)
        finally:
            self._sign_out()
        logger.info("synology: downloaded the configuration backup (%d KB)", os.path.getsize(path) // 1024)
        return path

    def _wait(self, task):
        def finished():
            data = self._call(_BACKUP_API, "status", 1, "make the configuration export", task_id=task)
            return True if data.get("finish") is True else None

        def waiting(seconds):
            logger.info("synology: still exporting the configuration (%d s)...", seconds)

        if poll_until(finished, _EXPORT_TIMEOUT, _POLL_INTERVAL, on_wait=waiting) is None:
            raise SynologyError("synology: DSM didn't finish the configuration export within an hour")

    def _download(self, task, path):
        res = self._post(self._api_paths()[_BACKUP_API], {
            "api": _BACKUP_API, "version": "1", "method": "download", "task_id": json.dumps(task), "_sid": self._sid},
            timeout=(self.timeout, _EXPORT_TIMEOUT), stream=True)
        if res.status_code == 200 and res.headers.get("Content-Type", "").startswith("application/octet-stream"):
            with res:
                save_stream(res, path)
            return
        body = self._json(res)
        raise _api_error(self._error_code(body), "send the configuration export")
