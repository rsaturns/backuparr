"""Backup/restore driver for Pi-hole v6, over its API's Teleporter.

Pi-hole v6's FTL serves the API at <url>/api (src/api/api.c). Every call but
the login needs a session: POST /api/auth {"password": ...} returns
session.sid, sent back in the X-FTL-SID header, and DELETE /api/auth ends
it. FTL has only webserver.api.max_sessions (16) of them, so a backup always
logs out - left open, they'd lock the web page out until they expire
(webserver.session.timeout, 30 minutes).

The card's password is an app password (Settings > Web interface / API >
Configure app password, Expert mode): unlike the web password it never
asks for a 2FA code. An app session can only use Teleporter while
webserver.api.app_sudo is on (src/api/teleporter.c), as the archive holds
the password hashes and the 2FA secret. Test connection reads that setting
and says where to turn it on.

BACKUP: GET /api/teleporter returns a zip of etc/pihole/pihole.toml (every
setting, local DNS records included), gravity.db's group, adlist,
domainlist and client tables, dhcp.leases, /etc/hosts and a reduced
pihole-FTL.db.

RESTORE: POST /api/teleporter with the zip as the multipart "file" field
(the filename must end in .zip) imports pihole.toml, dhcp.leases and the
gravity tables - /etc/hosts and pihole-FTL.db are skipped - then FTL
restarts itself, within a second or two. pihole.toml brings back the
backup's web password, app password, 2FA secret and app_sudo, except for
settings forced by an environment variable (FTLCONF_*), which stay as they
are. So after the restart the driver signs in again, to say whether the
card's app password and app_sudo still work.

Logins are kept at least a second apart: FTL allows 3 password checks per
second (MAX_PASSWORD_ATTEMPTS_PER_SECOND), an app password takes two (the
web password is tried first), and a rate-limited app password is reported
as "password incorrect" (verify_login in src/config/password.c) - so a
restore's login straight after its version check was refused.

The version recorded for a backup is FTL's, the part that writes and reads
Teleporter archives ("FTL v6.8", as Pi-hole's web page footer shows it).

Pi-hole v5 has no /api (its API is /admin/api.php, without Teleporter);
Test connection says v6 is needed.
"""
import logging
import os
import threading
import time
import zipfile
from contextlib import contextmanager

import requests

from apps.base import AppError, poll_until, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

_APP_SESSION = "app-password correct"  # session.message for an app password (src/api/auth.c)
_APP_PASSWORD_WHERE = "Settings > Web interface / API > Configure app password, in Expert mode"
_APP_SUDO_WHERE = "Settings > All settings > Webserver and API > webserver.api.app_sudo, in Expert mode"
_RESTART_DOWN_S = 10  # FTL stops within ~0.5 s of answering the import
_RESTART_UP_S = 120
_LOGIN_GAP_S = 1.1  # more than a second, so two logins never share one of FTL's rate-limit seconds
_last_login = {}  # Pi-hole URL -> time.monotonic() of Backuparr's last login there
_login_lock = threading.Lock()


class PiholeError(AppError):
    pass


