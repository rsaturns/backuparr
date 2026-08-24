"""Resolves each enabled destination's rclone remote root generically, so
backup.py/restore_actions.py/webui/app.py don't need destination-specific
branching beyond "which id is this". Currently local and gdrive are the
only ones with real backends - dropbox/onedrive are coming_soon in
config_store.DESTINATION_META and never reach here (enabled_destinations()
already filters them out).
"""
import os

import gdrive_oauth
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
    raise DestinationError(f"unknown or unsupported destination: {dest_id}")


def sync(cfg):
    """Keeps rclone.conf in sync with any OAuth-connected destinations
    before an operation touches them - safe/cheap to call unconditionally."""
    gdrive_oauth.sync_rclone_remote(cfg["destinations"]["gdrive"])
