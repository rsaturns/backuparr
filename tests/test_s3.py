"""Cloud Storage (s3.py): every provider's remote, target and defaults,
the enable checks, and the rclone remote sync - including the secret
never reaching a command line unredacted."""
import zipfile

import pytest

import backup
import destination_util
import s3


def config(**overrides):
    dest = {"provider": "aws", "endpoint": "", "region": "", "bucket": "my-bucket",
            "path_prefix": "Backuparr", "access_key_id": "AKID", "secret_access_key": "SECRET"}
    dest.update(overrides)
    return dest


# provider, endpoint, region, label, rclone provider, default endpoint, default region
PROVIDERS = [
    ("aws", "", "", "AWS S3", "AWS", "", "us-east-1"),
    ("backblaze", "", "", "Backblaze B2", "Other", "https://s3.us-west-000.backblazeb2.com", "us-west-000"),
    ("cloudflare", "https://abc.r2.cloudflarestorage.com", "", "Cloudflare R2", "Cloudflare",
     "https://abc.r2.cloudflarestorage.com", "auto"),
    ("wasabi", "", "", "Wasabi", "Wasabi", "https://s3.wasabisys.com", "us-east-1"),
    ("minio", "http://minio:9000", "", "MinIO", "Minio", "http://minio:9000", ""),
    ("other", "https://s3.example.com", "eu-west-1", "Other S3-compatible", "Other",
     "https://s3.example.com", "eu-west-1"),
]


@pytest.mark.parametrize("provider,endpoint,region,label,rclone_provider,default_endpoint,default_region",
                         PROVIDERS)
def test_remote_fields_translate_each_provider(provider, endpoint, region, label, rclone_provider,
                                               default_endpoint, default_region):
    assert s3._remote_fields(config(provider=provider, endpoint=endpoint, region=region)) == {
        "provider": rclone_provider, "endpoint": default_endpoint, "region": default_region,
        "access_key_id": "AKID", "secret_access_key": "SECRET"}


@pytest.mark.parametrize("provider,endpoint,region,label,rclone_provider,default_endpoint,default_region",
                         PROVIDERS)
def test_remote_root_and_backup_target_for_each_provider(provider, endpoint, region, label, rclone_provider,
                                                         default_endpoint, default_region):
    dest = config(provider=provider, endpoint=endpoint, region=region)
    assert s3.remote_root(dest) == "backuparr-s3:my-bucket/Backuparr"
    assert s3.backup_target(dest) == f"the Backuparr folder in bucket my-bucket ({label})"


def test_enable_error_reports_the_first_missing_field():
    assert s3.enable_error(config(provider="")) == "Cloud Storage: choose a storage provider to enable it"
    assert s3.enable_error(config(provider="nope")) == "Cloud Storage: choose a storage provider to enable it"
    assert s3.enable_error(config(provider="cloudflare")) == "Cloud Storage: an endpoint URL is required for this provider"
    assert s3.enable_error(config(bucket="")) == "Cloud Storage: a bucket is required to enable it"
    assert s3.enable_error(config(access_key_id="")) == "Cloud Storage: an access key ID is required to enable it"
    assert s3.enable_error(config(secret_access_key="")) == "Cloud Storage: a secret access key is required to enable it"
    assert s3.enable_error(config()) is None


def test_path_prefix_is_trimmed_and_defaults_to_backuparr():
    assert s3.remote_root(config(path_prefix=" /photos/ ")) == "backuparr-s3:my-bucket/photos"
    assert s3.remote_root(config(path_prefix="")) == "backuparr-s3:my-bucket/Backuparr"


def test_remote_root_needs_a_complete_config():
    with pytest.raises(s3.S3Error, match="a bucket is required"):
        s3.remote_root(config(bucket=""))


def test_destination_util_resolves_s3():
    assert destination_util.remote_root("s3", config()) == "backuparr-s3:my-bucket/Backuparr"
    with pytest.raises(destination_util.DestinationError, match="a secret access key is required"):
        destination_util.remote_root("s3", config(secret_access_key=""))


