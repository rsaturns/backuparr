"""Backup/restore driver for Vaultwarden's data folder.

Vaultwarden has no API that exports its data (the admin page's "Backup
database" only writes a copy of the SQLite file next to the original), so
its data folder (/data in the official image) is mounted into Backuparr as
data_dir, and the driver works on what Vaultwarden's backup guide lists
(https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault):

- db.sqlite3: users, organisations, every vault item (encrypted with its
  owner's keys), devices, 2FA. Copied with SQLite's online backup, which
  takes a consistent snapshot while Vaultwarden keeps writing to it.
- rsa_key*: the key that signs login tokens; without it everyone is signed
  out and open invitations stop working.
- config.json: what the admin page saved, admin token and SMTP password
  included - only there if the admin page was ever used.
- attachments/ and sends/: file attachments and Send files, which aren't in
  the database.

Left out: icon_cache/ and tmp/, which Vaultwarden rebuilds itself. Only the
default SQLite database is supported: with PostgreSQL or MySQL the data
isn't in the folder. Backups are always encrypted (config.json and the RSA
key are plain text).

The URL is only asked for Vaultwarden's version (GET /api/version, no
login) and, before a restore, whether it's still running (GET /alive).

A restore writes the backup's files back while Vaultwarden is stopped:
overwriting the database of a running server would corrupt it. Vaultwarden
keeps its database open while it runs, so the restore asks SQLite for sole
use of it - refused until it has stopped - which also folds a stopped
Vaultwarden's leftover -wal into the database, so no stale log is replayed
over the restored one. Files the backup doesn't have are left alone.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile

import requests

from apps import mounted, sqlite_snapshot
from apps.base import AppError
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

MANIFEST = "backuparr-vaultwarden.json"
DATABASE = "db.sqlite3"
CONFIG = "config.json"
FOLDERS = ("attachments", "sends")
# rsa_key.pem, plus the .der/.pub files older versions wrote beside it.
_RSA_KEY = re.compile(r"rsa_key(\.pub)?\.(pem|der)")
# A file inside attachments/ or sends/: <uuid>/<file id>, kept to plain names.
_FOLDER_FILE = re.compile(r"(attachments|sends)(/[A-Za-z0-9][A-Za-z0-9._-]*)+")


class VaultwardenError(AppError):
    pass


def migration_level(path):
    """The newest of Vaultwarden's database migrations applied to the
    database at path - Vaultwarden migrates older databases forward on
    start, but an older Vaultwarden can't use a newer one."""
    db = sqlite_snapshot.open_ro(path)
    try:
        return db.execute("SELECT max(version) FROM __diesel_schema_migrations").fetchone()[0] or ""
    finally:
        db.close()


def is_vaultwarden_backup(path):
    return mounted.is_backup_zip(path, {MANIFEST, DATABASE})


def _allowed_member(name):
    return name in (MANIFEST, DATABASE, CONFIG) or bool(_RSA_KEY.fullmatch(name)) or (
        bool(_FOLDER_FILE.fullmatch(name)) and ".." not in name.split("/"))


