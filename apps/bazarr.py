"""Backup/restore driver for Bazarr's own backup feature.

Bazarr's backup zip (bazarr.db snapshotted via sqlite's native .backup(),
plus config.yaml) is created through its REST API
(POST/GET/DELETE /api/system/backups, authenticated with X-API-KEY like the
rest of Bazarr's API). The one wrinkle: the download route itself,
/system/backup/download/<filename>, lives outside the /api blueprint and is
gated by Bazarr's own web-auth setting (Settings > General > Security)
rather than the API key - so if that's set to "Forms", automated download
isn't supported (switch it to "None" or "Basic" for this tool to work, or
protect it at the reverse-proxy layer instead).

Restore is the other wrinkle: Bazarr only restores from a file already
sitting in its own backup folder (there's no upload-restore endpoint), so
restore_from_file() takes the *local path to that mounted folder* and places
the file there itself before calling the restore API. Bazarr also only
accepts a restore filename matching its own naming convention
(bazarr_backup_v<anything>.zip, see utilities/backup.py's
_BACKUP_FILENAME_RE) - anything else, including our own bazarr_<timestamp>
storage name, gets refused with a 500. restore_from_file() writes the copy
under a name that fits instead of reusing the source filename.

A third wrinkle in backup(): the filename appears in Bazarr's listing the
moment the file is created, before a multi-MB db backup is done being
written - downloading right then silently returns a truncated, unreadable
zip (no error; the response's Content-Length just matches whatever was on
disk at that instant). Fine for a small backup, hit reliably for a real
production-sized one. backup() re-downloads until the zip actually
validates.
"""
import logging
import os
import time
import zipfile

import requests

logger = logging.getLogger(f"backuparr.{__name__}")


class BazarrError(RuntimeError):
    pass


class BazarrApp:
    def __init__(self, url, api_key, username=None, password=None, timeout=30):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})
        self.download_auth = (username, password) if username and password else None

    def test_connection(self):
        res = self.session.get(f"{self.url}/api/system/backups", timeout=10)
        if res.status_code == 401:
            raise BazarrError("bazarr: unauthorized - check the API key")
        res.raise_for_status()
        count = len(res.json().get("data", []))
        return f"bazarr reachable ({count} existing backup(s) on server)"

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

        deadline = time.time() + timeout_s
        new_filename = None
        waited = 0
        while time.time() < deadline:
            after = {b["filename"] for b in self.list_backups()}
            new = after - before
            if new:
                new_filename = sorted(new)[-1]
                break
            time.sleep(poll_interval)
            waited += poll_interval
            if waited % 10 == 0:
                logger.info("bazarr: still waiting on the backup file (%ds)...", waited)
        if not new_filename:
            raise BazarrError("bazarr: backup job did not produce a new file in time")

        dest = os.path.join(dest_dir, new_filename)
        download_url = f"{self.url}/system/backup/download/{new_filename}"

        # The filename appears in the listing the moment Bazarr creates the
        # file, before it's done writing a multi-MB db backup - downloading
        # right then silently returns a truncated, unreadable zip (no
        # error: the response's Content-Length just matches whatever was on
        # disk at that instant). Small backups write fast enough this never
        # shows up; a real production-sized one hits it reliably. Keep
        # re-downloading until the zip actually validates.
        while True:
            res = self.session.get(download_url, auth=self.download_auth, timeout=self.timeout)
            if res.status_code == 401:
                raise BazarrError(
                    "bazarr: download unauthorized - if Settings > General > Security is set to "
                    "'Forms', switch it to 'None' or 'Basic' (and set BAZARR_USERNAME/BAZARR_PASSWORD) "
                    "for automated backups to work"
                )
            res.raise_for_status()

            with open(dest, "wb") as f:
                f.write(res.content)

            try:
                zipfile.ZipFile(dest).testzip()
                break
            except (zipfile.BadZipFile, OSError):
                if time.time() >= deadline:
                    raise BazarrError(f"bazarr: {new_filename} never finished writing on the server in time")
                logger.info("bazarr: backup file still being written, retrying download...")
                time.sleep(poll_interval)

        try:
            self.delete_backup(new_filename)
        except requests.RequestException:
            logger.warning("bazarr: failed to delete server-side backup copy %s", new_filename)

        return dest

    def restore_from_file(self, local_zip_path, bazarr_backup_dir):
        """Place `local_zip_path` into Bazarr's own backup folder (as seen on
        the local filesystem, e.g. a bind-mounted /config/backup) and trigger
        the restore. Bazarr restarts itself once the restore completes.

        Bazarr's restore endpoint only accepts filenames matching its own
        naming convention (utilities/backup.py's _BACKUP_FILENAME_RE:
        ^bazarr_backup_v[\\w.\\-]+\\.zip$) - anything else gets refused with
        a 500 ("Invalid backup filename refused for restore" in Bazarr's
        log), which is what our own bazarr_<timestamp>.zip storage name
        hits. So the copy is written under a name that fits the pattern
        rather than reusing the source filename as-is.
        """
        stem = os.path.splitext(os.path.basename(local_zip_path))[0]
        filename = f"bazarr_backup_v{stem}.zip"
        target = os.path.join(bazarr_backup_dir, filename)
        with open(local_zip_path, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())

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
        res.raise_for_status()
        return res
