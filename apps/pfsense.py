"""Backup driver for pfSense's configuration (#153), CE and Plus.

pfSense has no official API. The configuration comes from the web UI's own
Diagnostics > Backup & Restore page, signed in the way Netgate documents for
scripted backups (docs.netgate.com/pfsense/en/latest/backup/remote-backup):

1. GET the login page for its CSRF token (csrf-magic's __csrf_magic),
2. POST login=Login with usernamefld/passwordfld and that token,
3. GET diag_backup.php for a fresh token,
4. POST download=download&donotbackuprrd=yes with it: config.xml (the
   graphs' RRD data left out, as the page's default does).

The user needs "WebCfg - Diagnostics: Backup & Restore" (diag_backup.php).
pfSense has no read-only backup privilege: that page also restores, and
pfSense marks it as root-equivalent, so the login is effectively an admin's
and is kept encrypted. "WebCfg - System: Login / Logout / Dashboard" (the
dashboard) gives the version recorded with each backup, from its System
Information widget. Backuparr signs out after each use.

pfSense counts failed logins and blocks the address after too many, so a
wrong password is reported and never retried. Restores are by hand
(manual_restore, #151). A self-signed web UI certificate is trusted on the
card and pinned (apps/cert_pin.py).
"""
import html
import logging
import os
import re

from apps import cert_pin, config_xml
from apps.base import AppError
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

CONFIG_FILE = "config.xml"
_MAX_CONFIG = 256 * 1024 * 1024
_CSRF = re.compile(r"""name=['"]__csrf_magic['"]\s+value=['"]([^'"]+)['"]""")
_CSRF_JS = re.compile(r"""csrfMagicToken\s*=\s*["']([^"']+)["']""")
# The System Information widget: <th>Version</th> ... <strong>2.7.2-RELEASE</strong>
_VERSION = re.compile(r"<th>\s*Version\s*</th>\s*<td>\s*<strong>\s*([^<\s]+)\s*</strong>", re.IGNORECASE)
PRIVILEGES = '"WebCfg - Diagnostics: Backup & Restore" and "WebCfg - System: Login / Logout / Dashboard"'


class PfsenseError(AppError):
    pass


def check_config(path):
    """Raises PfsenseError unless path is a whole pfSense config.xml."""
    config_xml.check(path, "pfsense", PfsenseError, "pfsense", "a pfSense")


def _token(page):
    match = _CSRF.search(page) or _CSRF_JS.search(page)
    return html.unescape(match.group(1)) if match else None


def _is_login_page(page):
    return 'name="usernamefld"' in page or "name='usernamefld'" in page or 'id="usernamefld"' in page


class PfsenseApp:
    def __init__(self, url, username=None, password=None, trusted_cert=None, timeout=60):
        self.url = url.rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self.session = SameHostSession(self.url, strict=True)
        cert_pin.pin(self.session, "pfsense", trusted_cert)
        self._signed_in = False

    def _request(self, method, path, **kwargs):
        res = self.session.request(method, f"{self.url}{path}", timeout=self.timeout, allow_redirects=False, **kwargs)
        if 300 <= res.status_code < 400:
            target = res.headers.get("Location", "")
            if target.startswith("/") or target.startswith(self.url):
                # pfSense sends a signed-in user to a page they may see.
                return res
            raise PfsenseError("pfsense: the URL redirects - use the firewall's https:// address")
        if res.status_code == 404:
            raise PfsenseError("pfsense: no pfSense web UI at this URL - check it")
        res.raise_for_status()
        return res

    def _sign_in(self):
        if not self.username or not self.password:
            raise PfsenseError("pfsense: enter the username and password of the backup user")
        page = self._request("GET", "/").text
        token = _token(page)
        if not token or not _is_login_page(page):
            raise PfsenseError("pfsense: the URL doesn't show pfSense's login page - check it")
        res = self._request("POST", "/", data={"login": "Login", "usernamefld": self.username,
                                               "passwordfld": self.password, "__csrf_magic": token})
        if res.status_code == 200 and _is_login_page(res.text):
            raise PfsenseError("pfsense: the username or password is wrong (pfSense blocks the address after "
                               "repeated failures, so check them before testing again)")
        self._signed_in = True

    def _sign_out(self):
        if not self._signed_in:
            return
        try:
            page = self._request("GET", "/index.php").text
            token = _token(page)
            if token:
                self._request("POST", "/index.php?logout", data={"logout": "", "__csrf_magic": token})
        except Exception as exc:  # noqa: BLE001 - the session expires on its own
            logger.debug("pfsense: sign-out failed (%s)", type(exc).__name__)
        self._signed_in = False

    def _backup_page(self):
        res = self._request("GET", "/diag_backup.php")
        if res.status_code != 200 or _is_login_page(res.text) or not _token(res.text) or 'name="download"' not in res.text:
            raise PfsenseError(f"pfsense: the user can't open Diagnostics > Backup & Restore - it needs the {PRIVILEGES} privileges")
        return res.text

    def _version(self):
        res = self._request("GET", "/index.php")
        match = _VERSION.search(res.text) if res.status_code == 200 else None
        if not match:
            raise PfsenseError("pfsense: the user can't read the version from the dashboard - give it the "
                               "\"WebCfg - System: Login / Logout / Dashboard\" privilege too, and keep the System "
                               "Information widget on its dashboard")
        return match.group(1)

    def app_version(self):
        self._sign_in()
        try:
            return self._version()
        finally:
            self._sign_out()

    def test_connection(self):
        self._sign_in()
        try:
            self._backup_page()
            version = self._version()
        finally:
            self._sign_out()
        return f"pfsense {version} reachable, signed in as {self.username} with access to Backup & Restore"

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, CONFIG_FILE)
        self._sign_in()
        try:
            token = _token(self._backup_page())
            logger.info("pfsense: downloading the current configuration (without RRD graph data)...")
            res = self._request("POST", "/diag_backup.php", stream=True,
                                data={"download": "download", "donotbackuprrd": "yes", "__csrf_magic": token})
            size = 0
            with open(path, "wb") as f:
                for chunk in res.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > _MAX_CONFIG:
                        raise PfsenseError("pfsense: the download is far too large to be a configuration")
                    f.write(chunk)
        finally:
            self._sign_out()
        check_config(path)
        logger.info("pfsense: downloaded the configuration (%d bytes)", size)
        return path

