"""Trigger each configured app's own backup mechanism over its API, zip the
result if needed, and upload it to the configured rclone remote (Google
Drive). No app config volumes are read directly - everything goes through
each app's HTTP API. Settings come from config_store (edited via the web UI
or config.json directly), not environment variables.

run_backup()/build_app() are imported directly by webui/app.py, which is
the only caller now that scheduling happens in-process there instead of via
a cron job shelling out to this file.
"""
import logging
import logging.handlers
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import requests

import destination_util
import rclone_util
from apps.bazarr import BazarrApp
from apps.prowlarr import ProwlarrApp
from apps.radarr import RadarrApp
from apps.sabnzbd import SabnzbdApp
from apps.sonarr import SonarrApp
from apps.tdarr import TdarrApp
from config_store import enabled_apps, enabled_destinations

LOG_DIR = os.environ.get("BACKUPARR_LOG_DIR", "/var/log/backuparr")
LOG_FILE = os.path.join(LOG_DIR, "backup.log")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("backuparr")

# So the web UI's status view can show the result of cron-triggered runs
# too, not just ones it started itself in-process (see webui/app.py).
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_file_handler)
except OSError:
    log.warning("could not open %s for writing, file logging disabled", LOG_FILE)


def build_app(name, app_cfg):
    if name == "radarr":
        return RadarrApp(app_cfg["url"], app_cfg["api_key"])
    if name == "sonarr":
        return SonarrApp(app_cfg["url"], app_cfg["api_key"])
    if name == "prowlarr":
        return ProwlarrApp(app_cfg["url"], app_cfg["api_key"])
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
    raise ValueError(f"Unknown app: {name}")


def zip_dir(src_dir, zip_path):
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, src_dir))


def notify(notify_url, message):
    if not notify_url:
        return
    try:
        requests.post(notify_url, data=message.encode("utf-8"), timeout=10)
    except requests.RequestException:
        log.warning("notify: failed to reach NOTIFY_URL")


def run_backup(cfg):
    """Run one backup pass for every enabled app in cfg, uploading each to
    every enabled destination. Returns (ok, failed) - ok lists app names
    that made it to every destination, failed lists "<app>: <message>"
    strings (used both for per-app failures and destination-level ones, so
    the Overview tab's app-id parsing keeps working either way)."""
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
        for name in apps:
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
                    # Already a zip produced by the app itself (Radarr/Sonarr/
                    # Prowlarr/Bazarr) - just stage it under our naming scheme.
                    shutil.copy(result_path, zip_path)

                size = os.path.getsize(zip_path)
                dest_failures = []
                for dest_id, root in dest_roots.items():
                    remote_dest = f"{root}/{name}/{zip_name}"
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
                failed.append(f"{name}: {exc}")
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
