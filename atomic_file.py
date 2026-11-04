"""Writing Backuparr's own files (config.json, auth.json, its keys)."""
import os
import tempfile


def write_private(path, data):
    """Writes data (str or bytes) to path, readable by this user only. The
    file is replaced in one step, so a reader - or a crash - sees the old
    file or the whole new one, never half of it; a unique temporary name
    keeps concurrent writes from colliding."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=folder, prefix=f".{os.path.basename(path)}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        os.remove(tmp_path)
        raise
