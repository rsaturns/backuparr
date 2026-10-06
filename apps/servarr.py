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

ALLOW_CROSS_HOST_ENV = "BACKUPARR_ALLOW_CROSS_HOST_REDIRECTS"

_BACKUP_ACCESS_HINT = (
    "Use the app's internal URL if it already permits local access, or allow "
    "Backuparr through the reverse proxy; external login pages cannot be "
    "authenticated with an API key."
)


class ServarrError(RuntimeError):
    pass


class UnsafeRedirectError(requests.RequestException):
    """A redirect would send the service's API key to a different host."""


def _redirect_ok(source, target):
    """Whether a redirect to ``target`` can safely carry the API key.

    Requests keeps a custom X-Api-Key header across redirects, so only the same
    host qualifies (an http->https upgrade or port change is fine), never a
    downgrade to http. The target may carry the configured URL's own
    credentials (http://user:pass@host works) but never new ones.
    """
    try:
        src, dst = urlsplit(source), urlsplit(target)
        _ = dst.port  # raises ValueError on a malformed port
        return (
            src.hostname is not None and src.hostname == dst.hostname
            and (dst.username, dst.password) in ((None, None), (src.username, src.password))
            and (dst.scheme == src.scheme or (src.scheme, dst.scheme) == ("http", "https"))
        )
    except ValueError:
        return False


def _cross_host_allowed():
    return os.environ.get(ALLOW_CROSS_HOST_ENV, "").lower() in ("1", "true", "yes")


def _same_origin(source, target):
    try:
        src, dst = urlsplit(source), urlsplit(target)
        default = {"http": 80, "https": 443}
        return (_redirect_ok(source, target) and src.scheme == dst.scheme
                and (src.port or default[src.scheme]) == (dst.port or default[dst.scheme]))
    except (ValueError, KeyError):
        return False


class _ServarrSession(requests.Session):
    def __init__(self, service_url, strict=False):
        super().__init__()
        self._service_url = requests.Request("GET", service_url).prepare().url
        self._strict = strict
        self._warned = False

    def redirect_allowed(self, target):
        if self._strict:
            return _same_origin(self._service_url, target)
        if _redirect_ok(self._service_url, target):
            return True
        if _cross_host_allowed():
            if not self._warned:
                self._warned = True
                logger.warning("following a redirect to a different host because %s is set", ALLOW_CROSS_HOST_ENV)
            return True
        return False

    def send(self, request, **kwargs):
        # Runs on every hop, so redirected POSTs and uploads are refused too.
        if not self.redirect_allowed(request.url):
            raise UnsafeRedirectError(
                "the request was redirected to a different host and not sent, to avoid "
                "exposing the API key. Update the app's URL to its final address, or set "
                f"{ALLOW_CROSS_HOST_ENV}=true to follow cross-host redirects."
            )
        return super().send(request, **kwargs)


class ServarrApp:
    api_version = "v3"

    def __init__(self, name, url, api_key, timeout=30, strict_redirects=False):
        self.name = name
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = _ServarrSession(self.url, strict=strict_redirects)
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
        """Run a Backup command and wait for it; returns the finished command."""
        res = self.session.post(self._api("/command"), json={"name": "Backup"},
                                timeout=self.timeout, headers=request_headers)
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

    def _next_download_url(self, response, current, base):
        """The same-host /backup/ URL a redirect points to, or a ServarrError."""
        status = response.status_code
        location = response.headers.get("Location")
        target = urljoin(current, location) if location else ""
        parts = urlsplit(target)
        if target and not parts.fragment and self.session.redirect_allowed(target):
            path = parts.path.rstrip("/").lower()
            if path == base.path.rstrip("/").lower() + "/login":
                raise ServarrError(
                    f"{self.name}: backup download redirects to the app's web login (HTTP {status}). "
                    f"Its API key cannot authenticate /backup/. {_BACKUP_ACCESS_HINT}")
            if parts.hostname != base.hostname or parts.path.startswith(base.path.rstrip("/") + "/backup/"):
                return target
        raise ServarrError(
            f"{self.name}: backup download was redirected (HTTP {status}) "
            f"outside the app's /backup/ route. {_BACKUP_ACCESS_HINT}")

    def _save_zip(self, response, dest, max_bytes):
        """Stream ``response`` to ``dest`` via a private temp file, only if it is a real ZIP."""
        fd, staged = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(dest)), prefix=".backup-")
        try:
            size = 0
            with os.fdopen(fd, "wb") as out:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ServarrError(f"{self.name}: backup is too large to download")
                    out.write(chunk)
            if not zipfile.is_zipfile(staged):
                raise ServarrError(
                    f"{self.name}: backup download did not return a valid ZIP archive. "
                    "Check authentication and reverse proxy access to /backup/.")
            os.replace(staged, dest)
        finally:
            if os.path.exists(staged):
                os.remove(staged)

    def download_backup(self, path, dest, max_bytes=None):
        """Download a backup zip with the API key, following only safe redirects."""
        if not isinstance(path, str) or not path.startswith("/backup/"):
            raise ServarrError(f"{self.name}: invalid backup path")
        base = urlsplit(self.url)
        url = self.url + path
        try:
            for _ in range(6):
                with self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = self._next_download_url(response, url, base)
                        continue
                    if response.status_code != 200:
                        raise ServarrError(
                            f"{self.name}: could not download the backup (HTTP {response.status_code}). "
                            f"Check the API key and access to /backup/. {_BACKUP_ACCESS_HINT}")
                    self._save_zip(response, dest, max_bytes)
                    return dest
            raise ServarrError(
                f"{self.name}: too many backup redirects. Check authentication and reverse proxy access to /backup/.")
        except UnsafeRedirectError:
            raise
        except requests.RequestException:
            raise ServarrError(
                f"{self.name}: could not connect while downloading the backup. "
                "Check the app URL and reverse proxy access to /backup/.") from None

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
