import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from webui.plex_auth import PlexLogin


class PlexAPI:
    """Only Plex is stubbed: exercise the real login helper and Flask routes."""
    def __init__(self):
        self.claimed = False
        self.calls = []
        self.resources = [
            {"owned": True, "provides": "server", "name": "My server", "accessToken": "server-secret"},
            {"owned": False, "provides": "server", "name": "Shared server", "accessToken": "shared-secret"},
            {"owned": True, "provides": "player", "name": "Player", "accessToken": "player-secret"},
        ]
        self.error = None
        self.status = None
        self.body = None
        self.client_id = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == (5, 15)
        assert kwargs["headers"]["Accept"] == "application/json"
        if self.error:
            raise self.error
        if method == "POST" and url == "https://plex.tv/api/v2/pins":
            assert kwargs["data"] == {"strong": "true"}
            self.client_id = kwargs["headers"]["X-Plex-Client-Identifier"]
            body = {"id": 123, "code": "strong-pin-secret", "expiresIn": 1800}
        elif url == "https://plex.tv/api/v2/pins/123":
            assert kwargs["headers"]["X-Plex-Client-Identifier"] == self.client_id
            assert kwargs["params"] == {"code": "strong-pin-secret"}
            body = {"id": 123, "code": "strong-pin-secret", "authToken": "account-secret" if self.claimed else None}
        elif url == "https://clients.plex.tv/api/v2/resources":
            assert kwargs["headers"]["X-Plex-Token"] == "account-secret"
            body = self.resources
        else:
            raise AssertionError("Unexpected outbound request")
        response = requests.Response()
        response.status_code = self.status or (201 if method == "POST" else 200)
        response._content = self.body if self.body is not None else json.dumps(body).encode()
        response._content_consumed = True
        return response


@pytest.fixture
def plex_api(monkeypatch):
    api = PlexAPI()
    monkeypatch.setattr("webui.plex_auth.requests.request", api.request)
    return api


def start(client):
    response = client.post("/api/plex/auth", json={})
    assert response.status_code == 201
    return response, "/api/plex/auth/" + response.json["job_id"]


def test_login_returns_owned_server_token_without_saving(authed_client, isolated_webui, plex_api):
    original = authed_client.get("/api/config").json
    response, path = start(authed_client)
    assert response.headers["Cache-Control"] == "no-store"
    cookie = authed_client.get_cookie(isolated_webui.PLEX_OWNER_COOKIE)
    assert cookie.http_only and cookie.same_site == "Strict"
    assert response.json["expires_in"] == 600
    auth_url = urlsplit(response.json["auth_url"])
    assert auth_url.scheme == "https" and auth_url.netloc == "app.plex.tv"
    assert auth_url.path == "/auth"
    query = parse_qs(auth_url.fragment.removeprefix("?"))
    assert query["clientID"] == [plex_api.client_id]
    assert query["code"] == ["strong-pin-secret"]
    assert query["context[device][product]"] == ["Backuparr"]
    assert authed_client.get(path).json == {"state": "pending"}
    plex_api.claimed = True
    isolated_webui.PLEX_LOGINS[response.json["job_id"]][1].next_poll = 0
    result = authed_client.get(path)
    assert result.json == {"state": "completed", "servers": [{"name": "My server", "api_key": "server-secret"}]}
    assert result.headers["Cache-Control"] == "no-store"
    assert "account-secret" not in result.text and "shared-secret" not in result.text
    assert authed_client.get("/api/config").json == original
    assert authed_client.get(path).json == result.json  # Retry a lost response until acknowledged.
    assert authed_client.delete(path).status_code == 200
    assert authed_client.get(path).status_code == 404
    assert not isolated_webui.PLEX_LOGINS


def test_multiple_owned_servers_are_available_for_selection(authed_client, plex_api):
    plex_api.resources.append({"owned": True, "provides": "server,player", "name": "Second", "accessToken": "second-secret"})
    plex_api.claimed = True
    _, path = start(authed_client)
    assert [s["name"] for s in authed_client.get(path).json["servers"]] == ["My server", "Second"]


@pytest.mark.parametrize("auth_disabled", [False, True])
def test_another_browser_cannot_read_or_cancel_login(authed_client, isolated_webui, plex_api, monkeypatch, auth_disabled):
    monkeypatch.setattr(isolated_webui, "_AUTH_DISABLED", auth_disabled)
    _, path = start(authed_client)
    other = isolated_webui.app.test_client()
    with other.session_transaction() as session:
        session["authed"] = True
    assert other.get(path).status_code == 404
    assert other.delete(path).status_code == 404
    assert authed_client.get(path).json == {"state": "pending"}


