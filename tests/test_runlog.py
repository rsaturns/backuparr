"""Per-app run logs (#109): what a run saves for each app, how they're kept,
and how the Overview, History and the log panel read them."""
import logging
import os
import threading
from datetime import datetime

import pytest

import backup
import destination_util
import manifest
import rclone_util
import runlog


# ------------------------------------------------------------- capture ----

def test_capture_keeps_only_its_own_threads_lines():
    logger = logging.getLogger("backuparr.apps.radarr")
    logger.setLevel(logging.INFO)
    capture = runlog.Capture().start()
    try:
        logger.info("from the run")
        other = threading.Thread(target=lambda: logger.warning("from a web request"))
        other.start()
        other.join()
        logger.warning("sabnzbd: masked passwords")
    finally:
        capture.stop()
    lines = capture.result()
    assert [(line["level"], line["message"]) for line in lines] == [("INFO", "from the run"), ("WARNING", "sabnzbd: masked passwords")]
    assert all(line["time"].endswith("+00:00") for line in lines)


def test_capture_is_bounded(monkeypatch):
    monkeypatch.setattr(runlog, "MAX_LINES", 3)
    logger = logging.getLogger("backuparr")
    logger.setLevel(logging.INFO)
    capture = runlog.Capture().start()
    try:
        for i in range(10):
            logger.info("line %d", i)
    finally:
        capture.stop()
    assert [line["message"] for line in capture.result()] == ["line 0", "line 1", "line 2", "7 more line(s) not kept"]


# --------------------------------------------------------------- store ----

def run(app, stamp, ok=True, file=None, **extra):
    return {"id": f"{app}_{stamp}", "app": app, "started": "2026-11-01T03:00:00+00:00", "finished": "2026-11-01T03:01:00+00:00",
            "file": file if file is not None else (f"{app}_{stamp}.zip" if ok else None), "ok": ok,
            "error": None if ok else "boom", "app_version": "1.0", "destinations": {"local": {"ok": ok, "error": None if ok else "boom"}},
            "lines": [{"time": "2026-11-01T03:00:00+00:00", "level": "INFO", "message": "hello"}], **extra}


def test_prune_keeps_recent_runs_and_always_the_latest():
    for stamp in ("20261001_030000", "20261025_030000", "20261030_030000"):
        runlog.save(run("radarr", stamp))
    runlog.save(run("sonarr", "20260901_030000"))  # old, but its only run
    runlog.prune(7, now=datetime(2026, 10, 31, 3, 0, 0))
    runs = runlog.all_runs()
    assert [r["id"] for r in runs["radarr"]] == ["radarr_20261025_030000", "radarr_20261030_030000"]
    assert [r["id"] for r in runs["sonarr"]] == ["sonarr_20260901_030000"]


def test_latest_and_get_and_forget_file():
    runlog.save(run("radarr", "20261101_030000"))
    runlog.save(run("radarr", "20261102_030000", ok=False))
    assert runlog.latest()["radarr"]["id"] == "radarr_20261102_030000" and "lines" not in runlog.latest()["radarr"]
    assert runlog.get("radarr", "radarr_20261101_030000")["lines"][0]["message"] == "hello"
    runlog.forget_file("radarr", "radarr_20261101_030000.zip")
    assert runlog.get("radarr", "radarr_20261101_030000") is None


def test_a_log_that_cannot_be_written_is_only_a_warning(monkeypatch, caplog):
    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runlog.atomic_file, "write_private", full)
    runlog.save(run("radarr", "20261101_030000"))
    assert "couldn't save this run's log" in caplog.text


def test_an_unreadable_file_is_set_aside(tmp_path):
    with open(runlog.path(), "w") as f:
        f.write("{oops")
    assert runlog.latest() == {}
    assert os.path.exists(runlog.path() + ".bad")


# ---------------------------------------------------------- backup run ----

class FakeApp:
    def __init__(self, fail=None):
        self.fail = fail

    def backup(self, dest_dir):
        logging.getLogger("backuparr.apps.sabnzbd").warning("sabnzbd: server passwords are masked")
        if self.fail:
            raise self.fail
        path = os.path.join(dest_dir, "out.zip")
        with open(path, "wb") as f:
            f.write(b"archive")
        return path

    def app_version(self):
        return "5.1.3"


