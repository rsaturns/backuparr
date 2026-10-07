"""Settings and the connectivity-test endpoints."""
import json
import os

import pytest
import requests

import destination_util
from config_store import APP_NAMES, DEST_NAMES


def save(client, body):
    return client.post("/api/config", json=body)


def stored(webui):
    with open(webui.CONFIG_PATH) as f:
        return json.load(f)


# --- reading ---------------------------------------------------------------

def test_a_new_install_has_every_app_off_and_local_storage_on(authed_client):
    cfg = authed_client.get("/api/config").get_json()
    assert set(cfg["apps"]) == set(APP_NAMES) and set(cfg["destinations"]) == set(DEST_NAMES)
    assert not any(app["enabled"] for app in cfg["apps"].values())
    assert cfg["destinations"]["local"]["enabled"] is True
    assert cfg["retention_days"] == 7 and cfg["cron_schedule"] == "0 3 * * *"


def test_meta_and_destinations_describe_what_the_ui_can_configure(authed_client):
    apps = authed_client.get("/api/meta").get_json()
    assert {a["id"] for a in apps} == set(APP_NAMES)
    assert next(a for a in apps if a["id"] == "seerr")["status"] == "coming_soon"
    destinations = authed_client.get("/api/destinations").get_json()
    assert [d["id"] for d in destinations] == DEST_NAMES


def test_the_dashboard_shows_the_release_version(authed_client, isolated_webui):
    page = authed_client.get("/").get_data(as_text=True)
    assert f"v{isolated_webui.VERSION}" in page
    assert isolated_webui.VERSION == open(os.path.join(os.path.dirname(isolated_webui.__file__), "..", "VERSION")).read().strip()


# --- validation ------------------------------------------------------------

@pytest.mark.parametrize("body, fragment", [
    ({"retention_days": 0}, "positive"),
    ({"retention_days": -3}, "positive"),
    ({"retention_days": "abc"}, "number"),
    ({"retention_days": None}, "number"),
    ({"cron_schedule": "* * *"}, "5 space-separated"),
    ({"cron_schedule": "61 * * * *"}, "not a valid"),
    ({"cron_schedule": "a b c d e"}, "not a valid"),
    ({"apps": {"nope": {}}}, "unknown app"),
    ({"apps": {"radarr": {"enabled": True}}}, "URL is required"),
    ({"apps": {"radarr": {"enabled": True, "url": "http://r"}}}, "API key is required"),
    ({"apps": {"seerr": {"enabled": True, "url": "http://s", "api_key": "k"}}}, "isn't available yet"),
    ({"destinations": {"nope": {}}}, "unknown destination"),
    ({"destinations": {"dropbox": {"enabled": True}}}, "connect it first"),
    ({"destinations": {"gdrive": {"enabled": True}}}, "Client ID is required"),
    ({"destinations": {"onedrive": {"enabled": True}}}, "connect it first"),
])
def test_invalid_settings_are_refused_and_nothing_is_saved(authed_client, isolated_webui, body, fragment):
    before = authed_client.get("/api/config").get_json()
    response = save(authed_client, body)
    assert response.status_code == 400 and fragment in response.get_json()["error"]
    assert authed_client.get("/api/config").get_json() == before


def test_an_app_without_a_required_key_can_still_be_saved_while_disabled(authed_client):
    assert save(authed_client, {"apps": {"radarr": {"enabled": False, "url": "http://r"}}}).status_code == 200


def test_tdarr_needs_no_api_key(authed_client):
    assert save(authed_client, {"apps": {"tdarr": {"enabled": True, "url": "http://tdarr:8266"}}}).status_code == 200


# --- saving ----------------------------------------------------------------

