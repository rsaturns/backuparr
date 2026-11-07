"""Resolves each enabled destination's rclone remote root generically, so
backup.py, restore_actions.py and the web UI don't need destination-specific
branching beyond "which id is this".
"""
import os
import re

import dropbox_oauth
import gdrive_oauth
import onedrive_oauth
import rclone_util
from config_store import APP_NAMES, DEFAULT_LOCAL_DIR, MULTI_INSTANCE_APPS, instance_type


class DestinationError(RuntimeError):
    pass


# Backuparr's own backups (#110) live beside the apps' folders.
SELF_BACKUP_FOLDER = "backuparr"


def is_backup_folder(name):
    """An app or instance's backup folder, or Backuparr's own."""
    return bool(instance_type(name)) or name == SELF_BACKUP_FOLDER


def is_backup_name(app, name):
    """Backuparr names every archive <app>_YYYYMMDD_HHMMSS.<ext> inside an
    <app>/ folder. Those are the only files retention and reset may delete:
    a destination folder can hold other files too."""
    return re.fullmatch(rf"{re.escape(app)}_\d{{8}}_\d{{6}}\.[A-Za-z0-9.]+", name) is not None


def backup_folder_pattern():
    """An rclone --include pattern for every folder Backuparr writes at a
    destination's root: each app, any instance of one (radarr-*) and its own."""
    names = [*APP_NAMES, *(f"{app}-*" for app in MULTI_INSTANCE_APPS), SELF_BACKUP_FOLDER]
    return f"/{{{','.join(names)}}}/**"


def list_backups(root):
    """Everything in Backuparr's folders at a destination's root, as rclone
    lsjson entries ("Path": "<folder>/<name>"). Nothing else there is
    opened: the root of a disk can hold folders Backuparr can't read, such
    as ext4's lost+found, and listing one fails the whole listing."""
    return rclone_util.lsjson(root, recursive=True, include=[backup_folder_pattern()])


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


# The connected destinations, each an rclone remote kept in rclone.conf.
# Each module has TOKEN_FIELD (the destination field set once connected),
# remote_root(), backup_target() and sync_rclone_remote().
REMOTES = {"gdrive": gdrive_oauth, "onedrive": onedrive_oauth, "dropbox": dropbox_oauth}
_REMOTE_ERRORS = (gdrive_oauth.GDriveOAuthError, onedrive_oauth.OneDriveOAuthError, dropbox_oauth.DropboxOAuthError)


def remote_root(dest_id, dest_cfg):
    """The rclone remote root (a plain local path, or "name:" for a
    connected OAuth remote) to pass to rclone_util for this destination."""
    if dest_id == "local":
        return local_root(dest_cfg)
    if dest_id not in REMOTES:
        raise DestinationError(f"unknown or unsupported destination: {dest_id}")
    try:
        return REMOTES[dest_id].remote_root(dest_cfg)
    except _REMOTE_ERRORS as exc:
        raise DestinationError(str(exc)) from exc


def sync(cfg):
    """Keeps rclone.conf in sync with any OAuth-connected destinations
    before an operation touches them - safe/cheap to call unconditionally:
    one `rclone config dump`, and a write only for a remote that differs."""
    remotes = rclone_util.config_dump()
    for dest_id, module in REMOTES.items():
        module.sync_rclone_remote(cfg["destinations"][dest_id], remotes=remotes)
