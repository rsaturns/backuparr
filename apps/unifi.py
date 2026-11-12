"""Backup driver for UniFi (#161): UniFi OS and the standalone UniFi Network
Server, signed in as a local admin.

Which one answers is found before signing in, from a page each serves
without a login: UniFi OS's /api/system (JSON with "hardware"), the
standalone app's /status (meta.server_version). It's found on every use, so a
move from the standalone app to UniFi OS Server at the same address keeps
working. Test connection hands it to the card (card_info), which then shows
the options that install has.

UniFi OS (UniFi OS Server, and consoles: UDM, UCG, Cloud Key): POST
/api/auth/login, then the X-CSRF-Token the sign-in returns on every POST
(UniFi OS refuses a POST without it). Two backups, the card's Backup option:

- Everything (the default): UniFi OS's own backup, the .unifi file of
  Settings > Control Plane > Backups > Download - GET /api/backup/download.
  UniFi OS, its users and every app's settings. Only a Super Admin (or the
  Owner) may download it: Network: Full Management, even with UniFi OS user
  or location management, gets 403 ACTION_FORBIDDEN (lab, 5.1.42). The
  sign-in's reply names the account's roles, which Test connection checks.
- Network only: the Network app's backup (.unf), as on the standalone app,
  under /proxy/network. A local admin with only Network: Full Management may
  make it.

The standalone UniFi Network Server (deprecated, still released): POST
/api/login and the Network calls, which take only a Super Admin - and its
Admins page (10.6) adds only site admins, so that's the admin its setup made.

The Network backup is POST .../api/s/default/cmd/backup {"cmd": "backup",
"days": N} (0 = settings only, -1 = all statistics: the History option). It
answers once the file is written, with its path (/dl/backup/<version>.unf),
downloaded with the same session. {"cmd": "list-backups"} is answered under
the same permission and changes nothing, so Test connection checks with it.
Site Admin, View Only and Hotspot Operator get api.err.NoPermission. Any admin
can read the version (stat/sysinfo). Backuparr signs out after each use.

UniFi OS counts sign-ins and answers 429 (AUTHENTICATION_FAILED_LIMIT_REACHED)
for about a minute after too many - even with the right password - so a
refused sign-in is reported, never retried.

Both files are encrypted with keys every UniFi install shares, so they
protect nothing: read_backup() decrypts a copy to check it and read the
version it records. A .unf is AES-128-CBC with a fixed IV and no padding (the
zip's last bytes are cut off); a .unifi starts with its IV, then AES-256-CBC
of a gzipped tar (the key as published by UniHosted's client-side Backup
Explorer). They hold Wi-Fi passwords, VPN keys and admin logins, so Backuparr
always encrypts them (#147). Restores are by hand (manual_restore, #151). A
self-signed certificate is trusted on the card and pinned (apps/cert_pin.py).
"""
import gzip
import json
import logging
import os
import re
import struct
import tarfile
import zipfile
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from apps import cert_pin
from apps.base import AppError, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

UNIFI_OS = "unifi-os"
STANDALONE = "standalone"
# The Backup option on UniFi OS.
EVERYTHING = "everything"
NETWORK_ONLY = "network"
# The History option: how many days of statistics a Network backup keeps.
HISTORY_DAYS = {"settings": 0, "7": 7, "30": 30, "90": 90, "365": 365, "all": -1}
DEFAULT_HISTORY = "settings"
RATE_LIMITED = "unifi: UniFi is limiting sign-ins. Wait a minute and try again."
_SUPER_ROLES = {"owner", "super_administrator"}

_TIMEOUT = 60
# Both backups are made when asked for: settings only takes seconds; all the
# statistics of a large network on a slow console (an SD card or HDD) can
# take many minutes. At most 1 h, as for every app.
_BACKUP_TIMEOUT = 3600
_DOWNLOAD_READ_TIMEOUT = 600
_DOWNLOAD_PATH = re.compile(r"/dl/backup/[A-Za-z0-9._-]+\.unf")
_UNF_KEY = b"bcyangkmluohmars"
_UNF_IV = b"ubntenterpriseap"
_UNIFI_KEY = bytes.fromhex("e383b7c53698b36d4baea4ed22181ef73676bfd5d5b90005d9845ffd5dce985f")
_CHUNK = 1024 * 1024
_EOCD = b"PK\x05\x06"
_CENTRAL = b"PK\x01\x02"
_ZIP64_LOCATOR = b"PK\x06\x07"
_UNF_REQUIRED = ("version", "format", "db.gz")
_UNIFI_META = "backup/metadata.json"
_UNIFI_PACKAGES = "backup/package-versions.json"


class UnifiError(AppError):
    pass


def _not_a_backup(why):
    return UnifiError(f"unifi: the file isn't a UniFi backup ({why})")


