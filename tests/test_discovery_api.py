import re
import re
import threading
import copy

import pytest
import requests

from conftest import load_webui


@pytest.fixture
def webui(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUPARR_SECRET_KEY_PATH", str(tmp_path / "session.key"))
    monkeypatch.setenv("BACKUPARR_LOG_DIR", str(tmp_path / "logs"))
    module = load_webui()
    import config_store
    monkeypatch.setattr(module.services, "load_config", lambda: copy.deepcopy(config_store.DEFAULTS))
    monkeypatch.setattr(module.auth.auth_store, "has_credentials", lambda: True)
    monkeypatch.setattr(module.services.DISCOVERY_JOBS, "jobs", {})
    monkeypatch.setattr(module.services, "DISCOVERY_RUN_LOCK", threading.Lock())

    class InlineThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(module.services.threading, "Thread", InlineThread)
    monkeypatch.setattr(module.services.discovery, "discover_prowlarr", lambda instance, progress: {
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
    assert not webui.services.DISCOVERY_JOBS.jobs


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
    webui.services.DISCOVERY_RUN_LOCK.acquire()
    response = client_for(webui).post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    assert response.status_code == 409


def test_late_session_refresh_cannot_erase_discovery_access(webui):
    client = client_for(webui)
    with client.session_transaction() as session:
        session.permanent = True
    older_reader = webui.app.test_client()
    older_reader.set_cookie("session", client.get_cookie("session").value)
    stale_response = older_reader.get("/api/restore/status")
    assert "Set-Cookie" in stale_response.headers
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    client.set_cookie("session", older_reader.get_cookie("session").value)
    path = "/api/discovery/prowlarr/" + response.json["job_id"]
    assert client.get(path).json["result"]["candidates"][0]["api_key"] == "test-secret"


def test_the_owner_cookie_is_set_once_and_never_echoed_back(webui):
    client = client_for(webui)
    body = {"url": "http://prowlarr:9696", "api_key": "key"}
    first = client.post("/api/discovery/prowlarr", json=body)
    owner = client.get_cookie(webui.services.DISCOVERY_JOBS.cookie).value
    assert any(c.startswith(webui.services.DISCOVERY_JOBS.cookie + "=") for c in first.headers.getlist("Set-Cookie"))
    second = client.post("/api/discovery/prowlarr", json=body)
    assert not any(c.startswith(webui.services.DISCOVERY_JOBS.cookie + "=") for c in second.headers.getlist("Set-Cookie"))
    assert client.get_cookie(webui.services.DISCOVERY_JOBS.cookie).value == owner
    assert client.get("/api/discovery/prowlarr/" + second.json["job_id"]).json["result"]["candidates"]


def test_logout_discards_discovery_cookie(webui):
    client = client_for(webui)
    client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    assert client.get_cookie(webui.services.DISCOVERY_JOBS.cookie)
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get_cookie(webui.services.DISCOVERY_JOBS.cookie) is None


def test_remote_errors_do_not_expose_secrets_and_allow_retry(webui, monkeypatch, caplog):
    def fail(instance, progress):
        raise requests.HTTPError("http://server?apikey=do-not-leak-key")

    monkeypatch.setattr(webui.services.discovery, "discover_prowlarr", fail)
    client = client_for(webui)
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    path = "/api/discovery/prowlarr/" + response.json["job_id"]
    result = client.get(path)
    assert result.json["state"] == "failed"
    assert "do-not-leak-key" not in result.text + caplog.text
    assert not webui.services.DISCOVERY_RUN_LOCK.locked()
    webui.services.DISCOVERY_JOBS.discard(response.json["job_id"])
    assert client.get(path).status_code == 404


def test_discovery_connects_with_strict_redirects(webui, monkeypatch):
    received = []

    class Instance:
        def __init__(self, url, api_key, **kwargs):
            received.append((url, api_key, kwargs))

    monkeypatch.setattr(webui.services, "ProwlarrApp", Instance)
    client = client_for(webui)
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": " key "})
    result = client.get("/api/discovery/prowlarr/" + response.json["job_id"])
    assert result.json["state"] == "completed"
    assert received == [("http://prowlarr:9696", "key", {"strict_redirects": True})]


def test_discovery_signs_in_to_prowlarr_with_its_login_when_given(webui, monkeypatch):
    received = []

    class Instance:
        def __init__(self, url, api_key, **kwargs):
            received.append(kwargs)

    monkeypatch.setattr(webui.services, "ProwlarrApp", Instance)
    client = client_for(webui)
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key",
                                                            "username": "admin", "password": "pw"})
    assert client.get("/api/discovery/prowlarr/" + response.json["job_id"]).json["state"] == "completed"
    assert received == [{"strict_redirects": True, "username": "admin", "password": "pw"}]


def test_finished_results_are_purged_after_the_ttl(webui, monkeypatch):
    client = client_for(webui)
    response = client.post("/api/discovery/prowlarr", json={"url": "http://prowlarr:9696", "api_key": "key"})
    path = "/api/discovery/prowlarr/" + response.json["job_id"]
    assert client.get(path).status_code == 200
    webui.services.DISCOVERY_JOBS.jobs[response.json["job_id"]][1]["finished"] -= webui.services.DISCOVERY_RESULT_TTL + 1
    assert client.get(path).status_code == 404
    assert not webui.services.DISCOVERY_JOBS.jobs


def test_running_jobs_are_never_purged(webui):
    webui.services.DISCOVERY_JOBS.jobs["job"] = ("o", {"state": "running", "message": "..."})
    with webui.services.DISCOVERY_JOBS.lock:
        webui.services.DISCOVERY_JOBS.purge_locked()
    assert "job" in webui.services.DISCOVERY_JOBS.jobs


def test_prowlarr_and_discovered_apps_lead_their_category(webui):
    html = client_for(webui).get("/").get_data(as_text=True)
    cards = [line for line in html.splitlines() if 'class="app-card' in line and "data-app=" in line]
    assert [c.split('data-app="')[1].split('"')[0] for c in cards] == [
        "prowlarr", "radarr", "sonarr", "lidarr", "whisparr", "lazylibrarian", "profilarr", "bazarr", "tdarr",
        "sabnzbd", "nzbget",
        "tautulli", "jellyfin", "emby", "plex", "seerr",
        "pihole",
        "vaultwarden"]
    assert html.count('id="discover-btn"') == 1
    assert html.index('data-app="prowlarr"') < html.index('id="discover-btn"') < html.index('class="help-tip"') < html.index('data-app="profilarr"')
    assert html.count('class="help-tip"') == 1


def test_each_app_card_sits_in_its_categorys_collapsed_section(webui):
    """#148: a section per category that has apps, in APP_CATEGORIES order,
    collapsed until app.js opens it."""
    from config_store import APP_CATEGORIES, APP_META
    html = client_for(webui).get("/").get_data(as_text=True)
    sections = re.findall(r'<section class="app-category" data-category="([a-z-]+)">', html)
    # Server holds Backuparr's own card (#165), so it shows with no app in it.
    assert sections == ["media-management", "downloaders", "media-servers", "network", "security", "server"]
    assert [c["id"] for c in APP_CATEGORIES if c["id"] != "home-automation"] == sections
    for meta in APP_META:
        section = html.split(f'data-category="{meta["category"]}"', 1)[1].split("</section>", 1)[0]
        assert f'data-app="{meta["id"]}"' in section
        assert f'class="category-body hidden" id="category-{meta["category"]}"' in section
    assert 'apps-enabled-heading' not in html


def test_every_app_has_a_known_category():
    from config_store import APP_CATEGORIES, APP_META
    known = {c["id"] for c in APP_CATEGORIES}
    assert all(meta["category"] in known for meta in APP_META)


def test_every_discoverable_card_can_show_what_prowlarr_found(webui):
    html = client_for(webui).get("/").get_data(as_text=True)
    starts = list(re.finditer(r'<div class="app-card[^"]*" data-app="([^"]+)"', html))
    cards = {m.group(1): html[m.start():(starts[i + 1].start() if i + 1 < len(starts) else len(html))]
             for i, m in enumerate(starts)}
    for app in ("radarr", "sonarr", "lidarr", "whisparr", "lazylibrarian", "sabnzbd", "nzbget"):
        for marker in ("discovery-origin", "app-discovery", "discovery-key-status"):
            assert marker in cards[app], (app, marker)
    for app in ("prowlarr", "bazarr", "tdarr", "plex", "seerr"):
        assert "discovery-origin" not in cards[app] and "discovery-key-status" not in cards[app], app
    # NZBGet has no API key field: its note sits under the password.
    nzbget = cards["nzbget"]
    assert "f-api_key" not in nzbget
    assert nzbget.index('data-field="password"') < nzbget.index("discovery-key-status")
