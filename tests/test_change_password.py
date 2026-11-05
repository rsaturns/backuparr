"""Changing the admin password from the user menu (#120)."""
import importlib
import sys

import pytest

import auth_store

OLD = "test-password"  # conftest's authed_client signs in with it
NEW = "a-brand-new-password"


def change(client, current=OLD, new=NEW, confirm=None):
    return client.post("/api/password", json={"current_password": current, "new_password": new,
                                              "confirm_password": new if confirm is None else confirm})


def test_the_password_changes_and_only_this_browser_stays_signed_in(authed_client, isolated_webui):
    other = isolated_webui.app.test_client()
    assert other.post("/api/login", json={"username": "admin", "password": OLD}).status_code == 200
    response = change(authed_client)
    assert response.status_code == 200 and response.get_json() == {"ok": True}
    assert authed_client.get("/api/config").status_code == 200  # still signed in
    assert other.get("/api/config").status_code == 401  # signed out
    assert auth_store.verify_password("admin", NEW) and not auth_store.verify_password("admin", OLD)
    fresh = isolated_webui.app.test_client()
    assert fresh.post("/api/login", json={"username": "admin", "password": OLD}).status_code == 401
    assert fresh.post("/api/login", json={"username": "admin", "password": NEW}).status_code == 200


@pytest.mark.parametrize("body, status, message", [
    ({"current": "wrong-password"}, 403, "The current password isn't right"),
    ({"new": "short", "confirm": "short"}, 400, "at least 8 characters"),
    ({"confirm": "something-else-entirely"}, 400, "don't match"),
])
def test_a_bad_change_changes_nothing(authed_client, body, status, message):
    response = change(authed_client, **body)
    assert response.status_code == status and message in response.get_json()["error"]
    assert auth_store.verify_password("admin", OLD)


def test_missing_fields_are_refused(authed_client):
    assert authed_client.post("/api/password", json={"current_password": OLD}).status_code == 400


def test_wrong_current_passwords_count_towards_the_login_lockout(authed_client, isolated_webui):
    codes = [change(authed_client, current="wrong-password").status_code for _ in range(12)]
    assert codes[0] == 403 and codes[-1] == 429
    # The lockout is shared with the login itself.
    anonymous = isolated_webui.app.test_client()
    assert anonymous.post("/api/login", json={"username": "admin", "password": OLD}).status_code == 429


def test_it_needs_a_signed_in_session(isolated_webui):
    client = isolated_webui.app.test_client()
    client.post("/api/setup", json={"username": "admin", "password": OLD})
    anonymous = isolated_webui.app.test_client()
    assert change(anonymous).status_code == 401


def test_the_page_shows_the_username_menu(authed_client):
    html = authed_client.get("/").get_data(as_text=True)
    assert 'id="user-menu-btn"' in html and '<span class="user-name">admin</span>' in html
    assert 'id="change-password-btn"' in html and 'id="logout-btn"' in html and 'id="password-modal"' in html


def test_without_local_login_there_is_no_menu_and_no_password_change(isolated_webui, monkeypatch):
    monkeypatch.setenv("BACKUPARR_DISABLE_AUTH", "true")
    for name in ("webui.auth", "webui.destinations", "webui.runs", "webui.backups", "webui.app"):
        module = importlib.reload(sys.modules[name])
    module.app.config.update(TESTING=True)
    module.app.secret_key = "test"
    client = module.app.test_client()
    html = client.get("/").get_data(as_text=True)
    assert 'id="user-menu-btn"' not in html and 'id="password-modal"' not in html
    assert change(client).status_code == 403