def _decrypt(src, dst, key, iv=None):
    """Decrypts src (AES-CBC, no padding) into dst. With no iv, the file's
    first 16 bytes are the IV."""
    size = os.path.getsize(src)
    if size == 0 or size % 16:
        raise _not_a_backup("its size isn't whole AES blocks")
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        if iv is None:
            iv = fin.read(16)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        for chunk in iter(lambda: fin.read(_CHUNK), b""):
            fout.write(decryptor.update(chunk))
        fout.write(decryptor.finalize())


def _first_block(path, key, iv=None):
    """The first decrypted bytes of path, or b"" if it's too short."""
    with open(path, "rb") as f:
        if iv is None:
            iv = f.read(16)
        block = f.read(16)
    if len(iv) < 16 or len(block) < 16:
        return b""
    return Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(block)


def kind(path):
    """"unifi" for UniFi OS's .unifi, "unf" for a Network .unf, None for
    neither - by what each decrypts to (a gzip, a zip)."""
    if _first_block(path, _UNF_KEY, _UNF_IV)[:4] == b"PK\x03\x04":
        return "unf"
    if _first_block(path, _UNIFI_KEY)[:2] == b"\x1f\x8b":
        return "unifi"
    return None


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
    """UniFi encrypts the .unf's zip without padding, so the last few bytes of
    its end-of-central-directory record are cut off. Writes a whole one."""
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


def _read_unf(path, plain):
    _decrypt(path, plain, _UNF_KEY, _UNF_IV)
    _repair_end(plain)
    try:
        with zipfile.ZipFile(plain) as archive:
            names = set(archive.namelist())
            missing = [name for name in _UNF_REQUIRED if name not in names]
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
    return f"Network {version}"


def _json_member(archive, member):
    try:
        data = json.loads(archive.extractfile(member).read())
    except ValueError:
        raise UnifiError(f"unifi: {member.name} in the backup isn't JSON") from None
    if not isinstance(data, dict):
        raise UnifiError(f"unifi: {member.name} in the backup isn't what UniFi OS writes")
    return data


def _read_unifi(path, plain):
    _decrypt(path, plain, _UNIFI_KEY)
    found = {}
    try:
        # Every member is read to its end, so a cut-off or damaged file fails.
        with tarfile.open(plain, "r|gz") as archive:
            for member in archive:
                if member.name in (_UNIFI_META, _UNIFI_PACKAGES) and member.isfile():
                    found[member.name] = _json_member(archive, member)
                elif member.isfile():
                    reader = archive.extractfile(member)
                    while reader.read(_CHUNK):
                        pass
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise UnifiError(f"unifi: the backup is damaged ({exc or type(exc).__name__})") from None
    missing = [name for name in (_UNIFI_META, _UNIFI_PACKAGES) if name not in found]
    if missing:
        raise _not_a_backup(f"{missing[0]} is missing")
    version = found[_UNIFI_META].get("firmware_version")
    if not isinstance(version, str) or not version.strip():
        raise _not_a_backup("it records no UniFi OS version")
    return f"UniFi OS {version.strip()}"


def read_backup(path, work_dir=None):
    """Checks a UniFi OS .unifi or a Network .unf - it decrypts to a whole
    archive, every file in it reads back, and it has what UniFi restores -
    and returns the version it records ("UniFi OS 5.1.42", "Network
    10.6.109"). Raises UnifiError."""
    size = os.path.getsize(path)
    if size == 0 or size % 16:
        raise _not_a_backup("its size isn't whole AES blocks")
    found = kind(path)
    if found is None:
        raise _not_a_backup("it doesn't decrypt to a .unifi or .unf")
    plain = os.path.join(work_dir or os.path.dirname(path) or ".", f".{os.path.basename(path)}.plain")
    try:
        return _read_unifi(path, plain) if found == "unifi" else _read_unf(path, plain)
    finally:
        if os.path.exists(plain):
            os.remove(plain)


def check_backup(path):
    """archive_check's check: raises UnifiError unless the backup is whole."""
    read_backup(path)


