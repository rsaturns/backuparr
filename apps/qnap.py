"""Backup driver for a QNAP NAS's system settings on QuTS hero (#169): the
system configuration file (.bin) Control Panel > System > Backup / Restore >
Backup/Restore Settings > Download to Computer makes. It holds the NAS's
settings - general settings, users and groups, shared folder settings,
network settings and a record of installed apps - never the data in its
shared folders. QTS has the same page; Backuparr takes QuTS hero only, for
now.

QNAP doesn't document the export, so this makes the Control Panel's own
calls (read from its scripts on QuTS hero h5.2.11, verified on a real NAS):

1. POST /cgi-bin/authLogin.cgi: user, pwd (base64, QNAP's ezEncode) and
   serviceKey=1 (QNAP's HTTP API Authentication doc). The answer is XML:
   authPassed, authSid, isAdmin and the account's delegated roles
   (role_delegation). A refused sign-in (authPassed 0, errorValue -1) is
   never retried: QNAP's Network Access Protection blocks an address after
   a few failures.
   2-step verification: QNAP answers need_2sv=1 instead of a session. The
   card's code goes in once, as security_code with remme=1, and QNAP answers
   a qtoken: user + qtoken then signs in with no password or code. Test
   connection hands it to the card (card_info), which keeps it encrypted like
   other secrets. QNAP refuses a token (errorValue -1) once the account's
   password changes, and the run asks for a new code. A wrong code answers
   need_2sv=1 again.
2. POST /cgi-bin/sys/sysRequest.cgi subfunc=firm_update: the OS and version
   (firmware name "QuTS hero", version "h5.2.11", number "3661", build
   "20261023"). Only the version is kept; the answer names the model too.
3. GET /qmigrate/v1/backup/schedule (Test connection; changes nothing) and
   GET /qmigrate/v1/backup/download: the REST service QTS 5.2 and QuTS hero
   h5.2 added for this page. download answers a fresh .bin in a few seconds,
   whether or not the NAS's own backup schedule is on. Both answer JSON
   errors: -2 for no session, -3 for an account without the right.
4. GET /cgi-bin/authLogout.cgi sid, logout=1 - which ends the session and
   keeps a remembered sign-in (the web UI's own sign-out adds del_client).

The right: an administrator, or an account with the System Management role
(Control Panel > Privilege > Delegated Administration). No other role, and
no plain user, may read the version or export (-3, verified).

The service takes the session id only in the URL (?sid=), so a network
error is re-raised without the URL, and the session is ended after every
run. The file (.bin) is encrypted by QNAP itself and can't be read
here: it ends with QNAP's 74-byte footer ("icpnas", then the length of the
encrypted part), which is all the pre-upload check can look at, and the
version comes from step 2. It holds the users and their settings, so
Backuparr always encrypts it (#147). Restores are by hand in the Control
Panel (manual_restore, #151): the NAS restarts. A self-signed certificate
(QNAP's default) is trusted on the card and pinned (apps/cert_pin.py).
"""
import base64
import logging
import os
import re
import struct
import xml.etree.ElementTree as ET

import requests

from apps import cert_pin
from apps.base import AppError, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

BACKUP_FILE = "qnap-settings.bin"
OS_NAME = "QuTS hero"
MIN_VERSION = (5, 2)
SYSTEM_MANAGEMENT_ROLE = "1"
_TIMEOUT = 60
# QNAP makes the file as it sends it: seconds on a NAS (9 MB), at most 1 h.
_DOWNLOAD_TIMEOUT = 3600
_XML_LIMIT = 1024 * 1024
FOOTER = 74
_FOOTER_MAGIC = b"icpnas"
_MIN_SIZE = 1024
_VERSION = re.compile(r"h?(\d+)\.(\d+)(?:\.\d+)*")
_TOKEN = re.compile(r"[0-9A-Za-z]{16,128}")
_DIGITS = re.compile(r"\d{1,12}")

NEW_CODE = "QNAP asked for a new 2FA code. Run Test connection on its card."
_RIGHT = ("an administrator, or an account with the System Management role "
          "(Control Panel > Privilege > Delegated Administration)")


class QnapError(AppError):
    pass


class CodeRequiredError(QnapError):
    """QNAP wants a 2-step verification code. Test connection then shows
    the card's 2FA code field."""
    needs_login = True


def _not_a_backup(why):
    return QnapError(f"qnap: the file isn't a QNAP system configuration file ({why})")


