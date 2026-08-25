import json
import logging
import subprocess

logger = logging.getLogger(__name__)


class RcloneError(RuntimeError):
    pass


def _run(args):
    # stdin closed so an inherited TTY (e.g. manual docker exec) can't hang.
    proc = subprocess.run(["rclone", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RcloneError(f"rclone {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def config_dump():
    """Every remote currently in rclone.conf, as {name: {key: value, ...}}."""
    return json.loads(_run(["config", "dump"]))


def config_set(name, backend_type, fields, force=False):
    """Creates or updates a remote to match `fields`. Uses `create` (full
    rewrite) only for a new remote or force=True; otherwise `update`,
    which only touches the given keys - preserves fields like an
    already-rotated OAuth token that `create` would discard.

    Always adds config_refresh_token=false, or rclone attempts its own
    token refresh as a side effect of touching any field."""
    existing = name in config_dump()
    args = ["config", "create" if (force or not existing) else "update", name]
    if force or not existing:
        args.append(backend_type)
    for key, value in fields.items():
        args += [key, value]
    args += ["config_refresh_token", "false", "--non-interactive"]
    _run(args)


def config_delete(name):
    if name in config_dump():
        _run(["config", "delete", name])


def copyto(local_path, remote_path):
    _run(["copyto", local_path, remote_path])


def delete_file(remote_path):
    """Deletes a single remote file, unlike `delete` which targets a dir."""
    _run(["deletefile", remote_path])


def delete_older_than(remote_dir, min_age):
    """min_age e.g. '14d'."""
    try:
        _run(["delete", "--min-age", min_age, remote_dir])
    except RcloneError as exc:
        logger.warning("retention cleanup failed for %s: %s", remote_dir, exc)


def lsf(remote_dir):
    """Returns [] instead of raising when the directory doesn't exist yet."""
    try:
        out = _run(["lsf", remote_dir])
    except RcloneError:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def lsjson(remote_dir):
    """Returns [] instead of raising when the directory doesn't exist yet."""
    try:
        out = _run(["lsjson", remote_dir])
    except RcloneError:
        return []
    return json.loads(out)


def check_remote(remote_dir):
    """Raises RcloneError if the remote can't be reached. Checks just the
    remote root, not the subfolder - rclone creates that lazily."""
    remote_root = remote_dir.split("/", 1)[0]
    if not remote_root.endswith(":"):
        remote_root += ":"
    _run(["lsd", "--max-depth", "1", remote_root])
