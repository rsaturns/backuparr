"""Drivers outside the Servarr family refuse to carry their API key, or a
request body, to another host on a redirect."""
import pytest

from apps.servarr import ALLOW_CROSS_HOST_ENV, UnsafeRedirectError
from backup import build_app
from test_servarr_redirects import RedirectAdapter

APPS = {
    "profilarr": ("X-Api-Key", {}),
    "bazarr": ("X-API-KEY", {}),
    "emby": ("X-Emby-Token", {}),
    "seerr": ("X-Api-Key", {}),
    "sabnzbd": (None, {}),
}


@pytest.fixture(autouse=True)
def strict_by_default(monkeypatch):
    monkeypatch.delenv(ALLOW_CROSS_HOST_ENV, raising=False)


@pytest.mark.parametrize("status", [302, 307])
@pytest.mark.parametrize("name", APPS)
def test_a_cross_host_redirect_is_not_followed(name, status, tmp_path):
    header, extra = APPS[name]
    app = build_app(name, {"url": "https://app.example", "api_key": "fixture-private-key",
                           "data_dir": str(tmp_path), **extra})
    app.session.trust_env = False
    adapter = RedirectAdapter("https://login.example/sso", status)
    app.session.mount("https://", adapter)
    with pytest.raises(UnsafeRedirectError):
        app.test_connection()
    assert [request.url.split("/")[2] for request in adapter.sent] == ["app.example"]
    if header:
        assert adapter.sent[0].headers[header] == "fixture-private-key"


@pytest.mark.parametrize("name", APPS)
def test_a_same_host_redirect_is_still_followed(name, tmp_path):
    header, extra = APPS[name]
    app = build_app(name, {"url": "http://app.example", "api_key": "fixture-private-key",
                           "data_dir": str(tmp_path), **extra})
    app.session.trust_env = False
    adapter = RedirectAdapter("https://app.example/moved")
    app.session.mount("https://", adapter)
    app.session.mount("http://", adapter)
    try:
        app.test_connection()
    except UnsafeRedirectError:
        pytest.fail("a same-host https upgrade was refused")
    except Exception:
        pass  # The fixture's "[]" body isn't a real answer; only the redirect matters.
    assert len(adapter.sent) == 2
