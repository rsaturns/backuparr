"""Cloud Storage settings, provider mapping and rclone boundaries."""
from copy import deepcopy
import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

import backup
import config_store
import destination_util
import rclone_util
import s3_storage


CONFIG = {"enabled": True, "provider": "aws", "bucket": "backups-test",
          "access_key_id": "example-key-id", "secret_access_key": "example-secret-value"}


@pytest.mark.parametrize("provider, endpoint, region, backend, expected_region, expected_endpoint", [
    ("aws", "", "", "AWS", "us-east-1", ""),
    ("aws", "https://s3.us-east-1.amazonaws.com", "", "AWS", "us-east-1", None),
    ("aws", "https://s3.amazonaws.com.example.test", "", "Other", "us-east-1", None),
    ("aws", "http://garage.example.test:3900", "garage", "Other", "garage", None),
    ("backblaze", "", "", "Other", "us-west-004", "https://s3.us-west-004.backblazeb2.com"),
    ("backblaze", "", "eu-central-003", "Other", "eu-central-003", "https://s3.eu-central-003.backblazeb2.com"),
    ("backblaze", "https://s3.eu-central-003.backblazeb2.com", "", "Other", "eu-central-003", None),
    ("cloudflare", "https://account.r2.cloudflarestorage.com", "", "Cloudflare", "auto", None),
    ("wasabi", "", "", "Wasabi", "us-east-1", "https://s3.wasabisys.com"),
    ("wasabi", "", "eu-central-1", "Wasabi", "eu-central-1", "https://s3.eu-central-1.wasabisys.com"),
    ("wasabi", "https://s3.eu-central-1.wasabisys.com", "", "Wasabi", "eu-central-1", None),
    ("minio", "http://minio:9000", "", "Minio", "us-east-1", None),
    ("other", "https://garage.example.test:3900", "garage", "Other", "garage", None),
])
def test_provider_roots_and_rclone_options(monkeypatch, provider, endpoint, region, backend,
                                          expected_region, expected_endpoint):
    dest = {**CONFIG, "provider": provider, "endpoint": endpoint, "region": region}
    set_remote = Mock()
    monkeypatch.setattr(rclone_util, "config_set", set_remote)
    s3_storage.sync_rclone_remote(dest, remotes={})
    name, kind, fields = set_remote.call_args.args
    root = destination_util.remote_root("s3", dest)
    assert root == f"{name}:backups-test/Backuparr"
    assert kind == "s3" and set_remote.call_args.kwargs == {"force": True}
    assert fields == {
        "provider": backend, "endpoint": endpoint if expected_endpoint is None else expected_endpoint,
        "region": expected_region, "env_auth": "false",
        "access_key_id": CONFIG["access_key_id"], "secret_access_key": CONFIG["secret_access_key"],
        "no_check_bucket": "true",
    }

    set_remote.reset_mock()
    s3_storage.sync_rclone_remote(dest, remotes={name: {"type": kind, **fields}})
    set_remote.assert_not_called()
    rotated = {**dest, "secret_access_key": "example-next-secret-value"}
    s3_storage.sync_rclone_remote(rotated, remotes={name: {"type": kind, **fields}})
    assert set_remote.call_args.args[0] == name
    assert set_remote.call_args.args[2]["secret_access_key"] == rotated["secret_access_key"]


def test_target_change_retires_only_the_old_cloud_remote(monkeypatch):
    set_remote, delete_remote = Mock(), Mock()
    monkeypatch.setattr(rclone_util, "config_set", set_remote)
    monkeypatch.setattr(rclone_util, "config_delete", delete_remote)
    old_name = s3_storage.remote_root(CONFIG).split(":", 1)[0]
    dest = {**CONFIG, "provider": "minio", "endpoint": "http://minio:9000", "prefix": "offsite/family"}
    s3_storage.sync_rclone_remote(dest, remotes={old_name: {"type": "s3"}, "my-s3": {"type": "s3"}})
    new_name = set_remote.call_args.args[0]
    assert new_name != old_name
    assert s3_storage.remote_root(dest) == f"{new_name}:backups-test/offsite/family"
    delete_remote.assert_called_once_with(old_name)

    set_remote.reset_mock()
    delete_remote.reset_mock()
    s3_storage.sync_rclone_remote({**dest, "enabled": False}, remotes={new_name: {"type": "s3"}})
    set_remote.assert_not_called()
    delete_remote.assert_called_once_with(new_name)


