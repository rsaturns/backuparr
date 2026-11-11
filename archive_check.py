"""Checks a backup archive before it's uploaded: it opens and every file in it
reads back intact, each SQLite database passes a quick check, and the files
the app's restore needs are there (and its JSON files parse). A backup that
fails is not uploaded, so a damaged archive is found the night it's made,
not when it's needed.

What each app needs comes from its restore. The *arrs and Bazarr can keep
their data in PostgreSQL, which their backups don't hold, so only their
config file is required; a database is checked whenever one is there."""
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import zipfile

from apps import immich, opnsense, pfsense
from apps.base import AppError
from apps.sqlite_snapshot import quick_check
from apps.tdarr import COLLECTIONS as TDARR_COLLECTIONS
from config_store import instance_type

_SERVARR = {"radarr", "sonarr", "lidarr", "whisparr", "prowlarr"}

# Per app type: groups of names, each group satisfied by any one of its
# names. JSON names are parsed too.
_REQUIRED = {
    **{app: [["config.xml"]] for app in _SERVARR},
    "profilarr": [["data/profilarr.db"]],
    "bazarr": [["config.yaml", "config.ini"]],
    "tdarr": [[f"{name}.json" for name in TDARR_COLLECTIONS]],
    "sabnzbd": [["sabnzbd_config.json"]],
    "nzbget": [["nzbget.conf"], ["backuparr-nzbget.json"]],
    "tautulli": [["tautulli.db"], ["config.ini"]],
    "jellyfin": [["manifest.json"]],
    "emby": [["backuparr-emby.json"], ["data/library.db"]],
    "plex": [["backuparr-plex.json"], ["com.plexapp.plugins.library.db"], ["Preferences.xml"]],
    "seerr": [["backuparr-seerr.json"], ["db/db.sqlite3"], ["settings.json"]],
    "lazylibrarian": [["lazylibrarian.db"], ["config.ini"]],
    "pihole": [["etc/pihole/pihole.toml"], ["etc/pihole/gravity.db"]],
    "vaultwarden": [["backuparr-vaultwarden.json"], ["db.sqlite3"]],
}
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_CHUNK = 1024 * 1024


class ArchiveCheckError(AppError):
    pass


class _Zip:
    def __init__(self, path):
        self.archive = zipfile.ZipFile(path)

    def names(self):
        return [info.filename for info in self.archive.infolist() if not info.is_dir()]

    def verify(self):
        """The first file whose contents don't match their CRC, or None."""
        return self.archive.testzip()

    def open(self, name):
        return self.archive.open(name)

    def close(self):
        self.archive.close()


class _Tar:
    def __init__(self, path):
        self.archive = tarfile.open(path)

    def names(self):
        return [member.name for member in self.archive.getmembers() if member.isfile()]

    def verify(self):
        # Reading every file to the end makes gzip check its CRC; a damaged
        # or cut-short stream raises.
        for member in self.archive.getmembers():
            if member.isfile():
                with self.archive.extractfile(member) as f:
                    while f.read(_CHUNK):
                        pass
        return None

    def open(self, name):
        return self.archive.extractfile(name)

    def close(self):
        self.archive.close()


def _open(path):
    if zipfile.is_zipfile(path):
        return _Zip(path)
    if tarfile.is_tarfile(path):
        return _Tar(path)
    raise ArchiveCheckError("it isn't a zip or tar archive")


_MISSING_COLLATION = re.compile(r"no such collation sequence: (\S+)")
_MISSING_MODULE = re.compile(r"unknown tokenizer|no such module|no such tokenizer")


def _binary(a, b):
    return (a > b) - (a < b)


def _drop_virtual_tables(db_path):
    """Plex's full-text and spellfix tables need modules stock SQLite
    doesn't have, and SQLite won't check a database whose schema it can't
    load. Their data lives in ordinary tables, which stay and are checked;
    only the definitions go, and only from this check's own copy."""
    db = sqlite3.connect(db_path)
    try:
        db.execute("PRAGMA writable_schema=ON")
        db.execute("DELETE FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'")
        db.commit()
    finally:
        db.close()


def _run_quick_check(db_path):
    """quick_check's problems, or None (type notes aren't problems: see
    apps.sqlite_snapshot). A collation the app registers itself (Plex's
    icu_root) is stood in for: quick_check checks the database's structure,
    not the order a collation would give."""
    collations, dropped = set(), False
    for _ in range(20):
        db = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            for collation in collations:
                db.create_collation(collation, _binary)
            problems = quick_check(db)
        except sqlite3.OperationalError as exc:
            missing = _MISSING_COLLATION.search(str(exc))
            if missing and missing.group(1) not in collations:
                collations.add(missing.group(1))
                continue
            if _MISSING_MODULE.search(str(exc)) and not dropped:
                db.close()
                _drop_virtual_tables(db_path)
                dropped = True
                continue
            return str(exc)
        except sqlite3.DatabaseError as exc:
            return str(exc)
        finally:
            db.close()
        return "; ".join(problems[:3]) or None
    return "its schema couldn't be loaded"


def _quick_check(archive, name, work_dir):
    """SQLite's quick_check on one database, copied out of the archive alone:
    a journal beside it isn't applied, so this reads exactly what a restore
    would get."""
    db_path = os.path.join(work_dir, "check.db")
    with archive.open(name) as src, open(db_path, "wb") as dst:
        shutil.copyfileobj(src, dst, _CHUNK)
    try:
        return _run_quick_check(db_path)
    finally:
        os.remove(db_path)


# Apps whose backup is a single file rather than an archive, and its check.
_SINGLE_FILE = {"opnsense": opnsense.check_config, "pfsense": pfsense.check_config, "immich": immich.check_dump}


def _check(app_type, path):
    if app_type in _SINGLE_FILE:
        try:
            _SINGLE_FILE[app_type](path)
        except AppError as exc:
            raise ArchiveCheckError(str(exc).partition(": ")[2] or str(exc)) from None
        return
    try:
        archive = _open(path)
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        raise ArchiveCheckError(f"it can't be opened ({type(exc).__name__})") from None
    try:
        try:
            bad = archive.verify()
        except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
            raise ArchiveCheckError(f"it's damaged ({exc or type(exc).__name__})") from None
        if bad:
            raise ArchiveCheckError(f"{bad} in it is damaged")
        names = set(archive.names())
        for group in _REQUIRED.get(app_type, []):
            present = [name for name in group if name in names]
            if not present:
                needed = group[0] if len(group) == 1 else " or ".join(group[:2]) + (" ..." if len(group) > 2 else "")
                raise ArchiveCheckError(f"{needed} is missing from it")
            for name in present:
                if name.endswith(".json"):
                    try:
                        with archive.open(name) as f:
                            json.load(f)
                    except ValueError:
                        raise ArchiveCheckError(f"{name} in it isn't valid JSON") from None
        work_dir = tempfile.mkdtemp(prefix="backuparr-check-", dir=os.path.dirname(path) or None)
        try:
            for name in sorted(names):
                if name.endswith(_SQLITE_SUFFIXES):
                    problem = _quick_check(archive, name, work_dir)
                    if problem:
                        raise ArchiveCheckError(f"{name} in it failed SQLite's quick check ({problem})")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    finally:
        archive.close()


def check(app, path):
    """Raises ArchiveCheckError, saying what's wrong, unless the archive at
    path is a sound backup of app (an app or instance id)."""
    try:
        _check(instance_type(app), path)
    except ArchiveCheckError as exc:
        raise ArchiveCheckError(f"{app}: the backup failed its check, so it wasn't uploaded - {exc}") from None
