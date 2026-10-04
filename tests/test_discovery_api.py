import importlib
import threading

import pytest
import requests


@pytest.fixture
def webui(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUPARR_SECRET_KEY_PATH", str(tmp_path / "session.key"))
    monkeypatch.setenv("BACKUPARR_LOG_DIR", str(tmp_path / "logs"))
    module = importlib.import_module("webui.app")
    monkeypatch.setattr(module.auth_store, "has_credentials", lambda: True)
    monkeypatch.setattr(module, "DISCOVERY_JOBS", {})
    monkeypatch.setattr(module, "DISCOVERY_RUN_LOCK", threading.Lock())

    class InlineThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    class IdleTimer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(module.threading, "Thread", InlineThread)
    monkeypatch.setattr(module.threading, "Timer", IdleTimer)
    monkeypatch.setattr(module.discovery, "discover_prowlarr", lambda instance, progress: {
        "candidates": [{"app": "radarr", "url": "http://radarr:7878", "api_key": "test-secret", "name": "Movies"}],
        "warnings": [],
    })
    return module


def client_for(webui):
    client = webui.app.test_client()
    with client.session_transaction() as session:
        session["authed"] = True
    return client


def test_discovery_requires_login(webui):
    response = webui.app.test_client().post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    assert response.status_code == 401


@pytest.mark.parametrize("data", [[], {}, {"url": "file:///etc/passwd", "api_key": "key"}, {"url": "http://prowlarr:9696", "api_key": 123}])
def test_discovery_validates_inputs(webui, data):
    response = client_for(webui).post("/api/discovery/prowlarr", json=data)
    assert response.status_code == 400
    assert not webui.DISCOVERY_JOBS


def test_result_is_private_uncached_and_can_be_discarded(webui):
    client = client_for(webui)
    other_session = client_for(webui)
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    assert response.status_code == 202
    path = "/api/discovery/prowlarr/" + response.json["job_id"]
    assert other_session.get(path).status_code == 404
    assert other_session.delete(path).status_code == 404
    response = client.get(path)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json["state"] == "completed"
    assert response.json["result"]["candidates"][0]["api_key"] == "test-secret"
    assert "owner" not in response.json
    assert client.delete(path).status_code == 200
    assert client.get(path).status_code == 404


def test_concurrent_discovery_is_rejected(webui):
    webui.DISCOVERY_RUN_LOCK.acquire()
    response = client_for(webui).post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    assert response.status_code == 409


def test_late_session_refresh_cannot_erase_discovery_access(webui):
    client = client_for(webui)
    with client.session_transaction() as session:
        session.permanent = True
    older_reader = webui.app.test_client()
    older_reader.set_cookie("session", client.get_cookie("session").value)
    stale_response = older_reader.get("/api/meta")
    assert "Set-Cookie" in stale_response.headers
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    client.set_cookie("session", older_reader.get_cookie("session").value)
    path = "/api/discovery/prowlarr/" + response.json["job_id"]
    assert client.get(path).json["result"]["candidates"][0]["api_key"] == "test-secret"


def test_logout_discards_discovery_cookie(webui):
    client = client_for(webui)
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    assert client.get_cookie(webui.DISCOVERY_OWNER_COOKIE)
    assert client.post("/api/logout").status_code == 200
    assert client.get_cookie(webui.DISCOVERY_OWNER_COOKIE) is None


def test_remote_errors_do_not_expose_secrets_and_allow_retry(webui, monkeypatch, caplog):
    def fail(instance, progress):
        raise requests.HTTPError("http://server?apikey=do-not-leak-key")

    monkeypatch.setattr(webui.discovery, "discover_prowlarr", fail)
    client = client_for(webui)
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    path = "/api/discovery/prowlarr/" + response.json["job_id"]
    result = client.get(path)
    assert result.json["state"] == "failed"
    assert "do-not-leak-key" not in result.text + caplog.text
    assert not webui.DISCOVERY_RUN_LOCK.locked()
    webui._forget_discovery(response.json["job_id"])
    assert client.get(path).status_code == 404


def test_session_cleanup_error_does_not_block_future_discovery(webui, monkeypatch):
    class BrokenSession:
        def close(self):
            raise requests.ConnectionError("closing failed")

    class Instance:
        session = BrokenSession()

        def __init__(self, *args):
            pass

    monkeypatch.setattr(webui, "ProwlarrApp", Instance)
    client = client_for(webui)
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    result = client.get("/api/discovery/prowlarr/" + response.json["job_id"])
    assert result.json["state"] == "completed"
    assert not webui.DISCOVERY_RUN_LOCK.locked()
