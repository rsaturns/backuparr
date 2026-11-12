"""Backup driver for a self-hosted Bitwarden server (#194).

Bitwarden has no backup API (https://bitwarden.com/help/backup-on-premise/),
so a folder is mounted into Backuparr as data_dir, read-only, and the kind
of install is told from what's in it:

- Standard (the multi-container install from bitwarden.sh): its ./bwdata.
  Bitwarden's guide says to keep the whole folder; the backup takes all of
  it - env/ (database and certificate passwords), core/ (attachments,
  aspnet-dataprotection, licenses), identity/ (the certificate that signs
  login tokens), ssl/, config.yml and the rest - except logs/ and the live
  database in mssql/data. The database comes from the newest of the nightly
  backups the mssql container makes into mssql/backups
  (vault_FULL_YYYYMMDD_HHMMSS.BAK, util/MsSql/backup-db.sh in
  bitwarden/server). SQL Server writes that file in place, so one changed in
  the last few minutes may still be being written: it's skipped for the one
  before it.
- Lite (the single container): its backups folder (/backups in the
  container). Lite 2026.10.1 and newer make a backup there daily
  (bitwarden-lite_YYYYMMDD_HHMMSS.tar.gz: the database dump, whichever
  database it uses, plus /etc/bitwarden; bitwarden-lite/backup/README.md in
  bitwarden/self-host). The newest one is backed up as it is, so it restores
  with lite's own restore-db.sh. They're written as .partial and renamed
  once checked, so a finished name is always a whole archive.

The URL is only asked for the server's version: GET /api/config, which
every Bitwarden client calls before signing in. Vaultwarden answers it too,
naming itself in "server". Bitwarden serves HTTPS - its own self-signed
certificate unless the install was given one - so a self-signed certificate
is trusted on the card and pinned (apps/cert_pin.py). Backups are always encrypted - they hold the
database and certificate passwords and the data-protection keys. Restores
are by hand (manual_restore): the database restore runs inside Bitwarden's
containers.
"""
import gzip
import json
import logging
import os
import re
import shutil
import stat
import tarfile
import time
import zipfile

from apps import cert_pin
from apps.base import AppError
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

STANDARD = "standard"
LITE = "lite"
MANIFEST = "backuparr-bitwarden.json"
# Lite's own backups began with 2026.10.1 (bitwarden/self-host#563).
LITE_BACKUPS_SINCE = (2026, 10, 1)
# A nightly backup older than this is worth a warning.
STALE_SECONDS = 2 * 24 * 3600
# A .BAK changed this recently may still be being written by SQL Server.
SETTLE_SECONDS = 10 * 60

_BACKUPS = "mssql/backups"
# <database>_FULL_<timestamp>.BAK - "vault" unless DATABASE is set.
_BAK = re.compile(r"[A-Za-z0-9_-]+_FULL_(\d{8}_\d{6})\.BAK")
_LITE_ARCHIVE = re.compile(r"bitwarden-lite_(\d{8}_\d{6})\.tar\.gz")
# Left out of a standard backup: logs, and the live database (the .BAK
# stands in for it).
_SKIPPED = ("logs", "mssql/data", _BACKUPS)
_MAX_MANIFEST = 1024 * 1024


class BitwardenError(AppError):
    pass


def _version_tuple(version):
    parts = re.findall(r"\d+", version or "")[:3]
    return tuple(int(p) for p in parts) if len(parts) == 3 else None


def _age(path):
    return time.time() - os.stat(path).st_mtime


def _days(seconds):
    days = seconds / 86400
    return f"{days:.0f} days" if days >= 1.5 else "a day"


def _member(name):
    """A lite archive member's name without tar's leading ./"""
    return name[2:] if name.startswith("./") else name


def read_lite_manifest(path):
    """The manifest.txt at the front of a lite archive, or None."""
    with tarfile.open(path, "r:gz") as tar:
        for member in tar:
            if _member(member.name) == "manifest.txt":
                if not member.isfile() or member.size > _MAX_MANIFEST:
                    return None
                return tar.extractfile(member).read().decode("utf-8", "replace")
    return None


