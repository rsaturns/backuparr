"""Backup/restore driver for Tautulli via its API v2.

Tautulli has no "create then download a backup archive" flow like the
Servarr apps or Profilarr - instead its API exposes two commands that
generate and stream a live, sanitized copy on every call:

- `cmd=download_database` - a fresh copy of tautulli.db with Plex/server
  tokens nulled server-side before it's streamed back.
- `cmd=download_config` - a fresh copy of config.ini, but only
  `PMS_TOKEN`/`JWT_SECRET` are stripped (Tautulli's own
  `_DO_NOT_DOWNLOAD_KEYS`) - the Tautulli API key itself and any
  notification agent credentials come through in plain text. See the
  README's Tautulli note.

Both are ordinary `@addtoapi()` commands, reachable through the same
apikey-authenticated `/api/v2?apikey=...&cmd=...` route as every other
call here - no separate session/cookie auth, and no trigger-then-poll
step since each call already returns the finished file.

Restore is two-part and API-driven too: `cmd=import_database` and
`cmd=import_config` each accept a multipart file upload
(`database_file`/`config_file`) and apply it - Tautulli restarts itself
after a config import. Both run as a background thread on Tautulli's side
and return immediately once accepted, so - like Radarr/Sonarr/Prowlarr/
Bazarr's restore here - this confirms the upload was received, not that
the import has finished.

DATABASE RESTORE IS NOT ACTUALLY REACHABLE OVER THE API: `import_database`
requires `app=` ("tautulli"/"plexwatch"/"plexivity") to know what it's
importing, but Tautulli's own `/api/v2` dispatcher (`api2.py`'s
`_api_validate`) unconditionally strips any `app` parameter first - it
reserves that name globally for an unrelated mobile-app-auth flag, before
the specific command ever runs. So `import_database` always sees `app=None`
and fails with "No app specified for import", regardless of what we send.
Confirmed by reading api2.py directly; this is an upstream bug, not
something fixable from the request side. restore() treats it as best-effort
(logs a warning, still restores config.ini) rather than failing outright -
restore tautulli.db by hand via Settings > Import & Backup > Import Database
until Tautulli fixes this.
"""
import logging
import os

import requests

logger = logging.getLogger(f"backuparr.{__name__}")


class TautulliError(RuntimeError):
    pass


class TautulliApp:
    def __init__(self, url, api_key, timeout=30):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()

    def _api_url(self):
        return f"{self.url}/api/v2"

    def _call(self, cmd, timeout=None, **params):
        payload = {"apikey": self.api_key, "cmd": cmd, **params}
        res = self.session.get(self._api_url(), params=payload, timeout=timeout or self.timeout)
        if res.status_code == 401:
            raise TautulliError("tautulli: unauthorized - check the API key")
        try:
            res.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            # Don't let requests' default message through - it embeds the
            # full request URL, apikey included.
            raise TautulliError(f"tautulli: HTTP {res.status_code} calling {cmd}") from exc
        return res

    def test_connection(self):
        res = self._call("get_settings", key="General", timeout=10)
        data = res.json().get("response", {})
        if data.get("result") != "success":
            raise TautulliError(f"tautulli: {data.get('message') or 'unexpected response'}")
        return "tautulli reachable"

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        for cmd, filename in (("download_database", "tautulli.db"), ("download_config", "config.ini")):
            res = self._call(cmd, timeout=120)
            with open(os.path.join(dest_dir, filename), "wb") as f:
                f.write(res.content)
        return dest_dir

    def _import(self, cmd, field_name, file_path, extra):
        with open(file_path, "rb") as f:
            payload = {"apikey": self.api_key, "cmd": cmd}
            files = {field_name: (os.path.basename(file_path), f, "application/octet-stream")}
            res = self.session.post(self._api_url(), params=payload, data=extra, files=files, timeout=120)
        if res.status_code == 401:
            raise TautulliError("tautulli: unauthorized - check the API key")
        # Tautulli's API wraps its own {"result": "error", "message": ...}
        # responses as an HTTP 400 - read the body before raise_for_status()
        # discards it as a generic "400 Bad Request".
        if res.status_code == 400:
            try:
                data = res.json().get("response", {})
            except ValueError:
                data = {}
            raise TautulliError(f"tautulli: {data.get('message') or res.text or 'import failed'}")
        try:
            res.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise TautulliError(f"tautulli: HTTP {res.status_code} calling {cmd}") from exc
        data = res.json().get("response", {})
        if data.get("result") != "success":
            raise TautulliError(f"tautulli: {data.get('message') or 'import failed'}")
        return data.get("message", "")

    def restore(self, extract_dir):
        """extract_dir must contain the files backup() wrote (tautulli.db
        and/or config.ini) - either or both may be present, since a user
        could restore an older backup taken before this pairing existed.

        Database import is best-effort: Tautulli's own /api/v2 dispatcher
        (api2.py's _api_validate) unconditionally pops any "app" parameter
        for its unrelated mobile-app-auth flag before the command handler
        ever runs - see _import()'s "app" field. import_database requires
        app="tautulli"/"plexwatch"/"plexivity" to know what it's importing,
        so that parameter can never actually arrive over the public API.
        This is a genuine upstream bug, not something a request shape on
        our end can work around - confirmed by tracing api2.py's source. If
        it ever gets fixed upstream, this simply stops triggering.
        """
        summary = {}
        db_path = os.path.join(extract_dir, "tautulli.db")
        if os.path.isfile(db_path):
            try:
                summary["database"] = self._import(
                    "import_database", "database_file", db_path, {"app": "tautulli", "method": "overwrite", "backup": "true"}
                )
            except TautulliError as exc:
                if "No app specified for import" in str(exc):
                    logger.warning(
                        "tautulli: database import skipped - Tautulli's own API strips the "
                        "required 'app' parameter before import_database runs (upstream bug, "
                        "not fixable from here). Restore tautulli.db by hand: Settings > "
                        "Import & Backup > Import Database."
                    )
                    summary["database_skipped"] = "not restorable via API - see README"
                else:
                    raise
        cfg_path = os.path.join(extract_dir, "config.ini")
        if os.path.isfile(cfg_path):
            summary["config"] = self._import("import_config", "config_file", cfg_path, {"backup": "true"})
        if not summary:
            raise TautulliError("tautulli: backup contained neither tautulli.db nor config.ini")
        return summary
