"""Backuparr's own backup (#110): the encrypted archive, the run that makes
it, where it shows up, and the encryption key prompt."""
import hashlib
import json
import os

import pytest
from cryptography.fernet import Fernet

import auth_store
import backup
import config_store
import destination_util
import manifest
import rclone_util
import runlog
import secrets_crypto
import self_backup

REMOTES = {"backuparr-gdrive": {"type": "drive", "token": '{"refresh_token": "drive-secret"}'}}


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Backuparr's files in tmp_path, a fresh key and a fake rclone."""
    monkeypatch.delenv("BACKUPARR_SECRETS_KEY", raising=False)
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(auth_store, "AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "secrets.key"))
    secrets_crypto.forget_key()
    monkeypatch.setattr(rclone_util, "config_dump", lambda: REMOTES)
    cfg = config_store.load_config()
    cfg["apps"]["radarr"].update(enabled=True, url="http://radarr:7878", api_key="radarr-secret-key")
    config_store.save_config(cfg)
    auth_store.set_credentials("admin", "a-long-password")
    manifest.record("radarr", "radarr_1.zip", ["local"], app_version="6.4", sha256="a", size=1, backuparr_version="2.5.0")
    runlog.save({"id": "radarr_20261101_030000", "app": "radarr", "lines": [], "destinations": {}, "ok": True})
    (tmp_path / "secret_key").write_bytes(b"session-signing-key")
    (tmp_path / "rclone.pass").write_text("rclone-password")
    yield tmp_path
    secrets_crypto.forget_key()


# -------------------------------------------------------------- archive ----

def test_the_archive_holds_everything_needed_and_opens_only_with_the_key(state):
    data = self_backup.build()
    assert b"radarr-secret-key" not in data and b"drive-secret" not in data  # it's encrypted
    members = self_backup.open_archive(data)
    assert set(members) == {"config.json", "auth.json", "manifest.json", "run_logs.json", "rclone-remotes.json", "backuparr-meta.json"}
    config = json.loads(members["config.json"])
    assert config["apps"]["radarr"]["api_key"].startswith(secrets_crypto.PREFIX)  # stays encrypted inside
    assert json.loads(members["auth.json"])["username"] == "admin"
    assert json.loads(members["rclone-remotes.json"]) == REMOTES
    meta = json.loads(members["backuparr-meta.json"])
    assert meta["format"] == 1 and meta["backuparr_version"] == backup.VERSION
    assert "radarr/radarr_1.zip" in json.loads(members["manifest.json"])["backups"]
    # The key, the session key and rclone's password are never in it.
    flat = b"".join(members.values())
    assert secrets_crypto.current_key().encode() not in flat
    assert b"session-signing-key" not in flat and b"rclone-password" not in flat


def test_the_archive_opens_with_the_key_given(state):
    data = self_backup.build()
    key = secrets_crypto.current_key()
    secrets_crypto.forget_key()
    os.remove(secrets_crypto.KEY_PATH)  # a new install, with its own key
    assert self_backup.open_archive(data, key)["config.json"]


def test_a_wrong_key_or_a_changed_archive_is_refused(state):
    data = self_backup.build()
    with pytest.raises(self_backup.SelfBackupError, match="isn't a Backuparr backup made with this key, or it's damaged"):
        self_backup.open_archive(data, Fernet.generate_key().decode())
    changed = bytearray(data)
    changed[len(changed) // 2] ^= 1
    with pytest.raises(self_backup.SelfBackupError):
        self_backup.open_archive(bytes(changed))
    with pytest.raises(self_backup.SelfBackupError):
        self_backup.open_archive(b"not even close")


def test_an_archive_without_a_config_is_refused(state):
    os.remove(config_store.CONFIG_PATH)
    with pytest.raises(self_backup.SelfBackupError, match="has no config.json"):
        self_backup.open_archive(self_backup.build())


# ------------------------------------------------------------------ run ----

@pytest.fixture
def uploads(monkeypatch):
    sent, failing = {}, set()

    def copyto(src, dst):
        if dst.split(":")[0] in failing:
            raise rclone_util.RcloneError("copyto failed", "quota exceeded")
        sent[dst] = open(src, "rb").read()

    monkeypatch.setattr(rclone_util, "copyto", copyto)
    return type("Uploads", (), {"sent": sent, "failing": failing})


@pytest.mark.self_backup
def test_run_uploads_to_every_destination_and_records_it(state, uploads):
    assert self_backup.run({"local": "local:", "gdrive": "gdrive:"}, "20261104_030000") is None
    assert set(uploads.sent) == {"local:/backuparr/backuparr_20261104_030000.enc", "gdrive:/backuparr/backuparr_20261104_030000.enc"}
    data = uploads.sent["gdrive:/backuparr/backuparr_20261104_030000.enc"]
    assert self_backup.open_archive(data)["config.json"]
    entry = manifest.get("backuparr", "backuparr_20261104_030000.enc")
    assert entry["destinations"] == ["gdrive", "local"] and entry["app_version"] == backup.VERSION
    assert entry["sha256"] == hashlib.sha256(data).hexdigest() and entry["size"] == len(data)


@pytest.mark.self_backup
def test_run_reports_failed_uploads_and_a_backup_it_couldnt_make(state, uploads, monkeypatch):
    uploads.failing.add("gdrive")
    assert self_backup.run({"local": "local:", "gdrive": "gdrive:"}, "20261104_030000") == "failed on gdrive: quota exceeded"

    def broken():
        raise rclone_util.RcloneError("config dump failed", "couldn't read rclone.conf")

    monkeypatch.setattr(rclone_util, "config_dump", broken)
    assert self_backup.run({"local": "local:"}, "20261104_040000") == "couldn't read rclone.conf"


@pytest.mark.self_backup
def test_run_keeps_its_log_like_an_app(state, uploads):
    """The Overview's Backuparr card and History read it (#110 follow-up)."""
    uploads.failing.add("gdrive")
    self_backup.run({"local": "local:", "gdrive": "gdrive:"}, "20261104_030000")
    latest = runlog.latest()["backuparr"]
    assert latest["id"] == "backuparr_20261104_030000" and latest["file"] == "backuparr_20261104_030000.enc"
    assert latest["ok"] is False and latest["error"] == "failed on gdrive: quota exceeded"
    assert latest["destinations"] == {"local": {"ok": True, "error": None}, "gdrive": {"ok": False, "error": "quota exceeded"}}
    lines = runlog.get("backuparr", "backuparr_20261104_030000")["lines"]
    assert any("uploaded its own backup to local" in line["message"] for line in lines)

    uploads.failing.clear()
    self_backup.run({"local": "local:"}, "20261104_040000")
    assert runlog.latest()["backuparr"]["ok"] is True


@pytest.fixture
def full_run(state, uploads, monkeypatch):
    calls = []
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["radarr"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local"])
    monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: "local:")
    monkeypatch.setattr(backup, "apply_retention", lambda *args, **kwargs: None)
    monkeypatch.setattr(self_backup, "run", lambda roots, stamp: calls.append((roots, stamp)) or calls_result[0])

    class App:
        def backup(self, dest_dir):
            path = os.path.join(dest_dir, "r.zip")
            open(path, "wb").write(b"x")
            return path

        def app_version(self):
            return "6.4"

    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: App())
    calls_result = [None]
    return type("Full", (), {"calls": calls, "result": calls_result})


def cfg(**extra):
    return {"apps": {"radarr": {"url": "http://radarr:7878"}}, "destinations": {"local": {}}, **extra}


@pytest.mark.self_backup
def test_a_full_run_ends_with_backuparrs_own_backup(full_run):
    progress = []
    ok, failed = backup.run_backup(cfg(), on_progress=lambda *step: progress.append(step))
    # Reported like an app (#165): the last step, and in the OK list.
    assert ok == ["radarr", "backuparr"] and failed == [] and len(full_run.calls) == 1
    assert full_run.calls[0][0] == {"local": "local:"}
    assert progress == [(1, 2, "radarr"), (2, 2, "backuparr")]


@pytest.mark.self_backup
def test_a_single_app_run_or_the_setting_turned_off_skips_it(full_run):
    progress = []
    assert backup.run_backup(cfg(), only="radarr") == (["radarr"], [])
    assert backup.run_backup(cfg(self_backup=False), on_progress=lambda *step: progress.append(step)) == (["radarr"], [])
    assert full_run.calls == [] and progress == [(1, 1, "radarr")]


@pytest.mark.self_backup
def test_a_failed_own_backup_is_a_failed_item(full_run):
    full_run.result[0] = "failed on local: disk full"
    ok, failed = backup.run_backup(cfg())
    assert ok == ["radarr"] and failed == ["backuparr: failed on local: disk full"]


@pytest.mark.self_backup
def test_backuparr_can_be_backed_up_on_its_own(full_run, monkeypatch):
    """Run & Status's picker (#165): no app is backed up, and retention only
    touches Backuparr's own backups."""
    retained = []
    monkeypatch.setattr(backup, "apply_retention", lambda root, days, only=None, dest_id=None: retained.append(only))
    monkeypatch.setattr(backup, "build_app", lambda *a: pytest.fail("no app is backed up"))
    assert backup.run_backup(cfg(), only="backuparr") == (["backuparr"], [])
    assert len(full_run.calls) == 1 and retained == ["backuparr"]


@pytest.mark.self_backup
def test_backuparr_on_its_own_needs_its_card_on(full_run):
    assert backup.run_backup(cfg(self_backup=False), only="backuparr") == (
        [], ["backuparr: its own backup is turned off in Settings"])
    assert full_run.calls == []


@pytest.mark.self_backup
def test_with_no_app_enabled_a_run_still_backs_up_backuparr(full_run, monkeypatch):
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: [])
    assert backup.run_backup(cfg()) == (["backuparr"], [])
    assert backup.run_backup(cfg(self_backup=False)) == ([], ["no apps enabled in config"])


