import pytest


@pytest.mark.parametrize("body", [
    {"apps": []},
    {"apps": "radarr"},
    {"apps": {"radarr": []}},
    {"apps": {"radarr": "x"}},
    {"destinations": []},
    {"destinations": {"local": []}},
    {"retention_days": True},
    {"retention_days": False},
])
def test_malformed_settings_are_rejected_with_400_not_a_server_error(authed_client, body):
    response = authed_client.post("/api/config", json=body)
    assert response.status_code == 400
    assert "error" in response.get_json()


@pytest.mark.parametrize("raw", ["[1]", '"text"', "7", "not json"])
def test_settings_body_must_be_a_json_object(authed_client, raw):
    response = authed_client.post("/api/config", data=raw, content_type="application/json")
    assert response.status_code == 400


def test_valid_settings_still_save(authed_client):
    response = authed_client.post("/api/config", json={"retention_days": 14, "cron_schedule": "0 4 * * *"})
    assert response.status_code == 200
    assert authed_client.get("/api/config").get_json()["retention_days"] == 14


def test_restore_override_must_be_an_object(authed_client):
    saved = authed_client.post("/api/config", json={
        "apps": {"radarr": {"enabled": True, "url": "http://radarr:7878", "api_key": "key"}},
        "destinations": {"local": {"enabled": True}},
    })
    assert saved.status_code == 200
    response = authed_client.post("/api/restore/local/radarr", json={"confirm": True, "override": ["x"]})
    assert response.status_code == 400


@pytest.mark.parametrize("name", ["%2e%2e", "%2e", "nonsense", "RADARR"])
def test_restore_backup_list_only_accepts_known_apps(authed_client, isolated_webui, monkeypatch, name):
    listed = []
    authed_client.post("/api/config", json={"destinations": {"local": {"enabled": True}}})
    monkeypatch.setattr(isolated_webui.backups.ra, "list_backups", lambda root, app: listed.append(app) or [])
    response = authed_client.get(f"/api/restore/local/{name}/backups")
    assert response.status_code == 404
    assert listed == []


def test_a_restore_to_another_url_never_takes_the_saved_secrets(authed_client, isolated_webui):
    authed_client.post("/api/secrets-key/acknowledge", json={"confirm": True})
    saved = authed_client.post("/api/config", json={
        "apps": {"radarr": {"enabled": True, "url": "http://radarr:7878", "api_key": "saved-key"},
                 "nzbget": {"enabled": True, "url": "http://nzbget:6789", "username": "nzb", "password": "saved-pass"}},
        "destinations": {"local": {"enabled": True}},
    })
    assert saved.status_code == 200
    target = isolated_webui.backups._restore_target
    with isolated_webui.app.test_request_context():
        *_, error = target("local", "radarr", {"override": {"url": "http://other:7878"}})
        assert error[1] == 400 and "enter the API key for that URL" in error[0].get_json()["error"]
        *_, app_cfg, error = target("local", "radarr", {"override": {"url": "http://other:7878", "api_key": "other-key"}})
        assert error is None and (app_cfg["url"], app_cfg["api_key"]) == ("http://other:7878", "other-key")
        *_, app_cfg, error = target("local", "nzbget", {"override": {"url": "http://other:6789"}})
        assert error is None and app_cfg["password"] == "" and app_cfg["username"] == "nzb"
        # The app's own URL keeps what's saved for it.
        *_, app_cfg, error = target("local", "radarr", {"override": {"url": "http://radarr:7878"}})
        assert error is None and app_cfg["api_key"] == "saved-key"