@pytest.mark.parametrize("edits, message", [
    ({"provider": "unknown"}, "supported storage provider"),
    ({"provider": "cloudflare"}, "endpoint URL is required"),
    ({"provider": "minio"}, "endpoint URL is required"),
    ({"provider": "other"}, "endpoint URL is required"),
    ({"provider": "wasabi", "endpoint": "http://s3.example.test"}, "S3 API endpoint URL"),
    ({"provider": "minio", "endpoint": "https://user:pass@minio.test"}, "S3 API endpoint URL"),
    ({"provider": "other", "endpoint": "https://garage.test?option=1"}, "S3 API endpoint URL"),
    ({"region": "foo/bar"}, "valid region"),
    ({"bucket": "not/a/bucket"}, "bucket name"),
    ({"bucket": "Backups-Test"}, "bucket name"),
    ({"provider": "backblaze", "bucket": "not/a/bucket"}, "bucket name"),
    ({"endpoint": "https://s3.example.test/unexpected-path"}, "S3 API endpoint URL"),
    ({"endpoint": "ftp://s3.example.test"}, "S3 API endpoint URL"),
    ({"prefix": "/backups/../other/"}, "relative path prefix"),
    ({"prefix": "/backups//other/"}, "relative path prefix"),
    ({"prefix": "/backups/\n"}, "single-line text"),
    ({"secret_access_key": "test\ninjected"}, "single-line text"),
])
def test_rejects_invalid_settings(authed_client, edits, message):
    result = authed_client.post("/api/config", json={"destinations": {"s3": {**CONFIG, **edits}}})
    assert result.status_code == 400
    assert message in result.get_json()["error"]


def test_old_config_loads_with_cloud_storage_disabled(authed_client):
    Path(config_store.CONFIG_PATH).write_text(json.dumps({"destinations": {"local": {"enabled": True}}}))
    cfg = authed_client.get("/api/config").get_json()["destinations"]
    assert cfg["local"]["enabled"] is True
    assert cfg["s3"]["enabled"] is False and cfg["s3"]["has_secret_access_key"] is False


def test_save_encrypts_credentials_and_keeps_an_omitted_secret(authed_client):
    def save(edits):
        return authed_client.post("/api/config", json={"destinations": {"s3": edits}})

    assert save(CONFIG).status_code == 200
    stored = Path(config_store.CONFIG_PATH).read_text()
    assert CONFIG["access_key_id"] not in stored and CONFIG["secret_access_key"] not in stored
    public = authed_client.get("/api/config").get_json()["destinations"]["s3"]
    assert public["has_secret_access_key"] is True and "secret_access_key" not in public

    assert save({"retention_days": 30}).status_code == 200
    assert config_store.load_config()["destinations"]["s3"]["secret_access_key"] == CONFIG["secret_access_key"]
    for change in ({"provider": "minio", "endpoint": "http://minio:9000"},
                   {"endpoint": "https://s3.example.test"}):
        result = save(change)
        assert result.status_code == 400
        assert "secret access key" in result.get_json()["error"]
    saved = config_store.load_config()["destinations"]["s3"]
    assert saved["provider"] == "aws" and saved["endpoint"] == ""


def test_connection_test_uses_unsaved_details_without_saving_them(authed_client, monkeypatch):
    check = Mock()
    monkeypatch.setattr(rclone_util, "check_bucket", check)
    endpoint = "http://garage.example.test:3900"
    result = authed_client.post("/api/test-destination/s3", json={**CONFIG, "endpoint": endpoint, "prefix": " /try/this/ "})
    assert result.status_code == 200
    assert result.get_json() == {"ok": True, "message": "can list backups-test/try/this"}
    assert check.call_args.args[0] == "backuparr_s3_test:backups-test/try/this"
    assert check.call_args.args[1]["endpoint"] == endpoint
    assert check.call_args.args[1]["provider"] == "Other"
    assert config_store.load_config()["destinations"]["s3"]["enabled"] is False
    assert config_store.load_config()["destinations"]["s3"]["secret_access_key"] == ""


def test_backblaze_accepts_bucket_name_as_displayed_and_uses_default_location(authed_client, monkeypatch):
    check = Mock()
    monkeypatch.setattr(rclone_util, "check_bucket", check)
    cfg = {**CONFIG, "provider": "backblaze", "bucket": "My-Backup-Bucket", "endpoint": "", "region": ""}

    tested = authed_client.post("/api/test-destination/s3", json=cfg)
    assert tested.get_json() == {"ok": True, "message": "can list my-backup-bucket/Backuparr"}
    root, fields = check.call_args.args
    assert root == "backuparr_s3_test:my-backup-bucket/Backuparr"
    assert fields["endpoint"] == "https://s3.us-west-004.backblazeb2.com"
    assert fields["region"] == "us-west-004"
    assert authed_client.post("/api/config", json={"destinations": {"s3": cfg}}).status_code == 200
    saved = config_store.load_config()["destinations"]["s3"]
    assert saved["bucket"] == "my-backup-bucket"
    assert destination_util.remote_root("s3", saved).endswith(":my-backup-bucket/Backuparr")

    new_region = {"region": "eu-central-003"}
    tested = authed_client.post("/api/test-destination/s3", json=new_region)
    assert tested.status_code == 400
    assert "secret access key" in tested.get_json()["message"]
    assert authed_client.post("/api/config", json={"destinations": {"s3": new_region}}).status_code == 400
    assert config_store.load_config()["destinations"]["s3"] == saved


