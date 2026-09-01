"""Backup driver for Profilarr's own backup API - backup only, no restore.

Profilarr (https://github.com/Dictionarry-Hub/profilarr) is a config
management layer in front of Radarr/Sonarr (quality profiles, custom
formats), not a media manager itself. REST API at /api/v1/openapi.json;
the parts used here:

- POST /backups triggers an async `backup.create` job and returns a
  jobId - trigger-then-poll, like the Servarr apps' /command endpoint.
- GET /backups lists existing backups, newest first.
- GET /backups/{filename} downloads one. Per Profilarr's own docs, this
  is DELIBERATELY SANITIZED: arr instance URLs/API keys, sync configs,
  drift/rename history, notification webhook URLs and tokens, user
  accounts and sessions, linked-database access tokens, and AI/TMDB API
  keys are all stripped before the file reaches the client - a Profilarr
  design decision, not a Backuparr limitation. A restored instance needs
  those re-added by hand regardless of how the backup got there.
- DELETE /backups/{filename} removes a backup, same as with
  Radarr/Sonarr/Prowlarr/Bazarr, to keep Profilarr's own list clean.

There is deliberately no restore() here. Profilarr's restore path is a
SvelteKit form action gated by browser session cookie, not part of the
/api/v1 REST API, and even then only stages a pending restore in a
sentinel file - the actual swap happens at the next Profilarr container
restart, which Backuparr can't trigger. POST /backups/upload exists but
only stores a file; it doesn't stage or apply anything. See the README's
Profilarr note for the manual restore steps this leaves you with.
"""
import logging
import os
import time

import requests

logger = logging.getLogger(f"backuparr.{__name__}")


class ProfilarrError(RuntimeError):
    pass


class ProfilarrApp:
    def __init__(self, url, api_key, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
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

    def trigger_backup(self, poll_interval=2, timeout_s=300):
        res = self.session.post(self._api("/backups"), timeout=self.timeout)
        res.raise_for_status()
        job_id = res.json()["jobId"]
        logger.info("profilarr: backup job %s started, waiting for it to finish...", job_id)

        deadline = time.time() + timeout_s
        waited = 0
        while time.time() < deadline:
            res = self.session.get(self._api(f"/jobs/{job_id}"), timeout=self.timeout)
            res.raise_for_status()
            status = res.json().get("status")
            if status == "success":
                return
            if status in ("failed", "cancelled"):
                raise ProfilarrError(f"profilarr: backup job {status}")
            time.sleep(poll_interval)
            waited += poll_interval
            if waited % 10 == 0:
                logger.info("profilarr: still waiting on the backup job (%ds)...", waited)
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

        res = self.session.get(self._api(f"/backups/{filename}"), timeout=self.timeout)
        res.raise_for_status()

        dest = os.path.join(dest_dir, filename)
        with open(dest, "wb") as f:
            f.write(res.content)
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
