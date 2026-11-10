"""Adds backups made before the manifest existed - or found on a destination
without an entry - to the manifest: each archive's SHA-256, and the app
version read from inside it. Runs in the background at startup; whatever it
already recorded is skipped next time, so an interrupted pass picks up where
it stopped.

Where each app's archive keeps its version (checked against real backups):
the *arrs' INFO file; Profilarr's data/INFO.json; Jellyfin's manifest.json;
Tautulli's version_info table; LazyLibrarian's config.ini (a commit);
Pi-hole's pihole.toml header (FTL's version, as apps/pihole.py records it); and
for Plex, Emby, Seerr and NZBGet the backuparr-<app>.json Backuparr writes.
Bazarr, SABnzbd and Tdarr archives hold none, so theirs is recorded as
unknown. An encrypted archive (#147) is decrypted with this install's key to
read it; one made with another key is recorded with its version unknown."""
import configparser
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import zipfile

import archive_crypto
import destination_util
import manifest
import rclone_util
from config_store import enabled_destinations, instance_type, load_config

log = logging.getLogger("backuparr.manifest")

_SERVARR = {"radarr", "sonarr", "lidarr", "whisparr", "prowlarr"}
_OWN_MANIFEST = {"plex", "emby", "seerr", "nzbget"}
_MAX_MEMBER = 1024 * 1024  # A version file larger than this isn't one.

_running = threading.Lock()
_PAUSE_POLL_SECONDS = 5


def _wait_while(should_pause):
    """Holds off while should_pause() is true - a backup or restore is
    running - so the backfill doesn't share the connection with it. The
    download already in progress, at most one archive, still finishes."""
    if not should_pause or not should_pause():
        return
    log.info("manifest: paused while a backup or restore runs")
    while should_pause():
        time.sleep(_PAUSE_POLL_SECONDS)
    log.info("manifest: carrying on with existing backups")


def _zip_text(archive, name):
    with zipfile.ZipFile(archive) as z:
        try:
            info = z.getinfo(name)
        except KeyError:
            return None
        if info.file_size > _MAX_MEMBER:
            return None
        return z.read(info).decode("utf-8", "replace")


def _tar_text(archive, name):
    with tarfile.open(archive) as t:
        try:
            member = t.getmember(name)
        except KeyError:
            return None
        if not member.isfile() or member.size > _MAX_MEMBER:
            return None
        return t.extractfile(member).read().decode("utf-8", "replace")


