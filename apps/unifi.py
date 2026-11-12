"""Backup driver for the UniFi Network app (#161).

UniFi has no documented backup API. Its web UI's own "Download backup" uses
the Network app's older API, which this uses too, signed in as a local admin:

- UniFi OS (UniFi OS Server, and consoles: UDM, UCG, Cloud Key): POST
  /api/auth/login, then every call under /proxy/network with the
  X-CSRF-Token the sign-in returns (UniFi OS refuses a POST without it).
- The standalone UniFi Network Application (deprecated, still released):
  POST /api/login and the same calls without /proxy/network.

Which one answers is found before signing in, from a page each serves
without a login: UniFi OS's /api/system (JSON with "hardware"), the
standalone app's /status (meta.server_version). It's found on every use, so a
move from the standalone app to UniFi OS Server at the same address keeps
working.

A backup is POST .../api/s/default/cmd/backup {"cmd": "backup", "days": N}
(0 = settings only, -1 = all statistics). It answers once the file is
written, with its path (/dl/backup/<version>.unf), downloaded with the same
session. Backuparr signs out after each use.

Privileges (lab, UniFi OS Server 5.1.42 / Network 10.5.67 and the standalone
app 10.6.109): on UniFi OS a local admin with only Network: Full Management,
and no UniFi OS rights, may back up; Site Admin, View Only and Hotspot
Operator get api.err.NoPermission. The standalone app needs a Super Admin,
and its Admins page (10.6) adds only site admins, so that's the admin its
setup made.
Any admin can read the version (stat/sysinfo). {"cmd": "list-backups"} is
answered under the same permission and changes nothing, so Test connection
checks it with that.

UniFi OS counts sign-ins and answers 429 (AUTHENTICATION_FAILED_LIMIT_REACHED)
for about a minute after too many - even with the right password - so a
refused sign-in is reported, never retried.

The .unf file is encrypted with a key every UniFi install shares (AES-128-CBC,
so it protects nothing): read_backup() decrypts it to check it and read its
version. It holds Wi-Fi passwords, VPN keys and admin logins, so Backuparr
always encrypts it (#147). Restores are by hand (manual_restore, #151). A
self-signed certificate is trusted on the card and pinned (apps/cert_pin.py).
"""
import gzip
import logging
import os
import re
import struct
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from apps import cert_pin
from apps.base import AppError, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

UNIFI_OS = "unifi-os"
STANDALONE = "standalone"
# The History option: how many days of statistics go into the backup.
HISTORY_DAYS = {"settings": 0, "7": 7, "30": 30, "90": 90, "365": 365, "all": -1}
DEFAULT_HISTORY = "settings"
PRIVILEGES = "on UniFi OS give it Network: Full Management; in the standalone app use the admin made during its setup"
RATE_LIMITED = "unifi: UniFi is limiting sign-ins. Wait a minute and try again."

_TIMEOUT = 60
# cmd/backup answers once the file is written: settings only takes a second;
# all the statistics of a large network on a slow console (an SD card or HDD)
# can take many minutes. At most 1 h, as for every app.
_BACKUP_TIMEOUT = 3600
_DOWNLOAD_READ_TIMEOUT = 600
_DOWNLOAD_PATH = re.compile(r"/dl/backup/[A-Za-z0-9._-]+\.unf")
_UNF_KEY = b"bcyangkmluohmars"
_UNF_IV = b"ubntenterpriseap"
_CHUNK = 1024 * 1024
_EOCD = b"PK\x05\x06"
_CENTRAL = b"PK\x01\x02"
_ZIP64_LOCATOR = b"PK\x06\x07"
_REQUIRED = ("version", "format", "db.gz")


class UnifiError(AppError):
    pass


def _not_a_backup(why):
    return UnifiError(f"unifi: the backup isn't a UniFi Network backup ({why})")


def _decrypt(src, dst):
    size = os.path.getsize(src)
    if size == 0 or size % 16:
        raise _not_a_backup("its size isn't whole AES blocks")
    decryptor = Cipher(algorithms.AES(_UNF_KEY), modes.CBC(_UNF_IV)).decryptor()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(_CHUNK), b""):
            fout.write(decryptor.update(chunk))
        fout.write(decryptor.finalize())


def _central_directory(tail, end):
    """(offset in tail, entry count) of the central directory that ends
    exactly at `end`, found by walking its records."""
    start = tail.find(_CENTRAL)
    while 0 <= start < end:
        pos, count = start, 0
        while pos < end and tail[pos:pos + 4] == _CENTRAL and pos + 46 <= end:
            name_len, extra_len, comment_len = struct.unpack("<HHH", tail[pos + 28:pos + 34])
            pos += 46 + name_len + extra_len + comment_len
            count += 1
        if pos == end and count:
            return start, count
        start = tail.find(_CENTRAL, start + 1)
    return None


