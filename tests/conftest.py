"""Fixtures for exercising webui/app.py against a throwaway config."""
import importlib
import socket
import sys
import threading

import pytest

import archive_check
import auth_store
import backup
import config_store
import destination_util
import manifest
import rclone_util
import restore_version
import runlog
import secrets_crypto

PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def manifest_in_tmp(tmp_path, monkeypatch):
    """Every test's manifest.json and run_logs.json live in its own
    tmp_path, never beside a real config.json."""
    path = str(tmp_path / "manifest.json")
    monkeypatch.setattr(manifest, "path", lambda: path)
    run_logs = str(tmp_path / "run_logs.json")
    monkeypatch.setattr(runlog, "path", lambda: run_logs)
    return path


@pytest.fixture(autouse=True)
def skip_archive_check(request, monkeypatch):
    """Most run_backup tests use fake drivers whose placeholder archives
    aren't real backups; tests marked archive_check run the real check."""
    if request.node.get_closest_marker("archive_check") is None:
        monkeypatch.setattr(archive_check, "check", lambda app, path: None)


@pytest.fixture(autouse=True)
def skip_version_check(request, monkeypatch):
    """Restore tests use fake apps that can't report a version; tests
    marked version_check run the real check."""
    if request.node.get_closest_marker("version_check") is None:
        monkeypatch.setattr(restore_version, "check", lambda app, file, app_cfg: {
            "status": "match", "backup_version": "1", "installed_version": "1", "message": "versions match"})


def load_webui():
    """webui.app freshly reloaded, so its module-level settings read the
    environment again, with the session key create_app() would load - but no
    scheduler thread and no log file."""
    # Dependencies first, so each module binds the fresh state of the next.
    for name in ("webui.settings", "webui.services", "webui.auth", "webui.destinations", "webui.runs",
                 "webui.backups", "webui.app"):
        module = importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)
    module.app.secret_key = module.auth._load_or_create_secret_key()
    module.app.config.update(TESTING=True)
    return module


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
    monkeypatch.setattr(backup, "LOG_FILE", str(tmp_path / "logs" / "backup.log"))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "secrets.key"))
    monkeypatch.setattr(secrets_crypto, "_fernet", None)
    monkeypatch.setattr(destination_util, "DEFAULT_LOCAL_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(destination_util, "sync", lambda *args, **kwargs: None)

    def no_real_rclone(*args, **kwargs):
        raise AssertionError("this test ran the real rclone; stub the rclone_util function it needs (CI has no rclone binary)")

    monkeypatch.setattr(rclone_util, "_run", no_real_rclone)
    module = load_webui()
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


@pytest.fixture
def dns_down(monkeypatch):
    """Every hostname lookup fails, as for a container that isn't on the
    caller's Docker network - the real requests/urllib3 error chain, no
    network needed."""
    def refuse(*args, **kwargs):
        raise socket.gaierror(-2, "Name does not resolve")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