def _tautulli_version(archive):
    with zipfile.ZipFile(archive) as z:
        try:
            info = z.getinfo("tautulli.db")
        except KeyError:
            return None
        with tempfile.TemporaryDirectory(prefix="backuparr-tautulli-") as tmp:
            db_path = os.path.join(tmp, "tautulli.db")
            with z.open(info) as src, open(db_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = db.execute("SELECT value FROM version_info WHERE key = 'version'").fetchone()
            finally:
                db.close()
    return row[0] if row else None


def _lazylibrarian_version(archive):
    text = _tar_text(archive, "config.ini")
    if text is None:
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    for section in parser.sections():
        if parser.has_option(section, "current_version"):
            return parser.get(section, "current_version")
    return None


# pihole.toml's first line: "# Pi-hole configuration file (v6.8)".
_PIHOLE_TOML_VERSION = re.compile(r"#\s*Pi-hole configuration file \((v[0-9][^)\s]*)\)")


def _pihole_version(archive):
    text = _zip_text(archive, "etc/pihole/pihole.toml")
    match = _PIHOLE_TOML_VERSION.match(text or "")
    return f"FTL {match.group(1)}" if match else None


def read_version(app_type, archive):
    """The app version recorded inside a backup archive, as written there,
    or None when it has none (or it can't be read)."""
    try:
        if app_type in _SERVARR:
            text = _zip_text(archive, "INFO")
            return text.splitlines()[0] if text and text.strip() else None
        if app_type == "profilarr":
            text = _tar_text(archive, "data/INFO.json")
            return json.loads(text).get("appVersion") if text else None
        if app_type == "jellyfin":
            text = _zip_text(archive, "manifest.json")
            return json.loads(text).get("ServerVersion") if text else None
        if app_type == "tautulli":
            return _tautulli_version(archive)
        if app_type == "lazylibrarian":
            return _lazylibrarian_version(archive)
        if app_type == "pihole":
            return _pihole_version(archive)
        if app_type in _OWN_MANIFEST:
            text = _zip_text(archive, f"backuparr-{app_type}.json")
            return json.loads(text).get(f"{app_type}_version") if text else None
    except (OSError, ValueError, AttributeError, zipfile.BadZipFile, tarfile.TarError, sqlite3.Error,
            configparser.Error) as exc:
        log.warning("manifest: couldn't read the version inside %s (%s)", os.path.basename(archive), type(exc).__name__)
    return None


def _backups_on(root):
    """[(app, file, size)] for each of Backuparr's archives under root."""
    found = []
    for entry in destination_util.list_backups(root):
        app, sep, name = entry.get("Path", "").partition("/")
        if entry.get("IsDir") or not sep or not instance_type(app) or not destination_util.is_backup_name(app, name):
            continue
        found.append((app, name, entry.get("Size")))
    return found


def _examine(dest_id, root, app, name, tmp_dir):
    """(app_version, sha256, encrypted) of one archive. A local destination's
    file is read where it is; any other is downloaded to tmp_dir first. The
    SHA-256 is of the file as stored."""
    if dest_id == "local":
        local = os.path.join(root, app, name)
    else:
        local = os.path.join(tmp_dir, name)
        rclone_util.copyto(f"{root}/{app}/{name}", local)
    try:
        sha256 = manifest.file_sha256(local)
        encrypted = archive_crypto.is_encrypted(local)
        return _version_in(app, name, local, tmp_dir, encrypted), sha256, encrypted
    finally:
        if dest_id != "local":
            os.remove(local)


def _version_in(app, name, local, tmp_dir, encrypted):
    if not encrypted:
        return manifest.normalise_version(instance_type(app), read_version(instance_type(app), local))
    plain = os.path.join(tmp_dir, "plain-" + archive_crypto.plain_name(name))
    try:
        archive_crypto.decrypt_file(local, plain)
    except archive_crypto.ArchiveCryptoError as exc:
        log.warning("manifest: couldn't read the version inside %s (%s)", name, exc)
        return None
    try:
        return manifest.normalise_version(instance_type(app), read_version(instance_type(app), plain))
    finally:
        os.remove(plain)


def backfill_destination(dest_id, root, expected_generation, should_stop=None, should_pause=None):
    """Records every unrecorded backup on one destination. Returns how many
    it added, or None if the manifest was cleared meanwhile."""
    added = 0
    tmp_dir = tempfile.mkdtemp(prefix="backuparr-backfill-")
    try:
        for app, name, size in _backups_on(root):
            _wait_while(should_pause)
            if should_stop and should_stop():
                return added
            entry = manifest.get(app, name)
            if entry and dest_id in entry.get("destinations", []):
                continue
            if entry and entry.get("sha256") and entry.get("size") == size:
                # The same run's archive, already read from another destination.
                version, sha256, encrypted = entry.get("app_version"), entry["sha256"], entry.get("encrypted", False)
            else:
                try:
                    version, sha256, encrypted = _examine(dest_id, root, app, name, tmp_dir)
                except (OSError, rclone_util.RcloneError) as exc:
                    # Gone since the listing, or unreachable: tried again next start.
                    log.warning("manifest: skipped %s/%s on %s: %s", app, name, dest_id,
                                getattr(exc, "detail", None) or getattr(exc, "strerror", None) or type(exc).__name__)
                    continue
            if not manifest.add_found(app, name, dest_id, app_version=version, sha256=sha256, size=size,
                                      expected_generation=expected_generation, encrypted=encrypted):
                return None
            added += 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return added


def backfill(cfg=None, should_stop=None, should_pause=None):
    """One pass over every enabled destination; at most one runs at a time.
    should_pause(), if given, is checked before each destination and each
    archive, and the pass waits while it's true."""
    if not _running.acquire(blocking=False):
        return
    try:
        _wait_while(should_pause)
        expected_generation = manifest.generation()
        cfg = cfg or load_config()
        destinations = enabled_destinations(cfg)
        if not destinations:
            return
        destination_util.sync(cfg)
        for dest_id in destinations:
            _wait_while(should_pause)
            try:
                root = destination_util.remote_root(dest_id, cfg["destinations"][dest_id]).rstrip("/")
                added = backfill_destination(dest_id, root, expected_generation, should_stop, should_pause)
            except (destination_util.DestinationError, rclone_util.RcloneError) as exc:
                log.warning("manifest: couldn't check the backups on %s: %s", dest_id, exc.detail)
                continue
            if added is None:
                return
            if added:
                log.info("manifest: recorded %d existing backup(s) on %s", added, dest_id)
    except Exception:
        log.exception("manifest: checking existing backups failed")
    finally:
        _running.release()


def start(should_pause=None):
    """Runs backfill() in a background thread."""
    threading.Thread(target=backfill, kwargs={"should_pause": should_pause}, name="manifest-backfill", daemon=True).start()
