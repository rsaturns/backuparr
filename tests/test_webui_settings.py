"""Settings and the connectivity-test endpoints."""
import json
import os
import re

import pytest
import requests

import destination_util
from config_store import APP_CATEGORIES, APP_META, APP_NAMES, DEST_NAMES, DESTINATION_META, SELF_APP


def save(client, body):
    return client.post("/api/config", json=body)


def stored(webui):
    with open(webui.auth.CONFIG_PATH) as f:
        return json.load(f)


# --- reading ---------------------------------------------------------------

def test_a_new_install_has_every_app_off_and_local_storage_on(authed_client):
    cfg = authed_client.get("/api/config").get_json()
    assert set(cfg["apps"]) == set(APP_NAMES) and set(cfg["destinations"]) == set(DEST_NAMES)
    assert not any(app["enabled"] for app in cfg["apps"].values())
    assert cfg["destinations"]["local"]["enabled"] is True
    assert cfg["retention_days"] == 7 and cfg["cron_schedule"] == "0 3 * * *"


def test_the_config_says_whether_the_saved_schedule_is_valid(authed_client, isolated_webui):
    assert authed_client.get("/api/config").get_json()["schedule_valid"] is True
    cfg = isolated_webui.settings.load_config()
    cfg["cron_schedule"] = "0 0 * * 1#0"  # croniter 7.0 refuses it; 6.x took it
    isolated_webui.settings.save_config(cfg)
    assert authed_client.get("/api/config").get_json()["schedule_valid"] is False
    assert "schedule_valid" not in stored(isolated_webui)


def test_the_page_describes_what_the_ui_can_configure(authed_client):
    meta = ui_meta(authed_client)
    assert {a["id"] for a in meta["apps"]} == set(APP_NAMES)
    assert [d["id"] for d in meta["destinations"]] == DEST_NAMES


def test_the_dashboard_shows_the_release_version(authed_client, isolated_webui):
    page = authed_client.get("/").get_data(as_text=True)
    assert f"v{isolated_webui.VERSION}" in page
    assert isolated_webui.VERSION == open(os.path.join(os.path.dirname(isolated_webui.__file__), "..", "VERSION")).read().strip()


@pytest.mark.parametrize("field, input_type", [
    ("developer_key", "password"),
    ("client_secret", "password"),
    ("client_id", "text"),
])
def test_google_drive_credentials_use_appropriate_input_types(authed_client, field, input_type):
    response = authed_client.get("/")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    tag = re.search(rf'<input\b[^>]*data-field="{field}"[^>]*>', page)
    assert tag is not None
    assert f'type="{input_type}"' in tag.group(0)
    assert 'class="d-field"' in tag.group(0)


# --- validation ------------------------------------------------------------

