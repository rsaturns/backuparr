"""Backup/restore driver for Jellyfin's built-in backup (Jellyfin 10.11+).

Jellyfin's REST API (/api-docs/openapi.json) can create, list and restore
backups, all with an administrator API key (Dashboard > API Keys):

- POST /Backup/Create writes a zip to Jellyfin's own backups folder
  (<data dir>/backups: /config/data/backups in the official image,
  /config/data/data/backups in linuxserver's) and
  answers with its manifest once the file is complete. Only the database is
  requested: users, watch state, playlists, libraries and server settings
  come along with it (the archive always carries the config XML files too),
  while metadata images, trickplay and subtitles - large and regenerable -
  are left out.
- GET /Backup lists the archives in that folder.
- POST /Backup/Restore takes the *filename* of an archive in that folder,
  restarts Jellyfin in-process and applies the archive on startup.

There is no endpoint to download an archive or upload one. So Jellyfin's
backups folder is mounted into Backuparr (backup_dir): backup() copies the
new archive out of it and removes Jellyfin's copy, and restore_from_file()
writes the archive into it - under any name, Jellyfin doesn't care - before
calling the restore endpoint. A read-only mount still allows backups; the
copy then stays in Jellyfin's folder.

Auth: only `Authorization: MediaBrowser Token="<key>"` is accepted by
current versions. The legacy X-Emby-Token header and api_key query
parameter are refused (401), so the key never goes in the URL.
"""
import logging
import os
import re
import shutil
import time
import zipfile

import requests

logger = logging.getLogger(f"backuparr.{__name__}")

# Database only - see the module docstring.
BACKUP_OPTIONS = {"Database": True, "Metadata": False, "Trickplay": False, "Subtitles": False}

_SAFE_NAME = re.compile(r"^(?!\.)[A-Za-z0-9._-]+\Z")


class JellyfinError(RuntimeError):
    pass


def _server_filename(path):
    """The archive's filename from the server-side path in a manifest
    (Linux or Windows separators). It's joined onto a local folder, so
    anything that isn't a plain filename is refused."""
    name = re.split(r"[\\/]", path or "")[-1]
    if not _SAFE_NAME.match(name):
        raise JellyfinError(f"jellyfin: unexpected backup path in Jellyfin's answer: {path!r}")
    return name


def _server_folder(path):
    head = re.split(r"[\\/]", path or "")[:-1]
    return "/".join(head) or "its backups folder"