def lite_holds_settings(manifest_text):
    """Whether lite's manifest lists settings.env among the contents
    (BACKUP_INCLUDE_SETTINGS=true)."""
    return re.search(r"^settings\.env\s+host settings file\s*$", manifest_text or "", re.M) is not None


def lite_image_tag(manifest_text):
    """The version in lite's manifest, when its image tag is one (not
    "latest" or "unknown ...")."""
    match = re.search(r"^lite image tag:\s*v?(\d+\.\d+\.\d+)\s*$", manifest_text or "", re.M)
    return match.group(1) if match else None


def _check_lite(path):
    """Reads the whole archive - gzip's checksum covers every byte - and
    needs what lite's restore-db.sh does: the manifest, a dump and config/
    with its data-protection keys."""
    names = set()
    try:
        with gzip.open(path) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for member in tar:
                    names.add(_member(member.name).rstrip("/"))
                    if member.isfile():
                        with tar.extractfile(member) as f:
                            while f.read(1024 * 1024):
                                pass
            # tar stops at its end-of-archive blocks; gzip's checksum is after them.
            while stream.read(1024 * 1024):
                pass
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise BitwardenError(f"bitwarden: the lite backup is damaged ({exc or type(exc).__name__})") from None
    if "manifest.txt" not in names:
        raise BitwardenError("bitwarden: manifest.txt is missing from the lite backup")
    if not any(name.startswith("database/") and name != "database/" for name in names):
        raise BitwardenError("bitwarden: the lite backup has no database dump")
    if "config/data-protection" not in names:
        raise BitwardenError("bitwarden: the lite backup has no config/data-protection")


def _check_standard(path):
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                raise BitwardenError(f"bitwarden: {bad} in the backup is damaged")
            names = set(zf.namelist())
            try:
                manifest = json.loads(zf.read(MANIFEST))
            except (KeyError, ValueError):
                raise BitwardenError(f"bitwarden: {MANIFEST} is missing or unreadable") from None
            database = f"{_BACKUPS}/{manifest.get('database_backup')}"
            if database not in names:
                raise BitwardenError(f"bitwarden: {database} is missing from the backup")
            with zf.open(database) as f:
                if f.read(4) != b"TAPE":
                    raise BitwardenError(f"bitwarden: {database} isn't a SQL Server backup")
            if not any(name.startswith("env/") for name in names):
                raise BitwardenError("bitwarden: env/ is missing from the backup")
    except (OSError, zipfile.BadZipFile) as exc:
        raise BitwardenError(f"bitwarden: the backup can't be opened ({type(exc).__name__})") from None


def check_backup(path):
    """archive_check's check: a standard backup is Backuparr's zip, a lite
    one lite's own .tar.gz."""
    if zipfile.is_zipfile(path):
        _check_standard(path)
    else:
        _check_lite(path)


