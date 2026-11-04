"""Backup/restore driver for Tautulli via its API v2 (/api/v2?apikey=...).

Backup: `download_database` and `download_config` each stream a fresh copy,
so there's no trigger-then-poll step. The database comes with Plex/server
tokens nulled; the config only has PMS_TOKEN/JWT_SECRET stripped, so the API
key and notification credentials are in it (see docs/apps/tautulli.md).

Restore: `import_config` takes a multipart upload but only stages it; the
web route /restart_import_config, outside the API, applies it and restarts
Tautulli. With no Tautulli login that call answers 200 and the import runs;
with a login it redirects, and the config stays staged for the user to
finish (the API key only bypasses its CSRF check). This confirms the config
was accepted, not that Tautulli has finished restarting.

`import_database` can't work over the API: it needs `app=tautulli`, but
api2.py's _api_validate strips every `app` parameter before any command runs
(it reserves the name for a mobile-app flag), so it always fails with "No app
specified for import". An upstream bug; restore() skips it with a warning, and
tautulli.db is restored by hand (Settings > Import & Backup > Import Database).
"""
import json
import logging
import os

import requests

from apps.base import AppError, save_stream

logger = logging.getLogger(f"backuparr.{__name__}")

# Enough of a download to tell a file from an API error's JSON body.
_ERROR_BODY_MAX = 64 * 1024


class TautulliError(AppError):
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

    def _call(self, cmd, timeout=None, stream=False, **params):
        payload = {"apikey": self.api_key, "cmd": cmd, **params}
        res = self._send("GET", cmd, params=payload, timeout=timeout or self.timeout, stream=stream)
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

    @staticmethod
    def _check_download(cmd, path):
        """An API error comes back as a JSON body, not an HTTP error: refuse
        to keep it as a "backup" of the database or config."""
        with open(path, "rb") as f:
            content = f.read(_ERROR_BODY_MAX)
        if cmd == "download_database":
            if content.startswith(b"SQLite format 3\x00"):
                return
        elif content.strip() and not content.lstrip()[:1] in (b"{", b"<"):
            return
        try:
            message = json.loads(content).get("response", {}).get("message")
        except (ValueError, AttributeError):
            message = None
        raise TautulliError(f"tautulli: {cmd} didn't return the file ({message or 'unexpected response'})")

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        for cmd, filename in (("download_database", "tautulli.db"), ("download_config", "config.ini")):
            path = os.path.join(dest_dir, filename)
            # Streamed: tautulli.db can be gigabytes.
            with self._call(cmd, timeout=120, stream=True) as res:
                save_stream(res, path)
            self._check_download(cmd, path)
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
                        "tautulli: database import skipped - Tautulli's API can't import a database "
                        "(an upstream bug). Restore tautulli.db by hand: Settings > "
                        "Import & Backup > Import Database."
                    )
                    summary["database_skipped"] = "not restorable via API - see the Tautulli page in the docs"
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
