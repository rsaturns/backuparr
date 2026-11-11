"""Trigger each configured app's own backup mechanism, zip the result if
needed, and upload it to the configured rclone remote. Most apps are backed
up over their HTTP API. A few need their folder mounted into this container:
Jellyfin's and LazyLibrarian's APIs can't download the backup they create, so
it's picked up from the mounted folder; Emby, Seerr and Vaultwarden have no
backup API, so their files are read from their mounted data folders; and
Plex's settings and server identity come from its mounted data folder.
Settings come from config_store, not environment variables.
"""
import errno
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

import archive_check
import archive_crypto
import destination_util
import manifest
import rclone_util
import runlog
import self_backup
from apps.base import AppError
from apps.bazarr import BazarrApp
from apps.emby import EmbyApp
from apps.http_headers import RequestHeadersError, app_connection_config, configure_app_headers
from apps.jellyfin import JellyfinApp
from apps.lazylibrarian import LazyLibrarianApp
from apps.lidarr import LidarrApp
from apps.nzbget import NzbgetApp
from apps.pihole import PiholeApp
from apps.plex import PlexApp
from apps.profilarr import ProfilarrApp
from apps.prowlarr import ProwlarrApp
from apps.radarr import RadarrApp
from apps.sabnzbd import SabnzbdApp
from apps.seerr import SeerrApp
from apps.servarr import UnsafeRedirectError
from apps.sonarr import SonarrApp
from apps.tautulli import TautulliApp
from apps.tdarr import TdarrApp
from apps.vaultwarden import VaultwardenApp
from apps.whisparr import WhisparrApp
from config_store import ConfigError, app_meta, enabled_apps, enabled_destinations, encrypts, instance_type, retention_days
from webui.version import VERSION

LOG_DIR = os.environ.get("BACKUPARR_LOG_DIR", "/var/log/backuparr")
LOG_FILE = os.path.join(LOG_DIR, "backup.log")


def _log_level(value):
    """LOG_LEVEL as a logging level name: empty or unknown values mean INFO,
    so a blank field in a container template doesn't stop startup."""
    name = (value or "").strip().upper()
    return name if name in logging.getLevelNamesMapping() else "INFO"


log = logging.getLogger("backuparr")
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def configure_logging():
    """Logs to stdout at LOG_LEVEL, and Backuparr's own lines to LOG_FILE as
    well, so the web UI's status view can show past runs. Called once, at
    startup."""
    logging.basicConfig(level=_log_level(os.environ.get("LOG_LEVEL")), format=_LOG_FORMAT)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    except OSError:
        log.warning("could not open %s for writing, file logging disabled", LOG_FILE)
        return
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    log.addHandler(handler)


# Errors whose message was written for humans (and carries no URL or key).
_CLEAN_MESSAGE_ERRORS = (
    AppError,
    ConfigError,
    destination_util.DestinationError,
    RequestHeadersError,
    UnsafeRedirectError,
)