def _repair_end(path):
    """UniFi encrypts the zip without padding, so the last few bytes of its
    end-of-central-directory record are cut off. Writes a whole one."""
    size = os.path.getsize(path)
    tail_start = max(0, size - _CHUNK)
    with open(path, "r+b") as f:
        f.seek(tail_start)
        tail = f.read()
        eocd = tail.rfind(_EOCD)
        # The record is 22 bytes and at most 15 are missing.
        if eocd < 0 or len(tail) - eocd > 22:
            raise _not_a_backup("it has no zip directory")
        if eocd >= 20 and tail[eocd - 20:eocd - 16] == _ZIP64_LOCATOR:
            # A Zip64 archive: its own record (intact) says where everything is.
            record = struct.pack("<4sHHHHIIH", _EOCD, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
        else:
            found = _central_directory(tail, eocd)
            if not found:
                raise _not_a_backup("its zip directory can't be read")
            start, count = found
            record = struct.pack("<4sHHHHIIH", _EOCD, 0, 0, count, count, eocd - start, tail_start + start, 0)
        f.seek(tail_start + eocd)
        f.write(record)
        f.truncate()


def read_backup(path, work_dir=None):
    """Checks a .unf backup - it decrypts to a whole zip, every file in it
    reads back intact, and it holds the settings database - and returns the
    Network version inside it. Raises UnifiError."""
    plain = os.path.join(work_dir or os.path.dirname(path) or ".", f".{os.path.basename(path)}.zip")
    try:
        _decrypt(path, plain)
        _repair_end(plain)
        try:
            with zipfile.ZipFile(plain) as archive:
                names = set(archive.namelist())
                missing = [name for name in _REQUIRED if name not in names]
                if missing:
                    raise _not_a_backup(f"{missing[0]} is missing")
                bad = archive.testzip()
                if bad:
                    raise UnifiError(f"unifi: {bad} in the backup is damaged")
                with archive.open("db.gz") as db:
                    if db.read(2) != b"\x1f\x8b":
                        raise UnifiError("unifi: the backup's database (db.gz) isn't gzip")
                version = archive.read("version").decode("utf-8", "replace").strip()
        except (zipfile.BadZipFile, OSError, EOFError, gzip.BadGzipFile) as exc:
            raise UnifiError(f"unifi: the backup is damaged ({exc or type(exc).__name__})") from None
        if not version:
            raise _not_a_backup("it records no version")
        return version
    finally:
        if os.path.exists(plain):
            os.remove(plain)


def check_backup(path):
    """archive_check's check of a .unf: raises UnifiError unless whole."""
    read_backup(path)


class UnifiApp:
    def __init__(self, url, username=None, password=None, history=None, trusted_cert=None, timeout=_TIMEOUT):
        self.url = url.rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.history = history if history in HISTORY_DAYS else DEFAULT_HISTORY
        self.timeout = timeout
        self.session = SameHostSession(self.url, strict=True)
        cert_pin.pin(self.session, "unifi", trusted_cert)
        self.install = None
        self._version = None
        self._signed_in = False

    # ---------------------------------------------------------- requests ----
    def _request(self, method, path, timeout=None, **kwargs):
        res = self.session.request(method, f"{self.url}{path}", timeout=timeout or self.timeout,
                                   allow_redirects=False, **kwargs)
        if 300 <= res.status_code < 400:
            # Never followed: a user without access is sent to a plain-http page.
            raise UnifiError("unifi: the URL redirects - use UniFi's https:// address with its port "
                             "(443 on a console, 11443 for UniFi OS Server, 8443 for the standalone app)")
        if res.status_code == 429:
            raise UnifiError(RATE_LIMITED)
        token = res.headers.get("X-Updated-Csrf-Token")
        if token and self._signed_in:
            self.session.headers["X-CSRF-Token"] = token
        return res

    @staticmethod
    def _json(res):
        try:
            return res.json()
        except ValueError:
            return None

    @property
    def _prefix(self):
        return "/proxy/network" if self.install == UNIFI_OS else ""

    def _detect(self):
        body = self._json(self._request("GET", "/api/system"))
        if isinstance(body, dict) and isinstance(body.get("hardware"), dict):
            return UNIFI_OS
        body = self._json(self._request("GET", "/status"))
        meta = body.get("meta") if isinstance(body, dict) else None
        if isinstance(meta, dict) and meta.get("server_version"):
            return STANDALONE
        raise UnifiError("unifi: no UniFi Network at this URL - check it (443 on a console, 11443 for "
                         "UniFi OS Server, 8443 for the standalone app)")

    def _sign_in(self):
        if not self.username or not self.password:
            raise UnifiError("unifi: enter the username and password of a local admin")
        self.install = self._detect()
        if self.install == UNIFI_OS:
            res = self._request("POST", "/api/auth/login", json={
                "username": self.username, "password": self.password, "token": "", "rememberMe": False})
        else:
            res = self._request("POST", "/api/login", json={"username": self.username, "password": self.password})
        if res.status_code == 200:
            token = res.headers.get("X-Csrf-Token")
            if self.install == UNIFI_OS and not token:
                raise UnifiError("unifi: UniFi OS signed in but sent no CSRF token - update UniFi OS")
            if token:
                self.session.headers["X-CSRF-Token"] = token
            self._signed_in = True
            return
        text = res.text[:500]
        if re.search(r"mfa|2fa|two.?factor", text, re.IGNORECASE):
            raise UnifiError("unifi: the account signs in with two-factor authentication - use a local admin "
                             "(see the docs)")
        if res.status_code in (400, 401, 403) and self.install == STANDALONE:
            raise UnifiError("unifi: the username or password is wrong")
        if res.status_code in (400, 401, 403):
            raise UnifiError("unifi: the username or password is wrong (UniFi limits sign-ins, so check them "
                             "before testing again)")
        res.raise_for_status()
        raise UnifiError(f"unifi: the sign-in failed (HTTP {res.status_code})")

    def _sign_out(self):
        if not self._signed_in:
            return
        try:
            self._request("POST", "/api/auth/logout" if self.install == UNIFI_OS else "/api/logout")
        except Exception as exc:  # noqa: BLE001 - the session expires on its own
            logger.debug("unifi: sign-out failed (%s)", type(exc).__name__)
        self._signed_in = False
        self.session.headers.pop("X-CSRF-Token", None)

    def _api(self, method, path, **kwargs):
        res = self._request(method, f"{self._prefix}{path}", **kwargs)
        if res.status_code == 403:
            raise UnifiError(f"unifi: the user can't make backups - {PRIVILEGES}")
        if res.status_code == 401:
            raise UnifiError("unifi: UniFi ended the session - test the connection again")
        res.raise_for_status()
        return res

    def _read_version(self):
        body = self._json(self._api("GET", "/api/s/default/stat/sysinfo"))
        data = body.get("data") if isinstance(body, dict) else None
        version = data[0].get("version") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(version, str) or not version.strip():
            raise UnifiError("unifi: the URL didn't answer like UniFi Network - check it")
        self._version = version.strip()
        return self._version

    def _backup_command(self, command, **kwargs):
        body = self._json(self._api("POST", "/api/s/default/cmd/backup", json={"cmd": command, **kwargs},
                                    timeout=(self.timeout, _BACKUP_TIMEOUT)))
        meta = body.get("meta") if isinstance(body, dict) else None
        if not isinstance(meta, dict) or meta.get("rc") != "ok":
            raise UnifiError(f"unifi: UniFi Network refused the backup ({str(meta)[:120]})")
        return body.get("data") or []

    # ------------------------------------------------------------- driver ----
    def _kind(self):
        return "UniFi OS" if self.install == UNIFI_OS else "standalone app"

    def test_connection(self):
        self._sign_in()
        try:
            version = self._read_version()
            self._backup_command("list-backups")
        finally:
            self._sign_out()
        return (f"UniFi Network {version} reachable ({self._kind()}), signed in as {self.username} "
                "with permission to make backups")

    def app_version(self):
        # A backup reads it in its own sign-in; UniFi OS limits sign-ins.
        if self._version:
            return self._version
        self._sign_in()
        try:
            return self._read_version()
        finally:
            self._sign_out()

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        days = HISTORY_DAYS[self.history]
        what = {0: "settings only", -1: "with all statistics"}.get(days, f"with {days} days of statistics")
        self._sign_in()
        try:
            version = self._read_version()
            logger.info("unifi: making a backup of UniFi Network %s on %s (%s)...", version, self._kind(), what)
            data = self._backup_command("backup", days=days)
            url = data[0].get("url") if data and isinstance(data[0], dict) else None
            if not isinstance(url, str) or not _DOWNLOAD_PATH.fullmatch(url):
                raise UnifiError("unifi: UniFi Network didn't say where its backup is")
            path = os.path.join(dest_dir, os.path.basename(url))
            res = self._api("GET", url, stream=True, timeout=(self.timeout, _DOWNLOAD_READ_TIMEOUT))
            with res:
                save_stream(res, path)
        finally:
            self._sign_out()
        logger.info("unifi: downloaded the backup (%d bytes)", os.path.getsize(path))
        return path
