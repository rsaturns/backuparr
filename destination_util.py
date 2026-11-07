"""Resolves each enabled destination's rclone remote root generically, so
backup.py, restore_actions.py and the web UI don't need destination-specific
branching beyond "which id is this".
"""
import errno
import logging
import os
import re
import time

import atomic_file
import dropbox_oauth
import gdrive_oauth
import onedrive_oauth
import rclone_util
from config_store import DEFAULT_LOCAL_DIR, instance_type


log = logging.getLogger("backuparr")


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


def remove_local_backups(root, apps):
    """Deletes Backuparr's own archives under a local destination, each app
    folder left empty, and its wake file - never other files, nor the folder
    itself."""
    marker = os.path.join(root, WAKE_FILE)
    if os.path.isfile(marker) and not os.path.islink(marker):
        os.remove(marker)
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
    wait_until_ready(path)
    os.makedirs(path, exist_ok=True)
    return path


# A USB disk that spins down to save power can answer its first access
# with one of these while it spins back up; the same access works seconds
# later.
_NOT_READY = frozenset({errno.EIO, errno.ETIMEDOUT, errno.EBUSY, errno.EAGAIN})
STORAGE_WAIT_SECONDS = 60


WAKE_FILE = ".backuparr-wake"
_WAKE_TEXT = b"Backuparr reads this file to wake a disk that has gone to sleep before it writes a backup.\n"


def _wake(path):
    """Reads Backuparr's small wake file in `path` from the disk itself: its
    cached copy is dropped first, so the read can't be answered from memory
    (as a listing can). Only a read: a write that fails while a disk spins
    up can make a filesystem such as ext4 turn itself read-only. The file
    is written once, the first time the folder is reachable."""
    marker = os.path.join(path, WAKE_FILE)
    try:
        fd = os.open(marker, os.O_RDONLY)
    except FileNotFoundError:
        atomic_file.write_private(marker, _WAKE_TEXT)
        return
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.pread(fd, len(_WAKE_TEXT), 0)
    finally:
        os.close(fd)


def wait_until_ready(path, timeout=STORAGE_WAIT_SECONDS):
    """Waits up to `timeout` seconds for a local folder's disk to answer,
    logging only when it has to wait; raises DestinationError if it never
    does. Any other error means the disk did answer; it's left for the real
    operation to report, so a full or read-only destination fails, or
    lists, exactly as before."""
    start = time.monotonic()
    delay = 1
    waited = False
    while True:
        try:
            _wake(path)
        except OSError as exc:
            if exc.errno not in _NOT_READY:
                return
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise DestinationError(f"the storage at {path} didn't respond within {timeout}s "
                                       f"({os.strerror(exc.errno)}) - check the disk and its connection") from exc
            if not waited:
                log.info("waiting for storage to be ready...")
                waited = True
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 15)
            continue
        if waited:
            log.info("storage ready after %ds", round(time.monotonic() - start))
        return


def wait_for_storage(dest_id, root):
    """Before using a destination: Local storage may be a sleeping disk."""
    if dest_id == "local":
        wait_until_ready(root)


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