def _in_chain(exc, kind):
    """The first exception of class `kind` in exc's cause/context chain."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, kind):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def is_dns_failure(exc):
    """True when exc, or anything it was raised from, is a failed hostname
    lookup (requests wraps socket.gaierror in several layers)."""
    return _in_chain(exc, socket.gaierror) is not None


def _where(url):
    """host[:port] for log text. Never the full URL: the exception text
    repeats it, and some apps put their API key in the query string."""
    parsed = urlparse(url or "")
    if not parsed.hostname:
        return "the app"
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{parsed.hostname}:{port}" if port else parsed.hostname


def dns_failure_message(url):
    """For a lookup that failed; names the host only (see _where)."""
    host = urlparse(url or "").hostname
    target = f"hostname '{host}'" if host else "the hostname in the URL"
    return f"couldn't resolve {target} - check the URL and that this container can look it up (same Docker network, or working DNS)"


def _http_status_message(status, where):
    if status in (401, 403):
        return f"{where} refused the API key (HTTP {status}) - check the API key"
    if status == 404:
        return f"{where} answered HTTP 404 - check the URL, including any base path (e.g. /sonarr)"
    if status == 429:
        return f"{where} is rate limiting requests (HTTP 429) - try again later"
    if status >= 500:
        return f"{where} had an internal error (HTTP {status}) - check that app's own logs"
    return f"{where} answered HTTP {status}"


_PERMISSION_DENIED = "permission denied - Backuparr runs as PUID:PGID, so that folder must be writable by it"
_DISK_ERRORS = {
    errno.ENOSPC: "no space left on the device",
    errno.EDQUOT: "disk quota exceeded",
    errno.EROFS: "the filesystem is read-only",
    errno.EACCES: _PERMISSION_DENIED,
    errno.EPERM: _PERMISSION_DENIED,
}


def describe_failure(exc, url=None):
    """One readable, secret-free line for an operational failure the user
    can act on (unreachable app, wrong key, full disk, ...), or None for
    anything else - an unexpected error that deserves its full traceback.
    `url` is the app/destination the work was for; only its host is used."""
    where = _where(url)
    if isinstance(exc, (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL)):
        return "that doesn't look like a valid URL - it should start with http:// or https://"
    if isinstance(exc, rclone_util.RcloneError):
        return exc.detail
    if isinstance(exc, _CLEAN_MESSAGE_ERRORS):
        return str(exc)
    if isinstance(exc, (requests.exceptions.JSONDecodeError, json.JSONDecodeError)):
        return f"{where} didn't answer with API data - the URL probably points at something other than the app's API (check the URL and any base path)"
    if isinstance(exc, requests.exceptions.RequestException):
        if is_dns_failure(exc):
            return dns_failure_message(url)
        if isinstance(exc, requests.exceptions.SSLError):
            return f"TLS/SSL error talking to {where} - check http:// vs https:// and the app's certificate"
        if isinstance(exc, requests.exceptions.Timeout):
            return f"couldn't connect - timed out waiting for {where}; is the app overloaded, or a firewall dropping the traffic?"
        if isinstance(exc, requests.exceptions.ConnectionError):
            if _in_chain(exc, ConnectionRefusedError):
                return f"couldn't connect - connection refused by {where}; is the app running, and is the port right?"
            return "couldn't connect - check the URL and that it's reachable from this container"
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            return _http_status_message(exc.response.status_code, where)
        return f"request to {where} failed ({type(exc).__name__})"
    if isinstance(exc, zipfile.BadZipFile):
        return "the backup file isn't a valid zip - it may be corrupt or incomplete"
    if isinstance(exc, OSError) and exc.errno in _DISK_ERRORS:
        path = f" ({exc.filename})" if exc.filename else ""
        return f"{_DISK_ERRORS[exc.errno]}{path}"
    if isinstance(exc, FileNotFoundError):
        return str(exc)
    return None


def _without_app_prefix(message, app):
    """Driver errors start with "<app>: ", which the caller's own
    "<app>: ..." label would repeat. An instance's driver says its app type
    ("radarr: ...") under a "radarr-4k: " label."""
    if app:
        for name in (app, instance_type(app)):
            if name and message.startswith(f"{name}: "):
                return message[len(name) + 2:]
    return message


def humanize_error(exc, url=None, app=None):
    """The describe_failure() line, or str(exc) for anything it doesn't know.
    Pass `app` when the result goes after an "<app>: " label."""
    return _without_app_prefix(describe_failure(exc, url) or str(exc), app)


def log_failure(logger, summary, exc, url=None, app=None):
    """Logs a failure as one readable ERROR line when describe_failure()
    recognises it, otherwise with the full traceback - unexpected errors
    are bugs, and a bug report needs the stack."""
    message = describe_failure(exc, url)
    if message is None:
        logger.error("%s", summary, exc_info=exc)
    else:
        logger.error("%s - %s", summary, _without_app_prefix(message, app))


def build_app(name, app_cfg):
    """`name` is an app or an instance id - radarr-4k builds a Radarr."""
    return configure_app_headers(_build_app(instance_type(name), app_cfg), app_cfg.get("request_headers", []))


# Every app type's driver. Each takes (url, api_key, **extra fields) - the
# extra fields being its APP_META extra_fields - or, for an app with
# no_api_key, (url, **extra fields).
DRIVERS = {
    "radarr": RadarrApp,
    "sonarr": SonarrApp,
    "lidarr": LidarrApp,
    "whisparr": WhisparrApp,
    "prowlarr": ProwlarrApp,
    "profilarr": ProfilarrApp,
    "bazarr": BazarrApp,
    "tdarr": TdarrApp,
    "sabnzbd": SabnzbdApp,
    "nzbget": NzbgetApp,
    "tautulli": TautulliApp,
    "jellyfin": JellyfinApp,
    "emby": EmbyApp,
    "plex": PlexApp,
    "seerr": SeerrApp,
    "lazylibrarian": LazyLibrarianApp,
    "pihole": PiholeApp,
    "vaultwarden": VaultwardenApp,
}


def _build_app(name, app_cfg):
    meta = app_meta(name)
    if name not in DRIVERS or meta is None:
        raise ValueError(f"Unknown app: {name}")
    extras = {field["name"]: app_cfg.get(field["name"]) or None for field in meta["extra_fields"]}
    if meta.get("no_api_key"):
        return DRIVERS[name](app_cfg["url"], **extras)
    return DRIVERS[name](app_cfg["url"], app_cfg.get("api_key", ""), **extras)


_TAR_SUFFIX = re.compile(r"\.tar\.(?:gz|bz2|xz|zst)$")


def archive_suffix(path):
    """.zip for a real zip; an app's own non-zip archive (e.g. Profilarr's
    .tar.gz) keeps its extension rather than being renamed to .zip."""
    if zipfile.is_zipfile(path):
        return ".zip"
    name = Path(path).name
    match = _TAR_SUFFIX.search(name)
    return match.group(0) if match else Path(name).suffix or ".bin"


def zip_dir(src_dir, zip_path):
    # zlib's default level: 9 is far slower on multi-GB databases for a few
    # percent less size.
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, src_dir))


# Discord/Slack/Telegram/Gotify each need their own JSON envelope, not
# the plain-text body the fallback sends (ntfy.sh's native shape).
# Matched by URL shape so notify_url alone still configures everything.
_DISCORD_WEBHOOK_RE = re.compile(r"discord(?:app)?\.com/api/webhooks/")
_SLACK_WEBHOOK_RE = re.compile(r"hooks\.slack\.com/services/")
_TELEGRAM_RE = re.compile(r"api\.telegram\.org/bot")


def _is_gotify_url(parsed):
    # Gotify is self-hosted, so match by shape: /message path + ?token=.
    return parsed.path.rstrip("/").endswith("/message") and "token" in parse_qs(parsed.query)


def notify(notify_url, message, raise_on_error=False):
    if not notify_url:
        return
    parsed = urlparse(notify_url)
    try:
        if _DISCORD_WEBHOOK_RE.search(notify_url):
            res = requests.post(notify_url, json={"content": message[:2000]}, timeout=10)  # 2000 = Discord's limit
        elif _SLACK_WEBHOOK_RE.search(notify_url) or _TELEGRAM_RE.search(notify_url):
            # Telegram's chat_id stays in notify_url's own query string.
            res = requests.post(notify_url, json={"text": message}, timeout=10)
        elif _is_gotify_url(parsed):
            res = requests.post(notify_url, json={"title": "Backuparr", "message": message}, timeout=10)
        else:
            res = requests.post(notify_url, data=message.encode("utf-8"), timeout=10)
        res.raise_for_status()
    except requests.RequestException:
        if raise_on_error:
            raise
        log.warning("notify: failed to reach NOTIFY_URL")


def format_run_message(ok, failed):
    """One line per app (✅/❌), with a header reflecting overall status."""
    header = "🎉 Backuparr completed successfully" if not failed else "⚠️ Backuparr completed with errors"
    lines = [header, ""]
    lines += [f"✅ {name}" for name in ok]
    lines += [f"❌ {item}" for item in failed]
    return "\n".join(lines)


def apply_retention(root, retention_days, now=None, only=None, dest_id=None):
    """Deletes each app's backups older than retention_days from one
    destination, always keeping its newest - so an app that keeps failing,
    or was switched off (or an instance that was removed), still has its
    last good backup. Only Backuparr's own <app>/<app>_<timestamp> archives
    are touched, <app> being an app or instance id; age comes from the
    timestamp in the name, written in this container's local time. only,
    if given, limits it to that one app or instance's backups.

    dest_id, if given, is the destination's id: the manifest drops what
    retention deletes, and any entry for a file the listing didn't find."""
    cutoff = (now or datetime.now()) - timedelta(days=int(retention_days))
    try:
        destination_util.wait_for_storage(dest_id, root)
        entries = destination_util.list_backups(root)
    except (rclone_util.RcloneError, destination_util.DestinationError) as exc:
        log.warning("retention: couldn't list %s, nothing removed: %s", root, exc.detail)
        return
    per_app = {}
    present = set()
    for entry in entries:
        app, sep, name = entry.get("Path", "").partition("/")
        if entry.get("IsDir") or not sep or not destination_util.is_backup_folder(app) or not destination_util.is_backup_name(app, name):
            continue
        present.add(manifest.key(app, name))
        if only is not None and app != only:
            continue
        per_app.setdefault(app, []).append(name)
    if dest_id is not None:
        manifest.reconcile(dest_id, present)
    for app, names in per_app.items():
        names.sort()  # The timestamp makes name order oldest-first.
        for name in names[:-1]:
            try:
                stamp = datetime.strptime(name[len(app) + 1:len(app) + 16], "%Y%m%d_%H%M%S")
            except ValueError:
                continue  # Not a real date, so not a name Backuparr wrote.
            if stamp >= cutoff:
                break
            try:
                rclone_util.delete_file(f"{root}/{app}/{name}")
                log.info("retention: removed %s/%s", app, name)
                if dest_id is not None:
                    manifest.remove(app, name, dest_id)
            except rclone_util.RcloneError as exc:
                log.warning("retention: couldn't remove %s/%s: %s", app, name, exc.detail)


