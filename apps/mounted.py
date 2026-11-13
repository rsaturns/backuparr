"""Helpers for the drivers that work on an app's folder mounted into this
container (Jellyfin, Emby, Plex, Seerr, LazyLibrarian, Vaultwarden)."""
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


# JPEG, PNG, GIF and WebP are compressed already: stored as they are, a
# metadata folder of images zips at disk speed instead of zlib's.
_COMPRESSED_IMAGES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8")


def _already_compressed(path):
    with open(path, "rb") as f:
        head = f.read(12)
    return head.startswith(_COMPRESSED_IMAGES) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _add_file(zf, path, arcname, real_root):
    """Adds one file of the app's folder; False when it's skipped. A symlink
    is followed only to a file inside the mounted folder: one pointing out
    of it could take Backuparr's own files into the archive. A file that
    went away since the folder was listed (the app removed it) is skipped."""
    real = os.path.realpath(path)
    if not _inside(real, real_root) or not os.path.isfile(real):
        return False
    try:
        compression = zipfile.ZIP_STORED if _already_compressed(real) else zipfile.ZIP_DEFLATED
        zf.write(real, arcname, compress_type=compression)
    except FileNotFoundError:
        return False
    return True


def write_archive(zip_path, staged_dir, sources, mounted_root, error, app, label):
    """Writes a backup zip: the files Backuparr made in staged_dir (database
    snapshots, its manifest), then each (path, arcname) in sources - a file
    or a whole folder of the app's mounted folder, read where it is. Nothing
    is copied first, so a large folder such as the app's metadata needs its
    size in free space once, not twice. A source that doesn't exist is left
    out; an unreadable file stops the backup. Returns how many files came
    from sources."""
    real_root = os.path.realpath(mounted_root)
    added = 0

    def unreadable(path, exc):
        rel = os.path.relpath(path, mounted_root)
        raise error(f"{app}: can't read {rel} in {mounted_root} ({exc.strerror or type(exc).__name__}) - "
                    f"run Backuparr with {label}'s PUID:PGID, or give it read access to {label}'s files") from None

    def walk_error(exc):
        # os.walk skips a folder it can't list unless told to raise; one
        # the app removed meanwhile is left out.
        if not isinstance(exc, FileNotFoundError):
            unreadable(exc.filename, exc)

    # Timestamps before 1980, which zip can't hold, are clamped rather than refused.
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        for root, _dirs, files in os.walk(staged_dir):
            for name in sorted(files):
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, staged_dir).replace(os.sep, "/"))
        for path, arcname in sources:
            if not os.path.exists(path):
                continue
            if os.path.isfile(path):
                try:
                    added += _add_file(zf, path, arcname, real_root)
                except PermissionError as exc:
                    unreadable(path, exc)
                continue
            for root, dirs, files in os.walk(path, onerror=walk_error):
                dirs.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    target = arcname + "/" + os.path.relpath(full, path).replace(os.sep, "/")
                    try:
                        added += _add_file(zf, full, target, real_root)
                    except PermissionError as exc:
                        unreadable(full, exc)
    return added


def check_writable(members, root, error, app, label):
    """Refuses before anything is written when a member of a backup zip
    couldn't be written into the app's folder at root, so a permission
    problem doesn't leave half a restore behind."""
    for info in members:
        target = os.path.join(root, *info.filename.split("/"))
        while not os.path.exists(target):
            target = os.path.dirname(target)
        if not os.access(target, os.W_OK):
            raise error(
                f"{app}: can't write {info.filename} in {root} - run Backuparr with "
                f"{label}'s PUID:PGID, or give it write access to {label}'s files"
            )


def write_files(zf, members, root):
    """Writes members of the open zip into the app's folder at root, each at
    its name (which the caller has checked)."""
    for info in members:
        parts = info.filename.split("/")
        folder = root
        for part in parts[:-1]:
            folder = os.path.join(folder, part)
            if not os.path.isdir(folder):
                os.mkdir(folder)
                os.chmod(folder, 0o755)  # Backuparr's private umask would hide it from the app.
        with zf.open(info) as src:
            write_in_place(src, os.path.join(folder, parts[-1]))


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
