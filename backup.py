"""Trigger each configured app's own backup mechanism over its API, zip the
result if needed, and upload it to the configured rclone remote. No app
config volumes are read directly - everything goes through each app's
HTTP API. Settings come from config_store, not environment variables.
"""
import logging
import logging.handlers
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

import destination_util
import rclone_util
from apps.bazarr import BazarrApp
from apps.profilarr import ProfilarrApp
from apps.prowlarr import ProwlarrApp
from apps.radarr import RadarrApp
from apps.sabnzbd import SabnzbdApp
from apps.sonarr import SonarrApp
from apps.tautulli import TautulliApp
from apps.tdarr import TdarrApp
from config_store import enabled_apps, enabled_destinations

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


def humanize_error(exc):
    """Turns a raw requests exception into a short, human-readable message
    instead of a Python exception repr. Anything else passes through as
    str(exc) unchanged."""
    if isinstance(exc, (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL)):
        return "that doesn't look like a valid URL - it should start with http:// or https://"
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return "couldn't connect - check the URL and that it's reachable from this container"
    return str(exc)


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


def run_backup(cfg, on_progress=None):
    """Run one backup pass for every enabled app, uploading to every
    enabled destination. Returns (ok, failed) - failed entries are
    "<app>: <message>" strings.

    on_progress(index, total, name), if given, is called right before each
    app starts - lets a caller (e.g. the web UI) show "app N of M"."""
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

    try:
        for i, name in enumerate(apps, start=1):
            if on_progress:
                on_progress(i, len(apps), name)
            log.info("=== %s ===", name)
            app_cfg = cfg["apps"][name]

            work_dir = os.path.join(run_tmp, name)
            os.makedirs(work_dir, exist_ok=True)
            zip_name = f"{name}_{timestamp}.zip"
            zip_path = os.path.join(run_tmp, zip_name)

            try:
                app = build_app(name, app_cfg)
                result = app.backup(work_dir)
                result_path = Path(result)

                if result_path.is_dir():
                    zip_dir(result_path, zip_path)
                else:
                    # Already a zip from the app itself - just rename it.
                    shutil.copy(result_path, zip_path)

                size = os.path.getsize(zip_path)
                dest_failures = []
                for dest_id, root in dest_roots.items():
                    remote_dest = f"{root}/{name}/{zip_name}"
                    log.info("%s: uploading to %s...", name, dest_id)
                    try:
                        rclone_util.copyto(zip_path, remote_dest)
                        log.info("%s: uploaded -> %s (%d bytes)", name, remote_dest, size)
                    except rclone_util.RcloneError as exc:
                        log.error("%s: upload to %s failed: %s", name, dest_id, exc)
                        dest_failures.append(f"{dest_id}: {exc}")

                if dest_failures:
                    failed.append(f"{name}: failed on {'; '.join(dest_failures)}")
                else:
                    ok.append(name)
            except Exception as exc:
                log.exception("%s: backup failed", name)
                failed.append(f"{name}: {humanize_error(exc)}")
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
                if os.path.exists(zip_path):
                    os.remove(zip_path)
    finally:
        shutil.rmtree(run_tmp, ignore_errors=True)

    log.info("Applying retention (%sd) per app per destination", retention_days)
    for root in dest_roots.values():
        for name in apps:
            rclone_util.delete_older_than(f"{root}/{name}/", f"{retention_days}d")

    return ok, failed