class RunCancelled(Exception):
    pass


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _app_version(app, name, url):
    """The app's version for the manifest, or None - logged - when the app
    can't say. A backup that worked isn't failed over it."""
    try:
        return manifest.normalise_version(instance_type(name), app.app_version())
    except Exception as exc:
        log.warning("%s: couldn't read its version for the backup record: %s", name, humanize_error(exc, url, app=name))
        return None


def encrypt_archive(name, archive_path):
    """Encrypts a checked archive with the install's key (#147), checks the
    encrypted file decrypts back to it, and removes the plain one. Returns
    the encrypted file's path."""
    log.info("%s: encrypting the backup...", name)
    encrypted_path = archive_crypto.encrypted_name(archive_path)
    plain_sha256 = archive_crypto.encrypt_file(archive_path, encrypted_path)
    try:
        archive_crypto.check_file(encrypted_path, plain_sha256)
    except BaseException:
        os.remove(encrypted_path)
        raise
    os.remove(archive_path)
    return encrypted_path


def run_backup(cfg, on_progress=None, should_cancel=None, only=None):
    """Run one backup pass for every enabled app instance, uploading to
    every enabled destination. Returns (ok, failed) - failed entries are
    "<instance id>: <message>" strings.

    only, if given, is one enabled app or instance id: back up just that
    one, and apply retention to its backups alone. "backuparr" backs up
    Backuparr's own configuration alone (#165); a full run backs it up
    last, after the apps, while self_backup is on.

    on_progress(index, total, name), if given, is called right before each
    app starts - lets a caller (e.g. the web UI) show "app N of M".

    should_cancel(), if given, is polled between apps and between
    destination uploads - a blocking API call or upload already in flight
    still runs to completion, so cancelling stops the run at the next
    safe point rather than instantly."""

    def check_cancel():
        if should_cancel and should_cancel():
            raise RunCancelled()

    backs_up_self = cfg.get("self_backup", True) is not False and only in (None, self_backup.FOLDER)
    apps = enabled_apps(cfg)
    if only == self_backup.FOLDER:
        if not backs_up_self:
            log.error("Backuparr's own backup is turned off - nothing to do")
            return [], ["backuparr: its own backup is turned off in Settings"]
        apps = []
    elif only is not None:
        if only not in apps:
            log.error("%s is not enabled - nothing to do", only)
            return [], [f"{only}: not enabled in config"]
        apps = [only]
    if not apps and not backs_up_self:
        log.error("No apps enabled - nothing to do")
        return [], ["no apps enabled in config"]
    total = len(apps) + backs_up_self

    destinations = enabled_destinations(cfg)
    if not destinations:
        log.error("No destinations enabled - nothing to do")
        return [], ["no destinations enabled in config"]

    destination_util.sync(cfg)

    dest_roots = {}
    failed = []
    for dest_id in destinations:
        try:
            dest_roots[dest_id] = destination_util.remote_root(dest_id, cfg["destinations"][dest_id]).rstrip("/")
        except (destination_util.DestinationError, OSError) as exc:
            message = humanize_error(exc)
            log.error("destination %s: %s", dest_id, message)
            failed.append(f"destination {dest_id}: {message}")

    if not dest_roots:
        return [], failed

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    ok = []
    run_tmp = tempfile.mkdtemp(prefix="backuparr-run-")
    cancelled = False

    try:
        for i, name in enumerate(apps, start=1):
            check_cancel()
            if on_progress:
                on_progress(i, total, name)
            log.info("=== %s ===", name)
            app_cfg = app_connection_config(cfg, name)

            work_dir = os.path.join(run_tmp, name)
            archive_path = None
            # This app's run log: None for a destination its upload never reached.
            run = {"id": f"{name}_{timestamp}", "app": name, "started": _utc_now(), "finished": None, "file": None,
                   "ok": False, "error": None, "app_version": None, "destinations": {d: None for d in dest_roots}}
            capture = runlog.Capture().start()

            try:
                os.makedirs(work_dir, exist_ok=True)
                app = build_app(name, app_cfg)
                result = app.backup(work_dir)
                result_path = Path(result)

                if result_path.is_dir():
                    archive_name = f"{name}_{timestamp}.zip"
                    archive_path = os.path.join(run_tmp, archive_name)
                    zip_dir(result_path, archive_path)
                else:
                    # The app's own archive - move it, keeping its format.
                    archive_name = f"{name}_{timestamp}{archive_suffix(result_path)}"
                    archive_path = os.path.join(run_tmp, archive_name)
                    shutil.move(result_path, archive_path)
                run["file"] = archive_name

                # A backup that fails is never uploaded; retention keeps the
                # app's newest, so its last good backup stays.
                log.info("%s: checking the backup...", name)
                archive_check.check(name, archive_path)
                encrypted = encrypts(cfg, name)
                if encrypted:
                    archive_path = encrypt_archive(name, archive_path)
                    archive_name = run["file"] = os.path.basename(archive_path)
                size = os.path.getsize(archive_path)
                sha256 = manifest.file_sha256(archive_path)
                app_version = run["app_version"] = _app_version(app, name, app_cfg.get("url"))
                uploaded = []
                dest_failures = []
                for dest_id, root in dest_roots.items():
                    check_cancel()
                    remote_dest = f"{root}/{name}/{archive_name}"
                    log.info("%s: uploading to %s...", name, dest_id)
                    try:
                        destination_util.wait_for_storage(dest_id, root)
                        rclone_util.copyto(archive_path, remote_dest)
                        log.info("%s: uploaded -> %s (%d bytes)", name, remote_dest, size)
                        uploaded.append(dest_id)
                        run["destinations"][dest_id] = {"ok": True, "error": None}
                    except (rclone_util.RcloneError, destination_util.DestinationError) as exc:
                        detail = exc.detail
                        log.error("%s: upload to %s failed: %s", name, dest_id, detail)
                        dest_failures.append(f"{dest_id}: {detail}")
                        run["destinations"][dest_id] = {"ok": False, "error": detail}

                if uploaded:
                    try:
                        manifest.record(name, archive_name, uploaded, app_version=app_version, sha256=sha256,
                                        size=size, backuparr_version=VERSION, encrypted=encrypted)
                    except OSError as exc:
                        # The backup itself is uploaded; only its record is missing.
                        log.warning("%s: couldn't write the backup record: %s", name, exc.strerror or type(exc).__name__)
                if dest_failures:
                    run["error"] = f"failed on {'; '.join(dest_failures)}"
                    failed.append(f"{name}: {run['error']}")
                else:
                    run["ok"] = True
                    ok.append(name)
            except RunCancelled:
                run["error"] = "the run was cancelled"
                raise
            except Exception as exc:
                log_failure(log, f"{name}: backup failed", exc, app_cfg.get("url"), app=name)
                run["error"] = humanize_error(exc, app_cfg.get("url"), app=name)
                failed.append(f"{name}: {run['error']}")
            finally:
                capture.stop()
                run["finished"] = _utc_now()
                run["lines"] = capture.result()
                runlog.save(run)
                shutil.rmtree(work_dir, ignore_errors=True)
                if archive_path and os.path.exists(archive_path):
                    os.remove(archive_path)
    except RunCancelled:
        cancelled = True
        log.warning("backup run cancelled")
    finally:
        shutil.rmtree(run_tmp, ignore_errors=True)

    if cancelled:
        failed.append("run cancelled")
        return ok, failed

    keep = {dest_id: retention_days(cfg, dest_id) for dest_id in dest_roots}
    log.info("Applying retention: %s", ", ".join(f"{dest_id} {days}d" for dest_id, days in keep.items()))
    for dest_id, root in dest_roots.items():
        apply_retention(root, keep[dest_id], only=only, dest_id=dest_id)
    # A run's log stays while any destination still keeps its backup.
    runlog.prune(max(keep.values()))

    # Last, so its manifest and run logs include this run. A single app's run
    # leaves Backuparr's backup as it was.
    if backs_up_self:
        _self_backup_step(dest_roots, timestamp, ok, failed, on_progress, total)

    return ok, failed


def _self_backup_step(dest_roots, timestamp, ok, failed, on_progress=None, total=None):
    """Backuparr's own backup, the last step of a run (#110), reported like
    an app's (#165)."""
    if on_progress:
        on_progress(total, total, self_backup.FOLDER)
    problem = self_backup.run(dest_roots, timestamp)
    if problem:
        failed.append(f"{self_backup.FOLDER}: {problem}")
    else:
        ok.append(self_backup.FOLDER)
