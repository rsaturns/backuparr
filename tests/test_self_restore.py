"""Restoring Backuparr from its own encrypted backup (#111): what's checked
before anything changes, what's written, and the setup-page and Settings
routes."""
import base64
import io
import json
import os
import zipfile

import pytest
from cryptography.fernet import Fernet

import archive_crypto
import auth_store
import config_store
import manifest
import rclone_util
import runlog
import secrets_crypto
import self_backup
import self_restore

PASSWORD = "the-old-password"


@pytest.fixture
def rclone(monkeypatch):
    """A fake rclone.conf: {name: fields}."""
    remotes = {"stale-remote": {"type": "drive"}}
    monkeypatch.setattr(rclone_util, "config_dump", lambda: {k: dict(v) for k, v in remotes.items()})
    monkeypatch.setattr(rclone_util, "config_delete", lambda name: remotes.pop(name))
    monkeypatch.setattr(rclone_util, "config_set", lambda name, kind, fields, force=False: remotes.update({name: {"type": kind, **fields}}))
    return remotes


@pytest.fixture
def old_install(isolated_webui, rclone):
    """Backuparr's own backup from an install with Radarr, a Drive remote, a
    login, a manifest and run logs - then that install wiped, as on a new
    server. Returns (archive bytes, key)."""
    cfg = config_store.load_config()
    cfg["apps"]["radarr"].update(enabled=True, url="http://radarr:7878", api_key="radarr-secret-key")
    cfg["secrets_key_acknowledged"] = True
    config_store.save_config(cfg)
    auth_store.set_credentials("olduser", PASSWORD)
    manifest.record("radarr", "radarr_1.zip", ["local"], app_version="6.4", sha256="a", size=1, backuparr_version="2.5.0")
    runlog.save({"id": "radarr_20261101_030000", "app": "radarr", "lines": [], "destinations": {}, "ok": True})
    rclone.clear()
    rclone["backuparr-gdrive"] = {"type": "drive", "token": '{"refresh_token": "r"}'}
    archive, key = self_backup.build(), secrets_crypto.current_key()
    for path in (config_store.CONFIG_PATH, auth_store.AUTH_PATH, secrets_crypto.KEY_PATH, manifest.path(), runlog.path()):
        os.remove(path)
    secrets_crypto.forget_key()
    config_store.forget_secrets_key_acknowledged()
    rclone.clear()
    rclone["fresh-install-remote"] = {"type": "dropbox"}
    return archive, key


def encrypted_zip(members, key):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return archive_crypto.encrypt_bytes(buffer.getvalue(), key)


META = json.dumps({"format": 1, "backuparr_version": "2.5.0", "created": "2026-11-05T00:00:00+00:00"})
AUTH = json.dumps({"username": "u", "password_hash": "h"})


# --------------------------------------------------------------- prepare ----

def test_a_wrong_or_malformed_key_or_a_damaged_file_changes_nothing(old_install):
    archive, key = old_install
    with pytest.raises(self_restore.SelfRestoreError, match="isn't a Backuparr backup made with this key"):
        self_restore.prepare(archive, Fernet.generate_key().decode())
    with pytest.raises(self_restore.SelfRestoreError, match="isn't a Backuparr encryption key"):
        self_restore.prepare(archive, "my-password")
    with pytest.raises(self_restore.SelfRestoreError):
        self_restore.prepare(archive[:-10], key)
    assert not os.path.exists(config_store.CONFIG_PATH)


@pytest.mark.parametrize("meta, message", [
    ({"format": 2, "backuparr_version": "2.5.0"}, "made by a newer Backuparr"),
    ({"format": 1, "backuparr_version": "9.0.0"}, "from Backuparr 9.0.0 and this is 2.5.0. Update Backuparr to 9.0.0 first"),
])
def test_a_backup_from_a_newer_backuparr_is_refused(isolated_webui, meta, message, monkeypatch):
    monkeypatch.setattr(self_restore, "VERSION", "2.5.0")
    key = Fernet.generate_key().decode()
    archive = encrypted_zip({"backuparr-meta.json": json.dumps(meta), "config.json": "{}", "auth.json": AUTH}, key)
    with pytest.raises(self_restore.SelfRestoreError, match=message):
        self_restore.prepare(archive, key)


