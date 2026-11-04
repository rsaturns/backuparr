"""Restores only go onto the app version a backup came from (#108). The
backup's version comes from the manifest; the installed one from the app,
asked as a backup asks it. They must match exactly, so a restore never
leaves an app half-migrated, and a mismatch names the version to install.

A backup with no recorded version (Bazarr, SABnzbd and Tdarr backups made
before the manifest, or one the startup backfill hasn't reached yet)
restores with a warning instead."""
import manifest
from apps.base import AppError
from backup import build_app, humanize_error
from config_store import app_meta, instance_type

# Restored while stopped: they can't be asked their version at restore time,
# so the check runs on the Restore tab first, while they're still running.
STOPPED_FOR_RESTORE = {"emby", "plex", "seerr", "lazylibrarian"}


class RestoreVersionError(AppError):
    pass


def app_label(app):
    meta = app_meta(instance_type(app))
    return meta["label"] if meta else app


def _same(app_type, backup_version, installed):
    if app_type == "lazylibrarian":
        # A commit, which may be shortened to different lengths.
        return backup_version.startswith(installed) or installed.startswith(backup_version)
    return backup_version == installed


def _install_hint(app_type, label, version):
    if app_type == "lazylibrarian":
        # LazyLibrarian has no release numbers; its image tags start with the commit.
        return f"Install the {label} Docker image whose tag starts with {version}, then restore."
    return f"Install {label} {version}, then restore."


def installed_version(app, app_cfg):
    """The running app's version, normalised. Raises RestoreVersionError,
    saying why, when the app can't be asked."""
    label = app_label(app)
    try:
        version = build_app(app, app_cfg).app_version()
    except Exception as exc:
        raise RestoreVersionError(
            f"{app}: couldn't ask {label} for its version, so the backup can't be checked against it - "
            f"{humanize_error(exc, app_cfg.get('url'), app=app)}. {label} must be running for the check."
        ) from None
    normalised = manifest.normalise_version(instance_type(app), version)
    if not normalised:
        raise RestoreVersionError(f"{app}: {label} didn't report its version, so the backup can't be checked against it.")
    return normalised


def check(app, file, app_cfg):
    """Compares a backup with the installed app. Returns
    {"status": "match" | "unknown", "backup_version", "installed_version",
    "message"}; raises RestoreVersionError on a mismatch or when the app
    can't be asked."""
    label, app_type = app_label(app), instance_type(app)
    installed = installed_version(app, app_cfg)
    entry = manifest.get(app, file)
    backup_version = entry.get("app_version") if entry else None
    if not backup_version:
        reason = ("This backup doesn't record " + label + "'s version" if entry
                  else "This backup's version hasn't been read yet")
        return {"status": "unknown", "backup_version": None, "installed_version": installed,
                "message": f"{reason}, so it can't be checked against the installed {label} {installed}."}
    if not _same(app_type, backup_version, installed):
        raise RestoreVersionError(
            f"{app}: this backup is from {label} {backup_version} and {label} {installed} is installed. "
            + _install_hint(app_type, label, backup_version))
    return {"status": "match", "backup_version": backup_version, "installed_version": installed,
            "message": f"This backup is from {label} {backup_version}, the installed version."}


def verify_checksum(app, file, local_path):
    """Raises RestoreVersionError if the downloaded archive isn't the one
    the manifest recorded. Returns whether there was a checksum to compare."""
    entry = manifest.get(app, file)
    expected = entry.get("sha256") if entry else None
    if not expected:
        return False
    if manifest.file_sha256(local_path) != expected:
        raise RestoreVersionError(
            f"{app}: the downloaded backup doesn't match the checksum recorded when it was made - it was changed "
            "or damaged since, so nothing was restored.")
    return True
