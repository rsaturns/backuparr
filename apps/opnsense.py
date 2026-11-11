"""Backup driver for OPNsense's configuration (#153).

A firewall's whole setup - rules, interfaces, VLANs, DHCP, VPNs, users and
their keys - is one config.xml. OPNsense's core API (since 24.1,
OPNsense/Core/Api/BackupController) serves it:

- GET /api/core/backup/download/this returns the newest file in the
  firewall's configuration history (/conf/backup/config-<time>.xml).
  Config::save() writes config.xml and then copies it there, so that newest
  entry is the configuration in use. With System > Configuration > Backups
  > Backup Count at 0 there's no history and the answer is empty.
- GET /api/core/backup/backups/this lists that history; Test connection
  uses it.

Both need the "Diagnostics: Configuration History" privilege
(ui/core/backup/history*, api/core/backup/*). The version recorded with
each backup comes from GET /api/diagnostics/system/system_information
("versions": ["OPNsense 26.7-amd64", ...]), which needs "Lobby: Dashboard"
(read-only system information; it can't change settings or update
firmware) - firmware/info would need "System: Firmware", which can, and
config.xml has had no <version> since 26.7. A dedicated user with just
those two is enough. The API key and secret go as HTTP Basic auth.

The API can't upload a configuration, only revert to the firewall's own
history, so restores are by hand (manual_restore, #151). config.xml holds
private keys and password hashes: always encrypted. A self-signed web UI
certificate is trusted on the card and pinned (apps/cert_pin.py).
"""
import logging
import os
import re
import xml.etree.ElementTree as ET

from apps import cert_pin
from apps.base import AppError
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

CONFIG_FILE = "config.xml"
_MAX_CONFIG = 256 * 1024 * 1024  # a config.xml this large isn't one
# The first of system_information's "versions": "<product name> <version>-<arch>".
_PRODUCT_VERSION = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:[_p.][0-9A-Za-z]+)*)-[A-Za-z0-9_]+$")
PRIVILEGES = '"Diagnostics: Configuration History" and "Lobby: Dashboard"'


class OpnsenseError(AppError):
    pass


def check_config(path):
    """Raises OpnsenseError unless path is a whole OPNsense config.xml: it
    parses to the end (a cut-off download doesn't) and its root is
    <opnsense>. Streamed, so a large file isn't held in memory."""
    root = None
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            if root is None:
                root = element.tag
            elif event == "end":
                element.clear()
    except ET.ParseError as exc:
        raise OpnsenseError(f"opnsense: the configuration isn't valid XML ({exc})") from None
    if root != "opnsense":
        raise OpnsenseError("opnsense: that isn't an OPNsense configuration (no <opnsense> root) - check the URL")


class OpnsenseApp:
    def __init__(self, url, api_key, api_secret=None, trusted_cert=None, timeout=60):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = SameHostSession(self.url, strict=True)
        self.session.auth = (api_key, api_secret or "")
        self.session.headers.update({"Accept": "application/json"})
        cert_pin.pin(self.session, "opnsense", trusted_cert)

    def _get(self, path, **kwargs):
        res = self.session.get(f"{self.url}{path}", timeout=self.timeout, allow_redirects=False, **kwargs)
        if res.status_code == 401:
            raise OpnsenseError("opnsense: the API key or secret is wrong (System > Access > Users > API keys)")
        if res.status_code == 403:
            raise OpnsenseError(f"opnsense: the API key's user needs the {PRIVILEGES} privileges")
        if res.status_code == 404:
            raise OpnsenseError("opnsense: no backup API at this URL - check it, and update OPNsense to 24.1 or newer")
        if 300 <= res.status_code < 400:
            raise OpnsenseError("opnsense: the URL redirects - use the firewall's https:// address")
        res.raise_for_status()
        return res

    def _history(self):
        try:
            items = self._get("/api/core/backup/backups/this").json().get("items")
        except ValueError:
            raise OpnsenseError("opnsense: the URL didn't answer like OPNsense's API - check it") from None
        if not isinstance(items, list):
            raise OpnsenseError("opnsense: the URL didn't answer like OPNsense's API - check it")
        return items

    def app_version(self):
        """The firmware version, e.g. "26.7" or "26.7.8_2"."""
        res = self.session.get(f"{self.url}/api/diagnostics/system/system_information", timeout=self.timeout,
                               allow_redirects=False)
        if res.status_code == 403:
            raise OpnsenseError("opnsense: the API key's user can't read the firmware version - give it the "
                                "\"Lobby: Dashboard\" privilege too")
        res.raise_for_status()
        try:
            first = res.json()["versions"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            raise OpnsenseError("opnsense: the URL didn't answer like OPNsense's API - check it") from None
        match = _PRODUCT_VERSION.search(str(first))
        if not match:
            raise OpnsenseError(f"opnsense: couldn't read the firmware version from {first!r}")
        return match.group(1)

    def test_connection(self):
        items = self._history()
        version = self.app_version()
        if not items:
            raise OpnsenseError("opnsense: the firewall keeps no configuration history, so there's nothing to download - "
                                "set Backup Count above 0 under System > Configuration > Backups")
        newest = items[0].get("time_iso") if isinstance(items[0], dict) else None
        count = f"{len(items)} configuration{'' if len(items) == 1 else 's'}"
        return f"opnsense {version} reachable, {count} in its history" + (
            f", the newest from {newest}" if newest else "")

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        logger.info("opnsense: downloading the current configuration...")
        path = os.path.join(dest_dir, CONFIG_FILE)
        res = self._get("/api/core/backup/download/this", stream=True)
        size = 0
        with open(path, "wb") as f:
            for chunk in res.iter_content(64 * 1024):
                size += len(chunk)
                if size > _MAX_CONFIG:
                    raise OpnsenseError("opnsense: the download is far too large to be a configuration")
                f.write(chunk)
        if size == 0:
            raise OpnsenseError("opnsense: the firewall sent no configuration - it keeps no history; set Backup Count "
                                "above 0 under System > Configuration > Backups")
        check_config(path)
        name = re.sub(r"[^A-Za-z0-9._-]", "", (res.headers.get("Content-Disposition") or "").rpartition("filename=")[2])
        logger.info("opnsense: downloaded %s (%d bytes)", name or CONFIG_FILE, size)
        return path