def test_bucket_test_does_not_put_secrets_in_argv_or_errors(monkeypatch):
    key, secret = CONFIG["access_key_id"], CONFIG["secret_access_key"]
    run = Mock(return_value=CompletedProcess([], 1, "", f"rejected {key}/{secret}"))
    monkeypatch.setattr(rclone_util.subprocess, "run", run)

    with pytest.raises(rclone_util.RcloneError) as error:
        s3_storage.test_connection(CONFIG)

    args = run.call_args.args[0]
    env = run.call_args.kwargs["env"]
    assert args == ["rclone", "lsf", "--max-depth", "1", "backuparr_s3_test:backups-test/Backuparr"]
    assert env["RCLONE_CONFIG_BACKUPARR_S3_TEST_TYPE"] == "s3"
    assert env["RCLONE_CONFIG_BACKUPARR_S3_TEST_SECRET_ACCESS_KEY"] == secret
    assert key not in str(error.value) and secret not in str(error.value)
    assert error.value.detail == "rejected ***/***"


def test_cloud_storage_space_is_unknown_without_calling_about(authed_client, monkeypatch):
    assert authed_client.post("/api/config", json={"destinations": {"s3": CONFIG}}).status_code == 200
    about = Mock()
    monkeypatch.setattr(rclone_util, "about", about)
    result = authed_client.get("/api/storage/s3/space")
    assert result.status_code == 200 and result.get_json() == {"total": None, "free": None}
    about.assert_not_called()


@pytest.mark.parametrize("pasted, saved, target", [
    (" /offsite/backups/ ", "offsite/backups", "offsite/backups"),
    ("/", "", "Backuparr"),
    (" existing folder ", " existing folder ", " existing folder "),
])
def test_pasted_prefix_is_saved_and_resolved_consistently(authed_client, pasted, saved, target):
    settings = {**CONFIG, "prefix": pasted}
    response = authed_client.post("/api/config", json={"destinations": {"s3": settings}})
    assert response.status_code == 200
    actual = config_store.load_config()["destinations"]["s3"]
    assert actual["prefix"] == saved
    assert s3_storage.remote_root(actual) == s3_storage.remote_root(settings)
    assert s3_storage.backup_target(actual) == f"backups-test/{target}"


def test_cloud_backup_run_uploads_and_applies_retention_in_the_same_prefix(tmp_path, monkeypatch):
    cfg = deepcopy(config_store.DEFAULTS)
    cfg["self_backup"] = False
    cfg["apps"]["radarr"].update(enabled=True, url="http://radarr.example.test", api_key="fixture", encrypt=False)
    cfg["destinations"]["local"]["enabled"] = False
    dest = {**CONFIG, "endpoint": "https://s3.example.test", "prefix": "/offsite/backups/", "retention_days": 1}
    cfg["destinations"]["s3"].update(dest)
    root = s3_storage.remote_root(dest)
    assert root.endswith(":backups-test/offsite/backups")

    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"new backup")
    app = Mock()
    app.backup.return_value = archive
    app.app_version.return_value = "6.4.0"
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: app)

    expired = f"{root}/radarr/radarr_20000101_120000.zip"
    unrelated = f"{root}/radarr/notes.txt"
    objects = {expired: b"expired backup", unrelated: b"keep me"}

    def copy_file(source, key):
        objects[key] = Path(source).read_bytes()

    def list_files(prefix, **kwargs):
        return [{"Path": key.removeprefix(prefix + "/")} for key in objects if key.startswith(prefix + "/")]

    monkeypatch.setattr(rclone_util, "config_dump", lambda: {})
    monkeypatch.setattr(rclone_util, "config_set", Mock())
    monkeypatch.setattr(rclone_util, "copyto", copy_file)
    monkeypatch.setattr(rclone_util, "lsjson", list_files)
    monkeypatch.setattr(rclone_util, "delete_file", objects.pop)

    assert backup.run_backup(cfg, only="radarr") == (["radarr"], [])
    assert expired not in objects and objects.pop(unrelated) == b"keep me"
    (uploaded, content), = objects.items()
    assert uploaded.startswith(f"{root}/radarr/radarr_") and uploaded.endswith(".zip")
    assert content == b"new backup"
