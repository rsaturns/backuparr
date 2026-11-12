"""OPNsense (#153): the driver against a fake API, and the card's certificate
routes. The pinning itself is tested against a real TLS server in
test_cert_pin.py."""
import base64
import io
import json

import pytest
import requests

import backup
from apps import cert_pin
from apps.cert_pin import CertificateUntrusted
from apps.opnsense import OpnsenseApp, OpnsenseError
from config_store import always_encrypted, restore_supported

KEY, SECRET = "opnsense-key", "opnsense-secret"
CONFIG = b'<?xml version="1.0"?>\n<opnsense>\n  <system><hostname>fw</hostname></system>\n</opnsense>\n'
HISTORY = {"items": [{"time": "1794378411.6", "time_iso": "2026-11-11T06:26:51+00:00", "id": "config-1794378411.6.xml"},
                     {"time": "1794378221.5", "time_iso": "2026-11-11T06:23:41+00:00", "id": "config-1794378221.5.xml"}]}
INFO = {"name": "OPNsense.internal", "versions": ["OPNsense 26.7-amd64", "FreeBSD 15.1-RELEASE-p1", "OpenSSL 3.5.7"]}
TRUSTED = {"sha256": "ab" * 32, "subject": "OPNsense.internal", "not_after": "2027-12-13T06:16:39+00:00"}


@pytest.fixture
def server(monkeypatch):
    """Answers each "METHOD /path" from `routes` (status, body); records requests."""
    state = type("Server", (), {})()
    state.sent = []
    state.routes = {
        "GET /api/core/backup/backups/this": (200, HISTORY),
        "GET /api/diagnostics/system/system_information": (200, INFO),
        "GET /api/core/backup/download/this": (200, CONFIG),
    }

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        status, body = state.routes.get(f"{request.method} {request.path_url.split('?')[0]}", (404, {}))
        if isinstance(body, Exception):
            raise body
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        res.raw = io.BytesIO(res._content)
        res.headers["Content-Disposition"] = "attachment; filename=config-OPNsense.internal-1794378411.6.xml"
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


@pytest.fixture
def app():
    return OpnsenseApp("https://fw.lab/", KEY, SECRET, trusted_cert=TRUSTED)


def test_the_card_is_always_encrypted_restored_by_hand_and_pins_its_certificate():
    app = backup.build_app("opnsense", {"url": "https://fw.lab", "api_key": KEY, "api_secret": SECRET, "trusted_cert": TRUSTED})
    assert isinstance(app, OpnsenseApp)
    adapter = app.session.get_adapter("https://fw.lab/")
    assert isinstance(adapter, cert_pin.PinnedAdapter) and adapter.trusted == TRUSTED
    assert always_encrypted("opnsense") and not restore_supported("opnsense")


def test_connection_reports_the_version_and_history_with_basic_auth(app, server):
    assert app.test_connection() == ("opnsense 26.7 reachable, 2 configurations in its history, "
                                     "the newest from 2026-11-11T06:26:51+00:00")
    auth = server.sent[0].headers["Authorization"]
    assert auth == "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    assert KEY not in server.sent[0].url


@pytest.mark.parametrize("status, fragment", [
    (401, "the API key or secret is wrong"),
    (403, "needs the \"Diagnostics: Configuration History\" and \"Lobby: Dashboard\" privileges"),
    (404, "update OPNsense to 24.1 or newer"),
])
def test_refusals_say_what_to_fix(app, server, status, fragment):
    server.routes["GET /api/core/backup/backups/this"] = (status, {})
    with pytest.raises(OpnsenseError, match=fragment):
        app.test_connection()


def test_the_version_needs_lobby_dashboard(app, server):
    server.routes["GET /api/diagnostics/system/system_information"] = (403, {"status": 403})
    with pytest.raises(OpnsenseError, match='give it the "Lobby: Dashboard" privilege too'):
        app.test_connection()


@pytest.mark.parametrize("first, version", [
    ("OPNsense 26.7-amd64", "26.7"), ("OPNsense 26.7.9-amd64", "26.7.9"),  # the lab VM before and after updating
    ("OPNsense 26.7.8_2-amd64", "26.7.8_2"),  # a hotfix package revision
    ("OPNsense Business 25.10.1-amd64", "25.10.1"), ("OPNsense 25.1-aarch64", "25.1"),
])
def test_the_firmware_version_is_read_from_system_information(app, server, first, version):
    server.routes["GET /api/diagnostics/system/system_information"] = (200, {**INFO, "versions": [first]})
    assert app.app_version() == version


def test_no_history_is_explained(app, server):
    server.routes["GET /api/core/backup/backups/this"] = (200, {"items": []})
    with pytest.raises(OpnsenseError, match="keeps no configuration history.*Backup Count above 0"):
        app.test_connection()


