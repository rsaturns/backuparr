"""Authentication modes: default local login vs BACKUPARR_DISABLE_AUTH."""
import importlib
import sys
from pathlib import Path

import pytest

import auth_store
import config_store
import destination_util
import secrets_crypto


def _load_webui():
    # Reload so module-level env parsing runs again. Block thread starts so
    # the scheduler never runs during tests.
    with pytest.MonkeyPatch.context() as startup:
        startup.setattr("threading.Thread.start", lambda self: None)
        if "webui.app" in sys.modules:
            module = importlib.reload(sys.modules["webui.app"])
        else:
            module = importlib.import_module("webui.app")
    module.app.config.update(TESTING=True)
    return module


@pytest.fixture
def webui(tmp_path, monkeypatch, request):
    value = getattr(request, "param", None)
    if value is None:
        monkeypatch.delenv("BACKUPARR_DISABLE_AUTH", raising=False)
    else:
        monkeypatch.setenv("BACKUPARR_DISABLE_AUTH", value)
    monkeypatch.delenv("BACKUPARR_FORCE_HTTPS", raising=False)
    monkeypatch.delenv("BACKUPARR_SECRETS_KEY", raising=False)
    monkeypatch.setenv("BACKUPARR_SECRET_KEY_PATH", str(tmp_path / "secret_key"))
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    monkeypatch.setenv("RCLONE_CONFIG_PASS_FILE", str(tmp_path / "rclone.pass"))
    monkeypatch.setattr(auth_store, "AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "secrets.key"))
    monkeypatch.setattr(secrets_crypto, "_fernet", None)
    monkeypatch.setattr(destination_util, "DEFAULT_LOCAL_DIR", str(tmp_path / "backups"))
    yield _load_webui()
    monkeypatch.setattr(secrets_crypto, "_fernet", None)


@pytest.mark.parametrize("webui", [None, "", "false", "0", "tru", " true "], indirect=True)
def test_auth_enabled_by_default_and_non_opt_in_values(webui):
    client = webui.app.test_client()
    assert client.get("/").headers["Location"] == "/setup"

    assert client.post("/api/setup", json={"username": "admin", "password": "test-password"}).status_code == 200
    assert auth_store.has_credentials()
    page = client.get("/").get_data(as_text=True)
    assert 'data-auth-disabled="false"' in page
    assert 'id="logout-btn"' in page

    assert client.post("/api/logout").status_code == 200
    assert client.get("/").headers["Location"] == "/login"
    assert client.get("/api/meta").status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "wrong-password"}).status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "test-password"}).status_code == 200
    assert client.get("/api/meta").status_code == 200


@pytest.mark.parametrize("webui", ["true", "TRUE", "1", "yes"], indirect=True)
def test_disabled_auth_fresh_install(webui, monkeypatch):
    # Saving config syncs rclone remotes; CI runners have no rclone binary.
    monkeypatch.setattr(destination_util, "sync", lambda *a, **k: None)
    client = webui.app.test_client()
    page = client.get("/")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'data-auth-disabled="true"' in body
    assert 'id="logout-btn"' not in body
    assert client.get("/api/meta").status_code == 200
    assert client.post("/api/config", json={"retention_days": 30}).status_code == 200
    assert client.get("/api/config").json["retention_days"] == 30
    assert not auth_store.has_credentials()

    for path in ("/setup", "/login"):
        assert client.get(path).headers["Location"] == "/"
        # Docker's wget healthcheck follows the /login redirect.
        assert client.get(path, follow_redirects=True).status_code == 200


@pytest.mark.parametrize("webui", ["true"], indirect=True)
@pytest.mark.parametrize("existing_account", [False, True])
def test_disabled_auth_blocks_auth_apis_and_preserves_files(webui, existing_account, tmp_path):
    if existing_account:
        auth_store.set_credentials("admin", "test-password")
    (tmp_path / "backups").mkdir(exist_ok=True)
    (tmp_path / "backups" / "backup.zip").write_bytes(b"backup-sentinel")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    client = webui.app.test_client()
    for path in ("/api/setup", "/api/login", "/api/logout", "/api/reset"):
        payload = {"confirm": webui.RESET_CONFIRM_PHRASE} if path == "/api/reset" else {}
        response = client.post(path, json=payload)
        assert response.status_code == 403
        assert response.is_json

    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert auth_store.has_credentials() == existing_account


@pytest.mark.parametrize("webui", ["true"], indirect=True)
def test_auth_api_paths_cover_every_auth_route(webui):
    auth_rules = {
        rule.rule for rule in webui.app.url_map.iter_rules()
        if rule.endpoint in ("api_setup", "api_login", "api_logout", "api_reset")
    }
    assert auth_rules == webui._AUTH_API_PATHS


@pytest.mark.parametrize("webui", ["true"], indirect=True)
def test_reenabling_auth_reuses_existing_account(webui, monkeypatch):
    auth_store.set_credentials("admin", "test-password")
    original_auth = Path(auth_store.AUTH_PATH).read_bytes()
    assert webui.app.test_client().get("/api/meta").status_code == 200

    monkeypatch.setenv("BACKUPARR_DISABLE_AUTH", "false")
    client = _load_webui().app.test_client()
    assert client.get("/api/meta").status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "test-password"}).status_code == 200
    assert client.get("/api/meta").status_code == 200
    assert Path(auth_store.AUTH_PATH).read_bytes() == original_auth
