"""Signed-in sessions listed on the server, so signing out ends them for a
copied cookie too (session_store.py)."""
import json
import os

import session_store
from conftest import PASSWORD

LOGIN = {"username": "admin", "password": PASSWORD}


def signed_in(webui):
    client = webui.app.test_client()
    assert client.post("/api/login", json=LOGIN).status_code == 200
    return client


def copy_of(webui, client):
    """Another browser holding a copy of client's session cookie."""
    other = webui.app.test_client()
    other.set_cookie("session", client.get_cookie("session").value)
    return other


def test_signing_out_ends_the_session_for_a_copy_of_its_cookie(authed_client, isolated_webui):
    browser = signed_in(isolated_webui)
    thief = copy_of(isolated_webui, browser)
    assert thief.get("/api/config").status_code == 200
    assert browser.post("/api/logout", json={}).status_code == 200
    assert thief.get("/api/config").status_code == 401
    assert authed_client.get("/api/config").status_code == 200  # another browser's own session goes on


def test_a_cookie_from_before_session_ids_is_signed_out(authed_client, isolated_webui):
    old = isolated_webui.app.test_client()
    with old.session_transaction() as session:
        session["authed"] = True  # what a session held before
    response = old.get("/api/config")
    assert response.status_code == 401
    assert any(c.startswith("session=;") for c in response.headers.getlist("Set-Cookie"))  # forgotten
    # The login page shows rather than sending it round in circles.
    assert old.get("/login").status_code == 200


def test_a_password_change_ends_every_other_session(authed_client, isolated_webui):
    other = signed_in(isolated_webui)
    response = authed_client.post("/api/password", json={
        "current_password": PASSWORD, "new_password": "a-new-password", "confirm_password": "a-new-password"})
    assert response.status_code == 200
    assert other.get("/api/config").status_code == 401
    assert authed_client.get("/api/config").status_code == 200


def test_sessions_last_across_a_restart(authed_client, isolated_webui):
    session_store.forget()  # a restart: the list comes back from the file
    assert authed_client.get("/api/config").status_code == 200


def test_the_file_holds_hashes_never_the_ids(authed_client):
    with authed_client.session_transaction() as session:
        sid = session["sid"]
    stored = open(session_store.SESSIONS_PATH).read()
    assert sid not in stored and len(json.loads(stored)["sessions"]) == 1
    assert oct(os.stat(session_store.SESSIONS_PATH).st_mode & 0o777) == "0o600"


def test_a_session_is_noted_used_at_most_once_a_day(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(session_store.time, "time", lambda: now[0])
    writes = []
    real_save = session_store._save
    monkeypatch.setattr(session_store, "_save", lambda: writes.append(1) or real_save())
    sid = session_store.start()
    for _ in range(50):
        now[0] += 60
        assert session_store.valid(sid)
    assert len(writes) == 1  # just the start
    now[0] += session_store.TOUCH_EVERY
    assert session_store.valid(sid) and len(writes) == 2


def test_an_unused_session_expires_and_is_dropped(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(session_store.time, "time", lambda: now[0])
    stale = session_store.start()
    now[0] += session_store.LIFETIME + session_store.TOUCH_EVERY + 1
    assert not session_store.valid(stale)
    fresh = session_store.start()  # starting one sweeps the expired
    assert session_store.valid(fresh) and len(session_store._load()) == 1


def test_reset_removes_the_list(authed_client, isolated_webui):
    assert os.path.exists(session_store.SESSIONS_PATH)
    authed_client.post("/api/reset", json={"confirm": isolated_webui.auth.RESET_CONFIRM_PHRASE})
    assert not os.path.exists(session_store.SESSIONS_PATH)


def test_an_unreadable_list_signs_everyone_out(authed_client):
    with open(session_store.SESSIONS_PATH, "w") as f:
        f.write("{not json")
    session_store.forget()
    assert authed_client.get("/api/config").status_code == 401
