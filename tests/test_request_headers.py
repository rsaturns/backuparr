"""Exercise real requests preparation/redirects and the app entry points."""
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
import zipfile

import pytest
import requests

import backup
import config_store
import restore_actions
from apps.http_headers import RequestHeadersError, configure_app_headers, validate_request_headers


SECRET = "Basic c2VydmljZTpmaXh0dXJlLXBhc3N3b3Jk"
HEADERS = [{"name": "Proxy-Authorization", "value": SECRET}, {"name": "X-Proxy-Token", "value": "private-fixture-token"}]


def reply(request, data=None, *, status=200, location=None, content=None):
    result = requests.Response()
    result.request, result.url, result.status_code = request, request.url, status
    result._content = content if content is not None else json.dumps(data or {}).encode()
    result.raw = io.BytesIO(result._content)
    if location:
        result.headers["Location"] = location
    return result


class Transport(requests.adapters.BaseAdapter):
    def __init__(self, redirect=None, status=302):
        self.sent, self.redirect, self.status = [], redirect, status

    def send(self, request, **kwargs):
        self.sent.append(request.copy())
        return reply(request, status=self.status if self.redirect and len(self.sent) == 1 else 200,
                     location=self.redirect if len(self.sent) == 1 else None)

    def close(self):
        pass


def client(redirect=None, status=302, rows=None):
    app = SimpleNamespace(url="https://app.example", session=requests.Session())
    app.session.trust_env = False
    transport = Transport(redirect, status)
    app.session.mount("http://", transport)
    app.session.mount("https://", transport)
    configure_app_headers(app, HEADERS if rows is None else rows)
    return app, transport


@pytest.mark.parametrize("rows", [
    {}, None, "secret", [{"name": "X-Test"}],
    [{"name": "X-Test", "value": 42}],
    [{"name": "X-Test\r\nInjected", "value": SECRET}],
    [{"name": "X-Test", "value": SECRET + "\r\nInjected: value"}],
    [{"name": "X-Test", "value": " leading"}],
    [{"name": "X-Test", "value": "\x00"}],
    [{"name": "X-Test", "value": "emoji \U0001f600"}],
    [{"name": "Host", "value": SECRET}],
    [{"name": "Content-Length", "value": "0"}],
    [{"name": "Transfer-Encoding", "value": "chunked"}],
    [{"name": "X-Test", "value": SECRET}, {"name": "x-test", "value": SECRET}],
    [{"name": "X-Test", "value": "x" * 16384}],
])
def test_invalid_headers_fail_without_echoing_values(rows):
    with pytest.raises(RequestHeadersError) as error:
        validate_request_headers(rows)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", ["https://login.example/path?secret=hidden", "http://app.example/path", "https://app.example:8443/path"])
def test_redirects_cannot_exfiltrate_any_global_header(status, target):
    app, transport = client(target, status)
    with pytest.raises(RequestHeadersError) as error:
        app.session.post(app.url + "/restore", files={"backup": ("backup.zip", b"private archive")})
    assert len(transport.sent) == 1
    assert transport.sent[0].headers["Proxy-Authorization"] == SECRET
    assert SECRET not in str(error.value) and "hidden" not in str(error.value)


def test_proxy_header_survives_requests_same_origin_redirect_rebuilding():
    app, transport = client("/backup/final.zip")
    app.session.get(app.url + "/backup/start.zip")
    assert len(transport.sent) == 2
    assert all(r.headers["Proxy-Authorization"] == SECRET for r in transport.sent)


def test_app_auth_and_upload_content_type_win_case_insensitively():
    app, transport = client(rows=HEADERS + [{"name": "authorization", "value": "Basic global"},
                                           {"name": "x-api-key", "value": "global-key"},
                                           {"name": "content-type", "value": "text/plain"}])
    app.session.headers["X-Api-Key"] = "native-key"
    app.session.post(app.url + "/restore", auth=("native", "password"), files={"file": ("backup.zip", b"data")})
    headers = transport.sent[0].headers
    assert headers["Authorization"] == requests.auth._basic_auth_str("native", "password")
    assert headers["X-Api-Key"] == "native-key"
    assert headers["Content-Type"].startswith("multipart/form-data;")
    assert headers["Proxy-Authorization"] == SECRET


def test_clients_without_settings_have_no_headers_or_new_redirect_restrictions():
    app, transport = client("https://other.example/", rows=[])
    app.session.get(app.url)
    assert len(transport.sent) == 2
    assert all("Proxy-Authorization" not in r.headers for r in transport.sent)
    assert "Proxy-Authorization" not in requests.Session().headers