def test_something_else_at_the_url_is_reported(app, server):
    server.routes["GET /api/core/backup/backups/this"] = (200, b"<html>not the API</html>")
    with pytest.raises(OpnsenseError, match="didn't answer like OPNsense's API"):
        app.test_connection()


def test_a_redirect_isnt_followed(app, server):
    server.routes["GET /api/core/backup/backups/this"] = (302, {})
    with pytest.raises(OpnsenseError, match="redirects"):
        app.test_connection()


def test_backup_is_the_current_config_xml(app, server, tmp_path):
    path = app.backup(str(tmp_path / "work"))
    assert path.endswith("config.xml") and open(path, "rb").read() == CONFIG
    assert backup.archive_suffix(path) == ".xml"


@pytest.mark.parametrize("body, fragment", [
    (b"", "sent no configuration"),
    (b"<html><body>Login</body></html>", "isn't an OPNsense configuration"),
    (b"<opnsense><system>", "isn't valid XML"),
])
def test_a_download_that_isnt_a_whole_config_fails(app, server, tmp_path, body, fragment):
    server.routes["GET /api/core/backup/download/this"] = (200, body)
    with pytest.raises(OpnsenseError, match=fragment):
        app.backup(str(tmp_path / "work"))


# --- the card's certificate routes ------------------------------------------

PRESENTED = {"sha256": "cd" * 32, "subject": "OPNsense.internal", "issuer": "OPNsense.internal",
             "not_after": "2028-01-01T00:00:00+00:00", "self_signed": True}


def test_test_connection_returns_the_certificate_to_trust(authed_client, monkeypatch, isolated_webui):
    def refuse(*args, **kwargs):
        raise CertificateUntrusted("opnsense", PRESENTED, TRUSTED)

    monkeypatch.setattr(OpnsenseApp, "test_connection", refuse)
    body = authed_client.post("/api/test/opnsense", json={"url": "https://fw.lab", "api_key": KEY, "api_secret": SECRET,
                                                           "trusted_cert": TRUSTED}).get_json()
    assert body == {"ok": False, "message": "OPNsense: the firewall's certificate changed. Check it and trust the new one to resume backups.",
                    "certificate": PRESENTED, "trusted_certificate": TRUSTED, "certificate_changed": True}


def test_trust_checks_the_certificate_is_still_the_one_shown(authed_client, monkeypatch):
    monkeypatch.setattr(cert_pin, "fetch_certificate", lambda url: b"der")
    monkeypatch.setattr(cert_pin, "describe", lambda der: PRESENTED)
    ok = authed_client.post("/api/trust-certificate/opnsense", json={"url": "https://fw.lab", "sha256": PRESENTED["sha256"]})
    assert ok.get_json() == {"ok": True, "trusted_cert": {k: PRESENTED[k] for k in ("sha256", "subject", "not_after")}}
    again = authed_client.post("/api/trust-certificate/opnsense", json={"url": "https://fw.lab", "sha256": "ef" * 32})
    assert again.status_code == 409 and "changed again" in again.get_json()["error"]


@pytest.mark.parametrize("app_id, body, status", [
    ("radarr", {"url": "https://r", "sha256": "ab" * 32}, 404),
    ("opnsense", {"url": "http://fw.lab", "sha256": "ab" * 32}, 400),
    ("opnsense", {"url": "https://fw.lab"}, 400),
])
def test_trust_is_only_for_pinning_apps_over_https(authed_client, app_id, body, status):
    assert authed_client.post(f"/api/trust-certificate/{app_id}", json=body).status_code == status


def test_the_trusted_certificate_saves_with_the_card_and_is_validated(authed_client, isolated_webui):
    card = {"enabled": True, "url": "https://fw.lab", "api_key": KEY, "api_secret": SECRET, "trusted_cert": TRUSTED}
    assert authed_client.post("/api/config", json={"apps": {"opnsense": card}}).status_code == 200
    saved = authed_client.get("/api/config").get_json()["apps"]["opnsense"]
    assert saved["trusted_cert"] == TRUSTED and saved["api_secret"] == SECRET
    with open(isolated_webui.auth.CONFIG_PATH) as f:
        assert SECRET not in f.read()  # encrypted at rest
    bad = authed_client.post("/api/config", json={"apps": {"opnsense": {**card, "trusted_cert": {"sha256": "nope"}}}})
    assert bad.status_code == 400 and "trusted_cert" in bad.get_json()["error"]
    other = authed_client.post("/api/config", json={"apps": {"radarr": {"trusted_cert": TRUSTED}}})
    assert other.status_code == 400