@pytest.fixture
def backup_run(monkeypatch):
    state = type("Run", (), {"fail_upload": set(), "app": FakeApp()})()
    logging.getLogger("backuparr").setLevel(logging.INFO)
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["sabnzbd"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local", "gdrive"])
    monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: f"{dest_id}:")
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: state.app)
    monkeypatch.setattr(backup, "apply_retention", lambda *args, **kwargs: None)

    def copyto(src, dst):
        if dst.split(":")[0] in state.fail_upload:
            raise rclone_util.RcloneError("copyto failed", "quota exceeded")

    monkeypatch.setattr(rclone_util, "copyto", copyto)
    cfg = {"apps": {"sabnzbd": {"url": "http://sabnzbd:8080", "api_key": "k"}}, "destinations": {"local": {}, "gdrive": {}}}
    state.go = lambda: backup.run_backup(cfg)
    return state


def test_a_run_saves_each_apps_log_and_result(backup_run):
    backup_run.go()
    saved = runlog.latest()["sabnzbd"]
    full = runlog.get("sabnzbd", saved["id"])
    assert saved["ok"] is True and saved["error"] is None and saved["app_version"] == "5.1.3"
    assert saved["file"] == saved["id"] + ".zip"
    assert saved["destinations"] == {"local": {"ok": True, "error": None}, "gdrive": {"ok": True, "error": None}}
    messages = [line["message"] for line in full["lines"]]
    assert "sabnzbd: server passwords are masked" in messages
    assert any(m.startswith("sabnzbd: uploaded -> gdrive:") for m in messages)
    assert not any("===" in m for m in messages)


def test_a_failed_upload_is_saved_per_destination(backup_run):
    backup_run.fail_upload = {"gdrive"}
    backup_run.go()
    saved = runlog.latest()["sabnzbd"]
    assert saved["ok"] is False and saved["error"] == "failed on gdrive: quota exceeded"
    assert saved["destinations"] == {"local": {"ok": True, "error": None}, "gdrive": {"ok": False, "error": "quota exceeded"}}


def test_a_failed_backup_saves_why_and_reaches_no_destination(backup_run):
    from apps.base import AppError
    backup_run.app = FakeApp(fail=AppError("sabnzbd: unauthorized - check the API key"))
    backup_run.go()
    saved = runlog.latest()["sabnzbd"]
    assert saved["ok"] is False and saved["file"] is None
    assert saved["error"] == "unauthorized - check the API key"
    assert saved["destinations"] == {"local": None, "gdrive": None}
    lines = runlog.get("sabnzbd", saved["id"])["lines"]
    assert lines[-1]["level"] == "ERROR" and "unauthorized" in lines[-1]["message"]


def test_a_cancelled_app_saves_what_it_did(backup_run, monkeypatch):
    calls = iter([False, True])  # cancelled at the first upload
    backup.run_backup({"apps": {"sabnzbd": {"url": "http://sabnzbd:8080"}}, "destinations": {"local": {}, "gdrive": {}}},
                      should_cancel=lambda: next(calls, True))
    saved = runlog.latest()["sabnzbd"]
    assert saved["ok"] is False and saved["error"] == "the run was cancelled"


# --------------------------------------------------------------- web UI ----

@pytest.fixture
def listing(authed_client, monkeypatch):
    files = []
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False: [
        {"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False, "Size": 1, "ModTime": "2026-11-0%sT03:00:30.123456789Z" % p[-12]}
        for p in files])
    monkeypatch.setattr(rclone_util, "delete_file", lambda path: None)
    authed_client.post("/api/config", json={"destinations": {"local": {"enabled": True}},
                                            "apps": {"radarr": {"enabled": True, "url": "http://radarr:1", "api_key": "k"}}})
    return files


def test_the_overview_reads_each_apps_latest_saved_result(authed_client):
    runlog.save(run("radarr", "20261101_030000"))
    runlog.save(run("radarr", "20261102_030000", ok=False))
    latest = authed_client.get("/api/runlogs").get_json()
    assert latest["radarr"]["ok"] is False and latest["radarr"]["error"] == "boom" and "lines" not in latest["radarr"]
    full = authed_client.get("/api/runlogs/radarr/radarr_20261101_030000").get_json()
    assert full["lines"][0]["message"] == "hello"
    assert authed_client.get("/api/runlogs/radarr/radarr_20261103_030000").status_code == 404


