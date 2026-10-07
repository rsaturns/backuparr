"""Google Drive, OneDrive and Dropbox connection routes."""
import json
import time
import urllib.parse

import pytest
import requests

import dropbox_oauth
import gdrive_oauth
import onedrive_oauth


def gdrive(webui):
    return webui.load_config()["destinations"]["gdrive"]


def with_google_client(client, secret="client-secret"):
    assert client.post("/api/config", json={"destinations": {"gdrive": {"client_id": "client-id", "client_secret": secret}}}).status_code == 200


def query(location):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)


# --- Google Drive: starting ---------------------------------------------------

def test_connecting_needs_a_client_id_and_secret_first(authed_client):
    response = authed_client.get("/api/destinations/gdrive/oauth/start")
    assert response.status_code == 302
    assert query(response.headers["Location"])["gdrive_error"] == ["Save a Client ID and Client Secret first"]
    authed_client.post("/api/config", json={"destinations": {"gdrive": {"client_id": "only-id"}}})
    assert "gdrive_error" in response.headers["Location"]


def test_start_sends_the_user_to_google_with_a_fresh_state(authed_client, isolated_webui):
    with_google_client(authed_client)
    response = authed_client.get("/api/destinations/gdrive/oauth/start")
    location = response.headers["Location"]
    assert location.startswith("https://accounts.google.com/")
    params = query(location)
    assert params["client_id"] == ["client-id"] and params["access_type"] == ["offline"]
    assert params["redirect_uri"] == ["http://localhost/api/destinations/gdrive/oauth/callback"]
    assert params["scope"] == [gdrive_oauth.SCOPE] and params["response_type"] == ["code"]
    state = params["state"][0]
    assert len(state) >= 24 and state in isolated_webui._OAUTH_STATE
    again = query(authed_client.get("/api/destinations/gdrive/oauth/start").headers["Location"])["state"][0]
    assert again != state


# --- Google Drive: callback -----------------------------------------------------

def start_flow(client, webui):
    with_google_client(client)
    return query(client.get("/api/destinations/gdrive/oauth/start").headers["Location"])["state"][0]


def callback(client, **params):
    response = client.get("/api/destinations/gdrive/oauth/callback", query_string=params)
    assert response.status_code == 302
    return response.headers["Location"]


def test_google_can_report_that_the_user_declined(authed_client):
    assert query(callback(authed_client, error="access_denied"))["gdrive_error"] == ["access_denied"]


@pytest.mark.parametrize("params", [{}, {"code": "abc"}, {"state": "forged", "code": "abc"}, {"state": "forged"}])
def test_a_callback_without_a_valid_state_and_code_is_rejected(authed_client, isolated_webui, monkeypatch, params):
    exchanged = []
    monkeypatch.setattr(gdrive_oauth, "exchange_code", lambda *a: exchanged.append(a))
    assert "invalid or expired" in query(callback(authed_client, **params))["gdrive_error"][0]
    assert exchanged == []


def test_a_successful_callback_stores_the_refresh_token_and_enables_drive(authed_client, isolated_webui, monkeypatch):
    synced = []
    monkeypatch.setattr(isolated_webui.destination_util, "sync", lambda cfg: synced.append(cfg))
    monkeypatch.setattr(gdrive_oauth, "exchange_code", lambda cid, secret, redirect, code: {"refresh_token": f"rt-for-{code}", "client": (cid, secret)})
    state = start_flow(authed_client, isolated_webui)
    synced.clear()
    assert callback(authed_client, state=state, code="auth-code") == "/?gdrive=connected"
    cfg = gdrive(isolated_webui)
    assert cfg["refresh_token"] == "rt-for-auth-code" and cfg["enabled"] is True
    assert "rt-for-auth-code" not in open(isolated_webui.CONFIG_PATH).read()  # encrypted at rest
    assert len(synced) == 1


def test_the_redirect_uri_sent_to_google_matches_the_one_used_to_exchange(authed_client, isolated_webui, monkeypatch):
    seen = []
    monkeypatch.setattr(gdrive_oauth, "exchange_code", lambda cid, secret, redirect, code: seen.append(redirect) or {"refresh_token": "rt"})
    state = start_flow(authed_client, isolated_webui)
    sent_to_google = query(authed_client.get("/api/destinations/gdrive/oauth/start").headers["Location"])["redirect_uri"]
    callback(authed_client, state=state, code="c")
    assert seen == sent_to_google


