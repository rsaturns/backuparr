"""Shared backup/restore driver for Radarr, Sonarr, and Prowlarr.

All three share the same .NET backend (the "Servarr" family), so they expose
identical system/backup endpoints - only the API version prefix differs
(Prowlarr is still on v1). This drives the app's own official backup
mechanism end to end via HTTP: trigger a backup command, wait for it to
finish, download the resulting zip, delete it from the server, and (for
restore) upload a zip straight back in. No filesystem/volume access at all.
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)


class ServarrError(RuntimeError):
    pass


class ServarrApp:
    api_version = "v3"

    def __init__(self, name, url, api_key, timeout=30):
        self.name = name
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    def _api(self, path):
        return f"{self.url}/api/{self.api_version}{path}"

    def test_connection(self):
        res = self.session.get(self._api("/system/status"), timeout=10)
        if res.status_code == 401:
            raise ServarrError(f"{self.name}: unauthorized - check the API key")
        res.raise_for_status()
        info = res.json()
        return f"{self.name} {info.get('version', '?')} reachable"

    def trigger_backup(self, poll_interval=2, timeout_s=300):
        res = self.session.post(self._api("/command"), json={"name": "Backup"}, timeout=self.timeout)
        res.raise_for_status()
        command_id = res.json()["id"]

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            res = self.session.get(self._api(f"/command/{command_id}"), timeout=self.timeout)
            res.raise_for_status()
            status = res.json().get("status")
            if status == "completed":
                return
            if status in ("failed", "aborted"):
                raise ServarrError(f"{self.name}: backup command {status}")
            time.sleep(poll_interval)
        raise ServarrError(f"{self.name}: backup command timed out after {timeout_s}s")

    def list_backups(self):
        res = self.session.get(self._api("/system/backup"), timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def download_latest_manual(self, dest_dir):
        manual = [b for b in self.list_backups() if b.get("type") == "manual"]
        if not manual:
            raise ServarrError(f"{self.name}: no manual backup found after trigger")
        manual.sort(key=lambda b: b.get("time", ""), reverse=True)
        latest = manual[0]

        download_url = f"{self.url}{latest['path']}"
        res = self.session.get(download_url, timeout=self.timeout)
        res.raise_for_status()

        filename = os.path.basename(latest["path"])
        dest = os.path.join(dest_dir, filename)
        with open(dest, "wb") as f:
            f.write(res.content)
        return dest, latest.get("id")

    def delete_backup(self, backup_id):
        res = self.session.delete(self._api(f"/system/backup/{backup_id}"), timeout=self.timeout)
        res.raise_for_status()

    def backup(self, dest_dir):
        self.trigger_backup()
        path, backup_id = self.download_latest_manual(dest_dir)
        if backup_id is not None:
            try:
                self.delete_backup(backup_id)
            except requests.RequestException:
                logger.warning("%s: failed to delete server-side backup id %s", self.name, backup_id)
        return path

    def restore_upload(self, file_path):
        """Restore via POST /system/backup/restore/upload - a genuine multipart
        upload endpoint, so no filesystem access to the app's config volume
        is required at all."""
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "application/zip")}
            res = self.session.post(self._api("/system/backup/restore/upload"), files=files, timeout=120)
        res.raise_for_status()
        return res.json()
