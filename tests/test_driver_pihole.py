"""Pi-hole v6 over its API's Teleporter: sign in with an app password, export
or import, always log out, and the checks around app_sudo and the restart."""
import io
import json
import zipfile

import pytest
import requests

import backup
import restore_actions
from apps import base, pihole
from apps.pihole import PiholeApp, PiholeError
from config_store import always_encrypted, app_meta, MULTI_INSTANCE_APPS, restore_supported

APP_PASSWORD = "app-password-fixture"


def teleporter_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("etc/pihole/pihole.toml", "# Pi-hole configuration file (v6.8)\n[dns]\n")
        z.writestr("etc/pihole/gravity.db", b"SQLite format 3\0")
    return buf.getvalue()


@pytest.fixture
def ph(monkeypatch):
    """A Pi-hole v6 API: records requests, answers from its state."""
    state = type("Pihole", (), {})()
    state.sent = []
    state.password, state.app_password = "web-password", APP_PASSWORD
    state.app_sudo = True
    state.sessions = {}  # sid -> is an app session
    state.v6 = True
    state.v5 = False
    state.login = None  # (status, body) to answer the next logins with
    state.export_status = 200
    state.export = teleporter_zip()
    state.imported = None
    state.import_reply = (200, {"files": ["etc/pihole/pihole.toml", "etc/pihole/gravity.db->domainlist"]})
    state.down_for = 0  # requests refused while FTL restarts
    state.after_import = None  # changes the import makes, e.g. a new app password
    state.counter = 0

    def reply(request, status, body=None, content=None, headers=None):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = content if content is not None else json.dumps(body or {}).encode()
        res.raw = io.BytesIO(res._content)
        res.headers.update(headers or {"Content-Type": "application/json"})
        return res

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        path = request.path_url.split("?")[0]
        if state.down_for:
            state.down_for -= 1
            raise requests.ConnectionError("connection refused")
        if path == "/admin/api.php":
            return reply(request, 200, {"version": 3}) if state.v5 else reply(request, 404, content=b"<html>404</html>")
        if not state.v6:
            return reply(request, 404, content=b"<html>404</html>", headers={"Content-Type": "text/html"})
        sid = request.headers.get("X-FTL-SID")
        authed = sid in state.sessions
        if path == "/api/auth":
            if request.method == "POST":
                if state.login:
                    return reply(request, *state.login)
                password = json.loads(request.body)["password"]
                if password not in (state.password, state.app_password):
                    return reply(request, 401, {"session": {"valid": False, "sid": None, "message": "password incorrect"}})
                state.counter += 1
                new = f"sid-{state.counter}"
                app = password == state.app_password
                state.sessions[new] = app
                return reply(request, 200, {"session": {"valid": True, "totp": False, "sid": new, "csrf": "c",
                                                        "validity": 1800,
                                                        "message": "app-password correct" if app else "password correct"}})
            if request.method == "DELETE":
                if not authed:
                    return reply(request, 401, {"session": {"valid": False}})
                del state.sessions[sid]
                return reply(request, 204, content=b"")
            return reply(request, 401, {"session": {"valid": False, "message": "no SID provided"}})
        if not authed:
            return reply(request, 401, {"session": {"valid": False}})
        app = state.sessions[sid]
        if path == "/api/info/version":
            return reply(request, 200, {"version": {"core": {"local": {"version": "v6.5"}},
                                                    "ftl": {"local": {"version": "v6.8"}},
                                                    "docker": {"local": "2026.10.1"}}})
        if path == "/api/config/webserver/api/app_sudo":
            return reply(request, 200, {"config": {"webserver": {"api": {"app_sudo": state.app_sudo}}}})
        if path == "/api/teleporter":
            if app and not state.app_sudo:
                return reply(request, 403, {"error": {"key": "forbidden", "message": "Unable to export configuration",
                                                      "hint": "webserver.api.app_sudo is false"}})
            if request.method == "GET":
                if state.export_status != 200:
                    return reply(request, state.export_status, {"error": {"key": "x", "message": "boom"}})
                return reply(request, 200, content=state.export, headers={"Content-Type": "application/zip"})
            state.imported = request
            status, body = state.import_reply
            if status == 200:
                # The import ends every session (FLAG_INVALIDATE_SESSIONS), the
                # reply still goes out, then FTL restarts.
                if state.after_import:
                    state.after_import(state)
            return reply(request, status, body)
        return reply(request, 404, {"error": {"key": "not_found"}})

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    state.sleeps = []
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    monkeypatch.setattr(pihole.time, "sleep", state.sleeps.append)
    monkeypatch.setattr(pihole, "_last_login", {})
    return state


@pytest.fixture
def app():
    return PiholeApp("http://pihole/admin/", APP_PASSWORD)


def calls(state):
    return [(r.method, r.path_url.split("?")[0]) for r in state.sent]


