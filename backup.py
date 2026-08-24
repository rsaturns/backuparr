#!/usr/bin/env python3
"""Trigger each configured app's own backup mechanism over its API, zip the
result if needed, and upload it to the configured rclone remote (Google
Drive). No app config volumes are read directly - everything goes through
each app's HTTP API. Settings come from config_store (edited via the web UI
or config.json directly), not environment variables.
"""
import logging
import logging.handlers
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import requests

import rclone_util
from apps.bazarr import BazarrApp
from apps.prowlarr import ProwlarrApp
from apps.radarr import RadarrApp
from apps.sabnzbd import SabnzbdApp
from apps.sonarr import SonarrApp
from apps.tdarr import TdarrApp
from config_store import enabled_apps, load_config

LOG_DIR = os.environ.get("ARR_BACKUP_LOG_DIR", "/var/log/arr-backup")
LOG_FILE = os.path.join(LOG_DIR, "backup.log")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("arr-backup")

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
    """Run one backup pass for every enabled app in cfg. Returns (ok, failed)."""
    apps = enabled_apps(cfg)
    if not apps:
        log.error("No apps enabled - nothing to do")
        return [], ["no apps enabled in config"]

    rclone_remote = (cfg.get("rclone_remote") or "").rstrip("/")
    if not rclone_remote:
        log.error("rclone_remote is not configured")
        return [], ["rclone_remote not configured"]

    retention_days = cfg.get("retention_days", 14)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    ok, failed = [], []
    run_tmp = tempfile.mkdtemp(prefix="arrbackup-run-")

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

                remote_dest = f"{rclone_remote}/{name}/{zip_name}"
                rclone_util.copyto(zip_path, remote_dest)
                size = os.path.getsize(zip_path)
                log.info("%s: uploaded -> %s (%d bytes)", name, remote_dest, size)
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

    log.info("Applying retention (%sd) per app", retention_days)
    for name in apps:
        rclone_util.delete_older_than(f"{rclone_remote}/{name}/", f"{retention_days}d")

    return ok, failed


def main():
    cfg = load_config()
    ok, failed = run_backup(cfg)
    notify_url = cfg.get("notify_url")

    if not failed:
        log.info("Backup run complete. OK: %s", ", ".join(ok) or "none")
        notify(notify_url, f"arr-backup OK: {', '.join(ok) or 'none'}")
        return 0
    else:
        log.error("Backup run finished with failures: %s", "; ".join(failed))
        notify(notify_url, f"arr-backup FAILED: {'; '.join(failed)} | OK: {', '.join(ok) or 'none'}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