def test_legacy_cross_host_opt_out_cannot_override_global_credential_protection(monkeypatch):
    from apps.prowlarr import ProwlarrApp
    monkeypatch.setenv("BACKUPARR_ALLOW_CROSS_HOST_REDIRECTS", "true")
    app = ProwlarrApp("https://app.example", "native-key")
    transport = Transport("https://other.example/")
    app.session.mount("https://", transport)
    configure_app_headers(app, HEADERS)
    with pytest.raises(RequestHeadersError):
        app.session.get(app.url)
    assert len(transport.sent) == 1


@pytest.fixture
def wire(monkeypatch):
    sent = []

    def send(_adapter, request, **kwargs):
        sent.append(request.copy())
        return reply(request, {"version": "test", "config": {}, "response": {"result": "success"}})

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return sent


@pytest.mark.parametrize("name", ["radarr", "sonarr", "prowlarr", "profilarr", "bazarr", "tdarr", "sabnzbd", "tautulli"])
def test_every_available_app_sends_headers_without_losing_its_key(name, wire):
    app = backup.build_app(name, {"url": "https://app.example", "api_key": "native-key", "request_headers": HEADERS})
    app.test_connection()
    request = wire[-1]
    assert request.headers["Proxy-Authorization"] == SECRET
    if name in {"radarr", "sonarr", "prowlarr", "profilarr", "bazarr"}:
        assert request.headers["X-Api-Key"] == "native-key"
    elif name == "tdarr":
        assert request.headers["Authorization"] == "Bearer native-key"
    elif name == "sabnzbd":
        assert "apikey=native-key" in request.body
    else:
        assert "apikey=native-key" in request.url


def test_settings_encrypt_all_header_values_and_allow_removal(authed_client, caplog):
    assert authed_client.get("/api/config").json["request_headers"] == []
    assert authed_client.post("/api/config", json={"request_headers": HEADERS}).status_code == 200
    assert authed_client.get("/api/config").json["request_headers"] == HEADERS
    on_disk = Path(config_store.CONFIG_PATH).read_text()
    assert all(row["value"] not in on_disk + caplog.text for row in HEADERS)
    assert all(row["value"].startswith("enc:v1:") for row in json.loads(on_disk)["request_headers"])
    assert authed_client.post("/api/config", json={"request_headers": []}).status_code == 200
    assert authed_client.get("/api/config").json["request_headers"] == []


def test_plaintext_header_config_is_migrated_without_losing_values(isolated_webui):
    cfg = copy.deepcopy(config_store.DEFAULTS)
    cfg["request_headers"] = HEADERS
    Path(config_store.CONFIG_PATH).write_text(json.dumps(cfg))
    assert config_store.load_config()["request_headers"] == HEADERS
    assert SECRET not in Path(config_store.CONFIG_PATH).read_text()


@pytest.mark.parametrize("route", ["/api/config", "/api/test/radarr", "/api/discovery/prowlarr"])
def test_invalid_api_header_input_does_not_reach_network(authed_client, wire, route):
    response = authed_client.post(route, json={"url": "https://app.example", "api_key": "native-key",
        "request_headers": [{"name": "X-Test", "value": SECRET + "\r\nInjected: 1"}]})
    assert response.status_code == 400 and SECRET not in response.text
    assert not wire


def test_connection_test_uses_unsaved_headers_without_changing_saved_settings(authed_client, wire):
    authed_client.post("/api/config", json={"request_headers": HEADERS})
    base = {"url": "https://app.example", "api_key": "native-key"}
    assert authed_client.post("/api/test/radarr", json=base).json["ok"]
    assert wire[-1].headers["Proxy-Authorization"] == SECRET
    unsaved = [{"name": "X-Proxy-Token", "value": "unsaved"}]
    assert authed_client.post("/api/test/radarr", json={**base, "request_headers": unsaved}).json["ok"]
    assert wire[-1].headers["X-Proxy-Token"] == "unsaved"
    assert "Proxy-Authorization" not in wire[-1].headers
    assert authed_client.post("/api/test/radarr", json={**base, "request_headers": []}).json["ok"]
    assert "X-Proxy-Token" not in wire[-1].headers
    assert authed_client.get("/api/config").json["request_headers"] == HEADERS