@pytest.mark.parametrize("body, fragment", [
    ({"retention_days": 0}, "positive"),
    ({"retention_days": -3}, "positive"),
    ({"retention_days": "abc"}, "number"),
    ({"retention_days": None}, "number"),
    ({"cron_schedule": "* * *"}, "5 space-separated"),
    ({"cron_schedule": "61 * * * *"}, "not a valid"),
    ({"cron_schedule": "a b c d e"}, "not a valid"),
    ({"cron_schedule": "\uff10 \uff10 * * *"}, "not a valid"),  # fullwidth digits, refused since croniter 7.0
    ({"apps": {"nope": {}}}, "unknown app"),
    ({"apps": {"radarr": {"enabled": True}}}, "URL is required"),
    ({"apps": {"radarr": {"enabled": True, "url": "http://r"}}}, "API key is required"),
    ({"apps": {"seerr": {"enabled": True, "url": "http://s", "api_key": "k"}}}, "Data folder is required"),
    ({"apps": {"lazylibrarian": {"enabled": True, "url": "http://l", "api_key": "k"}}}, "Data folder is required"),
    ({"destinations": {"nope": {}}}, "unknown destination"),
    ({"destinations": {"local": {"retention_days": 0}}}, "local: retention_days must be a positive number"),
    ({"destinations": {"gdrive": {"retention_days": -1}}}, "gdrive: retention_days must be a positive number"),
    ({"destinations": {"dropbox": {"retention_days": "abc"}}}, "dropbox: retention_days must be a number"),
    ({"destinations": {"onedrive": {"retention_days": True}}}, "onedrive: retention_days must be a positive number"),
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


def test_jellyfin_needs_its_backups_folder_and_keeps_it(authed_client, isolated_webui):
    app = {"enabled": True, "url": "http://jellyfin:8096", "api_key": "k"}
    refused = save(authed_client, {"apps": {"jellyfin": app}})
    assert refused.status_code == 400 and refused.get_json()["error"] == "jellyfin: Backups folder is required to enable it"
    assert save(authed_client, {"apps": {"jellyfin": {**app, "backup_dir": "/mnt/jellyfin-backups"}}}).status_code == 200
    assert stored(isolated_webui)["apps"]["jellyfin"]["backup_dir"] == "/mnt/jellyfin-backups"


def test_emby_needs_its_data_folder_and_keeps_it(authed_client, isolated_webui):
    app = {"enabled": True, "url": "http://emby:8096", "api_key": "k"}
    refused = save(authed_client, {"apps": {"emby": app}})
    assert refused.status_code == 400 and refused.get_json()["error"] == "emby: Data folder is required to enable it"
    assert save(authed_client, {"apps": {"emby": {**app, "data_dir": "/mnt/emby"}}}).status_code == 200
    assert stored(isolated_webui)["apps"]["emby"]["data_dir"] == "/mnt/emby"


def test_seerr_needs_its_data_folder_and_keeps_it(authed_client, isolated_webui):
    app = {"enabled": True, "url": "http://seerr:5055", "api_key": "k"}
    assert save(authed_client, {"apps": {"seerr": app}}).status_code == 400
    assert save(authed_client, {"apps": {"seerr": {**app, "data_dir": "/mnt/seerr"}}}).status_code == 200
    assert stored(isolated_webui)["apps"]["seerr"]["data_dir"] == "/mnt/seerr"


def test_nzbget_needs_no_api_key_and_keeps_its_credentials(authed_client, isolated_webui):
    app = {"enabled": True, "url": "http://nzbget:6789", "username": "nzbget", "password": "pass"}
    assert save(authed_client, {"apps": {"nzbget": app}}).status_code == 200
    on_disk = stored(isolated_webui)["apps"]["nzbget"]
    assert on_disk["username"] == "nzbget" and on_disk["password"].startswith("enc:")
    assert {k: authed_client.get("/api/config").get_json()["apps"]["nzbget"][k] for k in app} == app


def test_the_nzbget_card_has_no_api_key_field(authed_client):
    page = authed_client.get("/").get_data(as_text=True)
    card = page[page.index('data-app="nzbget"'):]
    card = card[:card.index('class="app-card-actions"')]
    assert 'class="f-api_key"' not in card
    assert 'data-field="username"' in card and 'type="password" class="f-extra" data-field="password"' in card


def test_plex_needs_its_token_and_data_folder_and_keeps_them(authed_client, isolated_webui):
    app = {"enabled": True, "url": "http://plex:32400"}
    refused = save(authed_client, {"apps": {"plex": app}})
    assert refused.status_code == 400 and refused.get_json()["error"] == "plex: Plex token is required to enable it"
    refused = save(authed_client, {"apps": {"plex": {**app, "api_key": "k"}}})
    assert refused.status_code == 400 and refused.get_json()["error"] == "plex: Data folder is required to enable it"
    assert save(authed_client, {"apps": {"plex": {**app, "api_key": "k", "data_dir": "/mnt/plex"}}}).status_code == 200
    assert stored(isolated_webui)["apps"]["plex"]["data_dir"] == "/mnt/plex"


def test_an_apps_extra_fields_are_saved_only_for_that_app(authed_client, isolated_webui):
    save(authed_client, {"apps": {"radarr": {"url": "http://r", "backup_dir": "/x"}}})
    assert "backup_dir" not in stored(isolated_webui)["apps"]["radarr"]


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
    assert cfg["apps"]["radarr"] == {"enabled": True, "url": "http://radarr:7878", "api_key": "radarr-key", "username": "", "password": "",
                                     "encrypt": True}
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
    raw = open(isolated_webui.auth.CONFIG_PATH).read()
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


def test_testing_an_unknown_app(authed_client):
    assert authed_client.post("/api/test/nope", json={}).status_code == 404


def test_testing_needs_a_url_and_where_required_a_key(authed_client):
    assert authed_client.post("/api/test/radarr", json={"api_key": "k"}).status_code == 400
    missing_key = authed_client.post("/api/test/radarr", json={"url": "http://r"})
    assert missing_key.status_code == 400 and "API key" in missing_key.get_json()["message"]


def test_a_successful_test_reports_the_apps_own_message(authed_client, isolated_webui, monkeypatch):
    built = []
    monkeypatch.setattr(isolated_webui.settings, "build_app", lambda name, cfg: built.append((name, cfg)) or StubApp())
    response = authed_client.post("/api/test/bazarr", json={"url": "http://b", "api_key": "k", "username": "u", "password": "p"})
    assert response.get_json() == {"ok": True, "message": "stub 1.0 reachable"}
    name, cfg = built[0]
    assert name == "bazarr" and (cfg["url"], cfg["api_key"], cfg["username"], cfg["password"]) == ("http://b", "k", "u", "p")


def test_tdarr_can_be_tested_without_a_key(authed_client, isolated_webui, monkeypatch):
    monkeypatch.setattr(isolated_webui.settings, "build_app", lambda name, cfg: StubApp("tdarr reachable"))
    assert authed_client.post("/api/test/tdarr", json={"url": "http://t"}).get_json()["ok"] is True


@pytest.mark.parametrize("error, expected", [
    (requests.exceptions.ConnectionError("HTTPConnectionPool(host='x'): refused"), "couldn't connect"),
    (requests.exceptions.ReadTimeout("slow"), "couldn't connect"),
    (requests.exceptions.MissingSchema("no scheme"), "valid URL"),
    (RuntimeError("radarr: unauthorized - check the API key"), "unauthorized"),
])
def test_a_failed_test_explains_itself_without_raw_exceptions(authed_client, isolated_webui, monkeypatch, error, expected):
    monkeypatch.setattr(isolated_webui.settings, "build_app", lambda name, cfg: StubApp(error=error))
    result = authed_client.post("/api/test/radarr", json={"url": "http://r", "api_key": "k"}).get_json()
    assert result["ok"] is False and expected in result["message"]
    assert "HTTPConnectionPool" not in result["message"]


def test_a_test_that_finds_the_apps_login_tells_the_card_to_ask_for_it(authed_client, isolated_webui, monkeypatch):
    from apps.servarr import LoginRequiredError
    error = LoginRequiredError("radarr 6.4 asks for a login. Enter its username and password.")
    monkeypatch.setattr(isolated_webui.settings, "build_app", lambda name, cfg: StubApp(error=error))
    result = authed_client.post("/api/test/radarr", json={"url": "http://r", "api_key": "k"}).get_json()
    assert result == {"ok": False, "needs_login": True, "message": "radarr 6.4 asks for a login. Enter its username and password."}
    monkeypatch.setattr(isolated_webui.settings, "build_app", lambda name, cfg: StubApp(error=RuntimeError("radarr: down")))
    assert "needs_login" not in authed_client.post("/api/test/radarr", json={"url": "http://r", "api_key": "k"}).get_json()


@pytest.mark.parametrize("app", ["radarr", "sonarr", "lidarr", "whisparr", "prowlarr"])
def test_servarr_cards_have_hidden_login_fields(authed_client, app):
    html = authed_client.get("/").get_data(as_text=True)
    card = html[html.index(f'data-app="{app}"'):]
    card = card[:card.index("app-card-actions")]
    assert card.count('class="field login-field hidden"') == 2
    assert 'data-field="username"' in card and 'type="password" class="f-extra" data-field="password"' in card


# --- notifications ---------------------------------------------------------

def test_notification_test_needs_a_url(authed_client):
    assert authed_client.post("/api/test-notify", json={}).status_code == 400


def test_notification_test_sends_a_message_and_reports_failures(authed_client, isolated_webui, monkeypatch):
    sent = []
    monkeypatch.setattr(isolated_webui.settings, "notify", lambda url, message, raise_on_error=False: sent.append((url, message, raise_on_error)))
    ok = authed_client.post("/api/test-notify", json={"notify_url": "https://ntfy.example/t"}).get_json()
    assert ok["ok"] is True and sent[0][0] == "https://ntfy.example/t" and sent[0][2] is True and "test notification" in sent[0][1]

    def fail(*args, **kwargs):
        raise requests.exceptions.ConnectionError("https://ntfy.example/t?token=secret refused")

    monkeypatch.setattr(isolated_webui.settings, "notify", fail)
    bad = authed_client.post("/api/test-notify", json={"notify_url": "https://ntfy.example/t"}).get_json()
    assert bad["ok"] is False and "secret" not in bad["message"]


# --- destination tests -----------------------------------------------------

def test_destination_test_rejects_unknown_ids(authed_client):
    assert authed_client.post("/api/test-destination/nope", json={}).status_code == 404
    not_connected = authed_client.post("/api/test-destination/dropbox", json={}).get_json()
    assert not_connected["ok"] is False and "rclone authorize dropbox" in not_connected["message"]


def test_local_destination_test_writes_and_removes_a_probe(authed_client, tmp_path):
    target = tmp_path / "nested" / "backups"
    result = authed_client.post("/api/test-destination/local", json={"path": str(target)}).get_json()
    assert result["ok"] is True and str(target) in result["message"]
    # Only the file it reads to wake a sleeping disk (#140) stays.
    assert target.is_dir() and [p.name for p in target.iterdir()] == [".backuparr-wake"]


def test_local_destination_test_reports_an_unusable_path(authed_client, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    result = authed_client.post("/api/test-destination/local", json={"path": str(blocker / "sub")}).get_json()
    assert result["ok"] is False and result["message"]


def test_local_destination_test_explains_a_folder_it_cant_write(authed_client, tmp_path, monkeypatch):
    real_open = open

    def denied(path, *args, **kwargs):
        if str(path).endswith(".backuparr-write-test"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    message = authed_client.post("/api/test-destination/local", json={"path": str(tmp_path)}).get_json()["message"]
    assert message.startswith("permission denied - Backuparr runs as PUID:PGID") and "Errno" not in message


def test_cloud_destinations_must_be_connected_first(authed_client):
    assert "Connect Google Drive" in authed_client.post("/api/test-destination/gdrive", json={}).get_json()["message"]
    assert "rclone authorize onedrive" in authed_client.post("/api/test-destination/onedrive", json={}).get_json()["message"]


@pytest.mark.parametrize("dest_id, label", [("onedrive", "OneDrive"), ("dropbox", "Dropbox")])
def test_testing_with_a_pasted_but_unconnected_token_points_at_connect(authed_client, dest_id, label):
    plain = authed_client.post(f"/api/test-destination/{dest_id}", json={}).get_json()
    assert plain["ok"] is False and f"rclone authorize {dest_id}" in plain["message"]

    pasted = authed_client.post(f"/api/test-destination/{dest_id}", json={"token_pasted": True}).get_json()
    assert pasted["ok"] is False
    assert f"click Connect {label}" in pasted["message"]


def test_token_pasted_cannot_make_a_destination_connected(authed_client, isolated_webui):
    authed_client.post("/api/test-destination/dropbox", json={"token_pasted": True, "token": "{}"})
    assert isolated_webui.settings.load_config()["destinations"]["dropbox"]["token"] == ""


def test_cloud_destination_test_checks_the_remote(authed_client, isolated_webui, monkeypatch):
    checked = []
    monkeypatch.setattr(isolated_webui.backups.rclone_util, "check_remote", checked.append)
    drive = {"refresh_token": "rt", "folder_name": "Backups", "client_id": "id"}
    result = authed_client.post("/api/test-destination/gdrive", json=drive).get_json()
    # refresh_token is not an editable field, so the request alone cannot make it "connected"
    assert result["ok"] is False and checked == []

    cfg = isolated_webui.settings.load_config()
    cfg["destinations"]["gdrive"].update(refresh_token="rt", folder_name="Backups")
    cfg["destinations"]["onedrive"].update(token="{}")
    cfg["destinations"]["dropbox"].update(token="{}")
    isolated_webui.settings.save_config(cfg)
    drive_result = authed_client.post("/api/test-destination/gdrive", json={}).get_json()
    assert drive_result["ok"] is True and '"Backups"' in drive_result["message"]
    one_result = authed_client.post("/api/test-destination/onedrive", json={}).get_json()
    assert one_result["ok"] is True and "OneDrive" in one_result["message"]
    dropbox_result = authed_client.post("/api/test-destination/dropbox", json={}).get_json()
    assert dropbox_result["ok"] is True and "Dropbox" in dropbox_result["message"]
    assert checked == ["backuparr-gdrive:", "backuparr-onedrive:", "backuparr-dropbox:Backuparr"]


def test_cloud_destination_test_reports_a_rejected_remote(authed_client, isolated_webui, monkeypatch):
    cfg = isolated_webui.settings.load_config()
    cfg["destinations"]["gdrive"].update(refresh_token="rt")
    isolated_webui.settings.save_config(cfg)

    def reject(root):
        raise isolated_webui.backups.rclone_util.RcloneError(
            "rclone lsd --max-depth 1 backuparr-gdrive: failed: invalid_grant", "invalid_grant (reconnect it in Settings)")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "check_remote", reject)
    result = authed_client.post("/api/test-destination/gdrive", json={}).get_json()
    # The cause, not rclone's command line.
    assert result == {"ok": False, "message": "invalid_grant (reconnect it in Settings)"}


# --- helpers ---------------------------------------------------------------

def test_restore_override_fields_are_the_url_key_and_the_apps_own_extras(isolated_webui):
    fields = isolated_webui.settings._restore_override_fields
    assert fields("radarr") == {"url", "api_key"}  # its web login is never needed to restore
    assert fields("bazarr") == {"url", "api_key", "username", "password"}
    assert fields("jellyfin") == {"url", "api_key", "backup_dir"}
    assert fields("emby") == {"url", "api_key", "data_dir"}
    assert fields("plex") == {"url", "api_key", "data_dir"}
    assert fields("seerr") == {"url", "api_key", "data_dir"}
    assert fields("lazylibrarian") == {"url", "api_key", "data_dir"}
    assert fields("nzbget") == {"url", "api_key", "username", "password"}
    assert fields("pihole") == {"url", "api_key"}
    assert fields("vaultwarden") == {"url", "api_key", "data_dir"}
    assert fields("immich") == {"url", "api_key"}
    assert fields("homeassistant") == {"url", "api_key"}  # its backup options aren't asked for
    assert fields("unknown") == {"url", "api_key"}


def test_the_session_key_is_created_once_private_and_reused(isolated_webui, tmp_path, monkeypatch):
    path = tmp_path / "keys" / "secret"
    monkeypatch.setattr(isolated_webui.auth, "SECRET_KEY_PATH", str(path))
    first = isolated_webui.auth._load_or_create_secret_key()
    assert len(first) == 32 and oct(path.stat().st_mode & 0o777) == "0o600"
    assert isolated_webui.auth._load_or_create_secret_key() == first
    assert [p.name for p in path.parent.iterdir()] == ["secret"]


def test_a_failed_key_write_leaves_no_temporary_file(isolated_webui, tmp_path, monkeypatch):
    path = tmp_path / "keys2" / "secret"
    monkeypatch.setattr(isolated_webui.auth, "SECRET_KEY_PATH", str(path))

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(isolated_webui.auth.os, "replace", explode)
    with pytest.raises(OSError):
        isolated_webui.auth._load_or_create_secret_key()
    assert list(path.parent.iterdir()) == []


def test_oauth_tokens_are_reported_as_connected_never_sent(authed_client, isolated_webui):
    cfg = isolated_webui.settings.load_config()
    cfg["destinations"]["gdrive"].update(client_id="id", refresh_token="gdrive-refresh")
    cfg["destinations"]["onedrive"]["token"] = '{"refresh_token":"onedrive-refresh"}'
    isolated_webui.settings.save_config(cfg)
    response = authed_client.get("/api/config")
    body = response.get_data(as_text=True)
    assert "gdrive-refresh" not in body and "onedrive-refresh" not in body
    dests = response.get_json()["destinations"]
    assert (dests["gdrive"]["refresh_token"], dests["onedrive"]["token"], dests["dropbox"]["token"]) == (True, True, False)
    # Saving the settings the UI got back leaves the stored tokens alone.
    assert save(authed_client, {"destinations": dests}).status_code == 200
    stored_cfg = isolated_webui.settings.load_config()["destinations"]
    assert stored_cfg["gdrive"]["refresh_token"] == "gdrive-refresh"
    assert stored_cfg["onedrive"]["token"] == '{"refresh_token":"onedrive-refresh"}'


def test_importing_the_web_app_has_no_side_effects(tmp_path, monkeypatch):
    import importlib
    import sys
    import threading
    monkeypatch.setenv("BACKUPARR_SECRET_KEY_PATH", str(tmp_path / "secret_key"))
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    importlib.reload(sys.modules["webui.app"]) if "webui.app" in sys.modules else importlib.import_module("webui.app")
    assert started == [] and not (tmp_path / "secret_key").exists()


def test_create_app_loads_the_session_key_and_starts_the_scheduler(isolated_webui, monkeypatch):
    calls = []
    monkeypatch.setattr(isolated_webui.backup, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(isolated_webui.runs, "start_scheduler", lambda: calls.append("scheduler"))
    isolated_webui.app.secret_key = None
    assert isolated_webui.create_app() is isolated_webui.app
    assert calls == ["logging", "scheduler"] and isolated_webui.app.secret_key == isolated_webui.auth._load_or_create_secret_key()


def ui_meta(client):
    """The metadata app.js reads from the dashboard page's data-ui-meta."""
    import html
    import json
    import re
    page = client.get("/").get_data(as_text=True)
    return json.loads(html.unescape(re.search(r"data-ui-meta='([^']*)'", page).group(1)))


def test_the_page_carries_the_ui_metadata_as_data(authed_client):
    assert ui_meta(authed_client) == {"apps": APP_META, "categories": APP_CATEGORIES, "destinations": DESTINATION_META,
                                      "self_app": SELF_APP}


def test_the_overview_has_its_status_bar_under_at_a_glance(authed_client):
    """#167: app.js fills it; it sits between At a Glance and the app cards."""
    html = authed_client.get("/").get_data(as_text=True)
    assert html.index("At a Glance") < html.index('id="overview-status"') < html.index('id="overview-apps"')


def test_backuparrs_own_card_heads_server_and_is_never_an_app(authed_client):
    """#165: its switch is self_backup, out of Schedule & Notifications; the
    card has no data-app, so Save never sends it as an app."""
    html = authed_client.get("/").get_data(as_text=True)
    server = html.split('data-category="server"', 1)[1].split("</section>", 1)[0]
    assert 'data-self-app="backuparr"' in server and 'id="s-self_backup"' in server
    assert "Always encrypted for safety." in server and 'data-app="backuparr"' not in html
    assert html.count('id="s-self_backup"') == 1
    assert "own configuration, encrypted with your encryption key" not in html


# --- each destination's own retention (#133) -------------------------------

def test_a_destination_starts_with_no_retention_of_its_own(authed_client):
    cfg = authed_client.get("/api/config").get_json()
    assert all(cfg["destinations"][d]["retention_days"] is None for d in DEST_NAMES)


def test_a_destinations_retention_saves_as_a_number_and_clears_with_null(authed_client, isolated_webui):
    assert save(authed_client, {"destinations": {"local": {"retention_days": "28"}, "gdrive": {"retention_days": 7}}}).status_code == 200
    days = {d: stored(isolated_webui)["destinations"][d]["retention_days"] for d in ("local", "gdrive", "dropbox")}
    assert days == {"local": 28, "gdrive": 7, "dropbox": None}
    assert save(authed_client, {"destinations": {"local": {"retention_days": None}}}).status_code == 200
    assert stored(isolated_webui)["destinations"]["local"]["retention_days"] is None
    assert stored(isolated_webui)["destinations"]["gdrive"]["retention_days"] == 7


def test_saving_a_destination_without_retention_keeps_its_days(authed_client, isolated_webui):
    save(authed_client, {"destinations": {"local": {"retention_days": 28}}})
    save(authed_client, {"destinations": {"local": {"enabled": True, "path": ""}}})
    assert stored(isolated_webui)["destinations"]["local"]["retention_days"] == 28


def test_every_destination_card_has_a_retention_field(authed_client):
    html = authed_client.get("/").get_data(as_text=True)
    for dest_id in DEST_NAMES:
        card = html.split(f'data-dest="{dest_id}"', 1)[1].split('class="app-card dest-card"', 1)[0]
        assert 'class="d-retention"' in card
