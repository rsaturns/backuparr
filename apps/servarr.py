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
import tempfile
import time
from urllib.parse import urljoin, urlsplit
import zipfile

import requests

logger = logging.getLogger(f"backuparr.{__name__}")


class ServarrError(RuntimeError):
    pass


class UnsafeRedirectError(requests.RequestException):
    """A redirect would send the service's credentials to another origin."""


def _same_origin(source, target):
    try:
        source, target = urlsplit(source), urlsplit(target)

        def origin(url):
            port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
            return url.scheme, url.hostname, port

        return (origin(source) == origin(target) and target.username is None
                and target.password is None)
    except ValueError:
        return False


class _ServarrSession(requests.Session):
    def __init__(self, service_url):
        super().__init__()
        self._service_url = requests.Request("GET", service_url).prepare().url

    def send(self, request, **kwargs):
        # Requests preserves custom X-Api-Key headers on redirects, even when
        # it strips Authorization. Reject the new destination before sending
        # any request there, including redirected POSTs and restore uploads.
        if not _same_origin(self._service_url, request.url):
            raise UnsafeRedirectError(
                "API request redirected outside the configured service. "
                "Use its final URL or an internal URL that bypasses the login proxy."
            )
        return super().send(request, **kwargs)


class ServarrApp:
    api_version = "v3"

    def __init__(self, name, url, api_key, timeout=30):
        self.name = name
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = _ServarrSession(self.url)
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

    def trigger_backup(self, poll_interval=2, timeout_s=300, request_headers=None):
        res = self.session.post(self._api("/command"), json={"name": "Backup"}, timeout=self.timeout, headers=request_headers)
        res.raise_for_status()
        command_id = res.json()["id"]

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            res = self.session.get(self._api(f"/command/{command_id}"), timeout=self.timeout)
            res.raise_for_status()
            command = res.json()
            status = command.get("status")
            if status == "completed":
                return command
            if status in ("failed", "aborted"):
                raise ServarrError(f"{self.name}: backup command {status}")
            time.sleep(poll_interval)
        raise ServarrError(f"{self.name}: backup command timed out after {timeout_s}s")

    def list_backups(self):
        res = self.session.get(self._api("/system/backup"), timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def _local_download_url(self, url):
        """Only send session credentials to the configured app's origin."""
        return _same_origin(self.url, url) and not urlsplit(url).fragment

    def download_backup(self, path, dest, max_bytes=None):
        """Download a real ZIP using the API key and local file redirects.

        Do not attempt web login or send the API key to another origin.
        Stage privately so failures never leave HTML or a partial backup.
        """
        if not isinstance(path, str) or not path.startswith("/backup/"):
            raise ServarrError(f"{self.name}: invalid backup path")
        download_url = self.url + path
        backup_prefix = urlsplit(self.url).path.rstrip("/") + "/backup/"
        login_path = urlsplit(self.url).path.rstrip("/") + "/login"
        staged = None
        try:
            for _ in range(6):
                with self.session.get(download_url, timeout=self.timeout, stream=True,
                                      allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        target = urljoin(download_url, location) if location else ""
                        if target and self._local_download_url(target):
                            target_path = urlsplit(target).path
                            if target_path.rstrip("/").lower() == login_path.lower():
                                raise ServarrError(f"{self.name}: backup download redirects to the app's web login (HTTP {response.status_code}). Its API key cannot authenticate /backup/. Use an internal URL if the app already permits local access; an external login proxy must allow Backuparr to access /backup/.")
                            if target_path.startswith(backup_prefix):
                                download_url = target
                                continue
                        raise ServarrError(f"{self.name}: backup download was redirected (HTTP {response.status_code}) outside the app's /backup/ route. Use its internal URL or allow Backuparr through the reverse proxy; external login pages cannot be authenticated with an API key.")
                    if response.status_code != 200:
                        raise ServarrError(f"{self.name}: could not download the backup (HTTP {response.status_code}). Check the API key and access to /backup/; use an internal URL if the app already permits local access, or allow Backuparr through the reverse proxy.")
                    fd, staged = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(dest)), prefix=".backup-")
                    size = 0
                    with os.fdopen(fd, "wb") as target:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            size += len(chunk)
                            if max_bytes is not None and size > max_bytes:
                                raise ServarrError(f"{self.name}: backup is too large to download")
                            target.write(chunk)
                    if not zipfile.is_zipfile(staged):
                        raise ServarrError(f"{self.name}: backup download did not return a valid ZIP archive. Check authentication and reverse proxy access to /backup/.")
                    os.replace(staged, dest)
                    staged = None
                    return dest
            raise ServarrError(f"{self.name}: too many backup redirects. Check authentication and reverse proxy access to /backup/.")
        except requests.RequestException:
            raise ServarrError(f"{self.name}: could not connect while downloading the backup. Check the app URL and reverse proxy access to /backup/.") from None
        finally:
            if staged is not None:
                os.remove(staged)

    def download_latest_manual(self, dest_dir):
        manual = [b for b in self.list_backups() if b.get("type") == "manual"]
        if not manual:
            raise ServarrError(f"{self.name}: no manual backup found after trigger")
        manual.sort(key=lambda b: b.get("time", ""), reverse=True)
        latest = manual[0]

        filename = os.path.basename(latest["path"])
        dest = os.path.join(dest_dir, filename)
        self.download_backup(latest["path"], dest)
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
        is required at all.

        The upload only stages the db/config for restore and responds
        {"RestartRequired": true} - it does nothing further on its own. The
        app's startup routine is what actually swaps the staged files in, so
        a restart has to be triggered here or the "restore" silently never
        applies.
        """
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "application/zip")}
            res = self.session.post(self._api("/system/backup/restore/upload"), files=files, timeout=120)
        res.raise_for_status()
        result = res.json()
        self.restart()
        return result

    def restart(self):
        try:
            res = self.session.post(self._api("/system/restart"), timeout=10)
            res.raise_for_status()
        except requests.exceptions.ConnectionError:
            # The app tears its connections down as it restarts - seeing
            # that happen right after a 200 here is the expected outcome,
            # not a failure.
            logger.info("%s: restart request sent (connection dropped as expected)", self.name)
