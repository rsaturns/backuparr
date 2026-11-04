"""Backup driver for Profilarr's own backup API - backup only, no restore.

Profilarr manages Radarr/Sonarr quality profiles and custom formats. Its REST
API (/api/v1/openapi.json): POST /backups starts a `backup.create` job
(polled at /jobs/{id}), GET /backups lists them newest first, and GET/DELETE
/backups/{filename} downloads or removes one.

A download is sanitized by design: arr URLs and API keys, sync configs,
history, notification webhooks, users and sessions, and linked-database, AI
and TMDB keys are stripped, so a restored Profilarr needs those re-added.

There's no restore() because Profilarr has no restore API: its restore is a
SvelteKit form action behind a browser session, which only stages the file
for the next container restart. POST /backups/upload just stores a file.
docs/apps/profilarr.md has the manual steps.
"""
import logging
import os
import tarfile
import zipfile

import requests

from apps.base import AppError, poll_until, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")


class ProfilarrError(AppError):
    pass


class ProfilarrApp:
    def __init__(self, url, api_key, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    def _api(self, path):
        return f"{self.url}/api/v1{path}"

    def test_connection(self):
        res = self.session.get(self._api("/status"), timeout=10)
        if res.status_code == 401:
            raise ProfilarrError("profilarr: unauthorized - check the API key")
        res.raise_for_status()
        info = res.json()
        return f"profilarr {info.get('version', '?')} reachable"

    def app_version(self):
        res = self.session.get(self._api("/status"), timeout=10)
        res.raise_for_status()
        return res.json().get("version")

    def trigger_backup(self, poll_interval=2, timeout_s=300):
        res = self.session.post(self._api("/backups"), timeout=self.timeout)
        res.raise_for_status()
        job_id = res.json()["jobId"]
        logger.info("profilarr: backup job %s started, waiting for it to finish...", job_id)

        def finished():
            res = self.session.get(self._api(f"/jobs/{job_id}"), timeout=self.timeout)
            res.raise_for_status()
            status = res.json().get("status")
            if status in ("failed", "cancelled"):
                raise ProfilarrError(f"profilarr: backup job {status}")
            return True if status == "success" else None

        if not poll_until(finished, timeout_s, poll_interval,
                          lambda waited: logger.info("profilarr: still waiting on the backup job (%ds)...", waited)):
            raise ProfilarrError(f"profilarr: backup job timed out after {timeout_s}s")

    def list_backups(self):
        res = self.session.get(self._api("/backups"), timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def download_latest(self, dest_dir):
        backups = self.list_backups()
        if not backups:
            raise ProfilarrError("profilarr: no backup found after trigger")
        filename = os.path.basename(backups[0]["filename"])  # API returns newest first

        dest = os.path.join(dest_dir, filename)
        with self.session.get(self._api(f"/backups/{filename}"), timeout=self.timeout, stream=True) as res:
            res.raise_for_status()
            save_stream(res, dest)
        if not (tarfile.is_tarfile(dest) or zipfile.is_zipfile(dest)):
            raise ProfilarrError(f"profilarr: {filename} didn't download as a backup archive - check the URL and API key")
        return dest, filename

    def delete_backup(self, filename):
        res = self.session.delete(self._api(f"/backups/{filename}"), timeout=self.timeout)
        res.raise_for_status()

    def backup(self, dest_dir):
        self.trigger_backup()
        path, filename = self.download_latest(dest_dir)
        try:
            self.delete_backup(filename)
        except requests.RequestException:
            logger.warning("profilarr: failed to delete server-side backup %s", filename)
        return path
