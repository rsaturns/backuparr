"""Backup/restore driver for LazyLibrarian.

Checked against LazyLibrarian master (fc698c6c, gitlab.com/LazyLibrarian)
and the linuxserver.io image, which upstream lists as its Docker package:

- `/api?apikey=...&cmd=backup` (full-access key only) runs LazyLibrarian's
  own backup, common.dbbackup: an online SQLite copy of the database plus
  config.ini, dicts.json, genres.json and any filetemplate.*.text /
  logintemplate.*.text, written as `api_<asctime>.tgz` into its data folder
  (/config in the linuxserver.io image). The API answers with that path
  inside LazyLibrarian's container, never the file, so the data folder is
  mounted into Backuparr as data_dir to collect it. The .tgz is kept as is
  and removed from the data folder afterwards (when the mount is writable).
- Every API answer is HTTP 200 JSON with "Success" and an "Error" code, a
  bad key included. The key travels in the query string, so error messages
  are built without the request URL.

There's no restore API. A restore writes the archive's files back into the
data folder while LazyLibrarian is stopped: replacing the database of a
running LazyLibrarian would corrupt it. It runs its database in WAL mode;
asking SQLite for sole use of it is refused while LazyLibrarian has it open,
and on a stopped one folds any leftover -wal into the database, so no stale
log is replayed over the restored one.
"""
import configparser
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile

import requests

from apps import sqlite_snapshot
from apps.base import AppError
from apps.servarr import ApiKeySession, UnsafeRedirectError

logger = logging.getLogger(f"backuparr.{__name__}")

DATABASE = "lazylibrarian.db"
CONFIG = "config.ini"
# Everything dbbackup can put in its archive - and so all a restore writes.
_MEMBER = re.compile(r"(lazylibrarian\.db|config\.ini|dicts\.json|genres\.json|"
                     r"(?:file|login)template\.[A-Za-z0-9_.-]+\.text)")
# The name dbbackup gives an archive made through the API.
_API_ARCHIVE = re.compile(r"api_[A-Za-z0-9_]+\.tgz")
_MIB = 1024 * 1024


class LazyLibrarianError(AppError):
    pass


