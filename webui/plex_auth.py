"""Plex's strong-PIN browser login; passwords stay on plex.tv.

https://developer.plex.tv/pms/ documents PIN login and server resource tokens.
The account token is only used to retrieve tokens for servers the user owns.
"""
import threading
import time
from urllib.parse import urlencode
import uuid

import requests


class PlexAuthError(RuntimeError):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class PlexLogin:
    def __init__(self, version):
        self.client_id = str(uuid.uuid4())
        self.headers = {
            "Accept": "application/json",
            "X-Plex-Product": "Backuparr",
            "X-Plex-Version": version,
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Device-Name": "Backuparr",
        }
        self.lock = threading.Lock()
        self.servers = None
        self.next_poll = 0
        pin = self._request("POST", "https://plex.tv/api/v2/pins", data={"strong": "true"})
        if (not isinstance(pin, dict) or type(pin.get("id")) is not int or pin["id"] <= 0
                or not isinstance(pin.get("code"), str) or not pin["code"]
                or type(pin.get("expiresIn")) is not int or pin["expiresIn"] <= 0):
            raise PlexAuthError("Plex returned an invalid sign-in request. Try again.")
        self.pin_id, self.code = pin["id"], pin["code"]
        self.expires_in = min(pin["expiresIn"], 600)
        self.expires_at = time.monotonic() + self.expires_in
        self.auth_url = "https://app.plex.tv/auth#?" + urlencode({
            "clientID": self.client_id,
            "code": self.code,
            "context[device][product]": "Backuparr",
        })

    def _request(self, method, url, *, token=None, **kwargs):
        headers = dict(self.headers)
        if token:
            headers["X-Plex-Token"] = token
        try:
            with requests.request(method, url, headers=headers, timeout=(5, 15),
                                  allow_redirects=False, **kwargs) as response:
                if response.status_code == 404 and method == "GET" and "/pins/" in url:
                    raise PlexAuthError("Plex sign-in expired. Click Get Plex token to try again.", 410)
                if response.status_code == 429:
                    raise PlexAuthError("Plex is limiting sign-in requests. Wait a moment and try again.")
                if response.status_code not in (200, 201):
                    raise PlexAuthError("Plex could not complete sign-in. Try again.")
                return response.json()
        except (requests.RequestException, ValueError):
            # Never echo remote response bodies, URLs, PINs or tokens.
            raise PlexAuthError("Could not contact Plex. Check internet access and try again.") from None

    def poll(self):
        # A slow or repeated browser request must not create parallel Plex polls.
        if not self.lock.acquire(blocking=False):
            return {"state": "pending"}
        try:
            if time.monotonic() >= self.expires_at:
                raise PlexAuthError("Plex sign-in expired. Click Get Plex token to try again.", 410)
            if self.servers is not None:
                return {"state": "completed", "servers": self.servers}
            if time.monotonic() < self.next_poll:
                return {"state": "pending"}
            self.next_poll = time.monotonic() + 2
            pin = self._request("GET", f"https://plex.tv/api/v2/pins/{self.pin_id}",
                                params={"code": self.code})
            if not isinstance(pin, dict) or pin.get("id") != self.pin_id or pin.get("code") != self.code:
                raise PlexAuthError("Plex returned an invalid sign-in response. Try again.")
            token = pin.get("authToken")
            if token is None:
                return {"state": "pending"}
            if not isinstance(token, str) or not token.strip():
                raise PlexAuthError("Plex returned an invalid token. Try again.")
            resources = self._request("GET", "https://clients.plex.tv/api/v2/resources", token=token)
            if not isinstance(resources, list):
                raise PlexAuthError("Plex returned an invalid server list. Try again.")
            servers = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                provides = resource.get("provides", "")
                access_token = resource.get("accessToken")
                if (resource.get("owned") is True and isinstance(provides, str)
                        and "server" in provides.split(",")
                        and isinstance(access_token, str) and access_token.strip()):
                    name = resource.get("name")
                    servers.append({"name": name if isinstance(name, str) and name else "Plex server",
                                    "api_key": access_token})
            if not servers:
                raise PlexAuthError("No owned Plex servers found. Sign in with the server owner's account.", 400)
            self.servers = servers
            return {"state": "completed", "servers": servers}
        finally:
            self.lock.release()