def test_retention_keeps_backuparrs_newest_and_drops_old_ones(monkeypatch):
    from datetime import datetime
    files = ["backuparr/backuparr_20261001_030000.enc", "backuparr/backuparr_20261030_030000.enc", "backuparr/notes.txt"]
    deleted = []
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): [
        {"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False} for p in files])
    monkeypatch.setattr(rclone_util, "delete_file", deleted.append)
    backup.apply_retention("dest:", 7, now=datetime(2026, 10, 31, 3))
    assert deleted == ["dest:/backuparr/backuparr_20261001_030000.enc"]


# --------------------------------------------------------------- web UI ----

def test_history_lists_backuparrs_own_backups_for_download_and_delete_only(authed_client, monkeypatch, tmp_path):
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): [
        {"Path": "backuparr/backuparr_20261104_030000.enc", "Name": "backuparr_20261104_030000.enc", "IsDir": False,
         "Size": 10, "ModTime": "2026-11-04T03:00:00Z"}])
    deleted = []
    monkeypatch.setattr(rclone_util, "delete_file", deleted.append)
    authed_client.post("/api/config", json={"destinations": {"local": {"enabled": True}}})
    history = authed_client.get("/api/history/local").get_json()
    assert [e["name"] for e in history["backuparr"]] == ["backuparr_20261104_030000.enc"]
    assert authed_client.delete("/api/history/local/backuparr/backuparr_20261104_030000.enc").status_code == 200
    assert deleted and deleted[0].endswith("backuparr/backuparr_20261104_030000.enc")
    assert authed_client.post("/api/restore/local/backuparr", json={"confirm": True}).status_code == 404
    assert authed_client.post("/api/restore/local/backuparr/check", json={"file": "backuparr_1.enc"}).status_code == 404