def archive_members(path):
    """The archive's file names, or a LazyLibrarianError if it isn't a
    LazyLibrarian backup: only its known flat files, the database included."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise LazyLibrarianError("lazylibrarian: this file isn't a LazyLibrarian backup") from exc
    names = [m.name for m in members]
    bad = [m.name for m in members if not m.isfile() or not _MEMBER.fullmatch(m.name)]
    if bad or len(set(names)) != len(names) or DATABASE not in names:
        raise LazyLibrarianError(
            f"lazylibrarian: unexpected contents in the backup: {(bad or names)[0]!r}"
            if bad else "lazylibrarian: this file isn't a LazyLibrarian backup (no database)")
    return names


class LazyLibrarianApp:
    def __init__(self, url, api_key, data_dir=None, timeout=60):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.data_dir = data_dir or None
        self.timeout = timeout
        self.session = ApiKeySession(self.url)

    def _call(self, cmd, timeout=None):
        try:
            res = self.session.get(f"{self.url}/api", params={"apikey": self.api_key, "cmd": cmd},
                                   timeout=timeout or self.timeout)
        except UnsafeRedirectError:
            raise  # its message says what to do, and holds no URL
        except requests.RequestException as exc:
            # requests' messages embed the full URL, apikey included.
            raise type(exc)(f"lazylibrarian: {type(exc).__name__} calling {cmd}") from None
        if res.status_code != 200:
            raise LazyLibrarianError(f"lazylibrarian: HTTP {res.status_code} calling {cmd}")
        return res

    def _json(self, cmd, timeout=None):
        try:
            data = self._call(cmd, timeout).json()
        except ValueError:
            raise LazyLibrarianError(f"lazylibrarian: {cmd} didn't return LazyLibrarian's API answer - check the URL") from None
        if not isinstance(data, dict):
            raise LazyLibrarianError(f"lazylibrarian: {cmd} didn't return LazyLibrarian's API answer - check the URL")
        if not data.get("Success"):
            error = data.get("Error") or {}
            if error.get("Code") == 401:
                raise LazyLibrarianError("lazylibrarian: unauthorized - check the API key (Config > Interface > API)")
            raise LazyLibrarianError(f"lazylibrarian: {error.get('Message') or cmd + ' failed'}")
        return data

    def version(self):
        """getVersion works with either key; help lists backup only for the
        full-access one, which the backup needs."""
        version = self._json("getVersion", timeout=10).get("current_version")
        if "<td>backup</td>" not in self._call("help", timeout=10).text:
            raise LazyLibrarianError(
                "lazylibrarian: this is the read-only API key - use the full-access API key (Config > Interface > API)")
        return version

    def _path(self, name):
        return os.path.join(self.data_dir, name)

    def _require_folder(self, match_key=True):
        if not self.data_dir:
            raise LazyLibrarianError(
                "lazylibrarian: set the data folder - LazyLibrarian's /config folder, mounted into this container")
        if not os.path.isdir(self.data_dir):
            raise LazyLibrarianError(
                f"lazylibrarian: data folder {self.data_dir} doesn't exist in this container - "
                "mount the folder LazyLibrarian has at /config there")
        if not os.path.isfile(self._path(DATABASE)) or not os.path.isfile(self._path(CONFIG)):
            raise LazyLibrarianError(
                f"lazylibrarian: {self.data_dir} doesn't look like LazyLibrarian's data folder (no {DATABASE} "
                f"and {CONFIG}) - mount the folder LazyLibrarian has at /config")
        if match_key:
            config = configparser.ConfigParser(interpolation=None)
            try:
                config.read(self._path(CONFIG), encoding="utf-8")
            except (configparser.Error, OSError, UnicodeError) as exc:
                raise LazyLibrarianError(f"lazylibrarian: can't read {CONFIG} in {self.data_dir} ({exc})") from exc
            if config.get("API", "api_key", fallback=None) != self.api_key:
                raise LazyLibrarianError(
                    f"lazylibrarian: {self.data_dir} holds another LazyLibrarian's data (its {CONFIG} has a "
                    "different API key) - check the volume mount")

    def test_connection(self):
        version = self.version()
        self._require_folder()
        if not os.access(self.data_dir, os.W_OK):
            return (f"lazylibrarian {version or '?'} reachable; data folder is read-only, so each backup's "
                    "file stays in it and restores won't work")
        return f"lazylibrarian {version or '?'} reachable, data folder OK"

    def backup(self, dest_dir):
        version = self.version()
        self._require_folder()
        logger.info("lazylibrarian: running LazyLibrarian's backup (database and settings)...")
        made = self._json("backup", timeout=max(self.timeout, 600)).get("Data") or ""
        name = made.replace("\\", "/").rsplit("/", 1)[-1]
        if not _API_ARCHIVE.fullmatch(name):
            raise LazyLibrarianError(f"lazylibrarian: unexpected backup file name {name!r}")
        source = self._path(name)
        if not os.path.isfile(source):
            raise LazyLibrarianError(
                f"lazylibrarian: LazyLibrarian wrote {made}, but {name} isn't in {self.data_dir} - "
                "mount the folder LazyLibrarian has at /config")
        dest = os.path.join(dest_dir, name)
        shutil.copyfile(source, dest)
        members = archive_members(dest)
        try:
            os.remove(source)
        except OSError as exc:
            logger.warning("lazylibrarian: couldn't remove %s from the data folder (%s) - "
                           "mount it read-write so backups don't pile up there", name, exc.strerror or exc)
        logger.info("lazylibrarian: backed up %s from LazyLibrarian %s", ", ".join(members), version or "?")
        return dest

    def _check_stopped(self):
        try:
            self._call("getVersion", timeout=5)
            running = True
        except (requests.RequestException, LazyLibrarianError):
            running = False
        if running:
            raise LazyLibrarianError("lazylibrarian: stop LazyLibrarian before restoring - it's still answering at " + self.url)
        # Leaving WAL mode needs sole use of the database: refused while
        # LazyLibrarian has it open, and when granted it folds the -wal into
        # the database and deletes it. LazyLibrarian turns WAL on again.
        db = sqlite3.connect(self._path(DATABASE), timeout=0, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=DELETE").fetchone()
        except sqlite3.OperationalError as exc:
            raise LazyLibrarianError(
                "lazylibrarian: stop LazyLibrarian before restoring - its database is in use"
                + (" (is it still running somewhere?)" if "locked" in str(exc) else f" ({exc})")
            ) from exc
        finally:
            db.close()

    def restore_from_file(self, local_path):
        """Writes the backup's files over LazyLibrarian's while it's stopped.
        Existing files are rewritten in place, so they keep their owner and
        permissions."""
        names = archive_members(local_path)
        self._require_folder(match_key=False)
        targets = [self._path(n) for n in names]
        if not os.access(self.data_dir, os.W_OK) or not all(
                os.access(t, os.W_OK) for t in targets if os.path.exists(t)):
            raise LazyLibrarianError(
                f"lazylibrarian: LazyLibrarian's files in {self.data_dir} aren't writable - mount the data folder "
                "read-write to restore, and run Backuparr as LazyLibrarian's user (its PUID/PGID)")
        with tempfile.TemporaryDirectory(prefix="lazylibrarian-restore-") as work:
            with tarfile.open(local_path, "r:gz") as tar:
                tar.extractall(work, filter="data")
            try:
                db = sqlite_snapshot.open_ro(os.path.join(work, DATABASE))
                try:
                    status = db.execute("PRAGMA quick_check").fetchone()[0]
                finally:
                    db.close()
            except sqlite3.Error as exc:
                raise LazyLibrarianError(f"lazylibrarian: the backup's database can't be read ({exc})") from exc
            if status != "ok":
                raise LazyLibrarianError(f"lazylibrarian: the backup's database failed SQLite's check ({status})")
            self._check_stopped()
            for name, target in zip(names, targets):
                # "wb" truncates in place: owner and mode stay LazyLibrarian's.
                with open(os.path.join(work, name), "rb") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, _MIB)
        # _check_stopped removed the -wal; any -shm index left is stale.
        for leftover in (self._path(DATABASE) + "-wal", self._path(DATABASE) + "-shm"):
            if os.path.exists(leftover):
                os.remove(leftover)
        logger.info("lazylibrarian: restored %s into %s - start LazyLibrarian to load them",
                    ", ".join(names), self.data_dir)
