"""Resolves each enabled destination's rclone remote root generically, so
backup.py/restore_actions.py/webui/app.py don't need destination-specific
branching beyond "which id is this". Local, gdrive, onedrive, and dropbox
all have real backends.
"""
import os
import re

import dropbox_oauth
import gdrive_oauth
import onedrive_oauth
from config_store import DEFAULT_LOCAL_DIR


class DestinationError(RuntimeError):
    pass


def is_backup_name(app, name):
    """Backuparr names every archive <app>_YYYYMMDD_HHMMSS.<ext> inside an
    <app>/ folder. Those are the only files retention and reset may delete:
    a destination folder can hold other files too."""
    return re.fullmatch(rf"{re.escape(app)}_\d{{8}}_\d{{6}}\.[A-Za-z0-9.]+", name) is not None


def remove_local_backups(root, apps):
    """Deletes Backuparr's own archives under a local destination, and each
    app folder left empty - never other files, nor the folder itself."""
    for app in apps:
        folder = os.path.join(root, app)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if is_backup_name(app, name) and os.path.isfile(path) and not os.path.islink(path):
                os.remove(path)
        try:
            os.rmdir(folder)
        except OSError:
            pass  # Not empty: something else lives there.


def local_root(dest_cfg):
    path = (dest_cfg.get("path") or "").strip() or DEFAULT_LOCAL_DIR
    os.makedirs(path, exist_ok=True)
    return path


def remote_root(dest_id, dest_cfg):
    """The rclone remote root (a plain local path, or "name:" for a
    connected OAuth remote) to pass to rclone_util for this destination."""
    if dest_id == "local":
        return local_root(dest_cfg)
    if dest_id == "gdrive":
        try:
            return gdrive_oauth.remote_root(dest_cfg)
        except gdrive_oauth.GDriveOAuthError as exc:
            raise DestinationError(str(exc)) from exc
    if dest_id == "onedrive":
        try:
            return onedrive_oauth.remote_root(dest_cfg)
        except onedrive_oauth.OneDriveOAuthError as exc:
            raise DestinationError(str(exc)) from exc
    if dest_id == "dropbox":
        try:
            return dropbox_oauth.remote_root(dest_cfg)
        except dropbox_oauth.DropboxOAuthError as exc:
            raise DestinationError(str(exc)) from exc
    raise DestinationError(f"unknown or unsupported destination: {dest_id}")


def sync(cfg):
    """Keeps rclone.conf in sync with any OAuth-connected destinations
    before an operation touches them - safe/cheap to call unconditionally."""
    gdrive_oauth.sync_rclone_remote(cfg["destinations"]["gdrive"])
    onedrive_oauth.sync_rclone_remote(cfg["destinations"]["onedrive"])
    dropbox_oauth.sync_rclone_remote(cfg["destinations"]["dropbox"])