def test_settings_turn_it_off_and_check_the_value(authed_client):
    assert authed_client.get("/api/config").get_json()["self_backup"] is True
    assert authed_client.post("/api/config", json={"self_backup": "no"}).status_code == 400
    assert authed_client.post("/api/config", json={"self_backup": False}).status_code == 200
    assert authed_client.get("/api/config").get_json()["self_backup"] is False


def test_reset_removes_local_own_backups_and_starts_a_new_key(authed_client, isolated_webui, tmp_path):
    folder = tmp_path / "backups" / "backuparr"
    folder.mkdir(parents=True)
    (folder / "backuparr_20261104_030000.enc").write_bytes(b"x")
    old_key = secrets_crypto.current_key()
    authed_client.post("/api/reset", json={"confirm": isolated_webui.auth.RESET_CONFIRM_PHRASE})
    assert not (folder / "backuparr_20261104_030000.enc").exists()
    # The key in use is the new one on disk, not the deleted one still in memory.
    new_key = secrets_crypto.current_key()
    assert new_key != old_key and open(secrets_crypto.KEY_PATH).read().strip() == new_key
    assert secrets_crypto.decrypt(secrets_crypto.encrypt("x")) == "x"


# ------------------------------------------------------------ key prompt ----

@pytest.mark.key_prompt
def test_until_the_key_is_saved_only_the_prompt_and_logout_work(authed_client):
    response = authed_client.get("/api/config")
    assert response.status_code == 403 and response.get_json()["key_required"] is True
    assert authed_client.post("/api/backup/run", json={}).status_code == 403
    shown = authed_client.get("/api/secrets-key")
    assert shown.status_code == 200 and shown.headers["Cache-Control"] == "no-store"
    assert shown.get_json() == {"acknowledged": False, "key": secrets_crypto.current_key(), "from_environment": False}
    assert authed_client.post("/api/logout", json={}).status_code == 200


