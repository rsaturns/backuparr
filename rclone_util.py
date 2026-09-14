import json
import logging
import re
import subprocess

logger = logging.getLogger(__name__)


class RcloneError(RuntimeError):
    pass


SENSITIVE_FIELDS = {"client_secret", "token", "password", "pass"}

# rclone logs one timestamped line per retry attempt (default 3 retries) on
# failure, each repeating the same underlying cause and often wrapping a
# full, unbroken API request URL - unreadable once shown as a single failed
# app's status line. "2026/09/14 03:02:00 ERROR : " / "... NOTICE: " prefix.
_LOG_PREFIX_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\s+(?:[A-Z]+\s*:\s*)?")
_URL_RE = re.compile(r'"https?://[^"]*"')


def _clean_stderr(stderr):
    """Collapses rclone's raw stderr into the single most useful line: the
    last one, since rclone always ends a failed command with its most
    complete NOTICE/Fatal summary line (earlier lines are just the same
    error repeated per retry attempt). Strips that line's timestamp/level
    prefix and collapses any embedded request URL, which carries no useful
    information for a human and is often long enough to overflow the UI."""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    if not lines:
        return "(no output)"
    last = _LOG_PREFIX_RE.sub("", lines[-1], count=1)
    return _URL_RE.sub('"<url>"', last)


def _run(args, redact=()):
    # stdin closed so an inherited TTY (e.g. manual docker exec) can't hang.
    proc = subprocess.run(["rclone", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        message = f"rclone {' '.join(args)} failed: {_clean_stderr(proc.stderr)}"
        for secret in redact:
            if secret:
                message = message.replace(secret, "***")
        raise RcloneError(message)
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
    existing = False if force else name in config_dump()
    args = ["config", "create" if (force or not existing) else "update", name]
    if force or not existing:
        args.append(backend_type)
    for key, value in fields.items():
        args += [key, value]
    args += ["config_refresh_token", "false", "--non-interactive"]
    _run(args, redact=[v for k, v in fields.items() if k in SENSITIVE_FIELDS])


def config_delete(name):
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


def lsjson(remote_dir, recursive=False):
    """Returns [] instead of raising when the directory doesn't exist yet."""
    args = ["lsjson", "--recursive", remote_dir] if recursive else ["lsjson", remote_dir]
    try:
        out = _run(args)
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