@pytest.fixture
def rclone(monkeypatch):
    state = {"remotes": {}, "sets": [], "deleted": []}
    monkeypatch.setattr(s3.rclone_util, "config_dump", lambda: dict(state["remotes"]))
    monkeypatch.setattr(s3.rclone_util, "config_set", lambda name, kind, fields, force=False: state["sets"].append((name, kind, dict(fields), force)))
    monkeypatch.setattr(s3.rclone_util, "config_delete", state["deleted"].append)
    return state


def test_sync_removes_the_remote_when_disconnected(rclone):
    rclone["remotes"]["backuparr-s3"] = {"type": "s3"}
    s3.sync_rclone_remote(config(secret_access_key=""))
    assert rclone["deleted"] == ["backuparr-s3"] and rclone["sets"] == []


def test_sync_runs_nothing_when_disconnected_and_no_remote_exists(rclone):
    s3.sync_rclone_remote(config(secret_access_key=""))
    assert rclone["deleted"] == [] and rclone["sets"] == []


def test_sync_creates_a_missing_remote_with_the_provider_defaults(rclone):
    s3.sync_rclone_remote(config())
    assert rclone["sets"] == [("backuparr-s3", "s3",
                               {"provider": "AWS", "region": "us-east-1", "access_key_id": "AKID",
                                "secret_access_key": "SECRET"}, True)]


def test_sync_writes_nothing_when_the_remote_already_matches(rclone):
    rclone["remotes"]["backuparr-s3"] = {"type": "s3", "provider": "AWS", "endpoint": "",
                                         "region": "us-east-1", "access_key_id": "AKID",
                                         "secret_access_key": "SECRET"}
    s3.sync_rclone_remote(config())
    assert rclone["sets"] == []


def test_sync_rewrites_a_remote_that_differs(rclone):
    rclone["remotes"]["backuparr-s3"] = {"type": "s3", "provider": "AWS", "endpoint": "",
                                         "region": "eu-west-1", "access_key_id": "AKID",
                                         "secret_access_key": "SECRET"}
    s3.sync_rclone_remote(config())
    assert rclone["sets"] == [("backuparr-s3", "s3",
                               {"provider": "AWS", "region": "us-east-1", "access_key_id": "AKID",
                                "secret_access_key": "SECRET"}, True)]


def test_sync_redacts_the_secret_from_the_rclone_command(monkeypatch):
    calls = []

    def run(args, redact=(), timeout=300):
        calls.append((args, list(redact)))
        if args[:2] == ["config", "dump"]:
            return "{}"
        return ""

    monkeypatch.setattr(s3.rclone_util, "_run", run)
    s3.sync_rclone_remote(config())
    assert [c[0][:2] for c in calls] == [["config", "dump"], ["config", "create"]]
    assert calls[1][0][2] == "backuparr-s3" and "SECRET" in calls[1][1] and "AKID" not in calls[1][1]


def test_backups_upload_under_the_backuparr_folder(tmp_path, monkeypatch):
    class FakeApp:
        def backup(self, work_dir):
            path = tmp_path / "radarr_backup.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("a.txt", "x")
            return str(path)

    uploaded, retention = [], []
    cfg = {"retention_days": 7, "apps": {"radarr": {}}, "destinations": {"s3": dict(config(), enabled=True)}}
    monkeypatch.setattr(backup, "enabled_apps", lambda c: ["radarr"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda c: ["s3"])
    monkeypatch.setattr(backup.destination_util, "sync", lambda c: None)
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: FakeApp())
    monkeypatch.setattr(backup.rclone_util, "copyto", lambda src, dst: uploaded.append(dst))
    monkeypatch.setattr(backup, "apply_retention", lambda root, days, **kw: retention.append(root))
    ok, failed = backup.run_backup(cfg)
    assert (ok, failed) == (["radarr"], [])
    assert len(uploaded) == 1 and uploaded[0].startswith("backuparr-s3:my-bucket/Backuparr/radarr/radarr_")
    assert retention == ["backuparr-s3:my-bucket/Backuparr"]
