"""Backup driver for Profilarr's own backup API - backup only, no restore.

Profilarr (https://github.com/Dictionarry-Hub/profilarr) is a config
management layer in front of Radarr/Sonarr (quality profiles, custom
formats), not a media manager itself. Its REST API is documented as an
OpenAPI spec at /api/v1/openapi.json; the parts used here:

- POST /backups triggers an async `backup.create` job and returns a
  jobId - same "trigger, then poll for completion" shape as the Servarr
  apps' /command endpoint, just with its own job queue instead.
- GET /backups lists existing backups, newest first.
- GET /backups/{filename} downloads one. Per Profilarr's own docs, this
  download is DELIBERATELY SANITIZED: arr instance URLs/API keys, sync
  configs, drift/rename history, notification webhook URLs and tokens,
  user accounts and sessions, personal access tokens for linked
  databases, and AI/TMDB API keys are all stripped before the file
  reaches the client. The local copy on Profilarr's own server is not
  affected - only what leaves over this endpoint. This is a Profilarr
  design decision, not a Backuparr limitation, and it means a restored
  instance will need those re-added by hand regardless of how the backup
  got there.
- DELETE /backups/{filename} removes a backup - used the same way as
  Radarr/Sonarr/Prowlarr/Bazarr, to keep the app's own backup list clean
  of copies Backuparr already pulled.

There is deliberately no restore() here. Profilarr's restore path
(confirmed in its source, src/routes/settings/backups/+page.server.ts) is
a SvelteKit form action gated by browser session cookie - not part of the
versioned /api/v1 REST API at all - and even then it only stages a
pending restore in a sentinel file; the actual swap happens at the next
Profilarr container restart, which Backuparr has no way to trigger.
POST /backups/upload exists in the API, but it only stores a file in
Profilarr's backups folder - it doesn't stage or apply anything, so
calling it wouldn't actually restore. See the README's Profilarr note for
the manual restore steps this leaves you with.
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)


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

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            res = self.session.get(self._api(f"/jobs/{job_id}"), timeout=self.timeout)
            res.raise_for_status()
            status = res.json().get("status")
            if status == "success":
                return
            if status in ("failed", "cancelled"):
                raise ProfilarrError(f"profilarr: backup job {status}")
            time.sleep(poll_interval)
        raise ProfilarrError(f"profilarr: backup job timed out after {timeout_s}s")

    def list_backups(self):
        res = self.session.get(self._api("/backups"), timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def download_latest(self, dest_dir):
        backups = self.list_backups()
        if not backups:
            raise ProfilarrError("profilarr: no backup found after trigger")
        filename = backups[0]["filename"]  # API returns newest first

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
