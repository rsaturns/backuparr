"""Restore logic for the web UI, one RESTORES entry per app type. See
apps/*.py for the actual API mechanics; this module just wires config +
rclone + zip handling around them. Each app is built by backup.build_app, exactly as a
backup builds it, so headers and credentials can't drift between the two.
"""
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Callable

import rclone_util
from apps.sabnzbd import MASKED
from backup import build_app
from config_store import instance_type
from destination_util import is_backup_name

# Restored by uploading the archive to the app, which then restarts.
UPLOAD_RESTORE_APPS = {"radarr", "sonarr", "lidarr", "whisparr", "prowlarr"}

# Backup filenames are always <app>_<timestamp>.<ext> (.zip, or an app's own
# archive: Profilarr's .tar.gz, LazyLibrarian's .tgz). filename can come
# straight from a request body, so reject
# anything else to block path traversal into a local path.join() or remote
# rclone path.
SAFE_FILENAME = re.compile(r"^(?!\.)[A-Za-z0-9._-]+\Z")  # no leading dot: "." and ".." are directories


def list_backups(rclone_remote, app_name):
    """Backuparr's archives in app_name's folder, oldest first: never a
    subfolder or another file kept there."""
    remote_dir = f"{rclone_remote.rstrip('/')}/{app_name}/"
    return sorted(name for name in rclone_util.lsf(remote_dir) if is_backup_name(app_name, name))


def latest_backup(rclone_remote, app_name):
    files = list_backups(rclone_remote, app_name)
    if not files:
        raise FileNotFoundError(f"no backups of {app_name} on this destination")
    return files[-1]


def fetch_backup(rclone_remote, app_name, filename=None):
    remote_dir = f"{rclone_remote.rstrip('/')}/{app_name}/"
    if not filename:
        filename = latest_backup(rclone_remote, app_name)
    elif not SAFE_FILENAME.match(filename):
        raise ValueError(f"invalid backup filename: {filename!r}")

    tmp_dir = tempfile.mkdtemp(prefix=f"arrrestore-{app_name}-")
    local_zip = os.path.join(tmp_dir, filename)
    rclone_util.copyto(f"{remote_dir}{filename}", local_zip)
    return tmp_dir, local_zip, filename


def extract_zip(local_zip, dest_dir):
    """zipfile has no built-in Zip-Slip protection, so member paths are
    checked by hand before anything is extracted."""
    os.makedirs(dest_dir, exist_ok=True)
    dest_root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(local_zip) as zf:
        for member in zf.namelist():
            target = os.path.realpath(os.path.join(dest_dir, member))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise ValueError(f"unsafe path in zip: {member!r}")
        zf.extractall(dest_dir)
    return dest_dir


def load_sabnzbd_config(tmp_dir, local_zip):
    extract_dir = extract_zip(local_zip, os.path.join(tmp_dir, "extracted"))
    with open(os.path.join(extract_dir, "sabnzbd_config.json")) as f:
        return json.load(f)


def sabnzbd_server_summary(config):
    """Servers needing a password, for the UI to prompt for."""
    servers = config.get("config", {}).get("servers", [])
    return [
        {"name": s.get("name"), "host": s.get("host"), "needs_password": s.get("password") == MASKED}
        for s in servers
    ]


def _upload(app, tmp_dir, local_zip, options):
    app.restore_upload(local_zip)


def _from_file(app, tmp_dir, local_zip, options):
    app.restore_from_file(local_zip)


def _extracted(app, tmp_dir, local_zip, options):
    return app.restore(extract_zip(local_zip, os.path.join(tmp_dir, "extracted")))


def _bazarr(app, tmp_dir, local_zip, options):
    if not options.get("bazarr_backup_dir"):
        raise ValueError("bazarr restore requires bazarr_backup_dir")
    app.restore_from_file(local_zip, options["bazarr_backup_dir"])


def _nzbget(app, tmp_dir, local_zip, options):
    return {"reloaded": app.restore_from_file(local_zip)}


def _tautulli(app, tmp_dir, local_zip, options):
    return {"summary": _extracted(app, tmp_dir, local_zip, options)}


def _sabnzbd(app, tmp_dir, local_zip, options):
    if options.get("sabnzbd_password_prompt") is None:
        raise ValueError("sabnzbd restore requires sabnzbd_password_prompt")
    config = options.get("sabnzbd_config")
    if config is None:
        config = load_sabnzbd_config(tmp_dir, local_zip)
    return {"summary": app.restore(config, options["sabnzbd_password_prompt"])}


@dataclass(frozen=True)
class Restore:
    """How one app restores. run(app, tmp_dir, local_zip, options) returns
    None or extra result fields; done is the finished message, or a
    function of the result. {name} is the app or instance id."""
    doing: str
    run: Callable
    done: str | Callable


_UPLOAD = Restore("uploading backup to {name}, it will restart", _upload, "{name} restore uploaded, app is restarting")


def _mounted(label):
    return Restore("writing the backup into {name}'s data folder", _from_file,
                   f"{{name}} restore complete, start {label} again to load it")


# Keyed by app type. An app missing here has no automated restore (see
# restore_supported in config_store).
RESTORES = {
    **{name: _UPLOAD for name in UPLOAD_RESTORE_APPS},
    "bazarr": Restore("triggering bazarr restore", _bazarr, "bazarr restore triggered, app is restarting"),
    "tdarr": Restore("restoring tdarr collections", _extracted, "tdarr restore complete"),
    "jellyfin": Restore("copying the backup into jellyfin's backups folder", _from_file,
                        "jellyfin restore started, Jellyfin is restarting to apply it"),
    "emby": _mounted("Emby"),
    "plex": _mounted("Plex"),
    "seerr": _mounted("Seerr"),
    "lazylibrarian": _mounted("LazyLibrarian"),
    "nzbget": Restore("restoring nzbget settings", _nzbget, lambda result: (
        "nzbget restore complete, NZBGet is reloading to apply it" if result.get("reloaded")
        else "nzbget settings restored, but NZBGet didn't confirm the reload - restart NZBGet to apply them")),
    "tautulli": Restore("restoring tautulli", _tautulli, lambda result: (
        "tautulli config staged, finish it in Tautulli" if (result.get("summary") or {}).get("config_staged")
        else "tautulli restore uploaded")),
    "sabnzbd": Restore("restoring sabnzbd config", _sabnzbd, "sabnzbd restore complete"),
}


def _restore_for(app_name):
    restore = RESTORES.get(instance_type(app_name))
    if restore is None:
        raise ValueError(f"no restore action for {app_name!r}")
    return restore


def restore_step(app_name):
    """What the log says while app_name's restore runs."""
    return _restore_for(app_name).doing.format(name=app_name)


def restore_message(app_name, result):
    """The finished message for a restore_app() result."""
    done = _restore_for(app_name).done
    return done(result) if callable(done) else done.format(name=app_name)


def restore_app(app_name, app_cfg, tmp_dir, local_zip, **options):
    """Runs an app or instance id's restore. The caller owns the UX around
    it (confirmation, progress reporting). options: bazarr_backup_dir for
    Bazarr; sabnzbd_password_prompt (and optionally a pre-loaded
    sabnzbd_config) for SABnzbd.

    Returns {"kind": "upload" or the app type, **whatever the restore adds}."""
    restore = _restore_for(app_name)
    extra = restore.run(build_app(app_name, app_cfg), tmp_dir, local_zip, options) or {}
    kind = "upload" if restore is _UPLOAD else instance_type(app_name)
    return {"kind": kind, **extra}
