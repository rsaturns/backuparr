import json
import logging
import subprocess

logger = logging.getLogger(__name__)


class RcloneError(RuntimeError):
    pass


def _run(args):
    proc = subprocess.run(["rclone", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RcloneError(f"rclone {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def copyto(local_path, remote_path):
    _run(["copyto", local_path, remote_path])


def delete_older_than(remote_dir, min_age):
    """min_age e.g. '14d'."""
    try:
        _run(["delete", "--min-age", min_age, remote_dir])
    except RcloneError as exc:
        logger.warning("retention cleanup failed for %s: %s", remote_dir, exc)


def lsf(remote_dir):
    """Returns [] instead of raising when the directory doesn't exist yet
    (e.g. an app that's never had a successful backup) - same as lsjson."""
    try:
        out = _run(["lsf", remote_dir])
    except RcloneError:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def lsjson(remote_dir):
    """Returns [] instead of raising when the directory doesn't exist yet
    (e.g. an app that's never had a successful backup)."""
    try:
        out = _run(["lsjson", remote_dir])
    except RcloneError:
        return []
    return json.loads(out)


def check_remote(remote_dir):
    """Raises RcloneError if the remote can't be reached (bad remote name,
    auth failure, etc.) - used to validate rclone_remote from the UI. Checks
    just the remote root (e.g. "gdrive:"), not the configured subfolder,
    since that subfolder may not exist yet on a first-ever setup - rclone
    creates it lazily on first upload."""
    remote_root = remote_dir.split("/", 1)[0]
    if not remote_root.endswith(":"):
        remote_root += ":"
    _run(["lsd", "--max-depth", "1", remote_root])