def check_backup(path):
    """archive_check's check: raises QnapError unless the file ends with
    QNAP's footer, whose encrypted length fits the file. The rest is
    encrypted by QNAP and can't be checked here."""
    size = os.path.getsize(path)
    if size < _MIN_SIZE:
        raise _not_a_backup(f"only {size} bytes")
    with open(path, "rb") as f:
        f.seek(size - FOOTER)
        footer = f.read(FOOTER)
    if not footer.startswith(_FOOTER_MAGIC):
        raise _not_a_backup("no QNAP footer")
    (encrypted,) = struct.unpack_from("<I", footer, len(_FOOTER_MAGIC))
    if not 0 < encrypted <= size - FOOTER:
        raise _not_a_backup("its footer doesn't fit the file")


def _text(root, path):
    node = root.find(path)
    return (node.text or "").strip() if node is not None else ""


def _parse_xml(text):
    if len(text) > _XML_LIMIT:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    return root if root.tag == "QDocRoot" else None


def parse_version(name, version, number="", build=""):
    """("QuTS hero", "h5.2.11.3661 build 20261023") from firm_update's
    fields, or None when they don't look like QNAP's."""
    if not _VERSION.fullmatch(version or ""):
        return None
    full = version
    if _DIGITS.fullmatch(number or ""):
        full += f".{number}"
    if _DIGITS.fullmatch(build or ""):
        full += f" build {build}"
    return (name or "").strip(), full


def _is_supported(version):
    match = _VERSION.match(version or "")
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= MIN_VERSION


