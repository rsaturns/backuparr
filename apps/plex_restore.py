"""Backuparr talks only to the optional agent's HTTP API, never Docker/files."""
import time
from urllib.parse import urlsplit
import uuid

import requests

from apps.plex import PlexError


class AgentUnavailable(PlexError):
    """No HTTP response: an operation may still have reached the agent."""


class PlexRestoreAgent:
    def __init__(self, url, token, *, timeout=3600):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None and not parsed.query and not parsed.fragment
                     and parsed.port != 0)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or not isinstance(token, str) or not token.strip():
            raise PlexError("plex: configure the restore agent URL and its separate token")
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = "Bearer " + token
        self.timeout = timeout

    def request(self, method, path, **kwargs):
        try:
            with self.session.request(method, self.url + path, allow_redirects=False,
                                      timeout=kwargs.pop("timeout", (10, 30)), **kwargs) as response:
                if response.status_code in (401, 403):
                    raise PlexError("plex: restore agent refused its token")
                if 300 <= response.status_code < 400:
                    raise PlexError("plex: restore agent redirect refused; use its direct URL")
                try:
                    data = response.json()
                except ValueError:
                    raise PlexError("plex: restore agent returned an invalid response") from None
                if not isinstance(data, dict):
                    raise PlexError("plex: invalid restore agent response")
                if response.status_code not in (200, 202):
                    raise PlexError("plex: " + str(data.get("error", "restore agent request failed")))
                return data
        except requests.RequestException:
            raise AgentUnavailable("plex: could not reach the restore agent; check its URL and network") from None

    def test_connection(self, plex):
        status = self.request("GET", "/v1/status")
        if status.get("api_version") != 1 or status.get("server") != plex.identity():
            raise PlexError("plex: restore agent and Backuparr must point to the same Plex server/version")
        return "Restore agent connected; container and database mount verified"

    def restore(self, archive, plex):
        self.test_connection(plex)
        identity = plex.identity()
        job_id = uuid.uuid4().hex
        path = "/v1/restores/" + job_id
        try:
            with open(archive, "rb") as source:
                self.request("PUT", path, data=source, timeout=(10, 900), headers={
                    "Content-Type": "application/zip",
                    "X-Plex-Token": plex.session.headers["X-Plex-Token"],
                    "X-Plex-Machine-Identifier": identity["machine_identifier"],
                })
        except AgentUnavailable:
            # A lost response does not mean a failed restore. Never submit a new
            # ID automatically: the agent may already be replacing the database.
            try:
                self.request("GET", path)
            except PlexError:
                raise PlexError(f"plex: could not confirm upload; check restore {job_id} on the agent before retrying") from None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                job = self.request("GET", path)
            except AgentUnavailable:
                # Agent can restart and recover while Backuparr keeps polling.
                time.sleep(2)
                continue
            phase = job.get("phase")
            if phase == "complete":
                return {"agent_job": job_id, "message": job.get("message", "Plex database restored")}
            if phase in ("failed", "rolled_back", "recovery_failed"):
                message = str(job.get("error", "restore failed"))
                if job.get("cause"):
                    message += " (" + str(job["cause"]) + ")"
                raise PlexError(f"plex: {message} [agent restore {job_id}]")
            time.sleep(2)
        raise PlexError(f"plex: restore status timed out; the agent may still be working. Check restore {job_id} before retrying")
