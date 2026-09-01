"""Restore logic shared by the restore.py CLI and the web UI, so both stay
in sync with a single implementation per app. See apps/*.py for the actual
API mechanics; this module just wires config + rclone + zip handling around
them.
"""
import json
import os
import re
import tempfile
import zipfile

import rclone_util
from apps.bazarr import BazarrApp
from apps.prowlarr import ProwlarrApp
from apps.radarr import RadarrApp
from apps.sabnzbd import MASKED, SabnzbdApp
from apps.sonarr import SonarrApp
from apps.tautulli import TautulliApp
from apps.tdarr import TdarrApp

UPLOAD_RESTORE_APPS = {"radarr": RadarrApp, "sonarr": SonarrApp, "prowlarr": ProwlarrApp}

# Backup filenames are always <app>_<timestamp>.zip. filename can come
# straight from a request body, so reject anything else to block path
# traversal into a local path.join() or remote rclone path.
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")


def list_backups(rclone_remote, app_name):
    remote_dir = f"{rclone_remote.rstrip('/')}/{app_name}/"
    return sorted(rclone_util.lsf(remote_dir))


def fetch_backup(rclone_remote, app_name, filename=None):
    remote_dir = f"{rclone_remote.rstrip('/')}/{app_name}/"
    if not filename:
        files = list_backups(rclone_remote, app_name)
        if not files:
            raise FileNotFoundError(f"No backups found at {remote_dir}")
        filename = files[-1]
    elif not SAFE_FILENAME.match(filename):
        raise ValueError(f"invalid backup filename: {filename!r}")

    tmp_dir = tempfile.mkdtemp(prefix=f"arrrestore-{app_name}-")
    local_zip = os.path.join(tmp_dir, filename)
    rclone_util.copyto(f"{remote_dir}{filename}", local_zip)
    return tmp_dir, local_zip, filename


def extract_zip(local_zip, dest_dir):
    """zipfile has no built-in Zip-Slip protection, so member paths are
    checked by hand before anything is extracted."""
    os.makedirs(dest_dir, exist_ok=True)
    dest_root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(local_zip) as zf:
        for member in zf.namelist():
            target = os.path.realpath(os.path.join(dest_dir, member))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise ValueError(f"unsafe path in zip: {member!r}")
        zf.extractall(dest_dir)
    return dest_dir


def restore_upload_app(name, app_cfg, local_zip):
    app_cls = UPLOAD_RESTORE_APPS[name]
    app = app_cls(app_cfg["url"], app_cfg["api_key"])
    return app.restore_upload(local_zip)


def restore_bazarr(app_cfg, local_zip, backup_dir):
    app = BazarrApp(app_cfg["url"], app_cfg["api_key"])
    return app.restore_from_file(local_zip, backup_dir)


def restore_tdarr(app_cfg, tmp_dir, local_zip):
    extract_dir = extract_zip(local_zip, os.path.join(tmp_dir, "extracted"))
    app = TdarrApp(app_cfg["url"], api_key=app_cfg.get("api_key") or None)
    app.restore(extract_dir)


def restore_tautulli(app_cfg, tmp_dir, local_zip):
    extract_dir = extract_zip(local_zip, os.path.join(tmp_dir, "extracted"))
    app = TautulliApp(app_cfg["url"], app_cfg["api_key"])
    return app.restore(extract_dir)


def load_sabnzbd_config(tmp_dir, local_zip):
    extract_dir = extract_zip(local_zip, os.path.join(tmp_dir, "extracted"))
    with open(os.path.join(extract_dir, "sabnzbd_config.json")) as f:
        return json.load(f)


def sabnzbd_server_summary(config):
    """Servers needing a password, for the UI/CLI to prompt for."""
    servers = config.get("config", {}).get("servers", [])
    return [
        {"name": s.get("name"), "host": s.get("host"), "needs_password": s.get("password") == MASKED}
        for s in servers
    ]


def restore_sabnzbd(app_cfg, config, password_prompt):
    app = SabnzbdApp(app_cfg["url"], app_cfg["api_key"])
    return app.restore(config, password_prompt)