def test_history_links_backups_to_their_runs_and_lists_runs_that_left_no_file(authed_client, listing):
    listing.append("radarr/radarr_20261101_030000.zip")  # made by a logged run
    listing.append("radarr/radarr_20261102_030000.zip")  # made before run logs existed
    runlog.save(run("radarr", "20261101_030000"))
    runlog.save(run("radarr", "20261103_030000", ok=False, finished="2026-11-03T03:01:00+00:00"))
    upload_failed = run("radarr", "20261104_030000", finished="2026-11-04T03:01:00+00:00")
    upload_failed.update(ok=False, error="failed on local: disk full", destinations={"local": {"ok": False, "error": "disk full"}})
    runlog.save(upload_failed)
    other_destination = run("radarr", "20261105_030000", ok=False, destinations={"gdrive": None})
    runlog.save(other_destination)

    rows = authed_client.get("/api/history/local").get_json()["radarr"]
    assert [(r["name"], r["run_id"], r.get("missing", False), r.get("reason")) for r in rows] == [
        ("radarr_20261104_030000.zip", "radarr_20261104_030000", True, "disk full"),
        ("", "radarr_20261103_030000", True, "boom"),
        ("radarr_20261102_030000.zip", None, False, None),
        ("radarr_20261101_030000.zip", "radarr_20261101_030000", False, None),
    ]


def test_a_run_whose_file_this_destinations_retention_removed_isnt_listed_as_failed(authed_client, listing):
    # Uploaded here, pruned here sooner than on a destination that keeps it longer (#133).
    runlog.save(run("radarr", "20261101_030000", destinations={"local": {"ok": True, "error": None}, "gdrive": {"ok": True, "error": None}}))
    listing.append("radarr/radarr_20261102_030000.zip")
    runlog.save(run("radarr", "20261102_030000"))
    rows = authed_client.get("/api/history/local").get_json()["radarr"]
    assert [(r["name"], r.get("missing", False)) for r in rows] == [("radarr_20261102_030000.zip", False)]


def test_history_links_backuparrs_own_backups_to_their_runs(authed_client, listing):
    listing.append("backuparr/backuparr_20261101_030000.enc")
    own = run("backuparr", "20261101_030000")
    own["file"] = "backuparr_20261101_030000.enc"
    runlog.save(own)
    runlog.save(run("backuparr", "20261102_030000", ok=False, finished="2026-11-02T03:01:00+00:00"))
    rows = authed_client.get("/api/history/local").get_json()["backuparr"]
    assert [(r["name"], r["run_id"], r.get("missing", False)) for r in rows] == [
        ("", "backuparr_20261102_030000", True),
        ("backuparr_20261101_030000.enc", "backuparr_20261101_030000", False),
    ]


def test_deleting_a_backup_forgets_its_log_once_no_destination_has_it(authed_client, listing):
    listing.append("radarr/radarr_20261101_030000.zip")
    runlog.save(run("radarr", "20261101_030000"))
    manifest.record("radarr", "radarr_20261101_030000.zip", ["local", "gdrive"], app_version="1", sha256="a", size=1,
                    backuparr_version="x")
    authed_client.delete("/api/history/local/radarr/radarr_20261101_030000.zip")
    assert runlog.get("radarr", "radarr_20261101_030000") is not None  # still on Google Drive
    manifest.remove("radarr", "radarr_20261101_030000.zip", "gdrive")
    manifest.record("radarr", "radarr_20261101_030000.zip", ["local"], app_version="1", sha256="a", size=1,
                    backuparr_version="x")
    authed_client.delete("/api/history/local/radarr/radarr_20261101_030000.zip")
    assert runlog.get("radarr", "radarr_20261101_030000") is None


def test_reset_deletes_the_run_logs(authed_client, isolated_webui):
    runlog.save(run("radarr", "20261101_030000"))
    authed_client.post("/api/reset", json={"confirm": isolated_webui.auth.RESET_CONFIRM_PHRASE})
    assert not os.path.exists(runlog.path())