def logged_out(state):
    return not state.sessions


# --- Test connection -------------------------------------------------------

def test_test_connection_signs_in_checks_app_sudo_and_logs_out(ph, app):
    assert app.test_connection() == "pihole v6.5 (FTL v6.8) reachable, backups allowed"
    assert calls(ph)[0] == ("POST", "/api/auth") and calls(ph)[-1] == ("DELETE", "/api/auth")
    assert ("GET", "/api/config/webserver/api/app_sudo") in calls(ph)
    assert logged_out(ph)
    # The password goes in the body, never the URL.
    assert all(APP_PASSWORD not in r.url for r in ph.sent)


def test_logins_to_one_pi_hole_are_kept_a_second_apart(ph, app, monkeypatch):
    """FTL refuses a second app-password login in the same second as a wrong
    password: a restore's version check and import must not share one."""
    clock = iter([100.0, 100.0, 100.2, 100.2, 105.0, 105.0])
    monkeypatch.setattr(pihole.time, "monotonic", lambda: next(clock))
    app.test_connection()                          # first login: no wait
    PiholeApp("http://pihole", APP_PASSWORD).test_connection()  # 0.2 s later: waits the rest
    PiholeApp("http://pihole", APP_PASSWORD).test_connection()  # much later: no wait
    assert [round(s, 2) for s in ph.sleeps] == [0.9]


def test_the_browser_address_with_admin_works(ph):
    assert PiholeApp("http://pihole/admin", APP_PASSWORD).url == "http://pihole"


def test_app_sudo_off_says_where_to_turn_it_on_and_still_logs_out(ph, app):
    ph.app_sudo = False
    with pytest.raises(PiholeError, match=r"turn on Settings > All settings > Webserver and API > webserver.api.app_sudo"):
        app.test_connection()
    assert logged_out(ph)


def test_the_web_password_without_2fa_needs_no_app_sudo(ph):
    ph.app_sudo = False
    assert PiholeApp("http://pihole", "web-password").test_connection().endswith("backups allowed")
    assert ("GET", "/api/config/webserver/api/app_sudo") not in calls(ph)


@pytest.mark.parametrize("login, message", [
    (None, r"the app password was refused - check it \(Settings > Web interface / API > Configure app password"),
    ((429, {"error": {"key": "api_seats_exceeded", "message": "API seats exceeded"}}), r"no free API sessions"),
    ((429, {"error": {"key": "rate_limiting", "message": "Rate-limiting login attempts"}}), r"limiting login attempts"),
    ((400, {"error": {"key": "bad_request", "message": "No 2FA token found in JSON payload"}}), r"needs a 2FA code - use an app password"),
])
def test_a_refused_login_explains_itself(ph, login, message):
    ph.login = login
    app = PiholeApp("http://pihole", "wrong" if login is None else APP_PASSWORD)
    with pytest.raises(PiholeError, match=message):
        app.test_connection()
    assert ("DELETE", "/api/auth") not in calls(ph)


def test_pi_hole_v5_is_told_v6_is_needed(ph, app):
    ph.v6, ph.v5 = False, True
    with pytest.raises(PiholeError, match="this is Pi-hole v5 - Pi-hole v6 or newer is needed"):
        app.test_connection()


def test_another_web_server_is_not_taken_for_pi_hole(ph, app):
    ph.v6 = False
    with pytest.raises(PiholeError, match="no Pi-hole v6 API at this address - check the URL"):
        app.test_connection()


# --- backup ----------------------------------------------------------------

def test_backup_saves_the_teleporter_zip_in_one_session(ph, app, tmp_path):
    path = app.backup(str(tmp_path / "work"))
    assert open(path, "rb").read() == ph.export
    assert app.app_version() == "FTL v6.8"
    # One login: the version was read in the backup's own session.
    assert calls(ph).count(("POST", "/api/auth")) == 1 and logged_out(ph)


def test_app_version_on_its_own_signs_in_and_out(ph, app):
    assert app.app_version() == "FTL v6.8" and logged_out(ph)


def test_a_backup_refused_for_app_sudo_says_where_and_logs_out(ph, app, tmp_path):
    ph.app_sudo = False
    with pytest.raises(PiholeError, match="webserver.api.app_sudo"):
        app.backup(str(tmp_path / "work"))
    assert logged_out(ph)


def test_a_failed_export_still_logs_out(ph, app, tmp_path):
    ph.export_status = 500
    with pytest.raises(requests.HTTPError):
        app.backup(str(tmp_path / "work"))
    assert logged_out(ph)


def test_an_export_that_isnt_a_zip_is_refused(ph, app, tmp_path):
    ph.export = b"<html>login</html>"
    with pytest.raises(PiholeError, match="didn't download as a Teleporter archive"):
        app.backup(str(tmp_path / "work"))


# --- restore ---------------------------------------------------------------