@pytest.mark.key_prompt
def test_confirming_lets_everything_through_and_the_key_is_never_shown_again(authed_client, isolated_webui):
    assert authed_client.post("/api/secrets-key/acknowledge", json={}).status_code == 400
    assert authed_client.post("/api/secrets-key/acknowledge", json={"confirm": True}).status_code == 200
    assert authed_client.get("/api/config").status_code == 200
    assert authed_client.get("/api/secrets-key").get_json() == {"acknowledged": True}
    # Remembered in config.json, so a restart doesn't ask again.
    config_store.forget_secrets_key_acknowledged()
    assert config_store.secrets_key_acknowledged() is True


@pytest.mark.key_prompt
def test_a_key_from_the_environment_is_shown_the_same_way(authed_client, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BACKUPARR_SECRETS_KEY", key)
    secrets_crypto.forget_key()
    assert authed_client.get("/api/secrets-key").get_json() == {"acknowledged": False, "key": key, "from_environment": True}


@pytest.mark.key_prompt
def test_an_anonymous_visitor_never_sees_the_key(isolated_webui):
    client = isolated_webui.app.test_client()
    client.post("/api/setup", json={"username": "admin", "password": "a-long-password"})
    anonymous = isolated_webui.app.test_client()
    assert anonymous.get("/api/secrets-key").status_code == 401


@pytest.mark.key_prompt
def test_reset_asks_for_the_new_key_to_be_saved(authed_client, isolated_webui):
    authed_client.post("/api/secrets-key/acknowledge", json={"confirm": True})
    authed_client.post("/api/reset", json={"confirm": isolated_webui.auth.RESET_CONFIRM_PHRASE})
    client = isolated_webui.app.test_client()
    client.post("/api/setup", json={"username": "admin", "password": "a-long-password"})
    assert client.get("/api/secrets-key").get_json()["acknowledged"] is False