@pytest.mark.parametrize("method,path", [("post", "/api/plex/auth"), ("get", "/api/plex/auth/job"), ("delete", "/api/plex/auth/job")])
def test_login_requires_backuparr_auth(authed_client, isolated_webui, plex_api, method, path):
    response = getattr(isolated_webui.app.test_client(), method)(path, json={})
    assert response.status_code == 401
    assert not plex_api.calls


def test_cross_origin_form_cannot_start_login(authed_client, plex_api):
    assert authed_client.post("/api/plex/auth", data={}).status_code == 400
    assert authed_client.post("/api/plex/auth", json=[]).status_code == 400
    assert not plex_api.calls


def test_logout_forgets_token_and_cookie(authed_client, isolated_webui, plex_api):
    plex_api.claimed = True
    _, path = start(authed_client)
    assert authed_client.get(path).json["state"] == "completed"
    assert authed_client.post("/api/logout").status_code == 200
    assert authed_client.get_cookie(isolated_webui.PLEX_OWNER_COOKIE) is None
    assert not isolated_webui.PLEX_LOGINS


def test_new_login_invalidates_previous_attempt(authed_client, isolated_webui, plex_api):
    _, old = start(authed_client)
    _, current = start(authed_client)
    assert old != current and len(isolated_webui.PLEX_LOGINS) == 1
    assert authed_client.get(old).status_code == 404
    assert authed_client.get(current).json["state"] == "pending"


def test_expiration_drops_login_without_network(authed_client, isolated_webui, plex_api):
    response, path = start(authed_client)
    isolated_webui.PLEX_LOGINS[response.json["job_id"]][1].expires_at = time.monotonic() - 1
    assert authed_client.get(path).status_code == 404
    assert len(plex_api.calls) == 1
    assert not isolated_webui.PLEX_LOGINS


def test_repeated_polls_are_throttled(authed_client, plex_api):
    _, path = start(authed_client)
    assert authed_client.get(path).json == {"state": "pending"}
    assert authed_client.get(path).json == {"state": "pending"}
    assert len(plex_api.calls) == 2


def test_cancel_during_remote_request_does_not_return_token(authed_client, isolated_webui, plex_api, monkeypatch):
    response, path = start(authed_client)
    login = isolated_webui.PLEX_LOGINS[response.json["job_id"]][1]
    def late_result():
        isolated_webui.PLEX_LOGINS.clear()
        return {"state": "completed", "servers": [{"name": "My server", "api_key": "late-secret"}]}
    monkeypatch.setattr(login, "poll", late_result)
    result = authed_client.get(path)
    assert result.status_code == 404 and "late-secret" not in result.text


@pytest.mark.parametrize("failure", ["network", "redirect", "html", "malformed", "rate_limit"])
def test_remote_start_errors_never_leak_secrets(authed_client, isolated_webui, plex_api, caplog, failure):
    if failure == "network":
        plex_api.error = requests.ConnectionError("account-secret server-secret strong-pin-secret")
    elif failure == "redirect":
        plex_api.status = 302
    elif failure == "html":
        plex_api.body = b"<html>account-secret</html>"
    elif failure == "malformed":
        plex_api.body = b'{"id": "account-secret", "code": "strong-pin-secret"}'
    else:
        plex_api.status = 429
    response = authed_client.post("/api/plex/auth", json={})
    assert response.status_code == 502
    assert all(secret not in response.text + caplog.text for secret in ("account-secret", "server-secret", "strong-pin-secret"))
    assert not isolated_webui.PLEX_LOGINS and not isolated_webui.PLEX_START_LOCK.locked()


def test_shared_account_cannot_supply_backup_token(authed_client, plex_api):
    plex_api.resources = plex_api.resources[1:]
    plex_api.claimed = True
    _, path = start(authed_client)
    response = authed_client.get(path)
    assert response.status_code == 400
    assert "server owner's account" in response.json["error"]
    assert "shared-secret" not in response.text


def test_mismatched_pin_is_rejected(authed_client, plex_api):
    _, path = start(authed_client)
    plex_api.body = b'{"id": 456, "code": "different-pin", "authToken": "account-secret"}'
    response = authed_client.get(path)
    assert response.status_code == 502 and "account-secret" not in response.text


def test_plex_auth_cookie_is_secure_behind_https(authed_client, isolated_webui, plex_api):
    isolated_webui.app.config["SESSION_COOKIE_SECURE"] = True
    start(authed_client)
    assert authed_client.get_cookie(isolated_webui.PLEX_OWNER_COOKIE).secure


def test_pending_logins_are_bounded(authed_client, isolated_webui, plex_api):
    login = PlexLogin("test")
    isolated_webui.PLEX_LOGINS.update({str(i): ("another-owner", login) for i in range(20)})
    response = authed_client.post("/api/plex/auth", json={})
    assert response.status_code == 429
    assert len(plex_api.calls) == 1
