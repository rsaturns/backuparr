"""Resolves each enabled destination's rclone remote root generically, so
backup.py/restore_actions.py/webui/app.py don't need destination-specific
branching beyond "which id is this". Currently local, gdrive, and onedrive
have real backends - dropbox is still coming_soon in
config_store.DESTINATION_META and never reaches here (enabled_destinations()
already filters it out).
"""
import os

import gdrive_oauth
import onedrive_oauth
from config_store import DEFAULT_LOCAL_DIR


class DestinationError(RuntimeError):
    pass


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
    raise DestinationError(f"unknown or unsupported destination: {dest_id}")


def sync(cfg):
    """Keeps rclone.conf in sync with any OAuth-connected destinations
    before an operation touches them - safe/cheap to call unconditionally."""
    gdrive_oauth.sync_rclone_remote(cfg["destinations"]["gdrive"])
    onedrive_oauth.sync_rclone_remote(cfg["destinations"]["onedrive"])