def test_a_state_can_only_be_used_once(authed_client, isolated_webui, monkeypatch):
    calls = []
    monkeypatch.setattr(gdrive_oauth, "exchange_code", lambda *a: calls.append(a) or {"refresh_token": "rt"})
    state = start_flow(authed_client, isolated_webui)
    assert callback(authed_client, state=state, code="c") == "/?gdrive=connected"
    assert "invalid or expired" in query(callback(authed_client, state=state, code="c"))["gdrive_error"][0]
    assert len(calls) == 1


def test_an_old_state_expires(authed_client, isolated_webui, monkeypatch):
    monkeypatch.setattr(gdrive_oauth, "exchange_code", lambda *a: pytest.fail("must not exchange"))
    state = start_flow(authed_client, isolated_webui)
    isolated_webui._OAUTH_STATE[state] = time.time() - isolated_webui._OAUTH_STATE_TTL - 1
    assert "invalid or expired" in query(callback(authed_client, state=state, code="c"))["gdrive_error"][0]
    assert state not in isolated_webui._OAUTH_STATE


def test_a_failed_exchange_is_reported_without_leaking_and_connects_nothing(authed_client, isolated_webui, monkeypatch):
    def fail(*args):
        raise requests.exceptions.ConnectionError("https://oauth2.googleapis.com/token?client_secret=client-secret refused")

    monkeypatch.setattr(gdrive_oauth, "exchange_code", fail)
    state = start_flow(authed_client, isolated_webui)
    error = query(callback(authed_client, state=state, code="c"))["gdrive_error"][0]
    assert "couldn't connect" in error and "client-secret" not in error
    assert gdrive(isolated_webui)["refresh_token"] == "" and gdrive(isolated_webui)["enabled"] is False


def test_google_refusing_the_code_shows_googles_reason(authed_client, isolated_webui, monkeypatch):
    def refuse(*args):
        raise gdrive_oauth.GDriveOAuthError("Google didn't return a refresh token")

    monkeypatch.setattr(gdrive_oauth, "exchange_code", refuse)
    state = start_flow(authed_client, isolated_webui)
    assert "refresh token" in query(callback(authed_client, state=state, code="c"))["gdrive_error"][0]


def test_expired_states_are_swept_when_a_new_one_is_made(isolated_webui):
    isolated_webui._OAUTH_STATE["old"] = time.time() - isolated_webui._OAUTH_STATE_TTL - 5
    isolated_webui._OAUTH_STATE["recent"] = time.time()
    fresh = isolated_webui._oauth_state_new()
    assert set(isolated_webui._OAUTH_STATE) == {"recent", fresh}


# --- Google Drive: token, folder, disconnect -------------------------------------

def connect_drive(webui):
    cfg = webui.load_config()
    cfg["destinations"]["gdrive"].update(client_id="id", client_secret="secret", refresh_token="rt", enabled=True)
    webui.save_config(cfg)


def test_the_picker_gets_a_short_lived_token_never_the_refresh_token(authed_client, isolated_webui, monkeypatch):
    connect_drive(isolated_webui)
    monkeypatch.setattr(gdrive_oauth, "refresh_access_token", lambda cid, secret, rt: {"access_token": f"access-from-{rt}"})
    response = authed_client.post("/api/destinations/gdrive/access-token")
    assert response.get_json() == {"access_token": "access-from-rt"} and "rt\"" not in response.get_data(as_text=True).replace("access-from-rt", "")


def test_the_picker_token_fails_cleanly_when_not_connected_or_rejected(authed_client, isolated_webui, monkeypatch):
    unconnected = authed_client.post("/api/destinations/gdrive/access-token")
    assert unconnected.status_code == 400 and "not connected" in unconnected.get_json()["error"]
    connect_drive(isolated_webui)

    def revoked(*args):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(gdrive_oauth, "refresh_access_token", revoked)
    assert authed_client.post("/api/destinations/gdrive/access-token").status_code == 400


