"""Exercise Requests' actual redirect handling without contacting other hosts."""
import pytest
import requests
from requests.adapters import BaseAdapter

from apps.prowlarr import ProwlarrApp
from apps.servarr import ALLOW_CROSS_HOST_ENV, UnsafeRedirectError


class RedirectAdapter(BaseAdapter):
    def __init__(self, location, status=302):
        self.location = location
        self.status = status
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = self.status if len(self.sent) == 1 else 200
        response._content = b"[]"
        if len(self.sent) == 1:
            response.headers["Location"] = self.location
        return response

    def close(self):
        pass


def driver(location, status=302, url="https://prowlarr.example"):
    app = ProwlarrApp(url, "fixture-private-key")
    app.session.trust_env = False  # Both protocols use the in-memory adapter.
    adapter = RedirectAdapter(location, status)
    app.session.mount("https://", adapter)
    app.session.mount("http://", adapter)
    return app, adapter


@pytest.fixture(autouse=True)
def strict_by_default(monkeypatch):
    monkeypatch.delenv(ALLOW_CROSS_HOST_ENV, raising=False)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", [
    "https://login.example/login?token=private-redirect-token",
    "https://other.prowlarr.example/login?token=private-redirect-token",
    "https://user:private-redirect-token@prowlarr.example/login",
])
def test_cross_host_redirect_cannot_receive_the_api_key(status, target):
    app, adapter = driver(target, status)
    with pytest.raises(UnsafeRedirectError, match="different host") as error:
        app.test_connection()
    assert len(adapter.sent) == 1
    assert adapter.sent[0].headers["X-Api-Key"] == "fixture-private-key"
    assert "fixture-private-key" not in str(error.value)
    assert "private-redirect-token" not in str(error.value)


@pytest.mark.parametrize("url,location", [
    # http -> https upgrade on the same host, the common canonicalisation.
    ("http://prowlarr.example", "https://prowlarr.example/api/v1/system/status"),
    ("http://prowlarr.example:9696", "https://prowlarr.example/api/v1/system/status"),
    ("https://prowlarr.example", "https://prowlarr.example:8443/api/v1/system/status"),
    ("https://prowlarr.example", "https://PROWLARR.example/api/v1/system/status"),
    ("https://prowlarr.example", "/api/v1/system/status/"),
    ("https://prowlarr.example", "status/"),
])
def test_same_host_redirect_keeps_the_api_key(url, location):
    app, adapter = driver(location, url=url)
    app.session.get(app._api("/system/status"))
    assert len(adapter.sent) == 2
    assert all(req.headers["X-Api-Key"] == "fixture-private-key" for req in adapter.sent)


def test_https_to_http_downgrade_is_blocked():
    app, adapter = driver("http://prowlarr.example/api/v1/system/status")
    with pytest.raises(UnsafeRedirectError):
        app.session.get(app._api("/system/status"))
    assert len(adapter.sent) == 1


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_opt_out_follows_cross_host_redirects(monkeypatch, value):
    monkeypatch.setenv(ALLOW_CROSS_HOST_ENV, value)
    app, adapter = driver("https://canonical.example/api/v1/system/status")
    app.session.get(app._api("/system/status"))
    assert len(adapter.sent) == 2


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_opt_out_is_off_unless_explicitly_enabled(monkeypatch, value):
    monkeypatch.setenv(ALLOW_CROSS_HOST_ENV, value)
    app, adapter = driver("https://canonical.example/api/v1/system/status")
    with pytest.raises(UnsafeRedirectError):
        app.session.get(app._api("/system/status"))
    assert len(adapter.sent) == 1


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_mutating_api_requests_are_protected_too(method):
    app, adapter = driver("https://login.example/receive", 307)
    with pytest.raises(UnsafeRedirectError):
        app.session.request(method, app._api("/command"), json={"name": "Backup"})
    assert len(adapter.sent) == 1


def test_restore_upload_is_not_forwarded_to_another_host(tmp_path):
    app, adapter = driver("https://login.example/receive", 308)
    backup = tmp_path / "backup.zip"
    backup.write_bytes(b"private-backup-fixture")
    with pytest.raises(UnsafeRedirectError):
        app.restore_upload(backup)
    assert len(adapter.sent) == 1


def test_manual_redirect_handling_can_inspect_the_response_without_following_it():
    app, adapter = driver("https://login.example/receive")
    with app.session.get(app._api("/system/status"), allow_redirects=False) as response:
        assert response.status_code == 302
    assert len(adapter.sent) == 1


@pytest.mark.parametrize("target", [
    "https://prowlarr.example:8443/api/v1/system/status",   # other port
    "http://prowlarr.example/api/v1/system/status",         # downgrade
])
def test_strict_mode_refuses_any_origin_change(target):
    app, adapter = driver(target)
    app.session._strict = True
    with pytest.raises(UnsafeRedirectError):
        app.session.get(app._api("/system/status"))
    assert len(adapter.sent) == 1


def test_strict_mode_ignores_the_opt_out(monkeypatch):
    monkeypatch.setenv(ALLOW_CROSS_HOST_ENV, "true")
    app, adapter = driver("https://canonical.example/api/v1/system/status")
    app.session._strict = True
    with pytest.raises(UnsafeRedirectError):
        app.session.get(app._api("/system/status"))


@pytest.mark.parametrize("url,location", [
    ("https://prowlarr.example", "https://prowlarr.example:443/api/v1/system/status"),
    ("https://prowlarr.example:443", "/api/v1/system/status/"),
])
def test_strict_mode_allows_the_same_origin(url, location):
    app, adapter = driver(location, url=url)
    app.session._strict = True
    app.session.get(app._api("/system/status"))
    assert len(adapter.sent) == 2


@pytest.mark.parametrize("url", ["https://user:pass@prowlarr.example", "https://user:pass@prowlarr.example/prowlarr"])
@pytest.mark.parametrize("location", [
    "/login",
    "https://prowlarr.example/login",
    "https://user:pass@prowlarr.example/login",
])
def test_url_with_embedded_credentials_works_and_keeps_following_same_host_redirects(url, location):
    app, adapter = driver(location, url=url)
    app.list_backups()
    assert len(adapter.sent) == 2
    assert all(request.headers["X-Api-Key"] == "fixture-private-key" for request in adapter.sent)
    assert adapter.sent[0].headers["Authorization"].startswith("Basic ")


def test_first_request_to_a_url_with_credentials_is_not_treated_as_a_redirect():
    app, adapter = driver("/ignored", status=200, url="http://user:pass@prowlarr.example:9696")
    app.list_backups()
    assert len(adapter.sent) == 1


def test_redirect_cannot_introduce_different_credentials():
    app, _ = driver("https://other:secret@prowlarr.example/login", url="https://user:pass@prowlarr.example")
    with pytest.raises(UnsafeRedirectError):
        app.list_backups()
