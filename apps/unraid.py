"""Backup driver for Unraid's boot device (#152): the web UI's own Boot
Device Backup (Main > Boot Device), signed in as root.

Unraid keeps its whole setup on the boot device: the USB flash drive, or
since 7.3 an internal boot pool (ZFS), both mounted at /boot. /boot is
root-only (vfat fmask=0177/dmask=0077, the same modes on ZFS), its GraphQL
API has no boot backup (initiateFlashBackup isn't implemented, API 4.37) and
its web UI has only the root user - so the card takes the root login.

1. POST /login (username=root, password): a 302 to the start page signs in
   (a PHP session cookie). A 200 is the login page again: a wrong password,
   or Unraid ignoring sign-ins from this address - it allows 3 failures,
   then none for 15 minutes - so a refused sign-in is never retried.
2. GET /plugins/dynamix.my.servers/data/server-state.php: JSON with the
   version (osVersion) and the CSRF token (csrf) every POST needs. It also
   holds an API key, so it's never logged. A redirect here means not signed
   in.
3. The zip, as the page makes it - everything on /boot but the previous OS
   release (prev/, previous/), about 1.2 GB:
   - 7.4 and later: GET /webGui/include/FlashBackup.php streams it.
   - Earlier (a 404 there): POST /webGui/include/Download.php cmd=backup
     makes it on the server and answers its name ("" if there's no room),
     GET /<name> downloads it, and cmd=unlink removes it again.
4. GET /logout.

The zip is kept as Unraid makes it: the USB Flash Creator's "Use custom"
restores it in one step, and a config-only one doesn't boot (tested). It
holds the license, SSH host keys, password hashes and WireGuard keys, so
Backuparr always encrypts it (#147). Restores are by hand (manual_restore,
#151). A self-signed web UI certificate is trusted on the card and pinned
(apps/cert_pin.py).
"""
import logging
import os
import re
import zipfile

from apps import cert_pin
from apps.base import AppError, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

USERNAME = "root"
BACKUP_FILE = "unraid-boot-backup.zip"
_TIMEOUT = 60
# The zip is built while the request waits (7.3) or as it streams (7.4):
# about a minute for 1.2 GB on a fast server, far longer on a slow USB 2
# stick or busy CPU. At most 1 h, as for every app.
_BACKUP_TIMEOUT = 3600
_STATE = "/plugins/dynamix.my.servers/data/server-state.php"
_STREAM = "/webGui/include/FlashBackup.php"
_DOWNLOAD = "/webGui/include/Download.php"
# What cmd=backup answers: <server>-v<version>-boot-backup-<date>-<time>.zip
_ZIP_NAME = re.compile(r"[A-Za-z0-9._-]+-boot-backup-[0-9-]+\.zip")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:-[0-9A-Za-z.]+)?")
# changes.txt starts "# Version 7.4.0 2026-11-09".
_CHANGES_VERSION = re.compile(r"#\s*Version\s+(\S+)")
_REQUIRED = ("changes.txt", "config/super.dat")
_CHUNK = 1024 * 1024


class UnraidError(AppError):
    pass


def _not_a_backup(why):
    return UnraidError(f"unraid: the file isn't an Unraid boot device backup ({why})")


def _version_in(zf):
    with zf.open("changes.txt") as f:
        match = _CHANGES_VERSION.search(f.read(200).decode("utf-8", "replace"))
    return match.group(1) if match else None


def read_backup(path):
    """The Unraid version a boot device backup records (changes.txt), after
    checking it's whole: a zip with Unraid's own files, every member's CRC
    right. Raises UnraidError otherwise."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            missing = [name for name in _REQUIRED if name not in names]
            if missing:
                raise _not_a_backup(f"no {missing[0]}")
            bad = zf.testzip()
            if bad is not None:
                raise _not_a_backup(f"{bad} is damaged")
            return _version_in(zf)
    except zipfile.BadZipFile:
        raise _not_a_backup("it isn't a whole zip") from None


def backup_version(path):
    """Just the version a backup records, without reading the whole zip (for
    the manifest backfill); None if it has none."""
    try:
        with zipfile.ZipFile(path) as zf:
            return _version_in(zf)
    except (zipfile.BadZipFile, KeyError):
        return None


def check_backup(path):
    """archive_check's check: raises UnraidError unless the backup is whole."""
    read_backup(path)


