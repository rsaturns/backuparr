import json
import logging
import subprocess

logger = logging.getLogger(__name__)


class RcloneError(RuntimeError):
    pass


def _run(args):
    # stdin explicitly closed, not just inherited - --non-interactive config
    # commands shouldn't need it, but an inherited TTY stdin (e.g. a manual
    # docker exec) would otherwise let a command hang waiting for input.
    proc = subprocess.run(["rclone", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RcloneError(f"rclone {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def config_dump():
    """Every remote currently in rclone.conf, as {name: {key: value, ...}}."""
    return json.loads(_run(["config", "dump"]))


def config_set(name, backend_type, fields, force=False):
    """Creates or updates a remote so its fields match `fields` (a dict of
    string->string). Uses `create` only for a brand-new remote, or when
    force=True (an explicit reconnect) - `create` replaces a remote's whole
    definition from scratch, discarding any field not passed this time, so a
    routine resync must use `update` instead, which only touches the given
    keys and leaves everything else alone (in particular, an already-rotated
    OAuth refresh token - see onedrive_oauth.py's sync_rclone_remote for why
    that matters).

    Always adds config_refresh_token=false: without it, `update`/`create` on
    an OAuth-capable backend (drive, onedrive) attempts its own token
    refresh as a side effect of touching *any* field, even ones unrelated to
    auth. Whether and when to write a fresh token is sync_rclone_remote()'s
    call to make from dest_cfg, not something rclone should decide on its
    own."""
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
    """Deletes a single remote file (as opposed to `delete`, which targets a
    directory) - used for manual per-backup deletion from the History tab."""
    _run(["deletefile", remote_path])


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
