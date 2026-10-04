"""Authentication modes, including fresh installs and preserved local state."""
import importlib
import sys
from pathlib import Path

import pytest

import auth_store
import config_store
import destination_util
import secrets_crypto


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

    # Reload to exercise the actual startup environment parsing. Prevent the
    # production scheduler from starting threads or touching any test state.
    with monkeypatch.context() as startup:
        startup.setattr("threading.Thread.start", lambda self: None)
        if "webui.app" in sys.modules:
            module = importlib.reload(sys.modules["webui.app"])
        else:
            module = importlib.import_module("webui.app")
    module.app.config.update(TESTING=True)
    yield module
    monkeypatch.setattr(secrets_crypto, "_fernet", None)


@pytest.mark.parametrize("webui", [None, "", "false", "FALSE", "0", "no", "tru", "unexpected", " true "], indirect=True)
def test_auth_enabled_by_default_and_non_opt_in_values(webui):
    client = webui.app.test_client()
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/setup"
    assert client.get("/setup").status_code == 200

    response = client.post("/api/setup", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    assert auth_store.has_credentials()
    page = client.get("/").get_data(as_text=True)
    assert 'data-auth-disabled="false"' in page
    assert 'id="logout-btn"' in page
    assert client.get("/api/meta").status_code == 200

    assert client.post("/api/logout").status_code == 200
    assert client.get("/").headers["Location"] == "/login"
    assert client.get("/api/meta").status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "wrong-password"}).status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "test-password"}).status_code == 200
    assert client.get("/api/meta").status_code == 200


@pytest.mark.parametrize("webui", ["true", "TRUE", "1", "yes", "YeS"], indirect=True)
def test_disabled_auth_fresh_install(webui):
    client = webui.app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert 'data-auth-disabled="true"' in page.get_data(as_text=True)
    assert 'id="logout-btn"' not in page.get_data(as_text=True)
    assert "Set-Cookie" not in page.headers
    assert client.get("/api/meta").status_code == 200
    assert client.get("/api/config").status_code == 200
    assert client.post("/api/config", json={"retention_days": 30}).status_code == 200
    assert client.get("/api/config").json["retention_days"] == 30
    assert not auth_store.has_credentials()
    with client.session_transaction() as session:
        assert not session.get("authed")

    for path in ("/setup", "/login"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"] == "/"
        # Docker's wget healthcheck follows the /login redirect.
        assert client.get(path, follow_redirects=True).status_code == 200
    assert client.get("/static/app.js").status_code == 200


@pytest.mark.parametrize("webui", ["true"], indirect=True)
@pytest.mark.parametrize("existing_account", [False, True])
def test_disabled_auth_blocks_auth_apis_and_preserves_files(webui, existing_account, tmp_path):
    if existing_account:
        auth_store.set_credentials("admin", "test-password")
    cfg = config_store.load_config()
    cfg["destinations"]["local"]["path"] = str(tmp_path / "backups")
    config_store.save_config(cfg)
    backup_dir = Path(destination_util.local_root(cfg["destinations"]["local"]))
    (backup_dir / "backup.zip").write_bytes(b"backup-sentinel")
    for name in ("rclone.conf", "rclone.pass", "secrets.key"):
        (tmp_path / name).write_bytes(b"state-sentinel")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    client = webui.app.test_client()
    # Check the APIs also stay blocked with an existing authenticated session.
    for authed in (False, True):
        with client.session_transaction() as session:
            session["authed"] = authed
        for path, payload in (
            ("/api/setup", {"username": "replacement", "password": "new-password"}),
            ("/api/login", {"username": "admin", "password": "test-password"}),
            ("/api/logout", {}),
            ("/api/reset", {"confirm": webui.RESET_CONFIRM_PHRASE}),
        ):
            response = client.post(path, json=payload)
            assert response.status_code == 403
            assert response.is_json
        assert client.get("/").status_code == 200
        assert client.get("/api/meta").status_code == 200

    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
    if existing_account:
        assert auth_store.verify_password("admin", "test-password")


@pytest.mark.parametrize("webui", ["true"], indirect=True)
def test_reenabling_auth_reuses_existing_account(webui, monkeypatch):
    auth_store.set_credentials("admin", "test-password")
    original_auth = Path(auth_store.AUTH_PATH).read_bytes()
    assert webui.app.test_client().get("/api/meta").status_code == 200

    monkeypatch.setenv("BACKUPARR_DISABLE_AUTH", "false")
    with monkeypatch.context() as startup:
        startup.setattr("threading.Thread.start", lambda self: None)
        webui = importlib.reload(webui)
    webui.app.config.update(TESTING=True)
    client = webui.app.test_client()
    assert client.get("/").headers["Location"] == "/login"
    assert client.get("/api/meta").status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "test-password"}).status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/api/meta").status_code == 200
    assert Path(auth_store.AUTH_PATH).read_bytes() == original_auth