class UnraidApp:
    def __init__(self, url, password=None, trusted_cert=None, timeout=_TIMEOUT):
        self.url = url.rstrip("/")
        self.password = password or ""
        self.timeout = timeout
        self.session = SameHostSession(self.url, strict=True)
        cert_pin.pin(self.session, "unraid", trusted_cert)
        self._signed_in = False
        self._version = None

    def _request(self, method, path, timeout=None, **kwargs):
        res = self.session.request(method, f"{self.url}{path}", timeout=timeout or self.timeout,
                                   allow_redirects=False, **kwargs)
        if 300 <= res.status_code < 400:
            target = res.headers.get("Location", "")
            if not (target.startswith("/") or target.startswith(self.url)):
                raise UnraidError("unraid: the URL redirects to another address - use the address Unraid's "
                                  "web UI ends up at (Settings > Management Access shows it)")
        return res

    def _sign_in(self):
        if not self.password:
            raise UnraidError("unraid: enter the root password")
        res = self._request("POST", "/login", data={"username": USERNAME, "password": self.password})
        if res.status_code in (301, 302, 303) and "/login" not in res.headers.get("Location", ""):
            self._signed_in = True
            return
        page = res.text if res.status_code == 200 else ""
        if "Too many previous login attempts" in page:
            raise UnraidError("unraid: Unraid is ignoring sign-ins from Backuparr after 3 failed ones - "
                              "wait 15 minutes, check the password, then try again")
        if "Invalid username or password" in page:
            raise UnraidError("unraid: the root password is wrong (Unraid ignores sign-ins for 15 minutes "
                              "after 3 failures, so check it before testing again)")
        if 'name="confirmPassword"' in page:
            raise UnraidError("unraid: set Unraid's root password first - open its web UI once")
        if res.status_code == 404 or 'name="password"' not in page:
            raise UnraidError("unraid: no Unraid web UI at this URL - check it")
        raise UnraidError("unraid: Unraid refused the sign-in")

    def _sign_out(self):
        if not self._signed_in:
            return
        try:
            self._request("GET", "/logout")
        except Exception as exc:  # noqa: BLE001 - the session expires on its own
            logger.debug("unraid: sign-out failed (%s)", type(exc).__name__)
        self._signed_in = False

    def _state(self):
        """(version, csrf token) from server-state.php. Its JSON holds an API
        key too, so nothing but these two is kept or logged."""
        res = self._request("GET", _STATE)
        if res.status_code != 200:
            raise UnraidError("unraid: this Unraid doesn't answer as 7.0 or later does - update it, "
                              "or check the URL is its web UI")
        try:
            state = res.json()
        except ValueError:
            raise UnraidError("unraid: the URL didn't answer like Unraid's web UI - check it") from None
        version = str(state.get("osVersion") or "") if isinstance(state, dict) else ""
        token = str(state.get("csrf") or "") if isinstance(state, dict) else ""
        match = _VERSION.fullmatch(version)
        if not match or not token:
            raise UnraidError("unraid: Unraid didn't report its version - check the URL is its web UI")
        self._version = version
        return version, token

    def app_version(self):
        if self._version:
            return self._version
        self._sign_in()
        try:
            return self._state()[0]
        finally:
            self._sign_out()

    def test_connection(self):
        self._sign_in()
        try:
            version, _token = self._state()
        finally:
            self._sign_out()
        return f"unraid {version} reachable, signed in as root"

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, BACKUP_FILE)
        self._sign_in()
        try:
            version, token = self._state()
            logger.info("unraid: making the boot device backup (Unraid %s, about 1 GB - this takes a few minutes)...",
                        version)
            res = self._request("GET", _STREAM, timeout=(self.timeout, _BACKUP_TIMEOUT), stream=True)
            if res.status_code == 404:
                res.close()
                self._backup_on_server(token, path)
            else:
                self._save(res, path)
        finally:
            self._sign_out()
        size = os.path.getsize(path)
        logger.info("unraid: downloaded the boot device backup (%d MB)", size // _CHUNK)
        return path

    def _backup_on_server(self, token, path):
        """Before 7.4: Unraid writes the zip on the server, then it's
        downloaded and removed."""
        res = self._request("POST", _DOWNLOAD, timeout=(self.timeout, _BACKUP_TIMEOUT),
                            data={"cmd": "backup", "csrf_token": token})
        name = res.text.strip() if res.status_code == 200 else ""
        if res.status_code == 200 and not name:
            raise UnraidError("unraid: Unraid couldn't make the backup - it needs free space for it (about 1.5 times "
                              "the boot device's used space) in RAM or on the array")
        if not _ZIP_NAME.fullmatch(name):
            raise UnraidError("unraid: Unraid didn't make the backup - check the user is root")
        try:
            self._save(self._request("GET", f"/{name}", timeout=(self.timeout, _BACKUP_TIMEOUT), stream=True), path)
        finally:
            try:
                self._request("POST", _DOWNLOAD, data={"cmd": "unlink", "file": name, "csrf_token": token})
            except Exception as exc:  # noqa: BLE001 - Unraid clears /tmp's copy at reboot; one on a share stays
                logger.warning("unraid: couldn't remove the backup's copy from the server (%s)", type(exc).__name__)

    def _save(self, res, path):
        with res:
            if res.status_code != 200 or "zip" not in res.headers.get("Content-Type", ""):
                raise UnraidError("unraid: Unraid didn't send the backup - check the user is root")
            save_stream(res, path)
