"""Backup/restore driver for Bazarr's own backup feature.

Bazarr's backup zip (bazarr.db via SQLite's .backup(), plus config.yaml) is
created, listed and deleted through /api/system/backups with the X-API-KEY
header. Its quirks:

- The download route, /system/backup/download/<filename>, is outside the
  /api blueprint and gated by Bazarr's web auth (Settings > General >
  Security) instead of the API key: it works with "None" or "Basic" (the
  card's username and password), not "Forms".
- A backup is listed as soon as the file is created, before a large
  database is written, and downloading it then returns a truncated zip with
  no error. backup() downloads again until the zip reads back whole.
- There's no upload restore: Bazarr only restores a file already in its own
  backup folder, named bazarr_backup_v<anything>.zip (anything else is a
  500). restore_from_file() copies the archive into that mounted folder
  under such a name, then calls the restore API.
"""
import logging
import os
import shutil
import time
import zipfile

import requests

from apps.base import AppError, poll_until, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")


class BazarrError(AppError):
    pass


def _complete_zip(path):
    """Whether every member reads back with the right CRC. testzip() returns
    the first bad member's name rather than raising."""
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except (zipfile.BadZipFile, OSError):
        return False


class BazarrApp:
    def __init__(self, url, api_key, username=None, password=None, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})
        self.download_auth = (username, password) if username and password else None

    def test_connection(self):
        res = self.session.get(f"{self.url}/api/system/backups", timeout=10)
        if res.status_code == 401:
            raise BazarrError("bazarr: unauthorized - check the API key")
        res.raise_for_status()
        count = len(res.json().get("data", []))
        return f"bazarr reachable ({count} existing backup(s) on server)"

    def app_version(self):
        res = self.session.get(f"{self.url}/api/system/status", timeout=10)
        res.raise_for_status()
        return (res.json().get("data") or {}).get("bazarr_version")

    def list_backups(self):
        res = self.session.get(f"{self.url}/api/system/backups", timeout=self.timeout)
        res.raise_for_status()
        return res.json().get("data", [])

    def trigger_backup(self):
        res = self.session.post(f"{self.url}/api/system/backups", timeout=self.timeout)
        res.raise_for_status()

    def delete_backup(self, filename):
        res = self.session.delete(
            f"{self.url}/api/system/backups", params={"filename": filename}, timeout=self.timeout
        )
        res.raise_for_status()

    def backup(self, dest_dir, poll_interval=2, timeout_s=180):
        before = {b["filename"] for b in self.list_backups()}
        self.trigger_backup()
        logger.info("bazarr: backup triggered, waiting for the file to appear...")

        deadline = time.monotonic() + timeout_s

        def new_file():
            new = {b["filename"] for b in self.list_backups()} - before
            return os.path.basename(sorted(new)[-1]) if new else None

        new_filename = poll_until(new_file, timeout_s, poll_interval,
                                  lambda waited: logger.info("bazarr: still waiting on the backup file (%ds)...", waited))
        if not new_filename:
            raise BazarrError("bazarr: backup job did not produce a new file in time")

        dest = os.path.join(dest_dir, new_filename)
        download_url = f"{self.url}/system/backup/download/{new_filename}"

        # Listed before it's fully written: download until it reads back whole.
        def complete_download():
            with self.session.get(download_url, auth=self.download_auth, timeout=self.timeout, stream=True) as res:
                if res.status_code == 401:
                    raise BazarrError(
                        "bazarr: download unauthorized - if Settings > General > Security is set to "
                        "'Forms', switch it to 'None' or 'Basic' (and enter the Basic auth username and "
                        "password for Bazarr in Backuparr's Settings) for automated backups to work"
                    )
                res.raise_for_status()
                save_stream(res, dest)
            if _complete_zip(dest):
                return True
            logger.info("bazarr: backup file still being written, retrying download...")
            return None

        if not poll_until(complete_download, deadline - time.monotonic(), poll_interval):
            raise BazarrError(f"bazarr: {new_filename} never finished writing on the server in time")

        try:
            self.delete_backup(new_filename)
        except requests.RequestException:
            logger.warning("bazarr: failed to delete server-side backup copy %s", new_filename)

        return dest

    def restore_from_file(self, local_zip_path, bazarr_backup_dir):
        """Copies local_zip_path into Bazarr's backup folder (as mounted
        here) as bazarr_backup_v<stem>.zip and triggers the restore. Bazarr
        restarts itself once it's done."""
        # Check Bazarr answers before the restore request: after it, a dropped
        # connection or a proxy's gateway error means a restart, not an outage.
        self.list_backups()
        stem = os.path.splitext(os.path.basename(local_zip_path))[0]
        filename = f"bazarr_backup_v{stem}.zip"
        target = os.path.join(bazarr_backup_dir, filename)
        shutil.copyfile(local_zip_path, target)
        # Backuparr's private umask would make this unreadable to Bazarr
        # when the two containers run as different users.
        os.chmod(target, 0o644)

        try:
            res = self.session.patch(
                f"{self.url}/api/system/backups", params={"filename": filename}, timeout=self.timeout
            )
        except requests.exceptions.ConnectionError:
            # Bazarr restores and restarts within this same request - the
            # process tearing itself down before the response finishes
            # sending is the expected outcome of a successful restore, not
            # a failure.
            logger.info("bazarr: connection dropped as expected during restart")
            return None
        if res.status_code in (502, 503, 504):
            # The same dropped connection, as reported by a reverse proxy.
            logger.info("bazarr: proxy returned HTTP %s as expected during restart", res.status_code)
            return None
        res.raise_for_status()
        return res