def is_jellyfin_backup(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return "manifest.json" in zf.namelist() and zf.testzip() is None
    except zipfile.BadZipFile:
        return False


class JellyfinApp:
    def __init__(self, url, api_key, backup_dir=None, timeout=30, backup_timeout=1800):
        self.url = url.rstrip("/")
        self.backup_dir = backup_dir or None
        self.timeout = timeout
        # Creating a backup exports the whole database before Jellyfin answers.
        self.backup_timeout = backup_timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f'MediaBrowser Token="{api_key}"', "Accept": "application/json"})

    def _check(self, res):
        if res.status_code == 401:
            raise JellyfinError("jellyfin: unauthorized - check the API key (Dashboard > API Keys)")
        if res.status_code == 403:
            raise JellyfinError("jellyfin: this key can't manage backups - use an API key from Dashboard > API Keys, not a user's token")
        res.raise_for_status()
        return res

    def server_version(self):
        return self._check(self.session.get(f"{self.url}/System/Info", timeout=10)).json().get("Version", "?")

    def list_backups(self):
        res = self.session.get(f"{self.url}/Backup", timeout=self.timeout)
        if res.status_code == 404:
            raise JellyfinError("jellyfin: this server has no backup API - Jellyfin 10.11 or newer is needed")
        return self._check(res).json()

    def _require_folder(self, writable=False):
        if not self.backup_dir:
            raise JellyfinError("jellyfin: set the backups folder - Jellyfin's backups folder, mounted into this container")
        if not os.path.isdir(self.backup_dir):
            raise JellyfinError(
                f"jellyfin: backups folder {self.backup_dir} doesn't exist in this container - "
                "mount Jellyfin's backups folder there (/config/data/backups in the official image, "
                "/config/data/data/backups in linuxserver's)"
            )
        if writable and not os.access(self.backup_dir, os.W_OK):
            raise JellyfinError(
                f"jellyfin: backups folder {self.backup_dir} isn't writable - a restore copies the archive "
                "into it, so mount it read-write and make it writable by Backuparr's PUID:PGID"
            )

    def test_connection(self):
        version = self.server_version()
        backups = self.list_backups()
        self._require_folder()
        paths = [b["Path"] for b in backups if b.get("Path")]
        if paths and not any(os.path.isfile(os.path.join(self.backup_dir, _server_filename(p))) for p in paths):
            raise JellyfinError(
                f"jellyfin: {self.backup_dir} doesn't hold the backups Jellyfin lists in "
                f"{_server_folder(paths[0])} - check the volume mount"
            )
        if not os.access(self.backup_dir, os.W_OK):
            return f"jellyfin {version} reachable; backups folder is read-only, so backups work but restores won't"
        return f"jellyfin {version} reachable, backups folder OK"

    def _wait_for_file(self, path, wait_s=10):
        # A network share can show a new file a moment after Jellyfin wrote it.
        deadline = time.time() + wait_s
        while not os.path.isfile(path) and time.time() < deadline:
            time.sleep(1)
        return os.path.isfile(path)

    def backup(self, dest_dir):
        self._require_folder()
        logger.info("jellyfin: creating a backup (database and config, no metadata images)...")
        res = self.session.post(f"{self.url}/Backup/Create", json=BACKUP_OPTIONS, timeout=self.backup_timeout)
        if res.status_code == 404:
            raise JellyfinError("jellyfin: this server has no backup API - Jellyfin 10.11 or newer is needed")
        manifest = self._check(res).json()
        server_path = manifest.get("Path")
        filename = _server_filename(server_path)
        source = os.path.join(self.backup_dir, filename)
        if not self._wait_for_file(source):
            raise JellyfinError(
                f"jellyfin: Jellyfin wrote {filename} to {_server_folder(server_path)}, but it isn't in "
                f"{self.backup_dir} - mount that folder of Jellyfin's container there"
            )

        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, filename)
        shutil.copyfile(source, dest)
        if not is_jellyfin_backup(dest):
            raise JellyfinError(f"jellyfin: {filename} isn't a valid Jellyfin backup archive")
        logger.info("jellyfin: backup %s created (Jellyfin %s)", filename, manifest.get("ServerVersion", "?"))

        try:
            os.remove(source)
        except OSError as exc:
            logger.warning(
                "jellyfin: couldn't remove %s from Jellyfin's backups folder (%s) - Jellyfin keeps that copy",
                filename, exc.strerror or type(exc).__name__,
            )
        return dest

    def restore_from_file(self, local_zip_path):
        """Writes the archive into Jellyfin's backups folder and starts the
        restore. Jellyfin answers, then restarts and applies it - the copy
        stays in the folder, listed in Jellyfin like its own backups."""
        if not is_jellyfin_backup(local_zip_path):
            raise JellyfinError("jellyfin: this file isn't a Jellyfin backup archive")
        self._require_folder(writable=True)
        self.list_backups()  # Fails on a bad key or old version before anything is written.

        filename = os.path.basename(local_zip_path)
        target = os.path.join(self.backup_dir, filename)
        shutil.copyfile(local_zip_path, target)
        # Backuparr's private umask would make this unreadable to Jellyfin
        # when the two containers run as different users.
        os.chmod(target, 0o644)

        listed = {_server_filename(b["Path"]) for b in self.list_backups() if b.get("Path")}
        if filename not in listed:
            os.remove(target)
            raise JellyfinError(
                f"jellyfin: Jellyfin doesn't list {filename} after it was copied to {self.backup_dir} - "
                "check that this is Jellyfin's own backups folder, and that the backup comes from a "
                "Jellyfin version this server can restore"
            )

        logger.info("jellyfin: restoring %s, Jellyfin will restart to apply it...", filename)
        res = self.session.post(f"{self.url}/Backup/Restore", json={"ArchiveFileName": filename}, timeout=self.timeout)
        self._check(res)
