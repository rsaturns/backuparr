"""Account routes: first-run setup, login, lockout, logout, the public reset
route and the auth gate in front of every other route."""
import os
import time

import pytest

import auth_store

PASSWORD = "test-password"


def creds(username="admin", password=PASSWORD):
    return {"username": username, "password": password}


@pytest.fixture
def anonymous(isolated_webui):
    """An account exists, but this client is not logged in."""
    auth_store.set_credentials("admin", PASSWORD)
    return isolated_webui.app.test_client()


# --- first-run setup -------------------------------------------------------

def test_a_fresh_install_sends_every_page_and_api_call_to_setup(isolated_webui):
    client = isolated_webui.app.test_client()
    for path in ("/", "/login", "/api/config", "/api/history/local"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"] == "/setup", path
    assert client.get("/setup").status_code == 200
    assert client.post("/api/login", json=creds()).status_code == 400


@pytest.mark.parametrize("body, message", [
    ({}, "required"),
    ({"username": "", "password": PASSWORD}, "required"),
    ({"username": "   ", "password": PASSWORD}, "required"),
    ({"username": "admin", "password": ""}, "required"),
    ({"username": "admin", "password": "short"}, "at least 8"),
])
def test_setup_validates_the_account(isolated_webui, body, message):
    response = isolated_webui.app.test_client().post("/api/setup", json=body)
    assert response.status_code == 400 and message in response.get_json()["error"]
    assert not auth_store.has_credentials()


def test_setup_creates_the_account_logs_in_and_can_only_run_once(isolated_webui):
    client = isolated_webui.app.test_client()
    assert client.post("/api/setup", json=creds("  admin  ")).status_code == 200
    assert auth_store.has_credentials()
    assert client.get("/api/meta").status_code == 200

    assert client.post("/api/setup", json=creds("other")).status_code == 403
    assert client.get("/setup").headers["Location"] == "/login"
    assert auth_store.verify_password("admin", PASSWORD)  # the username was trimmed
    assert not auth_store.verify_password("other", PASSWORD)


def test_the_password_is_not_stored_in_plaintext(isolated_webui):
    isolated_webui.app.test_client().post("/api/setup", json=creds())
    stored = open(auth_store.AUTH_PATH).read()
    assert PASSWORD not in stored and "argon2" in stored


# --- login / logout --------------------------------------------------------

def test_login_accepts_the_right_password_and_rejects_the_wrong_one(anonymous):
    assert anonymous.post("/api/login", json=creds(password="wrong-password")).status_code == 401
    assert anonymous.get("/api/meta").status_code == 401
    assert anonymous.post("/api/login", json=creds()).status_code == 200
    assert anonymous.get("/api/meta").status_code == 200


def test_login_trims_the_username_and_ignores_a_missing_body(anonymous):
    assert anonymous.post("/api/login", json=creds(" admin ")).status_code == 200
    assert anonymous.post("/api/logout").status_code == 200
    assert anonymous.post("/api/login", data="garbage", content_type="text/plain").status_code == 401


def test_an_already_logged_in_visitor_skips_the_login_page(anonymous):
    assert anonymous.get("/login").status_code == 200
    anonymous.post("/api/login", json=creds())
    assert anonymous.get("/login").headers["Location"] == "/"


def test_logout_ends_the_session(anonymous):
    anonymous.post("/api/login", json=creds())
    assert anonymous.post("/api/logout").status_code == 200
    assert anonymous.get("/api/meta").status_code == 401
    assert anonymous.get("/").headers["Location"] == "/login"


def test_logout_works_without_a_session(anonymous):
    assert anonymous.post("/api/logout").status_code == 200


def test_sessions_are_permanent_cookies_that_script_cannot_read(anonymous):
    response = anonymous.post("/api/login", json=creds())
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Expires=" in cookie


# --- lockout ---------------------------------------------------------------

def test_five_failures_lock_the_address_out_even_for_the_right_password(anonymous):
    for _ in range(5):
        assert anonymous.post("/api/login", json=creds(password="nope-nope")).status_code == 401
    locked = anonymous.post("/api/login", json=creds())
    assert locked.status_code == 429 and "try again in" in locked.get_json()["error"]


def test_a_successful_login_clears_earlier_failures(anonymous, isolated_webui):
    for _ in range(4):
        anonymous.post("/api/login", json=creds(password="nope-nope"))
    assert anonymous.post("/api/login", json=creds()).status_code == 200
    assert "127.0.0.1" not in isolated_webui._LOGIN_FAILURES


def test_lockout_grows_with_each_failure_then_caps(isolated_webui):
    now = time.time()
    remaining = isolated_webui._login_lockout_remaining
    cases = {4: 0, 5: 5, 6: 10, 7: 20, 9: 80, 11: 300, 40: 300}
    for failures, expected in cases.items():
        isolated_webui._LOGIN_FAILURES["10.0.0.1"] = (failures, now)
        assert remaining("10.0.0.1") == pytest.approx(expected, abs=1), failures


def test_lockout_expires_and_old_records_are_forgotten(isolated_webui):
    failures = isolated_webui._LOGIN_FAILURES
    failures["10.0.0.1"] = (5, time.time() - 6)  # a 5s lockout, 6s ago
    failures["10.0.0.2"] = (50, time.time() - isolated_webui._LOGIN_FAILURE_TTL - 1)
    assert isolated_webui._login_lockout_remaining("10.0.0.1") == 0
    assert "10.0.0.2" not in failures


def test_lockout_is_tracked_per_address(isolated_webui):
    isolated_webui._LOGIN_FAILURES["10.0.0.1"] = (9, time.time())
    assert isolated_webui._login_lockout_remaining("10.0.0.1") > 0
    assert isolated_webui._login_lockout_remaining("10.0.0.2") == 0


# --- the auth gate ---------------------------------------------------------

PROTECTED = [
    ("GET", "/api/config"), ("POST", "/api/config"), ("GET", "/api/meta"), ("GET", "/api/destinations"),
    ("POST", "/api/test/radarr"), ("POST", "/api/test-notify"), ("POST", "/api/test-destination/local"),
    ("POST", "/api/discovery/prowlarr"), ("GET", "/api/discovery/prowlarr/x"),
    ("POST", "/api/plex/auth"), ("GET", "/api/plex/auth/x"), ("DELETE", "/api/plex/auth/x"),
    ("POST", "/api/backup/run"), ("POST", "/api/backup/cancel"), ("GET", "/api/backup/status"),
    ("GET", "/api/history/local"), ("DELETE", "/api/history/local/radarr/a.zip"),
    ("GET", "/api/history/local/radarr/a.zip/download"),
    ("GET", "/api/restore/status"), ("GET", "/api/restore/local/radarr/backups"),
    ("POST", "/api/restore/local/sabnzbd/preview"), ("POST", "/api/restore/local/radarr"),
    ("GET", "/api/destinations/gdrive/oauth/start"), ("GET", "/api/destinations/gdrive/oauth/callback"),
    ("POST", "/api/destinations/gdrive/access-token"), ("POST", "/api/destinations/gdrive/folder"),
    ("POST", "/api/destinations/gdrive/disconnect"), ("POST", "/api/destinations/onedrive/connect"),
    ("POST", "/api/destinations/onedrive/disconnect"),
    ("POST", "/api/destinations/dropbox/connect"), ("POST", "/api/destinations/dropbox/disconnect"),
]


@pytest.mark.parametrize("method, path", PROTECTED)
def test_every_api_route_requires_a_login(anonymous, method, path):
    response = anonymous.open(path, method=method, json={})
    assert response.status_code == 401
    assert response.get_json() == {"error": "authentication required"}


def test_the_dashboard_redirects_visitors_to_the_login_page(anonymous):
    assert anonymous.get("/").headers["Location"] == "/login"


def test_static_files_need_no_login(anonymous):
    assert anonymous.get("/static/style.css").status_code == 200


def test_every_registered_api_route_is_covered_by_the_gate_list(isolated_webui):
    public = {"/api/logout", "/api/reset", "/api/setup", "/api/login"}
    routes = {rule.rule for rule in isolated_webui.app.url_map.iter_rules() if rule.rule.startswith("/api/")}
    listed = {path for _, path in PROTECTED}

    def normalise(rule):
        return rule.replace("<job_id>", "x").replace("<dest_id>", "local").replace("<app_name>", "radarr").replace("<filename>", "a.zip")

    uncovered = {normalise(rule) for rule in routes - public} - listed - {"/api/restore/local/sabnzbd/backups"}
    assert not uncovered, f"add these routes to PROTECTED: {sorted(uncovered)}"


# --- reset (deliberately reachable without a session) -----------------------

def seed_state(webui, client):
    client.post("/api/config", json={"apps": {"radarr": {"enabled": True, "url": "http://r", "api_key": "k"}}})
    backups = os.path.join(webui.destination_util.DEFAULT_LOCAL_DIR)
    os.makedirs(os.path.join(backups, "radarr"))
    open(os.path.join(backups, "radarr", "radarr_1.zip"), "w").write("data")
    for path in (os.environ["RCLONE_CONFIG"], os.environ["RCLONE_CONFIG_PASS_FILE"]):
        open(path, "w").write("x")
    return backups


@pytest.mark.parametrize("body", [{}, {"confirm": ""}, {"confirm": "yes"}, {"confirm": "I-WANT-TO-RESET-AND-DELETE-FILES"}])
def test_reset_needs_the_exact_phrase_and_deletes_nothing_without_it(isolated_webui, authed_client, body):
    backups = seed_state(isolated_webui, authed_client)
    response = authed_client.post("/api/reset", json=body)
    assert response.status_code == 400
    assert os.path.exists(auth_store.AUTH_PATH) and os.path.exists(backups) and os.path.exists(isolated_webui.CONFIG_PATH)


def test_reset_wipes_all_local_state_and_returns_to_setup(isolated_webui, authed_client):
    backups = seed_state(isolated_webui, authed_client)
    anonymous = isolated_webui.app.test_client()  # reset is the forgot-password path: no login needed
    response = anonymous.post("/api/reset", json={"confirm": isolated_webui.RESET_CONFIRM_PHRASE})
    assert response.status_code == 200

    assert not os.path.exists(backups)
    for path in (isolated_webui.CONFIG_PATH, auth_store.AUTH_PATH, os.environ["RCLONE_CONFIG"],
                 os.environ["RCLONE_CONFIG_PASS_FILE"], os.environ["BACKUPARR_SECRET_KEY_PATH"],
                 isolated_webui.secrets_crypto.KEY_PATH):
        assert not os.path.exists(path), path
    assert anonymous.get("/").headers["Location"] == "/setup"


def test_reset_removes_a_custom_local_backup_folder(isolated_webui, authed_client, tmp_path):
    custom = tmp_path / "nas"
    (custom / "radarr").mkdir(parents=True)
    authed_client.post("/api/config", json={"destinations": {"local": {"path": str(custom)}}})
    authed_client.post("/api/reset", json={"confirm": isolated_webui.RESET_CONFIRM_PHRASE})
    assert not custom.exists()


def test_reset_signs_out_the_caller_and_tolerates_missing_files(isolated_webui, authed_client):
    assert authed_client.post("/api/reset", json={"confirm": isolated_webui.RESET_CONFIRM_PHRASE}).status_code == 200
    again = authed_client.post("/api/reset", json={"confirm": isolated_webui.RESET_CONFIRM_PHRASE})
    assert again.status_code == 200
    assert authed_client.get("/api/meta").status_code == 302  # nothing left, so back to setup
