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
        res.raise_for_status()
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
        res.raise_for_status()
        data = res.json().get("response", {})
        if data.get("result") != "success":
            raise TautulliError(f"tautulli: {data.get('message') or 'import failed'}")
        return data.get("message", "")

    def restore(self, extract_dir):
        """extract_dir must contain the files backup() wrote (tautulli.db
        and/or config.ini) - either or both may be present, since a user
        could restore an older backup taken before this pairing existed."""
        summary = {}
        db_path = os.path.join(extract_dir, "tautulli.db")
        if os.path.isfile(db_path):
            summary["database"] = self._import(
                "import_database", "database_file", db_path, {"app": "tautulli", "method": "overwrite", "backup": "true"}
            )
        cfg_path = os.path.join(extract_dir, "config.ini")
        if os.path.isfile(cfg_path):
            summary["config"] = self._import("import_config", "config_file", cfg_path, {"backup": "true"})
        if not summary:
            raise TautulliError("tautulli: backup contained neither tautulli.db nor config.ini")
        return summary