def test_settings_round_trip(authed_client):
    body = {
        "retention_days": 14, "cron_schedule": "30 2 * * 1", "notify_url": "https://ntfy.example/topic",
        "bazarr_backup_dir": "/mnt/bazarr",
        "apps": {"radarr": {"enabled": True, "url": "http://radarr:7878", "api_key": "radarr-key"}},
        "destinations": {"local": {"enabled": True, "path": "/data/backups"}},
    }
    assert save(authed_client, body).status_code == 200
    cfg = authed_client.get("/api/config").get_json()
    assert (cfg["retention_days"], cfg["cron_schedule"], cfg["notify_url"], cfg["bazarr_backup_dir"]) == (14, "30 2 * * 1", "https://ntfy.example/topic", "/mnt/bazarr")
    assert cfg["apps"]["radarr"] == {"enabled": True, "url": "http://radarr:7878", "api_key": "radarr-key", "username": "", "password": ""}
    assert cfg["destinations"]["local"]["path"] == "/data/backups"


def test_a_partial_save_leaves_everything_else_alone(authed_client):
    save(authed_client, {"retention_days": 30, "apps": {"sonarr": {"enabled": True, "url": "http://s", "api_key": "k"}}})
    save(authed_client, {"cron_schedule": "0 5 * * *"})
    cfg = authed_client.get("/api/config").get_json()
    assert cfg["retention_days"] == 30 and cfg["apps"]["sonarr"]["url"] == "http://s" and cfg["cron_schedule"] == "0 5 * * *"


def test_unknown_and_non_editable_fields_are_ignored(authed_client, isolated_webui):
    save(authed_client, {
        "apps": {"radarr": {"url": "http://r", "evil": "x", "__class__": "x"}},
        "destinations": {"gdrive": {"client_id": "id", "refresh_token": "stolen", "folder_id": "f"}, "local": {"bogus": 1}},
    })
    on_disk = stored(isolated_webui)
    assert "evil" not in on_disk["apps"]["radarr"]
    assert on_disk["destinations"]["gdrive"]["refresh_token"] == ""
    assert on_disk["destinations"]["gdrive"]["folder_id"] == ""
    assert "bogus" not in on_disk["destinations"]["local"]
    assert authed_client.get("/api/config").get_json()["destinations"]["gdrive"]["client_id"] == "id"


def test_secrets_are_encrypted_on_disk_but_the_ui_gets_them_back(authed_client, isolated_webui):
    save(authed_client, {
        "apps": {"radarr": {"enabled": True, "url": "http://r", "api_key": "radarr-key"}},
        "destinations": {"gdrive": {"client_secret": "google-secret"}},
    })
    raw = open(isolated_webui.CONFIG_PATH).read()
    assert "radarr-key" not in raw and "google-secret" not in raw and "enc:v1:" in raw
    cfg = authed_client.get("/api/config").get_json()
    assert cfg["apps"]["radarr"]["api_key"] == "radarr-key"
    assert cfg["destinations"]["gdrive"]["client_secret"] == "google-secret"


def test_saving_resyncs_the_rclone_remotes(authed_client, isolated_webui, monkeypatch):
    calls = []
    monkeypatch.setattr(destination_util, "sync", lambda cfg: calls.append(cfg))
    save(authed_client, {"retention_days": 9})
    assert len(calls) == 1 and calls[0]["retention_days"] == 9


# --- Test connection -------------------------------------------------------

class StubApp:
    def __init__(self, message="stub 1.0 reachable", error=None):
        self.message, self.error = message, error

    def test_connection(self):
        if self.error:
            raise self.error
        return self.message


def test_testing_an_unknown_or_unavailable_app(authed_client):
    assert authed_client.post("/api/test/nope", json={}).status_code == 404
    unavailable = authed_client.post("/api/test/seerr", json={"url": "http://s", "api_key": "k"}).get_json()
    assert unavailable["ok"] is False and "isn't available yet" in unavailable["message"]


def test_testing_needs_a_url_and_where_required_a_key(authed_client):
    assert authed_client.post("/api/test/radarr", json={"api_key": "k"}).status_code == 400
    missing_key = authed_client.post("/api/test/radarr", json={"url": "http://r"})
    assert missing_key.status_code == 400 and "API key" in missing_key.get_json()["message"]