def test_choosing_a_drive_folder(authed_client, isolated_webui):
    assert authed_client.post("/api/destinations/gdrive/folder", json={"folder_name": "x"}).status_code == 400
    not_connected = authed_client.post("/api/destinations/gdrive/folder", json={"folder_id": "f1", "folder_name": "Backups"})
    assert not_connected.status_code == 400 and "not connected" in not_connected.get_json()["error"]
    connect_drive(isolated_webui)
    assert authed_client.post("/api/destinations/gdrive/folder", json={"folder_id": "f1", "folder_name": "Backups"}).get_json() == {"ok": True}
    assert (gdrive(isolated_webui)["folder_id"], gdrive(isolated_webui)["folder_name"]) == ("f1", "Backups")


def test_disconnecting_drive_forgets_the_account_but_keeps_the_client(authed_client, isolated_webui):
    connect_drive(isolated_webui)
    authed_client.post("/api/destinations/gdrive/folder", json={"folder_id": "f1", "folder_name": "Backups"})
    assert authed_client.post("/api/destinations/gdrive/disconnect").get_json() == {"ok": True}
    cfg = gdrive(isolated_webui)
    assert (cfg["enabled"], cfg["refresh_token"], cfg["folder_id"], cfg["folder_name"]) == (False, "", "", "")
    assert (cfg["client_id"], cfg["client_secret"]) == ("id", "secret")


# --- OneDrive ----------------------------------------------------------------------

APPROOT = {"id": "item-1", "parentReference": {"driveId": "drive-1", "driveType": "personal"}}


def onedrive(webui):
    return webui.load_config()["destinations"]["onedrive"]


def test_connecting_onedrive_stores_the_token_and_the_app_folder(authed_client, isolated_webui, monkeypatch):
    synced = []
    monkeypatch.setattr(onedrive_oauth, "parse_token_blob", lambda blob: ('{"access_token":"a","refresh_token":"r"}', "a"))
    monkeypatch.setattr(onedrive_oauth, "approot_metadata", lambda access: APPROOT if access == "a" else pytest.fail("wrong token"))
    monkeypatch.setattr(onedrive_oauth, "sync_rclone_remote", lambda cfg, force=False: synced.append((cfg["drive_id"], force)))
    response = authed_client.post("/api/destinations/onedrive/connect", json={"token_blob": "pasted"})
    assert response.get_json() == {"ok": True}
    cfg = onedrive(isolated_webui)
    assert (cfg["drive_id"], cfg["drive_type"], cfg["item_id"], cfg["enabled"]) == ("drive-1", "personal", "item-1", True)
    assert json.loads(cfg["token"])["refresh_token"] == "r"
    assert synced == [("drive-1", True)]  # a freshly pasted token must replace the stored one


@pytest.mark.parametrize("blob", ["", "not a token", "   "])
def test_a_bad_onedrive_token_is_refused_and_changes_nothing(authed_client, isolated_webui, blob):
    response = authed_client.post("/api/destinations/onedrive/connect", json={"token_blob": blob})
    assert response.status_code == 400 and response.get_json()["error"]
    assert onedrive(isolated_webui)["token"] == "" and onedrive(isolated_webui)["enabled"] is False


def test_microsoft_rejecting_the_token_is_reported(authed_client, isolated_webui, monkeypatch):
    monkeypatch.setattr(onedrive_oauth, "parse_token_blob", lambda blob: ("{}", "a"))

    def refuse(access):
        raise onedrive_oauth.OneDriveOAuthError("could not read the app folder: 401")

    monkeypatch.setattr(onedrive_oauth, "approot_metadata", refuse)
    response = authed_client.post("/api/destinations/onedrive/connect", json={"token_blob": "x"})
    assert response.status_code == 400 and "app folder" in response.get_json()["error"]
    assert onedrive(isolated_webui)["token"] == ""


def test_disconnecting_onedrive_clears_the_connection(authed_client, isolated_webui, monkeypatch):
    monkeypatch.setattr(onedrive_oauth, "parse_token_blob", lambda blob: ("{}", "a"))
    monkeypatch.setattr(onedrive_oauth, "approot_metadata", lambda access: APPROOT)
    monkeypatch.setattr(onedrive_oauth, "sync_rclone_remote", lambda cfg, force=False: None)
    authed_client.post("/api/destinations/onedrive/connect", json={"token_blob": "x"})
    assert authed_client.post("/api/destinations/onedrive/disconnect").get_json() == {"ok": True}
    cfg = onedrive(isolated_webui)
    assert (cfg["enabled"], cfg["token"], cfg["drive_id"], cfg["drive_type"], cfg["item_id"]) == (False, "", "", "", "")


