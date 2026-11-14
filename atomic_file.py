"""Writing Backuparr's own files (config.json, auth.json, its keys)."""
import errno
import os
import tempfile

# A filesystem that can't flush to disk on request says so with one of
# these; the write itself still worked.
_NO_FSYNC = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP})


def _fsync(fd):
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _NO_FSYNC:
            raise


def write_private(path, data):
    """Writes data (str or bytes) to path, readable by this user only. The
    file is replaced in one step, so a reader - or a crash - sees the old
    file or the whole new one, never half of it; a unique temporary name
    keeps concurrent writes from colliding. It's flushed to disk before the
    rename and the folder after it, so a power cut can't leave the new name
    with nothing in it (#229) - for secrets.key that would lose every stored
    secret and the key to every encrypted backup."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=folder, prefix=f".{os.path.basename(path)}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            _fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        os.remove(tmp_path)
        raise
    folder_fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        _fsync(folder_fd)
    finally:
        os.close(folder_fd)
