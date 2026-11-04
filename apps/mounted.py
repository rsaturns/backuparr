"""Helpers for the drivers that work on an app's folder mounted into this
container (Jellyfin, Emby, Plex, Seerr, LazyLibrarian)."""
import os
import shutil
import sqlite3
import zipfile

_MIB = 1024 * 1024


def require_dir(path, error, app, kind, folder, mount_hint):
    """Raises error unless path is set and exists. kind is "data folder" or
    "backups folder"; folder says which of the app's folders it is, and
    mount_hint where to mount it."""
    if not path:
        raise error(f"{app}: set the {kind} - {folder}, mounted into this container")
    if not os.path.isdir(path):
        raise error(f"{app}: {kind} {path} doesn't exist in this container - {mount_hint}")


def is_backup_zip(path, required):
    """Whether path is a sound zip holding every name in required."""
    try:
        with zipfile.ZipFile(path) as zf:
            return set(required) <= set(zf.namelist()) and zf.testzip() is None
    except zipfile.BadZipFile:
        return False


def write_in_place(src, target):
    """Copies the file object src to target. "wb" on an existing file
    truncates it in place, so it keeps the app's owner and mode; a new file
    is made readable, since Backuparr's private umask would hide it from an
    app running as another user."""
    existed = os.path.exists(target)
    with open(target, "wb") as dst:
        shutil.copyfileobj(src, dst, _MIB)
    if not existed:
        os.chmod(target, 0o644)


def copy_in_place(src_path, target):
    with open(src_path, "rb") as src:
        write_in_place(src, target)


def claim_database(path, error, app, label):
    """Leaving WAL mode needs sole use of the database: refused while the
    app has it open, and when granted it folds the -wal into the database
    and deletes it. The app turns WAL back on when it starts."""
    db = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.OperationalError as exc:
        raise error(
            f"{app}: stop {label} before restoring - its database is in use"
            + (" (is it still running somewhere?)" if "locked" in str(exc) else f" ({exc})")
        ) from exc
    finally:
        db.close()


def remove_journal_files(path):
    """Removes a database's -wal and -shm, stale once it has been replaced
    while the app was stopped."""
    for leftover in (f"{path}-wal", f"{path}-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)