@pytest.mark.parametrize("running, made_by, refused", [
    ("3.0.0-beta", "3.0.1", True),
    ("3.0.0-beta", "3.0.0-beta", False),
    ("3.0.0", "3.0.0-beta", False),
    ("3.0.1-beta", "3.0.0", False),
])
def test_a_beta_version_is_compared_as_its_release(isolated_webui, monkeypatch, running, made_by, refused):
    monkeypatch.setattr(self_restore, "VERSION", running)
    key = Fernet.generate_key().decode()
    meta = json.dumps({"format": 1, "backuparr_version": made_by})
    archive = encrypted_zip({"backuparr-meta.json": meta, "config.json": "{}", "auth.json": AUTH}, key)
    if refused:
        with pytest.raises(self_restore.SelfRestoreError, match=f"from Backuparr {made_by} and this is {running}"):
            self_restore.prepare(archive, key)
    else:
        assert "config.json" in self_restore.prepare(archive, key)


def test_a_backup_from_an_older_backuparr_is_fine(isolated_webui, monkeypatch):
    monkeypatch.setattr(self_restore, "VERSION", "2.6.0")
    key = Fernet.generate_key().decode()
    archive = encrypted_zip({"backuparr-meta.json": META, "config.json": "{}", "auth.json": AUTH}, key)
    assert set(self_restore.prepare(archive, key)) == {"backuparr-meta.json", "config.json", "auth.json"}


@pytest.mark.parametrize("members, message", [
    ({"backuparr-meta.json": META, "config.json": "{}"}, "has no login"),
    ({"backuparr-meta.json": META, "config.json": "{}", "auth.json": "{}"}, "login in the backup is incomplete"),
    ({"backuparr-meta.json": META, "config.json": "[]", "auth.json": AUTH}, "config.json in the backup isn't what Backuparr writes"),
    ({"backuparr-meta.json": META, "config.json": "{}", "auth.json": AUTH, "evil.sh": "x"}, "unexpected file"),
    ({"backuparr-meta.json": META, "config.json": "{}", "auth.json": AUTH, "rclone-remotes.json": '{"r": {}}'}, "rclone-remotes.json"),
])
def test_an_archive_that_isnt_backuparrs_is_refused(isolated_webui, members, message):
    key = Fernet.generate_key().decode()
    with pytest.raises(self_restore.SelfRestoreError, match=message):
        self_restore.prepare(encrypted_zip(members, key), key)


def test_an_archive_too_large_unzipped_is_refused(isolated_webui, monkeypatch):
    monkeypatch.setattr(self_backup, "MAX_UNZIPPED", 1000)
    key = Fernet.generate_key().decode()
    archive = encrypted_zip({"backuparr-meta.json": META, "config.json": " " * 5000, "auth.json": AUTH}, key)
    with pytest.raises(self_restore.SelfRestoreError, match="too large"):
        self_restore.prepare(archive, key)


# ----------------------------------------------------------------- apply ----

def test_apply_brings_back_the_key_config_login_records_and_remotes(old_install, rclone):
    archive, key = old_install
    warnings = self_restore.apply(self_restore.prepare(archive, key), key)
    assert warnings == []
    assert open(secrets_crypto.KEY_PATH).read() == key and secrets_crypto.current_key() == key
    assert config_store.load_config()["apps"]["radarr"]["api_key"] == "radarr-secret-key"  # readable with the restored key
    assert auth_store.verify_password("olduser", PASSWORD)
    assert manifest.get("radarr", "radarr_1.zip")["app_version"] == "6.4"
    assert runlog.latest()["radarr"]["id"] == "radarr_20261101_030000"
    assert rclone == {"backuparr-gdrive": {"type": "drive", "token": '{"refresh_token": "r"}'}}
    assert config_store.secrets_key_acknowledged() is True  # already saved: it was just typed in


