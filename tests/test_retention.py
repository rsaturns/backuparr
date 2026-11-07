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

    def lsjson(root, recursive=False, include=()):
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


def test_a_single_app_run_only_prunes_that_apps_backups(remote):
    remote.files = ["radarr/radarr_20260101_030000.zip", "radarr/radarr_20261029_030000.zip",
                    "radarr-4k/radarr-4k_20260101_030000.zip", "radarr-4k/radarr-4k_20261029_030000.zip"]
    backup.apply_retention("dest:", 7, now=NOW, only="radarr-4k")
    assert remote.deleted == ["dest:/radarr-4k/radarr-4k_20260101_030000.zip"]


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

    def lsjson(r, recursive=False, include=()):
        return [{"Path": p.relative_to(tmp_path).as_posix(), "Name": p.name, "IsDir": p.is_dir()} for p in tmp_path.rglob("*")]

    monkeypatch.setattr(backup.rclone_util, "lsjson", lsjson)
    monkeypatch.setattr(backup.rclone_util, "delete_file", lambda path: __import__("os").remove(path))
    backup.apply_retention(root, 7, now=NOW)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == [
        "media/old.mkv", "radarr/keep.txt", "radarr/radarr_20261029_030000.zip",
    ]


def test_a_destination_that_cant_be_listed_is_left_alone(monkeypatch, caplog):
    def expired(root, recursive=False, include=()):
        raise backup.rclone_util.RcloneError("rclone lsjson failed", "token expired", 1)

    monkeypatch.setattr(backup.rclone_util, "lsjson", expired)
    backup.apply_retention("dest:", 7, now=NOW)
    assert "retention: couldn't list dest:, nothing removed: token expired" in caplog.text


# --- each destination's own retention (#133) -------------------------------

def _cfg(**dest_days):
    return {"retention_days": 7, "destinations": {d: {"retention_days": days} for d, days in dest_days.items()}}


@pytest.mark.parametrize("own, expected", [(28, 28), (1, 1), (None, 7), (0, 7), (-2, 7), ("28", 7), (True, 7), (2.5, 7)])
def test_a_destination_keeps_its_own_days_or_the_global_ones(own, expected):
    assert backup.retention_days(_cfg(local=own), "local") == expected


def test_a_destination_missing_from_the_config_uses_the_global_days():
    assert backup.retention_days({"retention_days": 14, "destinations": {}}, "dropbox") == 14


class _FakeApp:
    def backup(self, dest_dir):
        path = __import__("os").path.join(dest_dir, "out.zip")
        with open(path, "wb") as f:
            f.write(b"archive")
        return path

    def app_version(self):
        return "5.1.3"


@pytest.fixture
def run(monkeypatch):
    state = type("Run", (), {})()
    state.retention, state.pruned = [], []
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["sabnzbd"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local", "gdrive", "dropbox"])
    monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: f"{dest_id}:")
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: _FakeApp())
    monkeypatch.setattr(backup.rclone_util, "copyto", lambda src, dst: None)
    monkeypatch.setattr(backup, "apply_retention",
                        lambda root, days, only=None, dest_id=None: state.retention.append((dest_id, days, only)))
    monkeypatch.setattr(backup.runlog, "prune", lambda days: state.pruned.append(days))
    state.cfg = {"retention_days": 7, "apps": {"sabnzbd": {"url": "http://sabnzbd:8080", "api_key": "k"}},
                 "destinations": {"local": {"retention_days": 28}, "gdrive": {"retention_days": None}, "dropbox": {"retention_days": 3}}}
    return state


def test_each_destination_is_pruned_with_its_own_days(run, caplog):
    caplog.set_level("INFO", logger="backuparr")
    backup.run_backup(run.cfg)
    assert run.retention == [("local", 28, None), ("gdrive", 7, None), ("dropbox", 3, None)]
    assert "Applying retention: local 28d, gdrive 7d, dropbox 3d" in caplog.text


def test_run_logs_are_kept_as_long_as_the_longest_retention(run):
    backup.run_backup(run.cfg)
    assert run.pruned == [28]


def test_with_no_destination_days_everything_uses_the_global_retention(run):
    for dest in run.cfg["destinations"].values():
        dest["retention_days"] = None
    backup.run_backup(run.cfg)
    assert run.retention == [("local", 7, None), ("gdrive", 7, None), ("dropbox", 7, None)]
    assert run.pruned == [7]


def test_a_single_app_run_uses_each_destinations_days_for_that_app_alone(run):
    backup.run_backup(run.cfg, only="sabnzbd")
    assert run.retention == [("local", 28, "sabnzbd"), ("gdrive", 7, "sabnzbd"), ("dropbox", 3, "sabnzbd")]
