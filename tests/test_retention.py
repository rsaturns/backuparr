"""Retention only ever deletes Backuparr's own <app>/<app>_<timestamp>
archives, and always keeps each app's newest one."""
from datetime import datetime

import pytest

import backup
import destination_util

NOW = datetime(2026, 10, 29, 3, 0, 0)


@pytest.fixture
def remote(monkeypatch):
    state = type("Remote", (), {})()
    state.files, state.deleted, state.fail = [], [], set()

    def lsjson(root, recursive=False):
        assert recursive and root == "dest:"
        return [{"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False} for p in state.files]

    def delete_file(path):
        if path in state.fail:
            raise backup.rclone_util.RcloneError("rclone deletefile failed", "permission denied")
        state.deleted.append(path)

    monkeypatch.setattr(backup.rclone_util, "lsjson", lsjson)
    monkeypatch.setattr(backup.rclone_util, "delete_file", delete_file)
    return state


def test_old_backups_go_but_the_newest_and_recent_ones_stay(remote):
    remote.files = ["radarr/radarr_20261001_030000.zip", "radarr/radarr_20261020_030000.zip",
                    "radarr/radarr_20261028_030000.zip", "radarr/radarr_20261029_030000.zip"]
    backup.apply_retention("dest:", 7, now=NOW)
    assert remote.deleted == ["dest:/radarr/radarr_20261001_030000.zip", "dest:/radarr/radarr_20261020_030000.zip"]


def test_an_app_that_stopped_backing_up_keeps_its_last_backup(remote):
    # Failing or switched off for longer than retention: its last good backup stays.
    remote.files = ["plex/plex_20260801_030000.zip", "plex/plex_20260901_030000.zip"]
    backup.apply_retention("dest:", 7, now=NOW)
    assert remote.deleted == ["dest:/plex/plex_20260801_030000.zip"]


@pytest.mark.parametrize("path", [
    "radarr/holiday.jpg", "radarr/radarr_backup.zip", "photos/radarr_20260101_030000.zip",
    "radarr_20260101_030000.zip", "radarr/sub/radarr_20260101_030000.zip",
    "notanapp/notanapp_20260101_030000.zip", "radarr/sonarr_20260101_030000.zip",
    "radarr/radarr_20261399_030000.zip",
])
def test_nothing_but_backuparrs_own_archives_is_ever_deleted(remote, path):
    remote.files = [path, "radarr/radarr_20261029_030000.zip"]
    backup.apply_retention("dest:", 1, now=NOW)
    assert remote.deleted == []


def test_profilarrs_tar_archives_count_too(remote):
    remote.files = ["profilarr/profilarr_20260101_030000.tar.gz", "profilarr/profilarr_20261029_030000.tar.gz"]
    backup.apply_retention("dest:", 7, now=NOW)
    assert remote.deleted == ["dest:/profilarr/profilarr_20260101_030000.tar.gz"]


def test_a_failed_delete_is_logged_and_the_rest_continue(remote, caplog):
    remote.files = ["radarr/radarr_20260101_030000.zip", "radarr/radarr_20260102_030000.zip", "radarr/radarr_20261029_030000.zip"]
    remote.fail = {"dest:/radarr/radarr_20260101_030000.zip"}
    backup.apply_retention("dest:", 7, now=NOW)
    assert remote.deleted == ["dest:/radarr/radarr_20260102_030000.zip"]
    assert "couldn't remove radarr/radarr_20260101_030000.zip: permission denied" in caplog.text


def test_names_are_recognised_exactly():
    assert destination_util.is_backup_name("radarr", "radarr_20261029_030000.zip")
    assert destination_util.is_backup_name("profilarr", "profilarr_20261029_030000.tar.gz")
    assert not destination_util.is_backup_name("radarr", "radarr_20261029_030000")
    assert not destination_util.is_backup_name("radarr", "xradarr_20261029_030000.zip")
    assert not destination_util.is_backup_name("radarr", "radarr_20261029_030000.zip/../x")


def test_a_real_local_folder_with_other_files(tmp_path, monkeypatch):
    # End to end against a local destination, through the real rclone listing shape.
    for rel in ("radarr/radarr_20260101_030000.zip", "radarr/radarr_20261029_030000.zip", "radarr/keep.txt", "media/old.mkv"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    root = str(tmp_path)

    def lsjson(r, recursive=False):
        return [{"Path": p.relative_to(tmp_path).as_posix(), "Name": p.name, "IsDir": p.is_dir()} for p in tmp_path.rglob("*")]

    monkeypatch.setattr(backup.rclone_util, "lsjson", lsjson)
    monkeypatch.setattr(backup.rclone_util, "delete_file", lambda path: __import__("os").remove(path))
    backup.apply_retention(root, 7, now=NOW)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == [
        "media/old.mkv", "radarr/keep.txt", "radarr/radarr_20261029_030000.zip",
    ]
