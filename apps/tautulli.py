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

Restore is two-part: `cmd=import_database` and `cmd=import_config` each
accept a multipart file upload (`database_file`/`config_file`).

`import_config` only STAGES the file, though: it saves it to Tautulli's
cache and creates an import thread that nothing starts. Tautulli's own UI
starts it by calling the web route `/restart_import_config`, which applies
the config and restarts Tautulli. That route is not part of the API and
uses Tautulli's login session; the API key only bypasses its CSRF check.
So restore() calls it after the upload: with no login set up Tautulli
answers 200 and the import runs, with a login it redirects to the login
page and the config stays staged for the user to finish (see restore()).
Like Radarr/Sonarr/Prowlarr/Bazarr's restore here, this confirms the
config was accepted, not that Tautulli has finished restarting.

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

    def _send(self, method, cmd, url=None, **kwargs):
        """The API key travels in the query string, and requests' network
        error messages embed the full URL, so they are re-raised without it
        (same exception class, so callers still tell a refused connection
        from a timeout) and without the chained original."""
        try:
            return self.session.request(method, url or self._api_url(), **kwargs)
        except requests.exceptions.RequestException as exc:
            raise type(exc)(f"tautulli: {type(exc).__name__} calling {cmd}") from None

    def _call(self, cmd, timeout=None, **params):
        payload = {"apikey": self.api_key, "cmd": cmd, **params}
        res = self._send("GET", cmd, params=payload, timeout=timeout or self.timeout)
        if res.status_code == 401:
            raise TautulliError("tautulli: unauthorized - check the API key")
        try:
            res.raise_for_status()
        except requests.exceptions.HTTPError:
            # Don't let requests' default message through - it embeds the
            # full request URL, apikey included - or chain it (from None).
            raise TautulliError(f"tautulli: HTTP {res.status_code} calling {cmd}") from None
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
            res = self._send("POST", cmd, params=payload, data=extra, files=files, timeout=120)
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
        except requests.exceptions.HTTPError:
            raise TautulliError(f"tautulli: HTTP {res.status_code} calling {cmd}") from None
        data = res.json().get("response", {})
        if data.get("result") != "success":
            raise TautulliError(f"tautulli: {data.get('message') or 'import failed'}")
        return data.get("message", "")

    def _start_config_import(self):
        """Asks Tautulli to run the config import that import_config staged.
        Returns None when it started, else a message telling the user how to
        finish it themselves."""
        route = f"{self.url}/restart_import_config"
        res = self._send(
            "GET", "restart_import_config", url=route,
            headers={"X-Api-Key": self.api_key}, allow_redirects=False, timeout=self.timeout,
        )
        if res.status_code == 200:
            return None
        if res.status_code in (301, 302, 303, 307, 308, 401, 403):
            reason = "Tautulli has a login set up, so Backuparr can't start the import"
        else:
            reason = f"Tautulli answered HTTP {res.status_code} when asked to start the import"
        return f"{reason}. The config is staged. To apply it, log in to Tautulli if asked, then open {route}"

    def restore(self, extract_dir):
        """extract_dir must contain the files backup() wrote (tautulli.db
        and/or config.ini) - either or both may be present, since a user
        could restore an older backup taken before this pairing existed.

        Database import is best-effort - see the module docstring for why
        it currently always fails upstream.
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
                        "tautulli: database import skipped - see module docstring "
                        "(upstream api2.py bug). Restore tautulli.db by hand: Settings > "
                        "Import & Backup > Import Database."
                    )
                    summary["database_skipped"] = "not restorable via API - see README"
                else:
                    raise
        cfg_path = os.path.join(extract_dir, "config.ini")
        if os.path.isfile(cfg_path):
            summary["config"] = self._import("import_config", "config_file", cfg_path, {"backup": "true"})
            staged = self._start_config_import()
            if staged:
                logger.warning("tautulli: %s", staged)
                summary["config_staged"] = staged
        if not summary:
            raise TautulliError("tautulli: backup contained neither tautulli.db nor config.ini")
        return summary
