"""Shared backup/restore driver for Radarr, Sonarr, Lidarr, Whisparr and
Prowlarr.

All five share the same .NET backend (the "Servarr" family), so they expose
identical system/backup endpoints - only the API version prefix differs
(Lidarr and Prowlarr are on v1, the rest on v3). This drives the app's own
official backup mechanism end to end via HTTP: trigger a backup command,
wait for it to finish, download the resulting zip, delete it from the server,
and (for restore) upload a zip straight back in. No filesystem/volume access
at all.
"""
import logging
import os
import tempfile
from urllib.parse import urljoin, urlsplit
import zipfile

import requests

from apps.base import AppError, poll_until

logger = logging.getLogger(f"backuparr.{__name__}")

ALLOW_CROSS_HOST_ENV = "BACKUPARR_ALLOW_CROSS_HOST_REDIRECTS"

_BACKUP_ACCESS_HINT = (
    "Use the app's internal URL if it already permits local access, or allow "
    "Backuparr through the reverse proxy; external login pages cannot be "
    "authenticated with an API key."
)


# Backup files are served by the app's web UI, not its API: they take the
# app's own login (Forms or Basic) and never the API key. With Authentication
# Required set to Disabled for Local Addresses, Backuparr's private address
# gets through without one; with Enabled, Backuparr signs in with the
# username and password from the app's card (checked against Sonarr 4.0,
# Radarr 6.4, Lidarr 3.1, Prowlarr 2.7 and Whisparr 2.2/3.6: Forms is a POST
# of username/password to /login answered by a redirect, ?loginFailed=true
# on a wrong password; Basic is the usual Authorization header. Whisparr v3
# has no Basic). The login method comes from the API's config/host.
_ASKS_FOR_LOGIN = "the app asks for a login to download backups. Enter its username and password in Settings."
_WRONG_LOGIN = "wrong username or password. Check them in Settings."
_LOGIN_METHODS = ("forms", "basic")


class ServarrError(AppError):
    pass


class LoginRequiredError(ServarrError):
    """The web UI wants a login Backuparr doesn't have, or that was refused.
    Test connection then shows the card's username and password fields."""
    needs_login = True


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


class SameHostSession(requests.Session):
    """A session that won't carry its API key, or a request body, to another
    host: requests drops Authorization on a cross-host redirect but keeps
    custom headers such as X-Api-Key, and resends the body on a 307/308."""

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
    api_version = "v3"  # Lidarr and Prowlarr override it with v1

    def __init__(self, name, url, api_key, timeout=30, strict_redirects=False, username=None, password=None):
        self.name = name
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = SameHostSession(self.url, strict=strict_redirects)
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})
        # Only for the web UI (backup downloads), and only once it asks.
        self.username = username or None
        self.password = password or ""
        self._web_auth = None  # (username, password) once Basic is in use
        self.signed_in = False

    def _api(self, path):
        return f"{self.url}/api/{self.api_version}{path}"

    def _status(self):
        res = self.session.get(self._api("/system/status"), timeout=10)
        if res.status_code == 401:
            raise ServarrError(f"{self.name}: unauthorized - check the API key")
        res.raise_for_status()
        return res.json()

    def test_connection(self):
        version = self._status().get("version", "?")
        response = self._front_page()
        if response is not None and self._is_login(response):
            if not self.username:
                raise LoginRequiredError(f"{self.name} {version} asks for a login. Enter its username and password.")
            self.sign_in(response)
            response = self._front_page()
            if response is not None and self._is_login(response):
                raise self._login_error()
            return f"{self.name} {version} reachable, signed in"
        return f"{self.name} {version} reachable"

    def _is_login(self, response):
        """Whether a response is the app's own login: a Basic challenge, or
        a redirect to its /login page on the same host."""
        if response.status_code == 401:
            return True
        location = response.headers.get("Location")
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            return False
        base, target = urlsplit(self.url), urlsplit(urljoin(response.url, location))
        return (target.hostname == base.hostname
                and target.path.rstrip("/").lower() == base.path.rstrip("/").lower() + "/login")

    def _web_get(self, url, timeout=None):
        """A web UI request (never redirected), with the Basic login once in use."""
        return self.session.get(url, timeout=timeout or self.timeout, stream=True,
                                allow_redirects=False, auth=self._web_auth)

    def _front_page(self):
        """The web UI's front page response, or None if it can't be reached.
        It's behind the same login check as /backup/, and asking for it
        leaves nothing in the app's log."""
        try:
            with self._web_get(self.url + "/", timeout=10) as response:
                return response
        except requests.RequestException:
            return None  # the API answered; a backup reports anything else

    def _login_error(self):
        return LoginRequiredError(f"{self.name}: {_WRONG_LOGIN if self.username else _ASKS_FOR_LOGIN}")

    def _login_method(self, response):
        """forms or basic: what config/host says, else what ``response`` was."""
        try:
            res = self.session.get(self._api("/config/host"), timeout=10)
            res.raise_for_status()
            method = res.json().get("authenticationMethod")
            if isinstance(method, str) and method:
                return method.lower()
        except (requests.RequestException, ValueError, AttributeError):
            pass
        return "basic" if response.status_code == 401 else "forms"

    def _forms_login(self):
        try:
            res = self.session.post(self.url + "/login", data={"username": self.username, "password": self.password},
                                    timeout=self.timeout, allow_redirects=False)
        except UnsafeRedirectError:
            raise
        except requests.RequestException:
            raise ServarrError(f"{self.name}: could not connect to sign in. Check the app URL.") from None
        location = res.headers.get("Location", "")
        if res.status_code not in (301, 302, 303) or "loginfailed" in location.lower():
            raise self._login_error()

    def sign_in(self, response):
        """Signs in to the web UI after ``response`` asked for a login - once:
        a refused login is never retried, as each is an Auth-Failure in the
        app's log, which fail2ban may act on."""
        if self.signed_in or not self.username:
            raise self._login_error()
        method = self._login_method(response)
        if method not in _LOGIN_METHODS:
            raise ServarrError(
                f"{self.name}: its {method} login can't be used by Backuparr. In the app, set "
                "Settings > General > Security > Authentication Required to Disabled for Local Addresses.")
        self.signed_in = True
        if method == "basic":
            self._web_auth = (self.username, self.password)
        else:
            self._forms_login()

    def app_version(self):
        return self._status().get("version")

    def trigger_backup(self, poll_interval=2, timeout_s=300, request_headers=None):
        """Run a Backup command and wait for it; returns the finished command."""
        res = self.session.post(self._api("/command"), json={"name": "Backup"},
                                timeout=self.timeout, headers=request_headers)
        res.raise_for_status()
        command_id = res.json()["id"]

        def finished():
            res = self.session.get(self._api(f"/command/{command_id}"), timeout=self.timeout)
            res.raise_for_status()
            command = res.json()
            status = command.get("status")
            if status in ("failed", "aborted"):
                raise ServarrError(f"{self.name}: backup command {status}")
            return command if status == "completed" else None

        command = poll_until(finished, timeout_s, poll_interval)
        if command is None:
            raise ServarrError(f"{self.name}: backup command timed out after {timeout_s}s")
        return command

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
                with self._web_get(url) as response:
                    if self._is_login(response):
                        self.sign_in(response)  # or the login error
                        url = self.url + path
                        continue
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
        latest = max(manual, key=lambda b: b.get("time", ""))

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
        """Uploads the zip to /system/backup/restore/upload. That only stages
        it ({"RestartRequired": true}): the app swaps the staged files in on
        startup, so it's restarted here, or the restore would never apply."""
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
