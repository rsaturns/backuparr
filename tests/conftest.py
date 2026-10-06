"""Fixtures for exercising webui/app.py against a throwaway config."""
import importlib
import sys
import threading

import pytest

import auth_store
import config_store
import destination_util
import secrets_crypto

PASSWORD = "test-password"


@pytest.fixture
def isolated_webui(tmp_path, monkeypatch):
    """webui.app reloaded with every state path under tmp_path, auth enabled,
    the scheduler thread never started and rclone syncing stubbed out."""
    for name in ("BACKUPARR_DISABLE_AUTH", "BACKUPARR_FORCE_HTTPS", "BACKUPARR_SECRETS_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BACKUPARR_SECRET_KEY_PATH", str(tmp_path / "secret_key"))
    monkeypatch.setenv("BACKUPARR_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    monkeypatch.setenv("RCLONE_CONFIG_PASS_FILE", str(tmp_path / "rclone.pass"))
    monkeypatch.setattr(auth_store, "AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "secrets.key"))
    monkeypatch.setattr(secrets_crypto, "_fernet", None)
    monkeypatch.setattr(destination_util, "DEFAULT_LOCAL_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(destination_util, "sync", lambda *args, **kwargs: None)
    with pytest.MonkeyPatch.context() as startup:
        startup.setattr("threading.Thread.start", lambda self: None)
        if "webui.app" in sys.modules:
            module = importlib.reload(sys.modules["webui.app"])
        else:
            module = importlib.import_module("webui.app")
    module.app.config.update(TESTING=True)
    yield module
    monkeypatch.setattr(secrets_crypto, "_fernet", None)


@pytest.fixture
def authed_client(isolated_webui):
    """A test client with the admin account created and logged in."""
    client = isolated_webui.app.test_client()
    response = client.post("/api/setup", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200
    return client


class InlineThread:
    """Stands in for threading.Thread: start() runs the work right away, so a
    request that starts a background run returns after the run is finished."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **extra):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


@pytest.fixture
def inline(isolated_webui, monkeypatch):
    monkeypatch.setattr(threading, "Thread", InlineThread)
    return isolated_webui