class QnapApp:
    def __init__(self, url, username=None, password=None, otp_code=None, device_token=None, trusted_cert=None,
                 timeout=_TIMEOUT):
        self.url = url.rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.otp_code = re.sub(r"\s+", "", otp_code or "")
        self.device_token = device_token or ""
        self.timeout = timeout
        self.session = SameHostSession(self.url, strict=True)
        cert_pin.pin(self.session, "qnap", trusted_cert)
        self.card_info = None  # a new remembered sign-in from Test connection, for the card
        self._sid = None
        self._version = None

    # ---------------------------------------------------------- requests ----
    def _send(self, method, path, doing, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        try:
            res = self.session.request(method, f"{self.url}{path}", allow_redirects=False, **kwargs)
        except cert_pin.CertificateUntrusted:
            raise
        except requests.exceptions.RequestException as exc:
            # The session id is in the URL: never let requests' message (which
            # holds it) through, or chain it.
            raise type(exc)(f"qnap: {type(exc).__name__} while trying to {doing}") from None
        if 300 <= res.status_code < 400:
            res.close()
            raise QnapError("qnap: the URL redirects - use the NAS's address as it ends up, with its port "
                            "(https://... for QNAP's secure port)")
        return res

    def _xml(self, res, doing):
        with res:
            if res.status_code != 200:
                raise QnapError(f"qnap: no QNAP NAS answered at this URL (HTTP {res.status_code}) - check it, "
                                "with its port")
            root = _parse_xml(res.text)
        if root is None:
            raise QnapError(f"qnap: the URL didn't answer like a QNAP NAS when asked to {doing} - check it, "
                            "with its port")
        return root

    def _json(self, res, doing):
        with res:
            if res.status_code == 404:
                raise QnapError("qnap: this NAS has no settings export - Backuparr needs QuTS hero h5.2 or later")
            if res.status_code != 200:
                raise QnapError(f"qnap: the NAS couldn't {doing} (HTTP {res.status_code})")
            try:
                body = res.json()
            except ValueError:
                raise QnapError(f"qnap: the NAS didn't answer like QuTS hero when asked to {doing}") from None
        if not isinstance(body, dict):
            raise QnapError(f"qnap: the NAS didn't answer like QuTS hero when asked to {doing}")
        self._check_error(body.get("error_code"), doing)
        return body

    @staticmethod
    def _check_error(code, doing):
        if code == 0:
            return
        if code == -3:
            raise QnapError(f"qnap: the account isn't allowed to export the settings - use {_RIGHT}")
        if code == -2:
            raise QnapError("qnap: the NAS ended Backuparr's session - try again")
        raise QnapError(f"qnap: the NAS couldn't {doing} (error {code})")

    # ------------------------------------------------------------ sign-in ----
    def _sign_in(self):
        if not self.username or not (self.password or self.device_token):
            raise QnapError(f"qnap: enter the username and password of {_RIGHT}")
        form = {"user": self.username, "serviceKey": "1"}
        using_code = bool(self.otp_code)
        using_token = bool(self.device_token) and not using_code
        if using_token:
            form.update(qtoken=self.device_token, remme="1")
        else:
            form["pwd"] = base64.b64encode(self.password.encode("utf-8")).decode("ascii")
            if using_code:
                form.update(security_code=self.otp_code, remme="1")
        root = self._xml(self._send("POST", "/cgi-bin/authLogin.cgi", "sign in", data=form), "sign in")
        if _text(root, "authPassed") != "1":
            raise self._login_error(root, using_code, using_token)
        sid = _text(root, "authSid")
        if not re.fullmatch(r"[0-9A-Za-z]{4,64}", sid):
            raise QnapError("qnap: the NAS didn't start a session - check the URL is the NAS's")
        self._sid = sid
        if using_code:
            token = _text(root, "qtoken")
            if not _TOKEN.fullmatch(token):
                self._sign_out()
                raise QnapError("qnap: the NAS accepted the code but didn't remember the sign-in - test again")
            self.device_token = token
            self.otp_code = ""  # one use only
            self.card_info = {"device_token": token}
        if _text(root, "isAdmin") != "1" and SYSTEM_MANAGEMENT_ROLE not in self._roles(root):
            self._sign_out()
            raise QnapError(f"qnap: the account isn't allowed to export the settings - use {_RIGHT}")

    @staticmethod
    def _roles(root):
        return {(role.findtext("id") or "").strip() for role in root.iterfind("role_delegation/role")}

    def _login_error(self, root, using_code, using_token):
        if _text(root, "need_2sv") == "1":
            if using_code:
                return CodeRequiredError("qnap: the 2FA code is wrong or expired - enter the current one and test again")
            return CodeRequiredError("qnap: the NAS asks for a 2FA code - enter one from the account's "
                                     "authenticator app and test again")
        if using_token:
            # A remembered sign-in QNAP no longer accepts (the password
            # changed): only a new code gets a new one.
            return CodeRequiredError(f"qnap: {NEW_CODE}")
        if _text(root, "errorValue") == "-1":
            return QnapError("qnap: the NAS refused the username or password - check them (QNAP blocks an address "
                             "after a few failed sign-ins)")
        return QnapError("qnap: the NAS refused the sign-in")

    def _sign_out(self):
        if not self._sid:
            return
        try:
            self._send("GET", "/cgi-bin/authLogout.cgi", "sign out",
                       params={"sid": self._sid, "logout": "1"}).close()
        except Exception as exc:  # noqa: BLE001 - the NAS ends an idle session on its own
            logger.debug("qnap: sign-out failed (%s)", type(exc).__name__)
        self._sid = None

    def _read_version(self):
        root = self._xml(self._send("POST", "/cgi-bin/sys/sysRequest.cgi", "read its version",
                                    data={"subfunc": "firm_update", "sid": self._sid}), "read its version")
        if _text(root, "authPassed") != "1":
            raise QnapError(f"qnap: the account isn't allowed to read the NAS's version - use {_RIGHT}")
        parsed = parse_version(_text(root, "firmware/name"), _text(root, "firmware/version"),
                               _text(root, "firmware/number"), _text(root, "firmware/build"))
        if parsed is None:
            raise QnapError("qnap: the NAS didn't say its version - check the URL is the NAS's")
        name, version = parsed
        if name != OS_NAME:
            running = name if re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{0,31}", name) else "an OS Backuparr doesn't know"
            raise QnapError(f"qnap: this NAS runs {running} - Backuparr backs up QuTS hero h5.2 or later for now")
        if not _is_supported(version):
            raise QnapError(f"qnap: the settings export needs QuTS hero h5.2 or later - this NAS runs {version}")
        self._version = version
        return version

    # -------------------------------------------------------------- driver ----
    def app_version(self):
        if self._version:
            return self._version
        self._sign_in()
        try:
            return self._read_version()
        finally:
            self._sign_out()

    def test_connection(self):
        self._sign_in()
        try:
            version = self._read_version()
            self._json(self._send("GET", "/qmigrate/v1/backup/schedule", "read its backup settings",
                                  params={"sid": self._sid}), "read its backup settings")
        finally:
            self._sign_out()
        message = f"qnap {version} reachable, signed in as {self.username}, who can export its settings"
        if self.card_info:
            message += ". 2FA: the sign-in is remembered - Save to keep it"
        return message

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, BACKUP_FILE)
        self._sign_in()
        try:
            version = self._read_version()
            logger.info("qnap: exporting the settings (QuTS hero %s)...", version)
            res = self._send("GET", "/qmigrate/v1/backup/download", "export the settings",
                             params={"sid": self._sid}, timeout=(self.timeout, _DOWNLOAD_TIMEOUT), stream=True)
            kind = res.headers.get("Content-Type", "")
            if res.status_code == 200 and not kind.startswith(("application/json", "text/")):
                with res:
                    save_stream(res, path)
            else:
                self._json(res, "export the settings")
                raise QnapError("qnap: the NAS didn't send the settings file")
        finally:
            self._sign_out()
        check_backup(path)
        logger.info("qnap: downloaded the settings file (%d KB)", os.path.getsize(path) // 1024)
        return path
