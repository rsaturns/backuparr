"""Backup/restore driver for Seerr's config folder.

Seerr has no API that exports or imports its data, so its config folder
(/app/config in the official image) is mounted into Backuparr as data_dir,
and the driver works on the two files Seerr's backup guide covers:

- db/db.sqlite3: users, requests, issues, the blocklist and watchlists.
  It's copied with SQLite's online backup, which takes a consistent
  snapshot while Seerr keeps writing to it - through a read-only mount too.
- settings.json: everything on Seerr's Settings pages - the Plex, Jellyfin
  or Emby connection, Radarr/Sonarr, notification agents - plus its API
  key and clientId, the id Plex knows this Seerr by. So a restored Seerr
  keeps working with Plex and with everything that calls its API.

Left out: the image cache and logs. Only Seerr's default SQLite database is
supported: with PostgreSQL the data isn't in the folder at all.

A restore writes both files back, and is only possible while Seerr is
stopped: overwriting the database of a running server would corrupt it.
Seerr doesn't close its database on shutdown, so its -wal file stays behind
and says nothing about whether it's running. Instead the restore asks
SQLite for sole use of the database - refused while Seerr has it open -
which also folds a stopped Seerr's leftover -wal into the database, so no
stale log is replayed over the restored one.

Auth: the API key goes in the X-Api-Key header, which leaves Authorization
free for a reverse proxy's own credentials. Seerr only accepts it once its
setup has created the admin account.
"""
import hmac
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile

import requests

from apps import sqlite_snapshot

logger = logging.getLogger(f"backuparr.{__name__}")

MANIFEST = "backuparr-seerr.json"
DATABASE = "db/db.sqlite3"
SETTINGS = "settings.json"
_MEMBERS = {MANIFEST, DATABASE, SETTINGS}
_MIB = 1024 * 1024


class SeerrError(RuntimeError):
    pass


def migration_level(path):
    """The newest of Seerr's database migrations applied to the database at
    path - Seerr migrates older databases forward on start, but can't read
    a newer one."""
    db = sqlite_snapshot.open_ro(path)
    try:
        return db.execute("SELECT max(timestamp) FROM migrations").fetchone()[0] or 0
    finally:
        db.close()