class PiholeApp:
    def __init__(self, url, api_key, timeout=60):
        # The address people copy from their browser ends in /admin.
        self.url = url.rstrip("/").removesuffix("/admin")
        self.password = api_key
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update({"Accept": "application/json"})
        self._version = None

    def _api(self, path):
        return f"{self.url}/api{path}"

    # ------------------------------------------------------------ session --
    def _not_pihole(self):
        """The error for an address that has no v6 API: Pi-hole v5 says so."""
        try:
            res = self.session.get(f"{self.url}/admin/api.php", params={"version": ""}, timeout=10)
            v5 = res.ok and isinstance(res.json(), dict) and "version" in res.json()
        except (requests.RequestException, ValueError):
            v5 = False
        if v5:
            return PiholeError("pihole: this is Pi-hole v5 - Pi-hole v6 or newer is needed")
        return PiholeError("pihole: no Pi-hole v6 API at this address - check the URL (e.g. http://pihole, without /admin)")

    @staticmethod
    def _error_text(res):
        try:
            error = res.json().get("error") or {}
        except (ValueError, AttributeError):
            return None
        return error.get("key"), " - ".join(part for part in (error.get("message"), error.get("hint")) if part)

    def _wait_for_login_slot(self):
        with _login_lock:
            wait = _last_login.get(self.url, float("-inf")) + _LOGIN_GAP_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_login[self.url] = time.monotonic()

    def _login(self):
        """Signs in; returns whether it's an app-password session."""
        self._wait_for_login_slot()
        res = self.session.post(self._api("/auth"), json={"password": self.password}, timeout=self.timeout)
        if res.status_code == 404:
            raise self._not_pihole()
        try:
            session = res.json().get("session")
        except (ValueError, AttributeError):
            session = None
        key, text = self._error_text(res) or (None, None)
        if res.status_code == 429:
            if key == "api_seats_exceeded":
                raise PiholeError(
                    "pihole: Pi-hole has no free API sessions (webserver.api.max_sessions) - log some out under "
                    "Settings > Web interface / API, or wait for them to expire")
            raise PiholeError("pihole: Pi-hole is limiting login attempts - wait a minute and try again")
        if res.status_code == 400 and text and "2FA" in text:
            raise PiholeError(f"pihole: this password needs a 2FA code - use an app password ({_APP_PASSWORD_WHERE})")
        if not isinstance(session, dict):
            if res.status_code >= 400 and key:
                raise PiholeError(f"pihole: login failed ({text})")
            raise self._not_pihole()
        if res.status_code == 401 or not session.get("valid"):
            raise PiholeError(f"pihole: the app password was refused - check it ({_APP_PASSWORD_WHERE})")
        res.raise_for_status()
        self.session.headers["X-FTL-SID"] = session["sid"]
        return session.get("message") == _APP_SESSION

    def _logout(self, restarting=False):
        """restarting: FTL is restarting after an import, which may drop the
        connection or the session first - no warning then."""
        if "X-FTL-SID" not in self.session.headers:
            return
        try:
            res = self.session.delete(self._api("/auth"), timeout=self.timeout)
            # 401: the session already ended (an import or a password change ends them all).
            if res.status_code not in (204, 401) and not restarting:
                logger.warning("pihole: couldn't log out its API session (HTTP %s) - it expires by itself", res.status_code)
        except requests.RequestException:
            if not restarting:
                logger.warning("pihole: couldn't log out its API session - it expires by itself")
        finally:
            self.session.headers.pop("X-FTL-SID", None)

    @contextmanager
    def _signed_in(self):
        app_session = self._login()
        try:
            yield app_session
        finally:
            self._logout()

    def _get(self, path):
        res = self.session.get(self._api(path), timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def _versions(self):
        version = self._get("/info/version").get("version") or {}
        core = ((version.get("core") or {}).get("local") or {}).get("version")
        ftl = ((version.get("ftl") or {}).get("local") or {}).get("version")
        return core, ftl

    def _app_sudo(self):
        api = ((self._get("/config/webserver/api/app_sudo").get("config") or {}).get("webserver") or {}).get("api") or {}
        return api.get("app_sudo") is True

    def _check_sudo(self, app_session):
        if app_session and not self._app_sudo():
            raise PiholeError(f"pihole: Pi-hole doesn't let app passwords make backups yet - turn on {_APP_SUDO_WHERE}")

    # -------------------------------------------------------------- backup --
    def test_connection(self):
        with self._signed_in() as app_session:
            core, ftl = self._versions()
            self._check_sudo(app_session)
        return f"pihole {core or '?'} (FTL {ftl or '?'}) reachable, backups allowed"

    def app_version(self):
        """FTL's version, e.g. "FTL v6.8". A backup reads it in its own
        session, so it doesn't sign in a second time."""
        if self._version is None:
            with self._signed_in():
                self._version = self._versions()[1]
        return f"FTL {self._version}" if self._version else None

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "teleporter.zip")
        with self._signed_in() as app_session:
            self._version = self._versions()[1]
            with self.session.get(self._api("/teleporter"), timeout=self.timeout, stream=True) as res:
                if res.status_code == 403:
                    self._check_sudo(app_session)
                    raise PiholeError(f"pihole: Pi-hole refused the export ({(self._error_text(res) or (None, 'forbidden'))[1]})")
                res.raise_for_status()
                save_stream(res, dest)
        if not zipfile.is_zipfile(dest):
            raise PiholeError("pihole: the export didn't download as a Teleporter archive")
        logger.info("pihole: exported its Teleporter archive (%d bytes)", os.path.getsize(dest))
        return dest

    # ------------------------------------------------------------- restore --
    def _answers(self):
        try:
            self.session.get(self._api("/auth"), timeout=2)
            return True
        except requests.RequestException:
            return False

    def _wait_for_restart(self):
        """FTL answers the import, then restarts. Waits to see it go, then
        come back; True once it answers again."""
        poll_until(lambda: True if not self._answers() else None, _RESTART_DOWN_S, 0.2)
        back = poll_until(lambda: True if self._answers() else None, _RESTART_UP_S, 1,
                          lambda waited: logger.info("pihole: waiting for Pi-hole to restart (%ds)...", waited))
        if back:
            time.sleep(1)  # the web server comes up a moment before the API is ready
        return bool(back)

    def restore_from_file(self, local_zip):
        """Imports a Teleporter archive. Returns {"back": Pi-hole answered
        after its restart, "password_ok": the card's app password still
        signs in, "sudo_ok": app_sudo still allows backups}."""
        if not zipfile.is_zipfile(local_zip):
            raise PiholeError("pihole: this backup isn't a Teleporter archive")
        self._login()
        imported = False
        try:
            with open(local_zip, "rb") as f:
                res = self.session.post(self._api("/teleporter"), timeout=self.timeout,
                                        files={"file": ("backuparr-teleporter.zip", f, "application/zip")})
            if res.status_code >= 400:
                text = (self._error_text(res) or (None, None))[1]
                if res.status_code == 403:
                    raise PiholeError(f"pihole: Pi-hole refused the import - turn on {_APP_SUDO_WHERE}")
                raise PiholeError(f"pihole: Pi-hole refused the backup ({text or f'HTTP {res.status_code}'})")
            imported = True
            try:
                imported = res.json().get("files") or []
            except (ValueError, AttributeError):
                imported = []
        finally:
            self._logout(restarting=imported is not False)
        logger.info("pihole: imported %s", ", ".join(imported) or "the archive")
        result = {"back": self._wait_for_restart(), "password_ok": None, "sudo_ok": None}
        if not result["back"]:
            return result
        try:
            with self._signed_in() as app_session:
                result["password_ok"] = True
                result["sudo_ok"] = not app_session or self._app_sudo()
        except PiholeError as exc:
            logger.warning("%s", exc)
            result["password_ok"] = False
        return result
