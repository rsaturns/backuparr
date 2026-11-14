"""Less work where a part does (#231): the backup record and run logs are
parsed once and read again only when they change, retention writes the
record once, an encrypted backup isn't read again for its checksum, and
History syncs rclone.conf only when the settings or rclone.conf changed."""
import os
from datetime import datetime

import pytest

import archive_crypto
import backup
import manifest
import rclone_util
import runlog


@pytest.fixture
def loads(monkeypatch):
    """How many times each file was read and parsed."""
    counts = {"manifest": 0, "runlog": 0}
    for name, module in (("manifest", manifest), ("runlog", runlog)):
        real = module._load

        def counted(real=real, name=name):
            counts[name] += 1
            return real()
        monkeypatch.setattr(module, "_load", counted)
    return counts


def record(name, destinations=("local",)):
    manifest.record("radarr", name, list(destinations), app_version="6.4", sha256="a", size=1, backuparr_version="x")


def test_the_record_is_parsed_once_and_again_only_when_it_changes(loads, manifest_in_tmp):
    record("radarr_20261101_030000.zip")
    assert loads["manifest"] == 1  # the first read; its own write needs none
    for _ in range(5):
        assert "radarr/radarr_20261101_030000.zip" in manifest.backups()
        assert manifest.get("radarr", "radarr_20261101_030000.zip")["app_version"] == "6.4"
    record("radarr_20261102_030000.zip")
    manifest.remove("radarr", "radarr_20261101_030000.zip", "local")
    assert len(manifest.backups()) == 1 and loads["manifest"] == 1
    # Changed by something else (Backuparr's own restore, say): read again.
    with open(manifest_in_tmp, "w") as f:
        f.write('{"schema": 1, "backups": {}}')
    assert manifest.backups() == {} and loads["manifest"] == 2


def test_what_get_returns_can_be_changed_without_changing_the_record():
    record("radarr_20261101_030000.zip")
    manifest.get("radarr", "radarr_20261101_030000.zip")["destinations"].append("gdrive")
    assert manifest.get("radarr", "radarr_20261101_030000.zip")["destinations"] == ["local"]


def test_a_failed_write_is_forgotten(monkeypatch):
    record("radarr_20261101_030000.zip")

    def full(path, data):
        raise OSError(28, "No space left on device")
    real = manifest.atomic_file.write_private
    monkeypatch.setattr(manifest.atomic_file, "write_private", full)
    with pytest.raises(OSError):
        record("radarr_20261102_030000.zip")
    monkeypatch.setattr(manifest.atomic_file, "write_private", real)
    assert list(manifest.backups()) == ["radarr/radarr_20261101_030000.zip"]


def test_reset_clears_the_copy_in_memory_too():
    record("radarr_20261101_030000.zip")
    runlog.save({"id": "radarr_20261101_030000", "app": "radarr", "lines": []})
    manifest.clear()
    runlog.clear()
    assert manifest.backups() == {} and runlog.all_runs() == {}


def test_the_run_logs_are_parsed_once_and_again_only_when_they_change(loads):
    runlog.save({"id": "radarr_20261101_030000", "app": "radarr", "ok": True, "lines": [{"message": "done"}]})
    for _ in range(5):
        assert runlog.latest()["radarr"]["ok"] is True
        assert runlog.get("radarr", "radarr_20261101_030000")["lines"] == [{"message": "done"}]
    runlog.get("radarr", "radarr_20261101_030000")["lines"].clear()
    assert runlog.get("radarr", "radarr_20261101_030000")["lines"] == [{"message": "done"}]
    assert loads["runlog"] == 1


def test_retention_writes_the_record_once_for_everything_it_deletes(monkeypatch, manifest_in_tmp):
    names = [f"radarr_202610{day:02d}_030000.zip" for day in range(1, 11)]
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): [
        {"Path": f"radarr/{n}", "Name": n, "IsDir": False} for n in names])
    deleted = []
    monkeypatch.setattr(rclone_util, "delete_file", deleted.append)
    for name in names:
        record(name, ("local", "gdrive"))
    writes = []
    real = manifest.atomic_file.write_private
    monkeypatch.setattr(manifest.atomic_file, "write_private", lambda path, data: writes.append(path) or real(path, data))
    backup.apply_retention("dest:", 3, now=datetime(2026, 10, 10, 4), dest_id="local")
    assert len(deleted) == 7 and writes == [manifest_in_tmp]
    assert [k for k, e in manifest.backups().items() if "local" in e["destinations"]] == [
        f"radarr/{n}" for n in names[7:]]


def test_the_check_before_upload_gives_the_encrypted_files_checksum(tmp_path):
    src, sealed = tmp_path / "a.zip", tmp_path / "a.zip.enc"
    src.write_bytes(os.urandom(3 * 1024 * 1024 + 5))
    digest = archive_crypto.encrypt_file(src, sealed)
    assert archive_crypto.check_file(sealed, digest) == manifest.file_sha256(sealed)


def test_an_encrypted_backup_is_read_once_for_its_check_and_checksum(monkeypatch, tmp_path):
    archive = tmp_path / "radarr.zip"
    archive.write_bytes(b"PK" + os.urandom(1000))
    hashed, real = [], manifest.file_sha256
    monkeypatch.setattr(manifest, "file_sha256", lambda path: hashed.append(path) or "x")
    path, sha256 = backup.encrypt_archive("radarr", str(archive))
    assert hashed == [] and not archive.exists()
    assert sha256 == real(path)


def test_history_syncs_rclone_conf_only_when_something_changed(authed_client, isolated_webui, monkeypatch, tmp_path):
    dumps = []
    monkeypatch.setattr(isolated_webui.backups.destination_util, "sync", lambda cfg: dumps.append(1))
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): [])
    conf = os.environ["RCLONE_CONFIG"]
    with open(conf, "w") as f:
        f.write("")
    for _ in range(3):
        assert authed_client.get("/api/history/local").status_code == 200
    assert len(dumps) == 1
    assert authed_client.post("/api/config", json={"retention_days": 9}).status_code == 200  # Save syncs itself
    dumps.clear()
    authed_client.get("/api/history/local")
    authed_client.get("/api/history/local")
    assert len(dumps) == 1  # config.json changed since History last synced
    with open(conf, "a") as f:
        f.write("# a token rclone refreshed\n")
    authed_client.get("/api/history/local")
    authed_client.get("/api/history/local")
    assert len(dumps) == 2