class VaultwardenApp:
    def __init__(self, url, data_dir=None, timeout=30):
        self.url = url.rstrip("/")
        self.data_dir = data_dir or None
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update({"Accept": "application/json"})

    def app_version(self):
        """Vaultwarden's own version, e.g. "1.37.5". /api/version needs no
        login, and answers with a JSON string."""
        res = self.session.get(f"{self.url}/api/version", timeout=10)
        if res.status_code == 404:
            raise VaultwardenError(f"vaultwarden: {self.url} doesn't look like Vaultwarden (no /api/version) - check the URL")
        res.raise_for_status()
        try:
            version = res.json()
        except ValueError:
            version = None
        if not isinstance(version, str):
            raise VaultwardenError(f"vaultwarden: {self.url} doesn't look like Vaultwarden - check the URL")
        return version or None

    def _path(self, *parts):
        return os.path.join(self.data_dir, *parts)

    def _require_folder(self):
        mounted.require_dir(self.data_dir, VaultwardenError, "vaultwarden", "data folder", "Vaultwarden's /data folder",
                            "mount the folder Vaultwarden has at /data there")
        if not os.path.isfile(self._path(DATABASE)):
            raise VaultwardenError(
                f"vaultwarden: {self.data_dir} has no {DATABASE} - mount the folder Vaultwarden has at /data. "
                "Backuparr backs up Vaultwarden's default SQLite database; one using PostgreSQL or MySQL isn't supported"
            )

    def _writable(self):
        """Whether a restore could write Vaultwarden's files: its folder, its
        database and whatever else is already there."""
        paths = [self.data_dir, self._path(DATABASE)]
        for name in os.listdir(self.data_dir):
            if name == CONFIG or _RSA_KEY.fullmatch(name) or name in FOLDERS:
                paths.append(self._path(name))
        return all(os.access(p, os.W_OK) for p in paths)

    def test_connection(self):
        version = self.app_version()
        self._require_folder()
        try:
            migration_level(self._path(DATABASE))
        except sqlite3.Error as exc:
            raise VaultwardenError(f"vaultwarden: can't read {DATABASE} in {self.data_dir} ({exc}) - check its permissions") from exc
        if not self._writable():
            return (f"vaultwarden {version or '?'} reachable; Backuparr can't write its files, so backups work but "
                    "restores won't (run Vaultwarden as Backuparr's user to restore here)")
        return f"vaultwarden {version or '?'} reachable, data folder OK"

    def _files(self):
        """(path relative to the data folder, absolute path) of every file
        backed up besides the database."""
        found = []
        for name in sorted(os.listdir(self.data_dir)):
            path = self._path(name)
            if (name == CONFIG or _RSA_KEY.fullmatch(name)) and os.path.isfile(path) and not os.path.islink(path):
                found.append((name, path))
        for folder in FOLDERS:
            top = self._path(folder)
            if not os.path.isdir(top) or os.path.islink(top):
                continue
            for root, dirs, files in os.walk(top):
                dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
                for name in sorted(files):
                    path = os.path.join(root, name)
                    relative = os.path.relpath(path, self.data_dir).replace(os.sep, "/")
                    if os.path.islink(path) or not stat.S_ISREG(os.stat(path).st_mode):
                        continue
                    if not _allowed_member(relative):
                        logger.warning("vaultwarden: left out %s - not a name Vaultwarden gives its files", relative)
                        continue
                    found.append((relative, path))
        return found

    def backup(self, dest_dir):
        version = self.app_version()
        self._require_folder()
        logger.info("vaultwarden: copying the database, RSA key, settings, attachments and Sends (no icon cache)...")
        os.makedirs(dest_dir, exist_ok=True)
        database = os.path.join(dest_dir, DATABASE)
        problems = sqlite_snapshot.snapshot(self._path(DATABASE), database)
        if problems:
            raise VaultwardenError(f"vaultwarden: the copy of {DATABASE} failed SQLite's integrity check "
                                   f"({'; '.join(problems[:3])})")
        files = self._files()
        size = 0
        for relative, path in files:
            target = os.path.join(dest_dir, *relative.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(path, target)
            size += os.path.getsize(target)
        if not any(_RSA_KEY.fullmatch(relative) for relative, _path in files):
            logger.warning("vaultwarden: no rsa_key.pem in %s - restoring this backup signs everyone out", self.data_dir)

        manifest = {"vaultwarden_version": version, "migration": migration_level(database)}
        with open(os.path.join(dest_dir, MANIFEST), "w") as f:
            json.dump(manifest, f, indent=2)
        attachments = sum(1 for relative, _path in files if relative.startswith(tuple(f"{f}/" for f in FOLDERS)))
        logger.info("vaultwarden: backed up the database and %d other files (%d attachment and Send files, %.1f MB) "
                    "from Vaultwarden %s", len(files), attachments, size / 1e6, version or "?")
        return dest_dir

    def _unwritable(self, names):
        """The first path a restore of these members would need to write and
        can't: the file if it's there, else the nearest folder that is."""
        for name in [DATABASE, *names]:
            path = self._path(*name.split("/"))
            while not os.path.lexists(path):
                path = os.path.dirname(path)
            if not os.access(path, os.W_OK):
                return path
        return None

    def _check_stopped(self):
        try:
            running = self.session.get(f"{self.url}/alive", timeout=5).ok
        except requests.RequestException:
            running = False
        if running:
            raise VaultwardenError("vaultwarden: stop Vaultwarden before restoring - it's still answering at " + self.url)
        mounted.claim_database(self._path(DATABASE), VaultwardenError, "vaultwarden", "Vaultwarden")

    def _write(self, zf, info, relative):
        """Writes one member in place: an existing file keeps Vaultwarden's
        owner and mode, a new one (and a new folder) is made readable."""
        target = self._path(*relative.split("/"))
        folder = os.path.dirname(target)
        missing = []
        while not os.path.isdir(folder):
            missing.append(folder)
            folder = os.path.dirname(folder)
        for path in reversed(missing):
            os.mkdir(path)
            os.chmod(path, 0o755)
        with zf.open(info) as src:
            mounted.write_in_place(src, target)

    def restore_from_file(self, local_zip_path):
        """Writes the backup's database and files over Vaultwarden's while
        it's stopped."""
        if not is_vaultwarden_backup(local_zip_path):
            raise VaultwardenError("vaultwarden: this file isn't a Vaultwarden backup made by Backuparr")
        with zipfile.ZipFile(local_zip_path) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
        unexpected = sorted(i.filename for i in members if not _allowed_member(i.filename))
        if unexpected:
            raise VaultwardenError(f"vaultwarden: unexpected contents in the backup: {unexpected[0]!r}")
        self._require_folder()
        # Checked before anything is written, so a restore never stops halfway.
        blocked = self._unwritable(i.filename for i in members if i.filename != MANIFEST)
        if blocked:
            raise VaultwardenError(
                f"vaultwarden: Backuparr can't write {blocked} - mount the data folder read-write to restore, "
                "and run Vaultwarden as Backuparr's user (it runs as root by default)"
            )

        target = self._path(DATABASE)
        with tempfile.TemporaryDirectory(prefix="vaultwarden-restore-") as work:
            with zipfile.ZipFile(local_zip_path) as zf:
                restored = zf.extract(DATABASE, work)
            try:
                backup_level = migration_level(restored)
            except sqlite3.Error as exc:
                raise VaultwardenError(f"vaultwarden: the backup's database can't be read ({exc})") from exc
            if backup_level > migration_level(target):
                raise VaultwardenError("vaultwarden: this backup comes from a newer Vaultwarden than the one installed - "
                                       "update Vaultwarden first")
            self._check_stopped()
            mounted.copy_in_place(restored, target)
        mounted.remove_journal_files(target)
        written = 0
        with zipfile.ZipFile(local_zip_path) as zf:
            for info in members:
                if info.filename not in (MANIFEST, DATABASE):
                    self._write(zf, info, info.filename)
                    written += 1
        logger.info("vaultwarden: restored the database and %d other files into %s - start Vaultwarden to load them",
                    written, self.data_dir)
