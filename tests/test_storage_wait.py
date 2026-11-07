"""Local storage on a disk that spins down to save power (#140): Backuparr
waits for it to answer before writing, and says so only when it waits."""
import errno
import logging
import os

import pytest

import backup
import destination_util
import rclone_util
import runlog
import self_backup

pytestmark = pytest.mark.storage_wait


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: sleeping moves it on instead of waiting."""
    state = type("Clock", (), {"now": 0.0, "sleeps": []})()

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(destination_util.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(destination_util.time, "sleep", sleep)
    return state


@pytest.fixture
def disk(monkeypatch):
    """A disk whose first `asleep` writes fail with `error`."""
    state = type("Disk", (), {"asleep": 0, "error": errno.EIO, "touches": 0})()
    real_wake = destination_util._wake

    def wake(path):
        state.touches += 1
        if state.touches <= state.asleep:
            raise OSError(state.error, os.strerror(state.error), os.path.join(path, destination_util.WAKE_FILE))
        real_wake(path)

    monkeypatch.setattr(destination_util, "_wake", wake)
    return state


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO, logger="backuparr")
    return lambda: [r.getMessage() for r in caplog.records]


def test_an_awake_disk_is_used_at_once_and_nothing_is_logged(tmp_path, clock, logs):
    destination_util.wait_until_ready(str(tmp_path))
    assert clock.sleeps == [] and logs() == []
    assert os.listdir(tmp_path) == [".backuparr-wake"]


def test_the_wake_file_is_written_once_then_only_read_from_the_disk(tmp_path, monkeypatch):
    destination_util.wait_until_ready(str(tmp_path / "backups"))
    marker = tmp_path / "backups" / ".backuparr-wake"
    assert marker.read_bytes().startswith(b"Backuparr reads this file")
    assert oct(marker.stat().st_mode & 0o777) == "0o600"

    # A write that fails while a disk spins up can make ext4 go read-only,
    # so once the file is there, waking the disk never writes.
    monkeypatch.setattr(destination_util.atomic_file, "write_private", lambda *a: pytest.fail("wrote while waking"))
    real_open = os.open
    flags = []
    monkeypatch.setattr(destination_util.os, "open", lambda p, f, *a: flags.append(f) or real_open(p, f, *a))
    dropped = []
    real_fadvise = os.posix_fadvise
    monkeypatch.setattr(destination_util.os, "posix_fadvise",
                        lambda fd, off, n, advice: dropped.append(advice) or real_fadvise(fd, off, n, advice))
    destination_util.wait_until_ready(str(tmp_path / "backups"))
    assert flags == [os.O_RDONLY]
    assert dropped == [os.POSIX_FADV_DONTNEED]  # read from the disk, not the cache


def test_a_sleeping_disk_is_waited_for_and_the_wait_logged(tmp_path, clock, disk, logs):
    disk.asleep = 3
    destination_util.wait_until_ready(str(tmp_path))
    assert disk.touches == 4 and clock.sleeps == [1, 2, 4]
    assert logs() == ["waiting for storage to be ready...", "storage ready after 7s"]


def test_retries_are_never_more_than_5s_apart(tmp_path, clock, disk, logs):
    disk.asleep = 6
    destination_util.wait_until_ready(str(tmp_path))
    assert clock.sleeps == [1, 2, 4, 5, 5, 5]
    assert logs()[-1] == "storage ready after 22s"


def test_a_disk_that_never_answers_fails_after_60s_with_a_plain_message(tmp_path, clock, disk):
    disk.asleep = 1000
    with pytest.raises(destination_util.DestinationError) as raised:
        destination_util.wait_until_ready(str(tmp_path))
    assert clock.now == 60
    message = str(raised.value)
    assert message == (f"the storage at {tmp_path} didn't respond within 60s ({os.strerror(errno.EIO)})"
                       " - check the disk and its connection")
    assert "Errno" not in message and ".backuparr-wake" not in message


@pytest.mark.parametrize("error", [errno.EROFS, errno.ENOSPC, errno.EACCES])
def test_other_errors_are_left_for_the_real_operation_to_report(tmp_path, clock, disk, logs, error):
    disk.asleep, disk.error = 1, error
    destination_util.wait_until_ready(str(tmp_path))
    assert disk.touches == 1 and clock.sleeps == [] and logs() == []


def test_local_root_waits_for_the_disk(tmp_path, clock, disk):
    disk.asleep = 2
    path = str(tmp_path / "backups")
    assert destination_util.local_root({"path": path}) == path
    assert disk.touches == 3 and os.path.isdir(path)


def test_only_local_storage_is_waited_for(monkeypatch):
    waited = []
    monkeypatch.setattr(destination_util, "wait_until_ready", waited.append)
    for dest_id in ("gdrive", "onedrive", "dropbox", "local"):
        destination_util.wait_for_storage(dest_id, f"{dest_id}:")
    assert waited == ["local:"]


# ------------------------------------------------------------ backup run ----

class FakeApp:
    def backup(self, dest_dir):
        path = os.path.join(dest_dir, "out.zip")
        with open(path, "wb") as f:
            f.write(b"archive")
        return path

    def app_version(self):
        return "5.1.3"


@pytest.fixture
def run(monkeypatch, tmp_path):
    """A run of two apps to Local storage (a real folder) and Google Drive."""
    state = type("Run", (), {"waits": [], "uploads": [], "never_ready": False})()
    local = str(tmp_path / "backups")
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["sabnzbd", "radarr"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local", "gdrive"])
    monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: local if dest_id == "local" else "gdrive:")
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: FakeApp())
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): [])

    def wait(path):
        state.waits.append(path)
        if state.never_ready:
            raise destination_util.DestinationError(f"the storage at {path} didn't respond within 60s")

    monkeypatch.setattr(destination_util, "wait_until_ready", wait)
    monkeypatch.setattr(rclone_util, "copyto", lambda src, dst: state.uploads.append(dst))
    cfg = {"apps": {"sabnzbd": {"url": "http://sabnzbd:8080"}, "radarr": {"url": "http://radarr:7878"}},
           "destinations": {"local": {}, "gdrive": {}}}
    state.local = local
    state.go = lambda: backup.run_backup(cfg)
    return state


def test_a_run_waits_before_every_write_to_local_storage(run):
    ok, failed = run.go()
    assert ok == ["sabnzbd", "radarr"] and failed == []
    # One per upload, then retention.
    assert run.waits == [run.local] * 3
    assert len(run.uploads) == 4


def test_storage_that_never_answers_fails_only_local_storage(run):
    run.never_ready = True
    ok, failed = run.go()
    assert ok == []
    assert failed == [f"sabnzbd: failed on local: the storage at {run.local} didn't respond within 60s",
                      f"radarr: failed on local: the storage at {run.local} didn't respond within 60s"]
    assert run.uploads and all(dst.startswith("gdrive:") for dst in run.uploads)
    saved = runlog.latest()["radarr"]
    assert saved["destinations"]["gdrive"] == {"ok": True, "error": None}
    assert saved["destinations"]["local"]["ok"] is False


def test_storage_that_never_answers_at_the_start_fails_only_local_storage(run, monkeypatch):
    def remote_root(dest_id, cfg):
        if dest_id == "local":
            raise destination_util.DestinationError("the storage at /mnt/usb didn't respond within 60s")
        return "gdrive:"

    monkeypatch.setattr(destination_util, "remote_root", remote_root)
    ok, failed = run.go()
    assert ok == ["sabnzbd", "radarr"]
    assert failed == ["destination local: the storage at /mnt/usb didn't respond within 60s"]


@pytest.mark.self_backup
def test_backuparrs_own_backup_waits_too_and_reports_storage_that_never_answers(monkeypatch):
    waits = []

    def wait(path):
        waits.append(path)
        raise destination_util.DestinationError(f"the storage at {path} didn't respond within 60s")

    monkeypatch.setattr(destination_util, "wait_until_ready", wait)
    monkeypatch.setattr(self_backup, "build", lambda: b"archive")
    monkeypatch.setattr(self_backup, "open_archive", lambda data: {})
    sent = []
    monkeypatch.setattr(rclone_util, "copyto", lambda src, dst: sent.append(dst))
    problem = self_backup.run({"local": "/mnt/usb", "gdrive": "gdrive:"}, "20261106_030000")
    assert waits == ["/mnt/usb"]
    assert problem == "failed on local: the storage at /mnt/usb didn't respond within 60s"
    assert sent == ["gdrive:/backuparr/backuparr_20261106_030000.enc"]


def test_retention_skips_storage_that_never_answers(monkeypatch, caplog):
    def wait(path):
        raise destination_util.DestinationError(f"the storage at {path} didn't respond within 60s")

    monkeypatch.setattr(destination_util, "wait_until_ready", wait)
    monkeypatch.setattr(rclone_util, "lsjson", lambda *a, **k: pytest.fail("listed storage that never answered"))
    caplog.set_level(logging.WARNING, logger="backuparr")
    backup.apply_retention("/mnt/usb", 7, dest_id="local")
    assert "couldn't list /mnt/usb, nothing removed: the storage at /mnt/usb didn't respond within 60s" in caplog.text


# ------------------------------------------------------- test connection ----

def test_test_connection_waits_for_a_sleeping_disk(authed_client, tmp_path, clock, disk):
    disk.asleep = 2
    result = authed_client.post("/api/test-destination/local", json={"path": str(tmp_path)}).get_json()
    assert result == {"ok": True, "message": f"{tmp_path} is writable"}
    assert disk.touches == 3
