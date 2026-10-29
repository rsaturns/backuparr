"""Backup/restore driver for Plex Media Server.

A backup holds two things:
- The library database (com.plexapp.plugins.library.db), from Plex's own
  API with the server owner's Plex token: GET /diagnostics/databases is
  the "Download Database" export, a consistent snapshot Plex makes itself.
  The database holds the libraries, metadata, playlists, collections and
  every user's watched state, progress and ratings. Its custom SQLite
  tokenizer means only Plex can check or rebuild it, which is why the
  snapshot comes from Plex rather than from the file.
- Preferences.xml, the server's settings and its identity
  (ProcessedMachineIdentifier, which plex.tv knows the server - and the
  libraries shared with other accounts - by). Plex's API can't export it,
  so it's read from Plex's data folder ("Plex Media Server", in Plex's
  /config at Library/Application Support/Plex Media Server in both the
  official and linuxserver images), mounted into Backuparr as data_dir.
Images, media, caches and codecs are left out.

A restore writes both back while Plex is stopped, as Plex's guide to
moving a server does by hand: a rebuilt server comes back as the same
server, so users keep their access. A running Plex keeps a .db-wal file next
to its databases, so any -wal file means Plex is running - or was not shut
down cleanly - and the restore is refused before anything is written.

Auth: the token goes in the X-Plex-Token header, never the URL.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests

logger = logging.getLogger(f"backuparr.{__name__}")

MANIFEST = "backuparr-plex.json"
LIBRARY_DB = "com.plexapp.plugins.library.db"
PREFERENCES = "Preferences.xml"
_DATABASES = ("Plug-in Support", "Databases")
_PROGRESS_INTERVAL = 10
_MIB = 1024 * 1024

# Plex's export names the snapshot databaseBackup.db, sometimes with a UUID
# appended; other variants use the on-disk name.
_EXPORTED_DB = re.compile(
    r"(?:com\.plexapp\.plugins\.library\.db|databaseBackup\.db"
    r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})?)", re.IGNORECASE,
)


class PlexError(RuntimeError):
    pass


def schema_version(path):
    """Newest migration Plex applied to a library database. Plex migrates
    an older database forward on startup, but can't open a newer one."""
    db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)
    try:
        row = db.execute("SELECT max(CAST(version AS INTEGER)) FROM schema_migrations").fetchone()
    except sqlite3.Error as exc:
        raise PlexError(f"plex: {os.path.basename(path)} isn't a readable Plex library database ({exc})") from None
    finally:
        db.close()
    return row[0] or 0