def test_a_successful_test_reports_the_apps_own_message(authed_client, isolated_webui, monkeypatch):
    built = []
    monkeypatch.setattr(isolated_webui, "build_app", lambda name, cfg: built.append((name, cfg)) or StubApp())
    response = authed_client.post("/api/test/bazarr", json={"url": "http://b", "api_key": "k", "username": "u", "password": "p"})
    assert response.get_json() == {"ok": True, "message": "stub 1.0 reachable"}
    name, cfg = built[0]
    assert name == "bazarr" and (cfg["url"], cfg["api_key"], cfg["username"], cfg["password"]) == ("http://b", "k", "u", "p")


def test_tdarr_can_be_tested_without_a_key(authed_client, isolated_webui, monkeypatch):
    monkeypatch.setattr(isolated_webui, "build_app", lambda name, cfg: StubApp("tdarr reachable"))
    assert authed_client.post("/api/test/tdarr", json={"url": "http://t"}).get_json()["ok"] is True


@pytest.mark.parametrize("error, expected", [
    (requests.exceptions.ConnectionError("HTTPConnectionPool(host='x'): refused"), "couldn't connect"),
    (requests.exceptions.ReadTimeout("slow"), "couldn't connect"),
    (requests.exceptions.MissingSchema("no scheme"), "valid URL"),
    (RuntimeError("radarr: unauthorized - check the API key"), "unauthorized"),
])
def test_a_failed_test_explains_itself_without_raw_exceptions(authed_client, isolated_webui, monkeypatch, error, expected):
    monkeypatch.setattr(isolated_webui, "build_app", lambda name, cfg: StubApp(error=error))
    result = authed_client.post("/api/test/radarr", json={"url": "http://r", "api_key": "k"}).get_json()
    assert result["ok"] is False and expected in result["message"]
    assert "HTTPConnectionPool" not in result["message"]


# --- notifications ---------------------------------------------------------

def test_notification_test_needs_a_url(authed_client):
    assert authed_client.post("/api/test-notify", json={}).status_code == 400


def test_notification_test_sends_a_message_and_reports_failures(authed_client, isolated_webui, monkeypatch):
    sent = []
    monkeypatch.setattr(isolated_webui, "notify", lambda url, message, raise_on_error=False: sent.append((url, message, raise_on_error)))
    ok = authed_client.post("/api/test-notify", json={"notify_url": "https://ntfy.example/t"}).get_json()
    assert ok["ok"] is True and sent[0][0] == "https://ntfy.example/t" and sent[0][2] is True and "test notification" in sent[0][1]

    def fail(*args, **kwargs):
        raise requests.exceptions.ConnectionError("https://ntfy.example/t?token=secret refused")

    monkeypatch.setattr(isolated_webui, "notify", fail)
    bad = authed_client.post("/api/test-notify", json={"notify_url": "https://ntfy.example/t"}).get_json()
    assert bad["ok"] is False and "secret" not in bad["message"]


# --- destination tests -----------------------------------------------------

def test_destination_test_rejects_unknown_ids(authed_client):
    assert authed_client.post("/api/test-destination/nope").status_code == 404
    not_connected = authed_client.post("/api/test-destination/dropbox").get_json()
    assert not_connected["ok"] is False and "rclone authorize dropbox" in not_connected["message"]


def test_local_destination_test_writes_and_removes_a_probe(authed_client, tmp_path):
    target = tmp_path / "nested" / "backups"
    result = authed_client.post("/api/test-destination/local", json={"path": str(target)}).get_json()
    assert result["ok"] is True and str(target) in result["message"]
    assert target.is_dir() and list(target.iterdir()) == []