class BitwardenApp:
    def __init__(self, url, data_dir=None, trusted_cert=None, timeout=30):
        self.url = url.rstrip("/")
        self.data_dir = data_dir or None
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        cert_pin.pin(self.session, "bitwarden", trusted_cert)
        self.session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------ server ----
    def app_version(self):
        """The server's version, e.g. "2026.10.1", from /api/config (no
        login)."""
        res = self.session.get(f"{self.url}/api/config", timeout=10)
        if res.status_code == 404:
            raise BitwardenError(f"bitwarden: {self.url} doesn't look like Bitwarden (no /api/config) - check the URL")
        res.raise_for_status()
        try:
            config = res.json()
        except ValueError:
            config = None
        if not isinstance(config, dict) or not isinstance(config.get("version"), str):
            raise BitwardenError(f"bitwarden: {self.url} doesn't look like Bitwarden - check the URL")
        server = config.get("server")
        if isinstance(server, dict) and str(server.get("name", "")).lower() == "vaultwarden":
            raise BitwardenError(f"bitwarden: {self.url} is Vaultwarden - back it up as Vaultwarden instead")
        return config["version"] or None

    # ------------------------------------------------------------ folder ----
    def _path(self, *parts):
        return os.path.join(self.data_dir, *parts)

    def install(self):
        """standard or lite, from the mounted folder."""
        if not self.data_dir:
            raise BitwardenError("bitwarden: set the folder - Bitwarden's bwdata folder, or lite's backups folder")
        if not os.path.isdir(self.data_dir):
            raise BitwardenError(f"bitwarden: {self.data_dir} doesn't exist in this container - mount Bitwarden's "
                                 "bwdata folder (or lite's backups folder) there")
        if not os.access(self.data_dir, os.R_OK | os.X_OK):
            raise BitwardenError(f"bitwarden: Backuparr can't read {self.data_dir} - check its permissions")
        if os.path.isdir(self._path("env")) or os.path.isdir(self._path("mssql")):
            return STANDARD
        if os.path.isdir(self._path("data-protection")) or os.path.isfile(self._path("identity.pfx")):
            raise BitwardenError(f"bitwarden: {self.data_dir} is lite's /etc/bitwarden - mount lite's backups "
                                 "folder (/backups in its container) instead")
        return LITE

    def _newest_bak(self):
        folder = self._path(*_BACKUPS.split("/"))
        if not os.path.isdir(folder):
            raise BitwardenError(f"bitwarden: {self.data_dir} has no {_BACKUPS} - mount Bitwarden's whole bwdata folder")
        found = sorted(((m.group(1), name) for name in os.listdir(folder) if (m := _BAK.fullmatch(name))
                        and os.path.isfile(os.path.join(folder, name))), reverse=True)
        if not found:
            raise BitwardenError(f"bitwarden: no nightly database backup in {self.data_dir}/{_BACKUPS} yet - "
                                 "Bitwarden makes one at midnight UTC, or run docker exec -i bitwarden-mssql /backup-db.sh")
        for _stamp, name in found:
            path = os.path.join(folder, name)
            if _age(path) >= SETTLE_SECONDS:
                return name, path
            logger.info("bitwarden: skipping %s - changed in the last few minutes, so it may still be being written",
                        name)
        raise BitwardenError("bitwarden: the only database backup is still being written - try again in a few minutes")

    def _newest_lite(self, version=None):
        found = sorted(((m.group(1), name) for name in os.listdir(self.data_dir)
                        if (m := _LITE_ARCHIVE.fullmatch(name)) and os.path.isfile(self._path(name))), reverse=True)
        if not found:
            older = _version_tuple(version)
            if older and older < LITE_BACKUPS_SINCE:
                raise BitwardenError(f"bitwarden: update Bitwarden lite to 2026.10.1 or newer ({version} is "
                                     "installed) - it makes the daily backups Backuparr copies")
            raise BitwardenError(f"bitwarden: no lite backups in {self.data_dir} - mount lite's backups folder "
                                 "(/backups in its container); lite 2026.10.1 and newer make one daily")
        name = found[0][1]
        return name, self._path(name)

    def _unreadable(self, path, lite):
        if os.access(path, os.R_OK):
            return None
        if lite:
            return (f"bitwarden: Backuparr can't read {path} - set BACKUP_FILE_MODE=640 in lite's settings.env "
                    "and give lite and Backuparr the same PGID")
        return (f"bitwarden: Backuparr can't read {path} - run Backuparr as the user that owns bwdata "
                "(PUID and PGID)")

    def _standard_files(self):
        """(path relative to bwdata, absolute path) of every file backed up
        besides the database."""
        found = []
        for root, dirs, files in os.walk(self.data_dir):
            relative_root = os.path.relpath(root, self.data_dir).replace(os.sep, "/")
            relative_root = "" if relative_root == "." else relative_root + "/"
            dirs[:] = sorted(d for d in dirs if relative_root + d not in _SKIPPED
                             and not os.path.islink(os.path.join(root, d)))
            for name in sorted(files):
                path = os.path.join(root, name)
                relative = relative_root + name
                if relative == MANIFEST or os.path.islink(path) or not stat.S_ISREG(os.stat(path).st_mode):
                    continue
                found.append((relative, path))
        return found

    def _warn_if_stale(self, name, path, where):
        age = _age(path)
        if age > STALE_SECONDS:
            logger.warning("bitwarden: the newest %s (%s) is %s old - check that its nightly backups still run",
                           where, name, _days(age))
            return True
        return False

    def test_connection(self):
        version = self.app_version()
        install = self.install()
        if install == STANDARD:
            name, path = self._newest_bak()
            problem = self._unreadable(path, False) or next(
                (self._unreadable(p, False) for _relative, p in self._standard_files() if not os.access(p, os.R_OK)),
                None)
            if problem:
                raise BitwardenError(problem)
            what = "standard install"
        else:
            name, path = self._newest_lite(version)
            problem = self._unreadable(path, True)
            if problem:
                raise BitwardenError(problem)
            what = "lite"
        stale = " (more than 2 days old)" if _age(path) > STALE_SECONDS else ""
        return f"bitwarden {version or '?'} reachable ({what}), newest backup {name}{stale}"

    # ------------------------------------------------------------ backup ----
    def backup(self, dest_dir):
        version = self.app_version()
        install = self.install()
        os.makedirs(dest_dir, exist_ok=True)
        if install == LITE:
            return self._backup_lite(dest_dir, version)
        return self._backup_standard(dest_dir, version)

    def _backup_lite(self, dest_dir, version):
        name, path = self._newest_lite(version)
        problem = self._unreadable(path, True)
        if problem:
            raise BitwardenError(problem)
        logger.info("bitwarden: copying lite's newest backup, %s...", name)
        self._warn_if_stale(name, path, "lite backup")
        target = os.path.join(dest_dir, name)
        shutil.copyfile(path, target)
        try:
            manifest = read_lite_manifest(target)
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise BitwardenError(f"bitwarden: {name} can't be read ({exc or type(exc).__name__})") from None
        if manifest is None:
            raise BitwardenError(f"bitwarden: {name} has no manifest.txt - it isn't a lite backup")
        if not lite_holds_settings(manifest):
            logger.warning("bitwarden: %s doesn't hold settings.env (installation ID and key, database password) - "
                           "set BACKUP_INCLUDE_SETTINGS=true in lite's settings.env, or keep a copy of it yourself",
                           name)
        logger.info("bitwarden: backed up %s (%.1f MB) from Bitwarden lite %s", name,
                    os.path.getsize(target) / 1e6, version or "?")
        return target

    def _backup_standard(self, dest_dir, version):
        name, path = self._newest_bak()
        files = self._standard_files()
        for _relative, p in [(name, path), *files]:
            problem = self._unreadable(p, False)
            if problem:
                raise BitwardenError(problem)
        logger.info("bitwarden: copying bwdata and the newest nightly database backup, %s (no logs or live "
                    "database)...", name)
        self._warn_if_stale(name, path, "nightly database backup")
        with open(path, "rb") as f:
            if f.read(4) != b"TAPE":
                raise BitwardenError(f"bitwarden: {name} isn't a SQL Server backup")
        size = 0
        for relative, source in [(f"{_BACKUPS}/{name}", path), *files]:
            target = os.path.join(dest_dir, *relative.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
            size += os.path.getsize(target)
        if not any(relative.startswith("env/") for relative, _p in files):
            logger.warning("bitwarden: no env folder in %s - the backup lacks the database and certificate passwords",
                           self.data_dir)
        if not any(relative.startswith("core/aspnet-dataprotection/") for relative, _p in files):
            logger.warning("bitwarden: no core/aspnet-dataprotection in %s - a vault restored without its keys "
                           "can't be decrypted", self.data_dir)
        with open(os.path.join(dest_dir, MANIFEST), "w") as f:
            json.dump({"bitwarden_version": version, "install": STANDARD, "database_backup": name}, f, indent=2)
        logger.info("bitwarden: backed up %s and %d files from bwdata (%.1f MB) from Bitwarden %s", name,
                    len(files), size / 1e6, version or "?")
        return dest_dir
