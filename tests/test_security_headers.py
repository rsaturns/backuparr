import re
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parent.parent / "webui"


@pytest.fixture
def webui(isolated_webui, authed_client):
    return isolated_webui


def authed(webui):
    client = webui.app.test_client()
    client.post("/api/login", json={"username": "admin", "password": "test-password"})
    return client


def directive(policy, name):
    return next(part for part in policy.split("; ") if part.startswith(name + " ")).split()[1:]


def test_pages_carry_a_policy_that_blocks_inline_and_foreign_scripts(webui):
    for path, client in (("/login", webui.app.test_client()), ("/", authed(webui))):
        policy = client.get(path).headers["Content-Security-Policy"]
        scripts = directive(policy, "script-src")
        assert "'unsafe-inline'" not in scripts and "'unsafe-eval'" not in scripts
        assert set(scripts) == {"'self'", "https://apis.google.com", "https://www.gstatic.com"}
        assert directive(policy, "default-src") == ["'self'"]
        assert "https://api.github.com" in directive(policy, "connect-src")  # footer update check
        assert directive(policy, "object-src") == ["'none'"]
        assert directive(policy, "frame-ancestors") == ["'none'"]


@pytest.mark.parametrize("path", ["/api/config", "/api/history/local", "/api/meta"])
def test_api_responses_are_never_cached(webui, monkeypatch, path):
    monkeypatch.setattr(webui.rclone_util, "lsjson", lambda root, recursive=False: [])
    assert authed(webui).get(path).headers["Cache-Control"] == "no-store"


def test_static_assets_are_still_cacheable(webui):
    assert "no-store" not in webui.app.test_client().get("/static/theme-init.js").headers.get("Cache-Control", "")


@pytest.mark.parametrize("template", sorted(p.name for p in (WEBUI / "templates").glob("*.html")))
def test_templates_have_no_inline_scripts_or_event_handlers(template):
    html = (WEBUI / "templates" / template).read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> would be blocked by the policy"
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler would be blocked by the policy"
    assert not re.search(r"javascript:", html)


# --- cross-site request forgery ------------------------------------------------

FORGED = '{"confirm":"i-want-to-reset-and-delete-files","x":"="}'


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None])
def test_a_forged_reset_from_another_site_is_refused(isolated_webui, content_type, tmp_path):
    # A page on any site can send these without a CORS preflight. The reset
    # route is public, so this must not depend on the session.
    client = isolated_webui.app.test_client()
    client.post("/api/setup", json={"username": "admin", "password": "password123"})
    anonymous = isolated_webui.app.test_client()
    kwargs = {"data": FORGED} if content_type is None else {"data": FORGED, "content_type": content_type}
    response = anonymous.post("/api/reset", **kwargs)
    assert response.status_code == 415
    assert isolated_webui.auth_store.has_credentials()


def test_a_forged_first_run_setup_is_refused(isolated_webui):
    response = isolated_webui.app.test_client().post(
        "/api/setup", data='{"username":"attacker","password":"password123","x":"="}', content_type="text/plain")
    assert response.status_code == 415
    assert not isolated_webui.auth_store.has_credentials()


@pytest.mark.parametrize("path", ["/api/config", "/api/backup/run", "/api/restore/local/radarr",
                                  "/api/destinations/gdrive/disconnect", "/api/logout", "/api/test-notify"])
def test_state_changes_need_json_even_with_a_session(authed_client, path):
    # A same-site page gets the session cookie (SameSite=Lax), but can't send JSON.
    response = authed_client.post(path, data='{"notify_url":"https://attacker.example","x":"="}', content_type="text/plain")
    assert response.status_code == 415


def test_reads_and_deletes_need_no_body(authed_client, monkeypatch):
    import rclone_util
    monkeypatch.setattr(rclone_util, "delete_file", lambda path: None)
    assert authed_client.get("/api/config").status_code == 200
    # DELETE isn't a simple method: another site needs a CORS preflight to send one.
    assert authed_client.delete("/api/history/local/radarr/radarr_1.zip").status_code == 200