def make_archive(tmp_path):
    path = tmp_path / "pihole_20261110_030000.zip"
    path.write_bytes(teleporter_zip())
    return str(path)


def ends_sessions(state):
    state.sessions.clear()
    state.down_for = 3


def test_restore_imports_waits_for_the_restart_and_checks_the_password_again(ph, app, tmp_path):
    ph.after_import = ends_sessions
    result = app.restore_from_file(make_archive(tmp_path))
    assert result == {"back": True, "password_ok": True, "sudo_ok": True}
    upload = ph.imported
    assert b'name="file"; filename="backuparr-teleporter.zip"' in upload.body
    assert teleporter_zip() in upload.body
    assert calls(ph).count(("POST", "/api/auth")) == 2 and logged_out(ph)
    assert restore_actions.restore_message("pihole", result) == "pihole restore complete, Pi-hole restarted to apply it"


def test_a_backup_with_another_app_password_is_reported(ph, app, tmp_path):
    def new_password(state):
        ends_sessions(state)
        state.app_password = "the-backups-app-password"
    ph.after_import = new_password
    result = app.restore_from_file(make_archive(tmp_path))
    assert result["back"] and result["password_ok"] is False
    assert "the app password on its card no longer works" in restore_actions.restore_message("pihole", result)


def test_a_backup_that_turns_app_sudo_off_is_reported(ph, app, tmp_path):
    def sudo_off(state):
        ends_sessions(state)
        state.app_sudo = False
    ph.after_import = sudo_off
    result = app.restore_from_file(make_archive(tmp_path))
    assert result == {"back": True, "password_ok": True, "sudo_ok": False}
    assert "turned webserver.api.app_sudo off" in restore_actions.restore_message("pihole", result)


def test_pi_hole_not_coming_back_is_reported(ph, app, tmp_path, monkeypatch):
    monkeypatch.setattr(pihole, "_RESTART_UP_S", 0)
    def gone(state):
        ends_sessions(state)
        state.down_for = 10_000
    ph.after_import = gone
    result = app.restore_from_file(make_archive(tmp_path))
    assert result["back"] is False and result["password_ok"] is None
    assert "didn't come back from its restart" in restore_actions.restore_message("pihole", result)


def test_a_refused_import_says_why_and_logs_out(ph, app, tmp_path):
    ph.import_reply = (400, {"error": {"key": "bad_request", "message": "Invalid request",
                                       "hint": "File etc/pihole/pihole.toml in ZIP archive contains an invalid value"}})
    with pytest.raises(PiholeError, match=r"refused the backup \(Invalid request - File etc/pihole/pihole.toml"):
        app.restore_from_file(make_archive(tmp_path))
    assert logged_out(ph)


def test_an_import_refused_for_app_sudo_says_where(ph, app, tmp_path):
    ph.app_sudo = False
    with pytest.raises(PiholeError, match="refused the import - turn on Settings > All settings"):
        app.restore_from_file(make_archive(tmp_path))
    assert logged_out(ph)


def test_a_file_that_isnt_a_zip_is_never_uploaded(ph, app, tmp_path):
    path = tmp_path / "pihole_20261110_030000.zip"
    path.write_bytes(b"not a zip")
    with pytest.raises(PiholeError, match="isn't a Teleporter archive"):
        app.restore_from_file(str(path))
    assert ph.sent == []


def test_restore_actions_flags_a_restore_that_needs_attention(ph, tmp_path):
    def sudo_off(state):
        ends_sessions(state)
        state.app_sudo = False
    ph.after_import = sudo_off
    result = restore_actions.restore_app("pihole", {"url": "http://pihole", "api_key": APP_PASSWORD},
                                         str(tmp_path), make_archive(tmp_path))
    assert result["sudo_ok"] is False and result["attention"] is True


def test_restore_actions_runs_the_import_for_an_instance(ph, tmp_path):
    ph.after_import = ends_sessions
    result = restore_actions.restore_app("pihole-2", {"url": "http://pihole2", "api_key": APP_PASSWORD},
                                         str(tmp_path), make_archive(tmp_path))
    assert result["password_ok"] is True and result["attention"] is False
    assert restore_actions.restore_step("pihole-2") == "importing the backup into pihole-2 with Teleporter"


# --- how it's set up -------------------------------------------------------

def test_pi_hole_is_an_always_encrypted_multi_instance_network_app():
    meta = app_meta("pihole")
    assert always_encrypted("pihole") and always_encrypted("pihole-2")
    assert "pihole" in MULTI_INSTANCE_APPS and meta["multi_instance"]
    assert meta["category"] == "network" and meta["key_label"] == "App password" and meta["key_required"]
    assert restore_supported("pihole") and meta["restore_warning"]["lead"] == "Replaces Pi-hole's settings"
    assert backup.DRIVERS["pihole"] is PiholeApp