def test_a_different_key_in_the_environment_is_a_warning(old_install, monkeypatch):
    archive, key = old_install
    monkeypatch.setenv("BACKUPARR_SECRETS_KEY", Fernet.generate_key().decode())
    warnings = self_restore.apply(self_restore.prepare(archive, key), key)
    assert len(warnings) == 1 and "Set it to the restored key (or remove it) and recreate the container" in warnings[0]
    assert open(secrets_crypto.KEY_PATH).read() == key


def test_remotes_that_cant_be_restored_are_a_warning(old_install, monkeypatch):
    archive, key = old_install

    def broken(*args, **kwargs):
        raise rclone_util.RcloneError("config create failed", "couldn't write rclone.conf")

    monkeypatch.setattr(rclone_util, "config_set", broken)
    warnings = self_restore.apply(self_restore.prepare(archive, key), key)
    assert "couldn't write rclone.conf" in warnings[0]


def test_only_backuparrs_own_remotes_come_back(isolated_webui, rclone, caplog):
    # A remote of another name or type isn't Backuparr's to restore: some
    # rclone remotes (sftp's ssh option) run a command.
    key = secrets_crypto.current_key()
    remotes = {
        "backuparr-dropbox": {"type": "dropbox", "token": "t"},
        "backuparr-gdrive": {"type": "sftp", "ssh": "sh -c 'id'"},
        "elsewhere": {"type": "sftp", "host": "example.com"},
    }
    config = json.dumps(json.loads(open(config_store.CONFIG_PATH).read()) if os.path.exists(config_store.CONFIG_PATH)
                        else config_store.DEFAULTS)
    archive = encrypted_zip({self_backup.META: META, "config.json": config, "auth.json": AUTH,
                             self_backup.REMOTES: json.dumps(remotes)}, key)
    self_restore.apply(self_restore.prepare(archive, key), key)
    assert rclone == {"backuparr-dropbox": {"type": "dropbox", "token": "t"}}
    assert "left out the rclone remote 'backuparr-gdrive' (sftp)" in caplog.text
    assert "'elsewhere'" in caplog.text


# --------------------------------------------------------------- web UI ----

def upload(archive, key, **extra):
    return {"archive": base64.b64encode(archive).decode(), "key": key, **extra}


def test_a_new_install_restores_on_the_setup_page_then_signs_in_with_the_old_login(old_install, isolated_webui):
    archive, key = old_install
    client = isolated_webui.app.test_client()
    assert client.get("/setup").status_code == 200
    response = client.post("/api/setup/restore", json=upload(archive, key))
    assert response.status_code == 200 and response.get_json() == {"ok": True, "warnings": []}
    assert client.get("/api/config").status_code == 401  # signed out
    assert client.post("/api/login", json={"username": "olduser", "password": PASSWORD}).status_code == 200
    assert config_store.load_config()["apps"]["radarr"]["api_key"] == "radarr-secret-key"
    # Set up now: the setup restore is closed.
    assert isolated_webui.app.test_client().post("/api/setup/restore", json=upload(archive, key)).status_code == 403


def test_the_setup_restore_reports_a_wrong_key_and_bad_uploads(old_install, isolated_webui):
    archive, _key = old_install
    client = isolated_webui.app.test_client()
    response = client.post("/api/setup/restore", json=upload(archive, Fernet.generate_key().decode()))
    assert response.status_code == 400 and "made with this key" in response.get_json()["error"]
    assert client.post("/api/setup/restore", json={"archive": "not base64!", "key": "k"}).status_code == 400
    assert client.post("/api/setup/restore", json={}).status_code == 400
    assert not auth_store.has_credentials()


