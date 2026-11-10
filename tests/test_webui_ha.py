"""Home Assistant's read-only status routes (#170): a bearer token of their
own, off by default, and every app's latest run."""
import importlib
import json
import sys

import pytest

import runlog
from conftest import load_webui


def enable(client):
    response = client.post("/api/ha/token", json={})
    assert response.status_code == 200
    return response.get_json()["token"]


def status(client, token, path="/api/ha/status"):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def ha(authed_client, isolated_webui):
    """The integration on, three apps enabled and a client with no login."""
    authed_client.post("/api/config", json={"apps": {
        "radarr": {"enabled": True, "url": "http://radarr", "api_key": "k"},
        "sonarr": {"enabled": True, "url": "http://sonarr", "api_key": "k"},
        "sonarr-4k": {"enabled": True, "url": "http://sonarr4k", "api_key": "k", "name": "4K"},
        "lidarr": {"enabled": False, "url": "http://lidarr", "api_key": "k"},
    }})
    token = enable(authed_client)
    return type("HA", (), {"token": token, "client": isolated_webui.app.test_client(), "admin": authed_client,
                           "webui": isolated_webui})


def run(app, stamp, ok, error=None):
    runlog.save({"id": f"{app}_{stamp}", "app": app, "started": f"2026-11-{stamp[6:8]}T03:00:00+00:00",
                 "finished": f"2026-11-{stamp[6:8]}T03:01:00+00:00", "file": None, "ok": ok, "error": error,
                 "app_version": "1", "destinations": {}, "lines": []})


# --- off, on, a new token -----------------------------------------------

def test_it_is_off_by_default_and_answers_404(isolated_webui):
    client = isolated_webui.app.test_client()
    for path in ("/api/ha/status", "/api/ha/status/radarr"):
        assert client.get(path).status_code == 404
        assert client.get(path, headers={"Authorization": "Bearer anything"}).status_code == 404


def test_only_the_tokens_hash_is_kept_and_it_never_comes_back(ha):
    stored = json.load(open(ha.webui.settings.load_config.__globals__["CONFIG_PATH"]))
    assert stored["ha_enabled"] is True and ha.token not in json.dumps(stored)
    assert len(stored["ha_token_hash"]) == 64
    cfg = ha.admin.get("/api/config").get_json()
    assert cfg["ha_enabled"] is True and "ha_token_hash" not in cfg


def test_a_new_token_replaces_the_old_one(ha):
    new = enable(ha.admin)
    assert new != ha.token
    assert status(ha.client, ha.token).status_code == 401
    assert status(ha.client, new).status_code == 200


def test_turning_it_off_removes_the_token(ha):
    assert ha.admin.post("/api/ha/disable", json={}).get_json() == {"ok": True}
    assert status(ha.client, ha.token).status_code == 404
    assert ha.admin.get("/api/config").get_json()["ha_enabled"] is False


def test_settings_can_not_set_it(ha):
    ha.admin.post("/api/config", json={"ha_enabled": False, "ha_token_hash": ""})
    assert status(ha.client, ha.token).status_code == 200


# --- the token ----------------------------------------------------------

@pytest.mark.parametrize("header", [None, "Bearer", "Basic YWRtaW46eA==", "Token abc"])
def test_no_bearer_token_is_refused(ha, header):
    response = ha.client.get("/api/ha/status", headers={"Authorization": header} if header else {})
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"


def test_a_wrong_token_is_refused_and_counts_towards_the_lockout(ha):
    for _ in range(5):
        assert status(ha.client, "wrong").status_code == 401
    assert status(ha.client, ha.token).status_code == 429  # locked out, even with the right one
    # The lockout is the login's: the admin login from this address waits too.
    assert ha.client.post("/api/login", json={"username": "admin", "password": "x"}).status_code == 429


def test_the_token_opens_nothing_else(ha):
    headers = {"Authorization": f"Bearer {ha.token}"}
    for method, path in (("GET", "/api/config"), ("POST", "/api/backup/run"), ("GET", "/api/runlogs"),
                         ("POST", "/api/restore/local/radarr"), ("POST", "/api/ha/token")):
        assert ha.client.open(path, method=method, json={}, headers=headers).status_code == 401


def test_the_session_login_does_not_open_it(ha):
    assert ha.admin.get("/api/ha/status").status_code == 401


def test_without_local_login_it_still_needs_the_token(ha, monkeypatch):
    monkeypatch.setenv("BACKUPARR_DISABLE_AUTH", "true")
    for name in ("webui.auth", "webui.destinations", "webui.runs", "webui.backups", "webui.ha", "webui.app"):
        module = importlib.reload(sys.modules[name])
    module.app.config.update(TESTING=True)
    client = module.app.test_client()
    assert client.get("/api/ha/status").status_code == 401
    assert status(client, "wrong").status_code == 401
    assert status(client, ha.token).status_code == 200


# --- what it says -------------------------------------------------------

def test_every_app_and_backuparr_with_its_latest_run(ha):
    run("radarr", "20261108_030000", False, "couldn't reach the app")
    run("radarr", "20261109_030000", True)
    run("sonarr", "20261108_030000", True)
    run("sonarr", "20261109_030000", False, "failed on gdrive: storage quota exceeded")
    run("backuparr", "20261109_030000", True)
    body = status(ha.client, ha.token).get_json()
    assert {k: body[k] for k in ("ok", "total", "succeeded", "failed", "running")} == {
        "ok": False, "total": 4, "succeeded": 2, "failed": 1, "running": False}
    assert body["version"] == ha.webui.ha.VERSION
    apps = body["apps"]
    assert list(apps) == ["backuparr", "radarr", "sonarr", "sonarr-4k"]  # lidarr is off
    assert apps["radarr"] == {"name": "Radarr", "ok": True, "last_run": "2026-11-09T03:01:00+00:00",
                              "last_success": "2026-11-09T03:01:00+00:00", "error": None}
    assert apps["sonarr"] == {"name": "Sonarr", "ok": False, "last_run": "2026-11-09T03:01:00+00:00",
                              "last_success": "2026-11-08T03:01:00+00:00", "error": "failed on gdrive: storage quota exceeded"}
    assert apps["sonarr-4k"] == {"name": "Sonarr (4K)", "ok": None, "last_run": None, "last_success": None, "error": None}
    assert apps["backuparr"]["name"] == "Backuparr" and apps["backuparr"]["ok"] is True


def test_ok_only_when_every_backup_succeeded(ha):
    for app in ("backuparr", "radarr", "sonarr", "sonarr-4k"):
        run(app, "20261109_030000", True)
    assert status(ha.client, ha.token).get_json()["ok"] is True
    ha.admin.post("/api/config", json={"self_backup": False})
    body = status(ha.client, ha.token).get_json()
    assert body["ok"] is True and "backuparr" not in body["apps"] and body["total"] == 3


def test_a_backup_running_is_said(ha):
    ha.webui.runs.RUN_STATE["running"] = True
    try:
        assert status(ha.client, ha.token).get_json()["running"] is True
    finally:
        ha.webui.runs.RUN_STATE["running"] = False


def test_one_app_on_its_own(ha):
    run("radarr", "20261109_030000", True)
    assert status(ha.client, ha.token, "/api/ha/status/radarr").get_json()["ok"] is True
    assert status(ha.client, ha.token, "/api/ha/status/sonarr-4k").get_json()["ok"] is None
    for missing in ("lidarr", "nope"):
        assert status(ha.client, ha.token, f"/api/ha/status/{missing}").status_code == 404
