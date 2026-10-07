"""Trigger each configured app's own backup mechanism over its API, zip the
result if needed, and upload it to the configured rclone remote. No app
config volumes are read directly - everything goes through each app's
HTTP API. Settings come from config_store, not environment variables.
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
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

import destination_util
import rclone_util
from apps.bazarr import BazarrApp, BazarrError
from apps.profilarr import ProfilarrApp, ProfilarrError
from apps.prowlarr import ProwlarrApp
from apps.radarr import RadarrApp
from apps.sabnzbd import SabnzbdApp, SabnzbdError
from apps.servarr import ServarrError, UnsafeRedirectError
from apps.sonarr import SonarrApp
from apps.tautulli import TautulliApp, TautulliError
from apps.tdarr import TdarrApp, TdarrError
from config_store import ConfigError, enabled_apps, enabled_destinations

LOG_DIR = os.environ.get("BACKUPARR_LOG_DIR", "/var/log/backuparr")
LOG_FILE = os.path.join(LOG_DIR, "backup.log")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("backuparr")

# So the web UI's status view can show past run results too.
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_file_handler)
except OSError:
    log.warning("could not open %s for writing, file logging disabled", LOG_FILE)


# Errors whose message was written for humans (and carries no URL or key).
_CLEAN_MESSAGE_ERRORS = (
    BazarrError,
    ConfigError,
    destination_util.DestinationError,
    ProfilarrError,
    rclone_util.RcloneError,
    SabnzbdError,
    ServarrError,
    TautulliError,
    TdarrError,
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
    "<app>: ..." label would repeat."""
    prefix = f"{app}: "
    return message[len(prefix):] if app and message.startswith(prefix) else message


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
    if name == "radarr":
        return RadarrApp(app_cfg["url"], app_cfg["api_key"])
    if name == "sonarr":
        return SonarrApp(app_cfg["url"], app_cfg["api_key"])
    if name == "prowlarr":
        return ProwlarrApp(app_cfg["url"], app_cfg["api_key"])
    if name == "profilarr":
        return ProfilarrApp(app_cfg["url"], app_cfg["api_key"])
    if name == "bazarr":
        return BazarrApp(
            app_cfg["url"],
            app_cfg["api_key"],
            username=app_cfg.get("username") or None,
            password=app_cfg.get("password") or None,
        )
    if name == "tdarr":
        return TdarrApp(app_cfg["url"], api_key=app_cfg.get("api_key") or None)
    if name == "sabnzbd":
        return SabnzbdApp(app_cfg["url"], app_cfg["api_key"])
    if name == "tautulli":
        return TautulliApp(app_cfg["url"], app_cfg["api_key"])
    raise ValueError(f"Unknown app: {name}")


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
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
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
        elif _SLACK_WEBHOOK_RE.search(notify_url):
            res = requests.post(notify_url, json={"text": message}, timeout=10)
        elif _TELEGRAM_RE.search(notify_url):
            # chat_id stays in notify_url's own query string.
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


class RunCancelled(Exception):
    pass


def run_backup(cfg, on_progress=None, should_cancel=None):
    """Run one backup pass for every enabled app, uploading to every
    enabled destination. Returns (ok, failed) - failed entries are
    "<app>: <message>" strings.

    on_progress(index, total, name), if given, is called right before each
    app starts - lets a caller (e.g. the web UI) show "app N of M".

    should_cancel(), if given, is polled between apps and between
    destination uploads - a blocking API call or upload already in flight
    still runs to completion, so cancelling stops the run at the next
    safe point rather than instantly."""

    def check_cancel():
        if should_cancel and should_cancel():
            raise RunCancelled()

    apps = enabled_apps(cfg)
    if not apps:
        log.error("No apps enabled - nothing to do")
        return [], ["no apps enabled in config"]

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
        except destination_util.DestinationError as exc:
            log.error("destination %s: %s", dest_id, exc)
            failed.append(f"destination {dest_id}: {exc}")

    if not dest_roots:
        return [], failed

    retention_days = cfg.get("retention_days", 7)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    ok = []
    run_tmp = tempfile.mkdtemp(prefix="backuparr-run-")
    cancelled = False

    try:
        for i, name in enumerate(apps, start=1):
            check_cancel()
            if on_progress:
                on_progress(i, len(apps), name)
            log.info("=== %s ===", name)
            app_cfg = cfg["apps"][name]

            work_dir = os.path.join(run_tmp, name)
            archive_path = None

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

                size = os.path.getsize(archive_path)
                dest_failures = []
                for dest_id, root in dest_roots.items():
                    check_cancel()
                    remote_dest = f"{root}/{name}/{archive_name}"
                    log.info("%s: uploading to %s...", name, dest_id)
                    try:
                        rclone_util.copyto(archive_path, remote_dest)
                        log.info("%s: uploaded -> %s (%d bytes)", name, remote_dest, size)
                    except rclone_util.RcloneError as exc:
                        log.error("%s: upload to %s failed: %s", name, dest_id, exc.detail)
                        dest_failures.append(f"{dest_id}: {exc.detail}")

                if dest_failures:
                    failed.append(f"{name}: failed on {'; '.join(dest_failures)}")
                else:
                    ok.append(name)
            except RunCancelled:
                raise
            except Exception as exc:
                log_failure(log, f"{name}: backup failed", exc, app_cfg.get("url"), app=name)
                failed.append(f"{name}: {humanize_error(exc, app_cfg.get('url'), app=name)}")
            finally:
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

    log.info("Applying retention (%sd) per destination", retention_days)
    for root in dest_roots.values():
        rclone_util.delete_older_than(root, f"{retention_days}d")

    return ok, failed