def test_discovery_includes_headers_on_backup_download(authed_client, isolated_webui, inline, monkeypatch, wire):
    def discover(app, progress):
        app.session.get(app.url + "/api/v1/applications")
        app.session.get(app.url + "/backup/manual/config.zip")
        return {"candidates": [], "warnings": []}

    monkeypatch.setattr(isolated_webui.discovery, "discover_prowlarr", discover)
    response = authed_client.post("/api/discovery/prowlarr", json={"url": "https://app.example", "api_key": "native-key", "request_headers": HEADERS})
    result = authed_client.get("/api/discovery/prowlarr/" + response.json["job_id"]).json
    assert result["state"] == "completed"
    assert len(wire) == 2 and all(r.headers["Proxy-Authorization"] == SECRET for r in wire)


def test_scheduled_backup_and_web_restore_send_headers_at_every_phase(authed_client, isolated_webui, inline, monkeypatch, tmp_path):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("config.xml", "<config />")
    zip_bytes, sent = content.getvalue(), []

    def send(adapter, request, **kwargs):
        sent.append(request.copy())
        assert request.headers["Proxy-Authorization"] == SECRET
        assert request.headers["X-Api-Key"] == "native-key"
        path = urlsplit(request.url).path
        if path == "/api/v3/command":
            return reply(request, {"id": 1})
        if path == "/api/v3/command/1":
            return reply(request, {"status": "completed"})
        if path == "/api/v3/system/backup":
            return reply(request, [{"id": 1, "type": "manual", "path": "/backup/manual/config.zip"}])
        if path == "/backup/manual/config.zip":
            return reply(request, content=zip_bytes)
        if path.endswith("/restore/upload"):
            assert b"config.xml" in request.body
        return reply(request)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    uploaded = []
    def copyto(src, dst):
        if str(src).startswith("/dest"):
            Path(dst).write_bytes(zip_bytes)
        else:
            uploaded.append(dst)
    monkeypatch.setattr(backup.rclone_util, "copyto", copyto)
    monkeypatch.setattr(backup.rclone_util, "delete_older_than", lambda *a: None)
    monkeypatch.setattr(backup.destination_util, "remote_root", lambda *a: "/dest")
    cfg = config_store.load_config()
    cfg["request_headers"] = HEADERS
    cfg["apps"]["radarr"].update(enabled=True, url="https://app.example", api_key="native-key")
    config_store.save_config(cfg)
    assert backup.run_backup(cfg) == (["radarr"], [])
    assert len(uploaded) == 1
    assert any(r.url.endswith("/backup/manual/config.zip") for r in sent)
    response = authed_client.post("/api/restore/local/radarr", json={"confirm": True, "file": "backup.zip", "override": {"url": "https://target.example"}})
    assert response.status_code == 200 and response.json["started"]
    assert isolated_webui.RESTORE_RUN_STATE["ok"]
    assert any(r.url == "https://target.example/api/v3/system/backup/restore/upload" for r in sent)
    assert sent[-1].url.endswith("/system/restart")
    assert "request_headers" not in config_store.load_config()["apps"]["radarr"]


@pytest.mark.parametrize("name, method", [("bazarr", "restore_from_file"), ("tdarr", "restore"), ("tautulli", "restore"), ("sabnzbd", "restore")])
def test_other_restore_drivers_receive_global_headers(name, method, monkeypatch, tmp_path, wire):
    classes = {"bazarr": restore_actions.BazarrApp, "tdarr": restore_actions.TdarrApp,
               "tautulli": restore_actions.TautulliApp, "sabnzbd": restore_actions.SabnzbdApp}
    def restore(app, *args):
        app.session.post(app.url + "/restore")
        return {}
    monkeypatch.setattr(classes[name], method, restore)
    monkeypatch.setattr(restore_actions, "extract_zip", lambda *a: str(tmp_path))
    cfg = {"url": "https://app.example", "api_key": "native-key", "request_headers": HEADERS}
    restore_actions.restore_app(name, cfg, str(tmp_path), "backup.zip", bazarr_backup_dir=str(tmp_path),
                               sabnzbd_config={"config": {}}, sabnzbd_password_prompt=lambda *a: None)
    assert wire[-1].headers["Proxy-Authorization"] == SECRET


def test_notifications_never_receive_app_credentials(authed_client, wire):
    authed_client.post("/api/config", json={"request_headers": HEADERS})
    backup.notify("https://ntfy.example/topic", "test", raise_on_error=True)
    assert "Proxy-Authorization" not in wire[-1].headers and "X-Proxy-Token" not in wire[-1].headers