def test_local_destination_test_reports_an_unusable_path(authed_client, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    result = authed_client.post("/api/test-destination/local", json={"path": str(blocker / "sub")}).get_json()
    assert result["ok"] is False and result["message"]


def test_cloud_destinations_must_be_connected_first(authed_client):
    assert "Connect Google Drive" in authed_client.post("/api/test-destination/gdrive").get_json()["message"]
    assert "rclone authorize onedrive" in authed_client.post("/api/test-destination/onedrive").get_json()["message"]


@pytest.mark.parametrize("dest_id, label", [("onedrive", "OneDrive"), ("dropbox", "Dropbox")])
def test_testing_with_a_pasted_but_unconnected_token_points_at_connect(authed_client, dest_id, label):
    plain = authed_client.post(f"/api/test-destination/{dest_id}").get_json()
    assert plain["ok"] is False and f"rclone authorize {dest_id}" in plain["message"]

    pasted = authed_client.post(f"/api/test-destination/{dest_id}", json={"token_pasted": True}).get_json()
    assert pasted["ok"] is False
    assert f"click Connect {label}" in pasted["message"]


def test_token_pasted_cannot_make_a_destination_connected(authed_client, isolated_webui):
    authed_client.post("/api/test-destination/dropbox", json={"token_pasted": True, "token": "{}"})
    assert isolated_webui.load_config()["destinations"]["dropbox"]["token"] == ""


def test_cloud_destination_test_checks_the_remote(authed_client, isolated_webui, monkeypatch):
    checked = []
    monkeypatch.setattr(isolated_webui.rclone_util, "check_remote", checked.append)
    drive = {"refresh_token": "rt", "folder_name": "Backups", "client_id": "id"}
    result = authed_client.post("/api/test-destination/gdrive", json=drive).get_json()
    # refresh_token is not an editable field, so the request alone cannot make it "connected"
    assert result["ok"] is False and checked == []

    cfg = isolated_webui.load_config()
    cfg["destinations"]["gdrive"].update(refresh_token="rt", folder_name="Backups")
    cfg["destinations"]["onedrive"].update(token="{}")
    cfg["destinations"]["dropbox"].update(token="{}")
    isolated_webui.save_config(cfg)
    drive_result = authed_client.post("/api/test-destination/gdrive").get_json()
    assert drive_result["ok"] is True and '"Backups"' in drive_result["message"]
    one_result = authed_client.post("/api/test-destination/onedrive").get_json()
    assert one_result["ok"] is True and "OneDrive" in one_result["message"]
    dropbox_result = authed_client.post("/api/test-destination/dropbox").get_json()
    assert dropbox_result["ok"] is True and "Dropbox" in dropbox_result["message"]
    assert checked == ["backuparr-gdrive:", "backuparr-onedrive:", "backuparr-dropbox:Backuparr"]


def test_cloud_destination_test_reports_a_rejected_remote(authed_client, isolated_webui, monkeypatch):
    cfg = isolated_webui.load_config()
    cfg["destinations"]["gdrive"].update(refresh_token="rt")
    isolated_webui.save_config(cfg)

    def reject(root):
        raise isolated_webui.rclone_util.RcloneError("invalid_grant: token expired")

    monkeypatch.setattr(isolated_webui.rclone_util, "check_remote", reject)
    result = authed_client.post("/api/test-destination/gdrive").get_json()
    assert result["ok"] is False and "invalid_grant" in result["message"]


# --- helpers ---------------------------------------------------------------

def test_restore_override_fields_are_the_url_key_and_the_apps_own_extras(isolated_webui):
    fields = isolated_webui._restore_override_fields
    assert fields("radarr") == {"url", "api_key"}
    assert fields("bazarr") == {"url", "api_key", "username", "password"}
    assert fields("unknown") == {"url", "api_key"}


def test_the_session_key_is_created_once_private_and_reused(isolated_webui, tmp_path, monkeypatch):
    path = tmp_path / "keys" / "secret"
    monkeypatch.setenv("BACKUPARR_SECRET_KEY_PATH", str(path))
    first = isolated_webui._load_or_create_secret_key()
    assert len(first) == 32 and oct(path.stat().st_mode & 0o777) == "0o600"
    assert isolated_webui._load_or_create_secret_key() == first
    assert [p.name for p in path.parent.iterdir()] == ["secret"]


def test_a_failed_key_write_leaves_no_temporary_file(isolated_webui, tmp_path, monkeypatch):
    path = tmp_path / "keys2" / "secret"
    monkeypatch.setenv("BACKUPARR_SECRET_KEY_PATH", str(path))

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(isolated_webui.os, "replace", explode)
    with pytest.raises(OSError):
        isolated_webui._load_or_create_secret_key()
    assert list(path.parent.iterdir()) == []