class UnifiApp:
    def __init__(self, url, username=None, password=None, backup=None, history=None, install_type=None,
                 trusted_cert=None, timeout=_TIMEOUT):
        self.url = url.rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.backup_choice = backup if backup in (EVERYTHING, NETWORK_ONLY) else EVERYTHING
        self.history = history if history in HISTORY_DAYS else DEFAULT_HISTORY
        self.timeout = timeout
        self.session = SameHostSession(self.url, strict=True)
        cert_pin.pin(self.session, "unifi", trusted_cert)
        # Found on every use; install_type (from the last Test) only drives the card.
        self.install = None
        self.card_info = None  # what Test connection found, for the card
        self._roles = None
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
    def _everything(self):
        return self.install == UNIFI_OS and self.backup_choice == EVERYTHING

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
        raise UnifiError("unifi: no UniFi at this URL - check it (443 on a console, 11443 for "
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
            body = self._json(res)
            roles = body.get("roles") if isinstance(body, dict) else None
            self._roles = {r.get("system_key") for r in roles if isinstance(r, dict)} if isinstance(roles, list) else None
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

    def _no_rights(self):
        if self._everything:
            return UnifiError("unifi: the user can't download UniFi OS's backup - make it a Super Admin, or back "
                              "up Network only")
        if self.install == UNIFI_OS:
            return UnifiError("unifi: the user can't make backups - give it Network: Full Management")
        return UnifiError("unifi: the user can't make backups - use the admin made during the app's setup")

    def _call(self, method, path, **kwargs):
        res = self._request(method, path, **kwargs)
        if res.status_code == 403:
            raise self._no_rights()
        if res.status_code == 401:
            raise UnifiError("unifi: UniFi ended the session - test the connection again")
        res.raise_for_status()
        return res

    def _system(self):
        """(name, UniFi OS version) of a UniFi OS install, from the signed-in
        /api/system ("UniFi OS Server", "5.1.42")."""
        body = self._json(self._call("GET", "/api/system"))
        hardware = body.get("hardware") if isinstance(body, dict) else None
        hardware = hardware if isinstance(hardware, dict) else {}
        return hardware.get("name") or "UniFi OS", hardware.get("firmwareVersion")

    def _network_version(self):
        body = self._json(self._call("GET", f"{self._prefix}/api/s/default/stat/sysinfo"))
        data = body.get("data") if isinstance(body, dict) else None
        version = data[0].get("version") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(version, str) or not version.strip():
            raise UnifiError("unifi: the URL didn't answer like UniFi Network - check it")
        return version.strip()

    def _backup_command(self, command, **kwargs):
        body = self._json(self._call("POST", f"{self._prefix}/api/s/default/cmd/backup", json={"cmd": command, **kwargs},
                                     timeout=(self.timeout, _BACKUP_TIMEOUT)))
        meta = body.get("meta") if isinstance(body, dict) else None
        if not isinstance(meta, dict) or meta.get("rc") != "ok":
            raise UnifiError(f"unifi: UniFi Network refused the backup ({str(meta)[:120]})")
        return body.get("data") or []

    # ------------------------------------------------------------- driver ----
    def test_connection(self):
        self._sign_in()
        try:
            if self._everything:
                # Only these may download UniFi OS's backup; asking changes nothing.
                if self._roles is not None and not self._roles & _SUPER_ROLES:
                    raise self._no_rights()
                name, version = self._system()
                message = (f"{name} {version or ''} reachable, signed in as {self.username}, who can download "
                           "its backup").replace("  ", " ")
            else:
                version = self._network_version()
                self._backup_command("list-backups")
                where = "UniFi OS" if self.install == UNIFI_OS else "standalone app"
                message = (f"UniFi Network {version} reachable ({where}), signed in as {self.username} "
                           "with permission to make backups")
        finally:
            self._sign_out()
        self.card_info = {"install": self.install}
        return message

    def app_version(self):
        # Read from the backup itself; otherwise one more sign-in, which UniFi OS limits.
        if self._version:
            return self._version
        self._sign_in()
        try:
            if self._everything:
                version = self._system()[1]
                return f"UniFi OS {version}" if version else None
            return f"Network {self._network_version()}"
        finally:
            self._sign_out()

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        self._sign_in()
        try:
            if self._everything:
                logger.info("unifi: downloading UniFi OS's backup (everything)...")
                path = os.path.join(dest_dir, "unifi_os_backup.unifi")
                res = self._call("GET", "/api/backup/download", stream=True, timeout=(self.timeout, _BACKUP_TIMEOUT))
            else:
                days = HISTORY_DAYS[self.history]
                what = {0: "settings only", -1: "with all statistics"}.get(days, f"with {days} days of statistics")
                logger.info("unifi: making a UniFi Network backup (%s)...", what)
                data = self._backup_command("backup", days=days)
                url = data[0].get("url") if data and isinstance(data[0], dict) else None
                if not isinstance(url, str) or not _DOWNLOAD_PATH.fullmatch(url):
                    raise UnifiError("unifi: UniFi Network didn't say where its backup is")
                path = os.path.join(dest_dir, os.path.basename(url))
                res = self._call("GET", f"{self._prefix}{url}", stream=True, timeout=(self.timeout, _DOWNLOAD_READ_TIMEOUT))
            with res:
                save_stream(res, path)
        finally:
            self._sign_out()
        self._version = read_backup(path)
        logger.info("unifi: downloaded the backup of %s (%d bytes)", self._version, os.path.getsize(path))
        return path
