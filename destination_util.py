"""Resolves each enabled destination's rclone remote root generically, so
backup.py, restore_actions.py and the web UI don't need destination-specific
branching beyond "which id is this".
"""
import errno
import logging
import os
import re
import threading
import time

import atomic_file
import config_store
import dropbox_oauth
import file_cache
import gdrive_oauth
import onedrive_oauth
import rclone_util
import s3_storage
from config_store import APP_NAMES, DEFAULT_LOCAL_DIR, MULTI_INSTANCE_APPS, instance_type


log = logging.getLogger("backuparr")


class DestinationError(RuntimeError):
    """Its message is written for the user. .detail is that message, as an
    rclone_util.RcloneError's is its cause, so either reports the same way."""

    @property
    def detail(self):
        return str(self)


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


# ------------------------------------------------------- Local storage ----
# A USB disk that spins down to save power can answer its first access
# with one of these while it spins back up; the same access works seconds
# later.
_NOT_READY = frozenset({errno.EIO, errno.ETIMEDOUT, errno.EBUSY, errno.EAGAIN})
STORAGE_WAIT_SECONDS = 60
# Retries come at 1, 2 and 4 s apart, then every 5 s, so a disk that has
# woken is noticed within seconds.
_MAX_RETRY_GAP = 5

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
            delay = min(delay * 2, _MAX_RETRY_GAP)
            continue
        if waited:
            log.info("storage ready after %ds", round(time.monotonic() - start))
        return


def wait_for_storage(dest_id, root):
    """Before using a destination: Local storage may be a sleeping disk."""
    if dest_id == "local":
        wait_until_ready(root)


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


# The OAuth destinations, each an rclone remote kept in rclone.conf.
# Each has TOKEN_FIELD (the destination field set once connected).
OAUTH_REMOTES = {"gdrive": gdrive_oauth, "onedrive": onedrive_oauth, "dropbox": dropbox_oauth}
REMOTES = {**OAUTH_REMOTES, "s3": s3_storage}
# These connections can hold refreshed OAuth tokens that config.json lacks.
OWN_REMOTES = {module.REMOTE_NAME: module.RCLONE_TYPE for module in OAUTH_REMOTES.values()}
_REMOTE_ERRORS = (gdrive_oauth.GDriveOAuthError, onedrive_oauth.OneDriveOAuthError,
                  dropbox_oauth.DropboxOAuthError, s3_storage.S3StorageError)


def remote_root(dest_id, dest_cfg):
    """The rclone path to pass to rclone_util for this destination."""
    if dest_id == "local":
        return local_root(dest_cfg)
    if dest_id not in REMOTES:
        raise DestinationError(f"unknown or unsupported destination: {dest_id}")
    try:
        return REMOTES[dest_id].remote_root(dest_cfg)
    except _REMOTE_ERRORS as exc:
        raise DestinationError(str(exc)) from exc


# config.json's and rclone.conf's stamps after sync_saved() last synced
# them, or None when anything else has synced since (#231).
_synced = None
_sync_lock = threading.RLock()


def _stamps():
    return (file_cache.stamp(config_store.CONFIG_PATH),
            file_cache.stamp(os.environ.get("RCLONE_CONFIG", "/config/backuparr/rclone.conf")))


def sync(cfg):
    """Keeps rclone.conf in sync with the connected destinations
    before an operation touches them - safe/cheap to call unconditionally:
    one `rclone config dump`, and a write only for a remote that differs.
    cfg may hold changes not saved yet (a destination's Test)."""
    global _synced
    with _sync_lock:
        _synced = None
        remotes = rclone_util.config_dump()
        for dest_id, module in REMOTES.items():
            module.sync_rclone_remote(cfg["destinations"][dest_id], remotes=remotes)


def sync_saved():
    """sync() for the settings config.json holds, skipped when neither
    config.json nor rclone.conf has changed since it last ran: every History
    and Restore request calls it (#231). rclone writes rclone.conf when it
    refreshes a token, so that counts as a change too."""
    global _synced
    with _sync_lock:  # no other sync() between this one and its stamps
        before = _stamps()  # before the settings are read: a save after this is synced next time
        if None not in before and before == _synced:
            return
        sync(config_store.load_config())
        after = _stamps()
        _synced = after if after[0] == before[0] else None