def is_seerr_backup(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return MANIFEST in zf.namelist() and zf.testzip() is None
    except zipfile.BadZipFile:
        return False


class SeerrApp:
    def __init__(self, url, api_key, data_dir=None, timeout=30):
        self.url = url.rstrip("/")
        self.data_dir = data_dir or None
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    def identity(self):
        """Seerr's version (from an admin-only endpoint, so it also proves
        the API key) and its clientId."""
        res = self.session.get(f"{self.url}/api/v1/settings/about", timeout=10)
        if res.status_code in (401, 403):
            raise SeerrError(
                "seerr: unauthorized - check the API key (Settings > General), "
                "and finish Seerr's setup first"
            )
        res.raise_for_status()
        version = res.json().get("version")
        public = self.session.get(f"{self.url}/api/v1/settings/public", timeout=10)
        public.raise_for_status()
        return version, public.json().get("plexClientIdentifier")

    def _path(self, *parts):
        return os.path.join(self.data_dir, *parts)

    @staticmethod
    def _settings(path):
        try:
            with open(path, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError) as exc:
            raise SeerrError(f"seerr: can't read {SETTINGS} ({exc})") from exc
        if not isinstance(settings, dict) or not isinstance(settings.get("main"), dict):
            raise SeerrError(f"seerr: {SETTINGS} isn't Seerr's settings file")
        return settings

    def _require_folder(self, client_id=None):
        if not self.data_dir:
            raise SeerrError("seerr: set the data folder - Seerr's /app/config folder, mounted into this container")
        if not os.path.isdir(self.data_dir):
            raise SeerrError(
                f"seerr: data folder {self.data_dir} doesn't exist in this container - "
                "mount the folder Seerr has at /app/config there"
            )
        if not os.path.isfile(self._path(SETTINGS)):
            raise SeerrError(
                f"seerr: {self.data_dir} doesn't look like Seerr's config folder (no {SETTINGS}) - "
                "mount the folder Seerr has at /app/config, not a folder inside it (a new install creates it on first start)"
            )
        if not os.path.isfile(self._path(*DATABASE.split("/"))):
            raise SeerrError(
                f"seerr: {self.data_dir} has no {DATABASE} - Backuparr backs up Seerr's default SQLite "
                "database; a Seerr using PostgreSQL isn't supported"
            )
        found = self._settings(self._path(SETTINGS)).get("clientId")
        if client_id and found and found != client_id:
            raise SeerrError(
                f"seerr: {self.data_dir} holds another Seerr server's data (client id {found}, "
                f"this server is {client_id}) - check the volume mount"
            )

    def test_connection(self):
        version, client_id = self.identity()
        self._require_folder(client_id)
        try:
            migration_level(self._path(*DATABASE.split("/")))
        except sqlite3.Error as exc:
            raise SeerrError(f"seerr: can't read {DATABASE} in {self.data_dir} ({exc}) - check its permissions") from exc
        if not os.access(self.data_dir, os.W_OK):
            return f"seerr {version or '?'} reachable; data folder is read-only, so backups work but restores won't"
        return f"seerr {version or '?'} reachable, data folder OK"

    def backup(self, dest_dir):
        version, client_id = self.identity()
        self._require_folder(client_id)
        logger.info("seerr: copying the database and settings (no image cache or logs)...")
        os.makedirs(os.path.join(dest_dir, "db"), exist_ok=True)

        database = os.path.join(dest_dir, *DATABASE.split("/"))
        status = sqlite_snapshot.snapshot(self._path(*DATABASE.split("/")), database)
        if status != "ok":
            raise SeerrError(f"seerr: the copy of {DATABASE} failed SQLite's integrity check ({status})")
        settings = os.path.join(dest_dir, SETTINGS)
        shutil.copyfile(self._path(SETTINGS), settings)
        self._settings(settings)

        manifest = {"seerr_version": version, "client_id": client_id, "migration": migration_level(database)}
        with open(os.path.join(dest_dir, MANIFEST), "w") as f:
            json.dump(manifest, f, indent=2)
        logger.info("seerr: backed up the database and settings from Seerr %s", version or "?")
        return dest_dir

    def _check_stopped(self):
        try:
            running = self.session.get(f"{self.url}/api/v1/status", timeout=5).ok
        except requests.RequestException:
            running = False
        if running:
            raise SeerrError("seerr: stop Seerr before restoring - it's still answering at " + self.url)
        # Leaving WAL mode needs sole use of the database: refused while
        # Seerr has it open, and when granted it folds the -wal into the
        # database and deletes it. Seerr turns WAL back on when it starts.
        db = sqlite3.connect(self._path(*DATABASE.split("/")), timeout=0, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=DELETE").fetchone()
        except sqlite3.OperationalError as exc:
            raise SeerrError(
                "seerr: stop Seerr before restoring - its database is in use"
                + (" (is it still running somewhere?)" if "locked" in str(exc) else f" ({exc})")
            ) from exc
        finally:
            db.close()

    def restore_from_file(self, local_zip_path):
        """Writes the backup's database and settings.json over Seerr's while
        Seerr is stopped. Files are rewritten in place, so they keep Seerr's
        owner and permissions."""
        if not is_seerr_backup(local_zip_path):
            raise SeerrError("seerr: this file isn't a Seerr backup made by Backuparr")
        with zipfile.ZipFile(local_zip_path) as zf:
            names = {i.filename for i in zf.infolist() if not i.is_dir()}
        if names != _MEMBERS:
            unexpected = sorted(names - _MEMBERS) or sorted(_MEMBERS - names)
            raise SeerrError(f"seerr: unexpected contents in the backup: {unexpected[0]!r}")
        self._require_folder()
        target = self._path(*DATABASE.split("/"))
        settings = self._path(SETTINGS)
        if not all(os.access(p, os.W_OK) for p in (self.data_dir, os.path.dirname(target), target, settings)):
            raise SeerrError(
                f"seerr: Seerr's files in {self.data_dir} aren't writable - mount the data folder "
                "read-write to restore, and run Backuparr as Seerr's user (1000:1000 in the official image)"
            )

        with tempfile.TemporaryDirectory(prefix="seerr-restore-") as work:
            with zipfile.ZipFile(local_zip_path) as zf:
                restored = zf.extract(DATABASE, work)
                restored_settings = zf.extract(SETTINGS, work)
            client_id = self._settings(restored_settings).get("clientId")
            try:
                backup_level = migration_level(restored)
            except sqlite3.Error as exc:
                raise SeerrError(f"seerr: the backup's database can't be read ({exc})") from exc
            installed = migration_level(target)
            if backup_level > installed:
                raise SeerrError(
                    "seerr: this backup comes from a newer Seerr than the one installed - update Seerr first"
                )
            self._check_stopped()
            # "wb" truncates in place: owner and mode stay Seerr's.
            for src_path, dst_path in ((restored, target), (restored_settings, settings)):
                with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
                    shutil.copyfileobj(src, dst, _MIB)
        # _check_stopped removed the -wal; any -shm index left is stale.
        for leftover in (target + "-wal", target + "-shm"):
            if os.path.exists(leftover):
                os.remove(leftover)
        logger.info("seerr: restored the database and settings (client id %s) into %s - start Seerr to load them",
                    client_id or "?", self.data_dir)
