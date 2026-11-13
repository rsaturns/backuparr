"""Backup/restore driver for Emby Server's data folder.

Emby has no API that exports or imports its data - its own Backup &
Restore plugin needs Emby Premiere - so Emby's data folder (ProgramData,
mounted at /config in both the official and linuxserver images) is mounted
into Backuparr as data_dir, and the driver works on the files Emby's
manual-backup guide covers:

- config/ (server and per-user settings) and plugins/ (plugin files and
  their settings). Emby replaces a plugin with its bundled copy when that
  copy is newer, so restoring an older plugin file onto a newer Emby is
  harmless.
- data/*.db: users, logins and API keys, and the library database. Watch
  state, favourites, playlists and collections live in library.db, so it
  can't be left out. Each one is copied with SQLite's online backup, which
  takes a consistent snapshot while Emby keeps writing to it - through a
  read-only mount too.
- data/device.txt (the server's id, so apps still recognise it after a
  restore), data/lastversion.txt (the version the databases were last
  migrated by), the playlist and collection folders.

With Include metadata on (#196), the whole metadata/ folder too: images,
custom artwork and people images, which Emby would otherwise download again
(and can't for artwork set by hand). Left out always: cache, logs and
transcoding files - rebuilt by Emby. The files are read where they are into
the archive (mounted.write_archive), not copied first.

A restore writes those files back, and is only possible while Emby is
stopped: overwriting the databases of a running server would corrupt them.
A running Emby always keeps a <name>.db-wal file next to each database, so
any -wal file means Emby is running - or was not shut down cleanly - and
the restore is refused before anything is written.

Auth: the API key goes in the X-Emby-Token header, which leaves
Authorization free for a reverse proxy's own credentials.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import zipfile

import requests

from apps import mounted, sqlite_snapshot
from apps.base import AppError
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

MANIFEST = "backuparr-emby.json"

# Taken whole; under data/ only the files and folders below are taken.
_TREES = ["config", "plugins", "data/playlists", "data/userplaylists", "data/collections", "metadata/collections"]
METADATA = "metadata"
_DATA_FILES = ["device.txt", "lastversion.txt"]
# Restored as an exact copy of the backup's: a playlist or collection folder
# left behind would be imported again by the next library scan.
_MIRRORED = ["data/playlists", "data/userplaylists", "data/collections", "metadata/collections"]
_RESTORABLE = re.compile(
    r"^(?:(?:config|plugins|data/(?:playlists|userplaylists|collections)|metadata)/"
    r"|data/(?:[^/]+\.db|device\.txt|lastversion\.txt)$)"
)


class EmbyError(AppError):
    pass


def _version(text):
    return tuple(int(p) for p in re.findall(r"\d+", text or "")[:4])


def _read_text(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def snapshot_db(src, dest):
    """Copies a live SQLite database with the online backup API, which
    takes a consistent snapshot while Emby keeps writing to it."""
    problems = sqlite_snapshot.snapshot(src, dest)
    if problems:
        raise EmbyError(f"emby: the copy of {os.path.basename(src)} failed SQLite's integrity check "
                        f"({'; '.join(problems[:3])})")


def is_emby_backup(path):
    return mounted.is_backup_zip(path, {MANIFEST})


class EmbyApp:
    def __init__(self, url, api_key, data_dir=None, metadata=None, timeout=30):
        # Emby answers on both /emby/... and /...; /emby/ also works behind
        # a base URL, so a URL ending in /emby is taken as the server root.
        self.url = re.sub(r"/emby/?$", "", url.rstrip("/"))
        self.data_dir = data_dir or None
        self.metadata = bool(metadata)
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update({"X-Emby-Token": api_key, "Accept": "application/json"})

    def server_info(self):
        res = self.session.get(f"{self.url}/emby/System/Info", timeout=10)
        if res.status_code == 401:
            raise EmbyError("emby: unauthorized - check the API key (Settings > Advanced > API Keys)")
        res.raise_for_status()
        return res.json()

    def app_version(self):
        return self.server_info().get("Version")

    def _path(self, *parts):
        return os.path.join(self.data_dir, *parts)

    def _require_folder(self, server_id=None):
        mounted.require_dir(self.data_dir, EmbyError, "emby", "data folder", "Emby's /config folder",
                            "mount the folder Emby has at /config there")
        if not os.path.isfile(self._path("data", "library.db")) or not os.path.isdir(self._path("config")):
            raise EmbyError(
                f"emby: {self.data_dir} doesn't look like Emby's data folder (no data/library.db) - "
                "mount the folder Emby has at /config, not a folder inside it (a new install creates it on first start)"
            )
        device_id = _read_text(self._path("data", "device.txt"))
        if server_id and device_id and device_id != server_id:
            raise EmbyError(
                f"emby: {self.data_dir} holds another Emby server's data (server id {device_id}, "
                f"this server is {server_id}) - check the volume mount"
            )

    def test_connection(self):
        info = self.server_info()
        self._require_folder(info.get("Id"))
        try:
            db = sqlite_snapshot.open_ro(self._path("data", "library.db"))
            try:
                db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise EmbyError(f"emby: can't read data/library.db in {self.data_dir} ({exc}) - check its permissions") from exc
        version = info.get("Version", "?")
        if not os.access(self.data_dir, os.W_OK):
            message = f"emby {version} reachable; data folder is read-only, so backups work but restores won't"
        else:
            message = f"emby {version} reachable, data folder OK"
        if self.metadata and not os.path.isdir(self._path(METADATA)):
            message += "; it has no metadata folder, so backups won't hold metadata"
        return message

    def backup(self, dest_dir):
        info = self.server_info()
        self._require_folder(info.get("Id"))
        metadata = self.metadata and os.path.isdir(self._path(METADATA))
        if self.metadata and not metadata:
            logger.warning("emby: %s has no metadata folder, so this backup holds no metadata - if Emby keeps it "
                           "elsewhere (its metadata path), mount that folder at %s", self.data_dir, self._path(METADATA))
        logger.info("emby: copying settings and databases%s...",
                    ", and metadata" if metadata else " (no metadata images or cache)")
        # metadata/ holds metadata/collections.
        trees = [METADATA] if metadata else []
        trees += [tree for tree in _TREES if not (metadata and tree.startswith(METADATA + "/"))]
        sources = [(self._path(*tree.split("/")), tree) for tree in trees]
        sources += [(self._path("data", name), f"data/{name}") for name in _DATA_FILES]
        stage = os.path.join(dest_dir, "stage")
        archive = os.path.join(dest_dir, "emby.zip")
        os.makedirs(os.path.join(stage, "data"))
        try:
            databases = sorted(n for n in os.listdir(self._path("data")) if n.endswith(".db"))
            for name in databases:
                snapshot_db(self._path("data", name), os.path.join(stage, "data", name))
            manifest = {"emby_version": info.get("Version"), "server_id": info.get("Id"), "databases": databases,
                        "metadata": metadata}
            with open(os.path.join(stage, MANIFEST), "w") as f:
                json.dump(manifest, f, indent=2)
            files = mounted.write_archive(archive, stage, sources, self.data_dir, EmbyError, "emby", "Emby")
        except BaseException:
            if os.path.exists(archive):
                os.remove(archive)
            raise
        finally:
            shutil.rmtree(stage)
        logger.info("emby: backed up %d database(s) and %d file(s) from Emby %s",
                    len(databases), files, info.get("Version", "?"))
        return archive

    def _check_stopped(self):
        data = self._path("data")
        open_dbs = sorted(n for n in os.listdir(data) if n.endswith(".db-wal"))
        if open_dbs:
            raise EmbyError(
                f"emby: stop Emby before restoring - its databases are in use (found data/{open_dbs[0]}). "
                "If Emby is already stopped, it didn't shut down cleanly: start it, then stop it again"
            )
        try:
            running = self.session.get(f"{self.url}/emby/System/Info/Public", timeout=5).ok
        except requests.RequestException:
            running = False
        if running:
            raise EmbyError("emby: stop Emby before restoring - it's still answering at " + self.url)

    def _check_version(self, manifest):
        backup_version = _version(manifest.get("emby_version"))
        installed = _version(_read_text(self._path("data", "lastversion.txt")))
        if backup_version and installed and backup_version > installed:
            raise EmbyError(
                f"emby: this backup comes from Emby {manifest['emby_version']}, newer than the installed "
                f"{'.'.join(map(str, installed))} - update Emby first"
            )
        if backup_version and backup_version < (4, 8) <= installed:
            raise EmbyError("emby: backups from before Emby 4.8 can't be restored onto Emby 4.8 or newer")

    def restore_from_file(self, local_zip_path):
        """Writes the backup's files over Emby's data folder while Emby is
        stopped. Existing files are rewritten in place, so they keep Emby's
        owner and permissions."""
        if not is_emby_backup(local_zip_path):
            raise EmbyError("emby: this file isn't an Emby backup made by Backuparr")
        self._require_folder()
        if not os.access(self.data_dir, os.W_OK):
            raise EmbyError(
                f"emby: data folder {self.data_dir} isn't writable - mount it read-write to restore, "
                "and run Backuparr with Emby's PUID:PGID"
            )
        self._check_stopped()

        with zipfile.ZipFile(local_zip_path) as zf:
            manifest = json.loads(zf.read(MANIFEST))
            self._check_version(manifest)
            members = [i for i in zf.infolist() if i.filename != MANIFEST and not i.is_dir()]
            for info in members:
                parts = info.filename.split("/")
                if not _RESTORABLE.match(info.filename) or any(p in ("", ".", "..") for p in parts):
                    raise EmbyError(f"emby: unexpected path in the backup: {info.filename!r}")
            # The metadata goes first: a disk filling up stops the restore
            # before Emby's databases and settings are touched.
            first = [i.filename.startswith(METADATA + "/")
                     and not any(i.filename.startswith(f + "/") for f in _MIRRORED) for i in members]
            bulk = [i for i, f in zip(members, first) if f]
            rest = [i for i, f in zip(members, first) if not f]
            mounted.check_writable(members, self.data_dir, EmbyError, "emby", "Emby")
            mounted.write_files(zf, bulk, self.data_dir)
            for folder in _MIRRORED:
                self._mirror_cleanup(folder, {i.filename for i in members})
            mounted.write_files(zf, rest, self.data_dir)
            # No -wal exists (checked above), so any -shm index is stale.
            for name in os.listdir(self._path("data")):
                if name.endswith(".db-shm"):
                    os.remove(self._path("data", name))
        logger.info("emby: restored %d file(s) into %s - start Emby to load them", len(members), self.data_dir)

    def _mirror_cleanup(self, folder, keep):
        root = self._path(*folder.split("/"))
        if not os.path.isdir(root):
            return
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.relpath(full, self.data_dir).replace(os.sep, "/") not in keep:
                    os.remove(full)
            if dirpath != root and not os.listdir(dirpath):
                os.rmdir(dirpath)
