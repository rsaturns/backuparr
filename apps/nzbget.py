"""Backup/restore driver for NZBGet's settings, over its JSON-RPC API.

loadconfig returns every option exactly as nzbget.conf stores it - real
Usenet server passwords included, and path variables such as
${MainDir}/completed left unexpanded. The backup writes them as Name=Value
lines, the same format as NZBGet's own Settings > System > Backup settings,
so the file can also be restored through NZBGet's web UI.

RESTORE: saveconfig rewrites nzbget.conf with exactly the options it's
given - an option left out is dropped from the file - so the backup's full
option list replaces the current settings, and reload applies them. That
includes the backup's Control username and password: after restoring onto
an install with other credentials, Backuparr needs the backup's.

Only the Control user (full access) can do either: the restricted user gets
"***" for passwords and other security options, and both it and the
add-only user are refused saveconfig.

Left out: the queue and history (transient, kept in NZBGet's queue folder)
and extension script files (reinstalled from NZBGet's Extension Manager;
their settings are options, so they are included).

Auth: HTTP basic auth with NZBGet's username and password, never in the
URL. That uses the Authorization header, so a reverse proxy's own
credential needs another header.
"""
import json
import logging
import os
import zipfile

import requests

logger = logging.getLogger(f"backuparr.{__name__}")

CONFIG = "nzbget.conf"
MANIFEST = "backuparr-nzbget.json"
_RESTRICTED = "***"


class NzbgetError(RuntimeError):
    pass


def format_options(options):
    return "".join(f"{o['Name']}={o['Value']}\n" for o in options)


def parse_options(text):
    options = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or not name.strip():
            raise NzbgetError(f"nzbget: unexpected line in {CONFIG}: {line[:60]!r}")
        options.append({"Name": name.strip(), "Value": value.strip()})
    return options


def is_nzbget_backup(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return {MANIFEST, CONFIG} <= set(zf.namelist()) and zf.testzip() is None
    except zipfile.BadZipFile:
        return False


class NzbgetApp:
    def __init__(self, url, username=None, password=None, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if username or password:
            self.session.auth = (username or "", password or "")

    def _call(self, method, *params, timeout=None):
        res = self.session.post(
            f"{self.url}/jsonrpc", json={"method": method, "params": list(params)},
            timeout=timeout or self.timeout, allow_redirects=False,
        )
        if res.status_code == 401:
            raise NzbgetError(
                "nzbget: unauthorized - check the username and password "
                "(Settings > Security > ControlUsername and ControlPassword)"
            )
        if 300 <= res.status_code < 400:
            raise NzbgetError("nzbget: the URL redirects - use NZBGet's final address")
        res.raise_for_status()
        try:
            data = res.json()
        except ValueError:
            raise NzbgetError(f"nzbget: {self.url} didn't answer like NZBGet's API - check the URL") from None
        error = data.get("error") if isinstance(data, dict) else None
        if error:
            message = error.get("message") if isinstance(error, dict) else error
            if message == "Access denied":
                raise NzbgetError(
                    "nzbget: access denied - use the Control username and password "
                    "(Settings > Security), not the restricted or add-only user"
                )
            raise NzbgetError(f"nzbget: {method} failed ({message})")
        if not isinstance(data, dict) or "result" not in data:
            raise NzbgetError(f"nzbget: {self.url} didn't answer like NZBGet's API - check the URL")
        return data["result"]

    def version(self):
        return self._call("version")

    def load_config(self):
        """nzbget.conf's options as NZBGet stores them. Only the Control user
        sees every value: the restricted user gets "***" for the security
        options, and ControlPort is always one of them."""
        options = self._call("loadconfig")
        if not isinstance(options, list) or not options:
            raise NzbgetError("nzbget: loadconfig returned no settings")
        port = next((o.get("Value") for o in options if str(o.get("Name", "")).lower() == "controlport"), None)
        if port == _RESTRICTED:
            raise NzbgetError(
                "nzbget: this is NZBGet's restricted user, which can't see passwords - "
                "use the Control username and password (Settings > Security)"
            )
        return [{"Name": o["Name"], "Value": o["Value"]} for o in options]

    def test_connection(self):
        version = self.version()
        self.load_config()
        return f"nzbget {version} reachable, full access OK"

    def backup(self, dest_dir):
        version = self.version()
        options = self.load_config()
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, CONFIG), "w", encoding="utf-8") as f:
            f.write(format_options(options))
        with open(os.path.join(dest_dir, MANIFEST), "w") as f:
            json.dump({"nzbget_version": version, "options": len(options)}, f, indent=2)
        logger.info("nzbget: backed up %d settings from NZBGet %s", len(options), version)
        return dest_dir

    def restore_from_file(self, local_zip_path):
        """Replaces NZBGet's settings with the backup's, then reloads NZBGet
        to apply them."""
        if not is_nzbget_backup(local_zip_path):
            raise NzbgetError("nzbget: this file isn't an NZBGet backup made by Backuparr")
        with zipfile.ZipFile(local_zip_path) as zf:
            try:
                options = parse_options(zf.read(CONFIG).decode("utf-8"))
            except UnicodeDecodeError:
                raise NzbgetError(f"nzbget: {CONFIG} in the backup isn't text") from None
        names = {o["Name"].lower() for o in options}
        if "controlport" not in names or "maindir" not in names:
            raise NzbgetError(f"nzbget: {CONFIG} in the backup doesn't hold NZBGet's settings")
        if any(o["Value"] == _RESTRICTED for o in options if o["Name"].lower() == "controlport"):
            raise NzbgetError("nzbget: this backup was made by the restricted user and has no passwords")

        self.load_config()  # Fails early, before writing, without full access.
        if self._call("saveconfig", options) is not True:
            raise NzbgetError("nzbget: NZBGet couldn't write its settings file - check that nzbget.conf is writable")
        logger.info("nzbget: restored %d settings - reloading NZBGet to apply them", len(options))
        try:
            self._call("reload", timeout=10)
        except (requests.RequestException, NzbgetError) as exc:
            # The settings are saved; NZBGet may drop the connection as it
            # reloads, or now want the backup's credentials.
            logger.info("nzbget: reload didn't answer (%s) - NZBGet applies the settings when it restarts",
                        type(exc).__name__)
