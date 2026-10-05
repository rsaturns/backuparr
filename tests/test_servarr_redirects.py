"""Exercise Requests' actual redirect handling without contacting other hosts."""
import pytest
import requests
from requests.adapters import BaseAdapter

import discovery
from apps.prowlarr import ProwlarrApp
from apps.servarr import UnsafeRedirectError


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


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", [
    "https://login.example/login?token=private-redirect-token",
    "https://prowlarr.example:8443/login?token=private-redirect-token",
    "http://prowlarr.example/login?token=private-redirect-token",
    "https://user:private-redirect-token@prowlarr.example/login",
])
def test_api_redirect_cannot_send_credentials_to_another_origin(status, target):
    app, adapter = driver(target, status)
    with pytest.raises(discovery.DiscoveryError, match="redirected outside") as error:
        discovery.discover_prowlarr(app)
    assert len(adapter.sent) == 1
    assert adapter.sent[0].headers["X-Api-Key"] == "fixture-private-key"
    assert "fixture-private-key" not in str(error.value)
    assert "private-redirect-token" not in str(error.value)


@pytest.mark.parametrize("location", [
    "applications/", "/api/v1/applications/",
    "https://prowlarr.example:443/api/v1/applications/",
])
def test_same_origin_api_redirect_keeps_the_api_key(location):
    app, adapter = driver(location)
    assert discovery._get_providers(app, "/applications") == []
    assert len(adapter.sent) == 2
    assert all(req.headers["X-Api-Key"] == "fixture-private-key" for req in adapter.sent)


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_mutating_api_requests_are_protected_too(method):
    app, adapter = driver("https://login.example/receive", 307)
    with pytest.raises(UnsafeRedirectError):
        app.session.request(method, app._api("/command"), json={"name": "Backup"})
    assert len(adapter.sent) == 1


def test_restore_upload_is_not_forwarded_to_another_origin(tmp_path):
    app, adapter = driver("https://login.example/receive", 308)
    backup = tmp_path / "backup.zip"
    backup.write_bytes(b"private-backup-fixture")
    with pytest.raises(UnsafeRedirectError):
        app.restore_upload(backup)
    assert len(adapter.sent) == 1


def test_manual_redirect_handling_can_inspect_the_response_without_following_it():
    app, adapter = driver("https://login.example/receive")
    with app.session.get(app._api("/applications"), allow_redirects=False) as response:
        assert response.status_code == 302
    assert len(adapter.sent) == 1