def is_plex_backup(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return {MANIFEST, LIBRARY_DB, PREFERENCES} <= set(zf.namelist()) and zf.testzip() is None
    except zipfile.BadZipFile:
        return False


class PlexApp:
    def __init__(self, url, api_key, data_dir=None, timeout=30):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None
                     and not parsed.query and not parsed.fragment and parsed.port != 0)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise PlexError("plex: enter an HTTP(S) server URL without credentials or query parameters")
        if not isinstance(api_key, str) or not api_key.strip():
            raise PlexError("plex: the server owner's Plex token is required")
        self.url = url.rstrip("/")
        self.data_dir = data_dir or None
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Plex-Token": api_key})

    def _get(self, path, **kwargs):
        url = self.url + path
        origin = urlsplit(self.url)
        endpoint = urlsplit(path).path
        # Follow only same-origin redirects, explicitly: Requests would
        # forward the custom X-Plex-Token header to another host.
        for hop in range(6):
            try:
                response = self.session.get(url, allow_redirects=False, **kwargs)
            except requests.RequestException:
                raise PlexError(f"plex: could not reach {endpoint}; check server network access") from None
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("Location", "")
            response.close()
            try:
                target = urlsplit(urljoin(url, location))
                default_port = 443 if origin.scheme == "https" else 80
                safe = (bool(location) and "\\" not in location
                        and not any(ord(c) < 32 for c in location)
                        and target.username is None and target.fragment == ""
                        and (target.scheme, target.hostname, target.port or default_port)
                        == (origin.scheme, origin.hostname, origin.port or default_port))
            except ValueError:
                safe = False
            if not safe:
                raise PlexError(f"plex: redirect refused at {endpoint}; destination must stay on the configured server and protocol")
            if hop == 5:
                raise PlexError(f"plex: too many redirects at {endpoint}; check the server URL and proxy")
            url = target.geturl()
            kwargs.pop("params", None)  # Location supplies the redirected query.
        if response.status_code != 200:
            status = response.status_code
            response.close()
            if status in (401, 403):
                raise PlexError("plex: access denied; use the server owner's Plex token")
            if 300 <= status < 400:
                raise PlexError(f"plex: redirect refused at {endpoint}; check the server URL and proxy")
            raise PlexError(f"plex: HTTP {status} at {endpoint}")
        return response

    def _xml(self, path):
        with self._get(path, timeout=self.timeout) as response:
            data = response.content
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise PlexError("plex: invalid XML response; check the server URL and proxy") from None
        if root.tag != "MediaContainer":
            raise PlexError("plex: expected a Plex MediaContainer response")
        return root, data

    def identity(self):
        root, _ = self._xml("/identity")
        version, identifier = root.get("version"), root.get("machineIdentifier")
        if not version or not identifier:
            raise PlexError("plex: server identity is missing its version or identifier")
        return {"version": version, "machine_identifier": identifier}

    def _path(self, *parts):
        return os.path.join(self.data_dir, *parts)

    def _require_folder(self, machine_identifier=None):
        if not self.data_dir:
            raise PlexError("plex: set the data folder - Plex's \"Plex Media Server\" folder, mounted into this container")
        if not os.path.isdir(self.data_dir):
            raise PlexError(
                f"plex: data folder {self.data_dir} doesn't exist in this container - mount Plex's "
                "\"Plex Media Server\" folder there (Library/Application Support/Plex Media Server in Plex's /config)"
            )
        if not os.path.isfile(self._path(*_DATABASES, LIBRARY_DB)) or not os.path.isfile(self._path(PREFERENCES)):
            raise PlexError(
                f"plex: {self.data_dir} doesn't look like Plex's data folder (no Preferences.xml or library database) - "
                "mount Library/Application Support/Plex Media Server from Plex's /config (a new install creates it on first start)"
            )
        if machine_identifier:
            folder_id = self._preferences(self._path(PREFERENCES)).get("ProcessedMachineIdentifier")
            if folder_id and folder_id != machine_identifier:
                raise PlexError(
                    f"plex: {self.data_dir} holds another Plex server's data (server {folder_id}, "
                    f"this server is {machine_identifier}) - check the volume mount"
                )

    @staticmethod
    def _preferences(path):
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            raise PlexError(f"plex: can't read {os.path.basename(path)} ({exc})") from None
        if root.tag != "Preferences":
            raise PlexError(f"plex: {os.path.basename(path)} isn't Plex's settings file")
        return root

    def test_connection(self):
        # /identity answers without a token; the settings need owner access.
        root, _ = self._xml("/:/prefs")
        if root.find("Setting") is None:
            raise PlexError("plex: the response is not Plex server settings")
        identity = self.identity()
        self._require_folder(identity["machine_identifier"])
        message = f"plex {identity['version']} reachable, owner access OK"
        if not os.access(self._path(*_DATABASES), os.W_OK):
            return message + "; data folder is read-only, so backups work but restores won't"
        return message + ", data folder OK"

    def _download_database(self, dest_dir):
        """Downloads Plex's database export and extracts the library
        database as dest_dir/com.plexapp.plugins.library.db."""
        export = Path(dest_dir) / "plex-databases.zip"
        target = Path(dest_dir) / LIBRARY_DB
        try:
            logger.info("plex: requesting database export; waiting for Plex to prepare the snapshot...")
            with self._get("/diagnostics/databases", timeout=(self.timeout, 600), stream=True) as response:
                logger.info("plex: downloading database export...")
                downloaded, last_report = 0, time.monotonic()
                with export.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=_MIB):
                        output.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= _PROGRESS_INTERVAL:
                            logger.info("plex: database download: %.1f MiB received", downloaded / _MIB)
                            last_report = now
            logger.info("plex: database downloaded (%.1f MiB); validating it...", downloaded / _MIB)
            with zipfile.ZipFile(export) as zf:
                databases = [i for i in zf.infolist()
                             if not i.is_dir() and _EXPORTED_DB.fullmatch(i.filename.replace("\\", "/").split("/")[-1])]
                if len(databases) != 1:
                    raise PlexError("plex: the export doesn't hold exactly one library database")
                with zf.open(databases[0]) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, _MIB)
            with target.open("rb") as db:
                if db.read(16) != b"SQLite format 3\0":
                    raise PlexError("plex: the export holds an invalid library database")
            return schema_version(target)
        except BaseException as exc:
            target.unlink(missing_ok=True)
            if isinstance(exc, requests.RequestException):
                raise PlexError("plex: database download interrupted; retry the backup") from None
            if isinstance(exc, (zipfile.BadZipFile, NotImplementedError)):
                raise PlexError("plex: expected a database ZIP; check the URL and proxy") from None
            raise
        finally:
            export.unlink(missing_ok=True)

    def backup(self, dest_dir):
        logger.info("plex: checking server identity...")
        identity = self.identity()
        self._require_folder(identity["machine_identifier"])
        os.makedirs(dest_dir, exist_ok=True)
        try:
            schema = self._download_database(dest_dir)
            if self.identity() != identity:
                raise PlexError("plex: server identity or version changed during the database export; retry once the server is stable")
            shutil.copyfile(self._path(PREFERENCES), os.path.join(dest_dir, PREFERENCES))
            self._preferences(os.path.join(dest_dir, PREFERENCES))  # A complete file, not one Plex was rewriting.
            manifest = {"plex_version": identity["version"], "machine_identifier": identity["machine_identifier"],
                        "schema_version": schema}
            with open(os.path.join(dest_dir, MANIFEST), "w") as f:
                json.dump(manifest, f, indent=2)
        except BaseException:
            for name in (LIBRARY_DB, PREFERENCES, MANIFEST):
                Path(dest_dir, name).unlink(missing_ok=True)
            raise
        logger.info("plex: backed up the library database and server settings from Plex %s", identity["version"])
        return dest_dir

    def _check_stopped(self):
        open_dbs = sorted(n for n in os.listdir(self._path(*_DATABASES)) if n.endswith(".db-wal"))
        if open_dbs:
            raise PlexError(
                f"plex: stop Plex before restoring - its databases are in use (found {open_dbs[0]}). "
                "If Plex is already stopped, it didn't shut down cleanly: start it, then stop it again"
            )
        try:
            running = self.session.get(f"{self.url}/identity", timeout=5, allow_redirects=False).ok
        except requests.RequestException:
            running = False
        if running:
            raise PlexError("plex: stop Plex before restoring - it's still answering at " + self.url)

    def restore_from_file(self, local_zip_path):
        """Writes the backup's library database and Preferences.xml over
        Plex's while Plex is stopped. Files are rewritten in place, so they
        keep Plex's owner and permissions."""
        if not is_plex_backup(local_zip_path):
            raise PlexError("plex: this file isn't a Plex backup made by Backuparr")
        self._require_folder()
        databases = self._path(*_DATABASES)
        target = os.path.join(databases, LIBRARY_DB)
        preferences = self._path(PREFERENCES)
        if not all(os.access(p, os.W_OK) for p in (databases, target, preferences)):
            raise PlexError(
                f"plex: Plex's databases in {self.data_dir} aren't writable - mount the data folder "
                "read-write to restore, and run Backuparr with Plex's PUID:PGID"
            )
        self._check_stopped()

        with tempfile.TemporaryDirectory(prefix="plex-restore-") as work:
            with zipfile.ZipFile(local_zip_path) as zf:
                restored = zf.extract(LIBRARY_DB, work)
                restored_prefs = zf.extract(PREFERENCES, work)
            server_id = self._preferences(restored_prefs).get("ProcessedMachineIdentifier")
            backup_schema, installed = schema_version(restored), schema_version(target)
            if backup_schema > installed:
                raise PlexError(
                    "plex: this backup comes from a newer Plex than the one installed (database schema "
                    f"{backup_schema}, installed {installed}) - update Plex first"
                )
            # "wb" truncates in place: owner and mode stay Plex's.
            for src_path, dst_path in ((restored, target), (restored_prefs, preferences)):
                with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
                    shutil.copyfileobj(src, dst, _MIB)
        # No -wal exists (checked above), so a -shm index is stale.
        shm = target + "-shm"
        if os.path.exists(shm):
            os.remove(shm)
        logger.info("plex: restored the library database and server settings (server %s) into %s - start Plex to load them",
                    server_id or "?", self.data_dir)
