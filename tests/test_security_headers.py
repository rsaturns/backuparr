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