def test_a_backup_from_before_3_0_says_to_restore_it_with_2_5_6(old_install, isolated_webui):
    _archive, key = old_install
    legacy = Fernet(key.encode()).encrypt(b"PK as 2.5.x wrote it")
    response = isolated_webui.app.test_client().post("/api/setup/restore", json=upload(legacy, key))
    assert response.status_code == 400
    assert response.get_json()["error"] == "this backup is from before Backuparr 3.0. Restore it with Backuparr 2.5.6, then update."
    assert not os.path.exists(config_store.CONFIG_PATH) and not auth_store.has_credentials()


def test_an_upload_over_the_limit_is_refused(isolated_webui):
    isolated_webui.app.config["MAX_CONTENT_LENGTH"] = 1000
    response = isolated_webui.app.test_client().post("/api/setup/restore", json={"archive": "A" * 5000, "key": "k"})
    assert response.status_code == 413


@pytest.fixture
def running_install(old_install, isolated_webui):
    """A set-up install with its own login, signed in."""
    archive, key = old_install
    client = isolated_webui.app.test_client()
    assert client.post("/api/setup", json={"username": "newadmin", "password": "new-admin-password"}).status_code == 200
    assert client.post("/api/secrets-key/acknowledge", json={"confirm": True}).status_code == 200
    return client, archive, key


def test_settings_restore_needs_the_confirmation_and_the_admin_password(running_install, isolated_webui):
    client, archive, key = running_install
    assert client.post("/api/backuparr/restore", json=upload(archive, key, password="new-admin-password")).status_code == 400
    response = client.post("/api/backuparr/restore", json=upload(archive, key, confirm=True, password="wrong"))
    assert response.status_code == 403
    assert auth_store.verify_password("newadmin", "new-admin-password")  # nothing changed


def test_wrong_passwords_count_towards_the_login_lockout(running_install, isolated_webui):
    client, archive, key = running_install
    codes = [client.post("/api/backuparr/restore", json=upload(archive, key, confirm=True, password="wrong")).status_code
             for _ in range(12)]
    assert codes[-1] == 429


def test_settings_restore_replaces_the_login_and_signs_everyone_out(running_install, isolated_webui):
    client, archive, key = running_install
    other = isolated_webui.app.test_client()
    other.post("/api/login", json={"username": "newadmin", "password": "new-admin-password"})
    old_session_key = isolated_webui.app.secret_key
    response = client.post("/api/backuparr/restore", json=upload(archive, key, confirm=True, password="new-admin-password"))
    assert response.status_code == 200
    assert isolated_webui.app.secret_key != old_session_key
    assert client.get("/api/config").status_code == 401 and other.get("/api/config").status_code == 401
    assert auth_store.verify_password("olduser", PASSWORD) and not auth_store.verify_password("newadmin", "new-admin-password")


def test_no_restore_while_a_backup_runs(running_install, isolated_webui):
    client, archive, key = running_install
    isolated_webui.runs.RUN_STATE["running"] = True
    try:
        response = client.post("/api/backuparr/restore", json=upload(archive, key, confirm=True, password="new-admin-password"))
    finally:
        isolated_webui.runs.RUN_STATE["running"] = False
    assert response.status_code == 409 and auth_store.verify_password("newadmin", "new-admin-password")


def test_settings_restore_of_a_backup_from_before_3_0_says_to_use_2_5_6(running_install, isolated_webui):
    client, _archive, key = running_install
    legacy = Fernet(key.encode()).encrypt(b"PK as 2.5.x wrote it")
    response = client.post("/api/backuparr/restore", json=upload(legacy, key, confirm=True, password="new-admin-password"))
    assert response.status_code == 400 and "Restore it with Backuparr 2.5.6, then update" in response.get_json()["error"]
    assert auth_store.verify_password("newadmin", "new-admin-password")