# --- Dropbox -----------------------------------------------------------------------

def dropbox(webui):
    return webui.load_config()["destinations"]["dropbox"]


def test_connecting_dropbox_verifies_then_stores_the_token(authed_client, isolated_webui, monkeypatch):
    synced, verified = [], []
    monkeypatch.setattr(dropbox_oauth, "parse_token_blob", lambda blob: ('{"access_token":"a","refresh_token":"r"}', "a"))
    monkeypatch.setattr(dropbox_oauth, "verify_access_token", verified.append)
    monkeypatch.setattr(dropbox_oauth, "sync_rclone_remote", lambda cfg, force=False: synced.append(force))
    response = authed_client.post("/api/destinations/dropbox/connect", json={"token_blob": "pasted"})
    assert response.get_json() == {"ok": True}
    cfg = dropbox(isolated_webui)
    assert cfg["enabled"] is True and json.loads(cfg["token"])["refresh_token"] == "r"
    assert verified == ["a"]
    assert synced == [True]  # a freshly pasted token must replace the stored one


@pytest.mark.parametrize("blob", ["", "not a token", "   "])
def test_a_bad_dropbox_token_is_refused_and_changes_nothing(authed_client, isolated_webui, blob):
    response = authed_client.post("/api/destinations/dropbox/connect", json={"token_blob": blob})
    assert response.status_code == 400 and "rclone authorize dropbox" in response.get_json()["error"]
    assert dropbox(isolated_webui)["token"] == "" and dropbox(isolated_webui)["enabled"] is False


def test_dropbox_rejecting_the_token_is_reported_and_nothing_is_saved(authed_client, isolated_webui, monkeypatch):
    monkeypatch.setattr(dropbox_oauth, "parse_token_blob", lambda blob: ("{}", "a"))

    def refuse(access):
        raise dropbox_oauth.DropboxOAuthError("Dropbox rejected that token: invalid_access_token")

    monkeypatch.setattr(dropbox_oauth, "verify_access_token", refuse)
    response = authed_client.post("/api/destinations/dropbox/connect", json={"token_blob": "x"})
    assert response.status_code == 400 and "rejected" in response.get_json()["error"]
    assert dropbox(isolated_webui)["token"] == ""


def test_disconnecting_dropbox_clears_the_connection(authed_client, isolated_webui, monkeypatch):
    monkeypatch.setattr(dropbox_oauth, "parse_token_blob", lambda blob: ("{}", "a"))
    monkeypatch.setattr(dropbox_oauth, "verify_access_token", lambda access: None)
    monkeypatch.setattr(dropbox_oauth, "sync_rclone_remote", lambda cfg, force=False: None)
    authed_client.post("/api/destinations/dropbox/connect", json={"token_blob": "x"})
    assert authed_client.post("/api/destinations/dropbox/disconnect").get_json() == {"ok": True}
    cfg = dropbox(isolated_webui)
    assert (cfg["enabled"], cfg["token"]) == (False, "")


def test_the_dropbox_token_is_encrypted_in_config_json(authed_client, isolated_webui, monkeypatch):
    secret = '{"access_token":"plain-access","refresh_token":"plain-refresh"}'
    monkeypatch.setattr(dropbox_oauth, "parse_token_blob", lambda blob: (secret, "a"))
    monkeypatch.setattr(dropbox_oauth, "verify_access_token", lambda access: None)
    monkeypatch.setattr(dropbox_oauth, "sync_rclone_remote", lambda cfg, force=False: None)
    authed_client.post("/api/destinations/dropbox/connect", json={"token_blob": "x"})
    with open(isolated_webui.CONFIG_PATH) as f:
        on_disk = f.read()
    assert "plain-refresh" not in on_disk and "plain-access" not in on_disk
    assert dropbox(isolated_webui)["token"] == secret
