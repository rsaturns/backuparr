"""Backup runs, the in-process scheduler and the run-tracking helpers."""
import errno
import logging
import os
import threading
from datetime import datetime

import pytest

import backup
from conftest import InlineThread


def status(client):
    return client.get("/api/backup/status").get_json()


# --- starting, finishing, failing ------------------------------------------

def test_a_run_reports_progress_results_and_notifies(inline, authed_client, monkeypatch):
    notified = []
    seen = {}

    def fake_run_backup(cfg, on_progress=None, should_cancel=None, only=None):
        on_progress(1, 2, "radarr")
        on_progress(2, 2, "sonarr")
        seen["cancel"] = should_cancel
        logging.getLogger("backuparr").warning("hello from the run")
        return ["radarr"], ["sonarr: boom"]

    monkeypatch.setattr(inline.runs, "run_backup", fake_run_backup)
    monkeypatch.setattr(inline.runs, "notify", lambda url, message, **kw: notified.append((url, message)))
    authed_client.post("/api/config", json={"notify_url": "https://ntfy.example/t"})

    response = authed_client.post("/api/backup/run", json={})
    assert response.status_code == 200 and response.get_json() == {"started": True}
    state = status(authed_client)
    assert state["running"] is False
    assert (state["ok"], state["failed"]) == (["radarr"], ["sonarr: boom"])
    assert (state["current_app"], state["current_index"], state["total_apps"]) == ("sonarr", 2, 2)
    assert state["started_at"] and state["finished_at"]
    assert any("hello from the run" in line for line in state["log"])
    assert notified == [("https://ntfy.example/t", backup.format_run_message(["radarr"], ["sonarr: boom"]))]
    assert seen["cancel"] == inline.runs.RUN_CANCEL_EVENT.is_set


def test_the_run_log_handler_is_removed_afterwards(inline, authed_client, monkeypatch):
    monkeypatch.setattr(inline.runs, "run_backup", lambda cfg, **kw: ([], []))
    before = list(logging.getLogger("backuparr").handlers)
    authed_client.post("/api/backup/run", json={})
    assert logging.getLogger("backuparr").handlers == before


def test_an_unexpected_crash_is_reported_not_raised(inline, authed_client, monkeypatch):
    def crash(cfg, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(inline.runs, "run_backup", crash)
    assert authed_client.post("/api/backup/run", json={}).status_code == 200
    state = status(authed_client)
    assert state["failed"] == ["unexpected error: kaboom"] and state["running"] is False and state["finished_at"]


def test_a_second_run_is_refused_while_one_is_in_progress(inline, authed_client, monkeypatch):
    started = []
    monkeypatch.setattr(inline.runs, "run_backup", lambda cfg, **kw: started.append(1) or ([], []))
    inline.runs.RUN_STATE["running"] = True
    response = authed_client.post("/api/backup/run", json={})
    assert response.status_code == 409 and "already running" in response.get_json()["error"]
    assert started == []


def test_each_run_starts_from_a_clean_state(inline, authed_client, monkeypatch):
    results = iter([(["radarr"], []), ([], ["sonarr: x"])])
    monkeypatch.setattr(inline.runs, "run_backup", lambda cfg, **kw: next(results))
    authed_client.post("/api/backup/run", json={})
    authed_client.post("/api/backup/run", json={})
    state = status(authed_client)
    assert (state["ok"], state["failed"]) == ([], ["sonarr: x"])


# --- one app ---------------------------------------------------------------

RADARR = {"enabled": True, "url": "http://radarr:7878", "api_key": "k"}


def test_a_run_can_back_up_just_one_enabled_app(inline, authed_client, monkeypatch):
    seen = []
    monkeypatch.setattr(inline.runs, "run_backup", lambda cfg, only=None, **kw: seen.append(only) or ([only or "all"], []))
    assert authed_client.post("/api/config", json={"apps": {"radarr": RADARR}}).status_code == 200

    assert authed_client.post("/api/backup/run", json={"app": "radarr"}).get_json() == {"started": True}
    assert status(authed_client)["ok"] == ["radarr"]
    # No app, or null, is the usual run of every enabled app.
    assert authed_client.post("/api/backup/run", json={}).status_code == 200
    assert authed_client.post("/api/backup/run", json={"app": None}).status_code == 200
    assert seen == ["radarr", None, None]


@pytest.mark.parametrize("target", ["sonarr", "radarr-4k", "nope", "", 7, ["radarr"]])
def test_a_one_app_run_needs_an_enabled_app(inline, authed_client, monkeypatch, target):
    started = []
    monkeypatch.setattr(inline.runs, "run_backup", lambda cfg, **kw: started.append(1) or ([], []))
    assert authed_client.post("/api/config", json={"apps": {"radarr": RADARR}}).status_code == 200
    response = authed_client.post("/api/backup/run", json={"app": target})
    assert response.status_code == 400 and "isn't enabled" in response.get_json()["error"]
    assert started == [] and inline.runs.RUN_STATE["running"] is False


# --- cancelling ------------------------------------------------------------

def test_cancel_needs_a_run_in_progress(inline, authed_client):
    response = authed_client.post("/api/backup/cancel", json={})
    assert response.status_code == 409 and not inline.runs.RUN_CANCEL_EVENT.is_set()


def test_cancel_flags_the_running_run_and_the_next_run_clears_it(inline, authed_client, monkeypatch):
    inline.runs.RUN_STATE["running"] = True
    response = authed_client.post("/api/backup/cancel", json={})
    assert response.get_json() == {"cancelling": True}
    assert inline.runs.RUN_CANCEL_EVENT.is_set() and inline.runs.RUN_STATE["cancel_requested"] is True

    inline.runs.RUN_STATE["running"] = False
    observed = []
    monkeypatch.setattr(inline.runs, "run_backup", lambda cfg, should_cancel=None, **kw: observed.append(should_cancel()) or ([], []))
    authed_client.post("/api/backup/run", json={})
    assert observed == [False] and inline.runs.RUN_STATE["cancel_requested"] is False


def test_a_refused_start_does_not_clear_a_pending_cancel(inline, authed_client):
    inline.runs.RUN_STATE["running"] = True
    inline.runs.RUN_CANCEL_EVENT.set()
    assert authed_client.post("/api/backup/run", json={}).status_code == 409
    assert inline.runs.RUN_CANCEL_EVENT.is_set()


# --- status log tail -------------------------------------------------------

def test_status_includes_the_last_200_log_lines(inline, authed_client):
    os.makedirs(os.environ["BACKUPARR_LOG_DIR"], exist_ok=True)
    with open(os.path.join(os.environ["BACKUPARR_LOG_DIR"], "backup.log"), "w") as f:
        f.writelines(f"line {i}\n" for i in range(300))
    tail = status(authed_client)["log_tail"]
    assert len(tail) == 200 and tail[0] == "line 100" and tail[-1] == "line 299"


@pytest.mark.parametrize("lines, chunk", [(5, 64 * 1024), (300, 64 * 1024), (5000, 64), (300, 7)])
def test_the_tail_is_read_from_the_end_of_any_size_log(isolated_webui, tmp_path, lines, chunk):
    path = tmp_path / "backup.log"
    path.write_text("".join(f"2026-10-30 line {i} é\n" for i in range(lines)), encoding="utf-8")
    expected = [f"2026-10-30 line {i} é" for i in range(max(0, lines - 200), lines)]
    assert isolated_webui.runs._tail_lines(str(path), chunk=chunk) == expected


def test_the_tail_keeps_a_last_line_without_a_newline(isolated_webui, tmp_path):
    path = tmp_path / "backup.log"
    path.write_text("one\ntwo")
    assert isolated_webui.runs._tail_lines(str(path)) == ["one", "two"]


def test_the_log_file_isnt_read_while_a_run_shows_its_own(inline, authed_client):
    os.makedirs(os.environ["BACKUPARR_LOG_DIR"], exist_ok=True)
    with open(os.path.join(os.environ["BACKUPARR_LOG_DIR"], "backup.log"), "w") as f:
        f.write("old run\n")
    inline.runs.RUN_STATE.update(running=True, log=["this run"])
    try:
        assert (status(authed_client)["log_tail"], status(authed_client)["log"]) == ([], ["this run"])
    finally:
        inline.runs.RUN_STATE["running"] = False


def test_a_runs_log_keeps_only_its_newest_lines(isolated_webui):
    sink = []
    handler = isolated_webui.runs._ListLogHandler(sink)
    for i in range(isolated_webui.runs._ListLogHandler.MAX_LINES + 50):
        handler.emit(logging.LogRecord("backuparr", logging.INFO, __file__, 1, f"line {i}", None, None))
    assert len(sink) == isolated_webui.runs._ListLogHandler.MAX_LINES and sink[-1] == f"line {len(sink) + 49}"


def test_status_copes_with_a_missing_log_file(inline, authed_client):
    assert status(authed_client)["log_tail"] == []


# --- run helpers -----------------------------------------------------------

def test_run_tracked_always_marks_the_run_finished(isolated_webui):
    state = {"running": True, "finished_at": None, "log": []}

    def work():
        logging.getLogger("backuparr").warning("inside work")
        raise ValueError("work failed")

    with pytest.raises(ValueError):
        isolated_webui.runs._run_tracked(state, work)
    assert state["running"] is False and state["finished_at"]
    assert any("inside work" in line for line in state["log"])
    assert not any(isinstance(h, isolated_webui.runs._ListLogHandler) for h in logging.getLogger("backuparr").handlers)


def test_start_tracked_run_applies_resets_only_when_it_really_starts(inline):
    state = {"running": False, "started_at": None, "finished_at": "old", "items": ["stale"], "log": []}
    ran, order = [], []
    lock = threading.Lock()
    assert inline.runs._start_tracked_run(state, lock, {"items": []}, lambda: ran.append(state["items"][:]), before_start=lambda: order.append("before"))
    assert ran == [[]] and order == ["before"] and state["started_at"] and state["running"] is False

    state.update(running=True, items=["keep"])
    assert not inline.runs._start_tracked_run(state, lock, {"items": []}, lambda: ran.append("never"), before_start=lambda: order.append("again"))
    assert state["items"] == ["keep"] and order == ["before"]


def test_scheduler_thread_is_a_daemon(isolated_webui, monkeypatch):
    created = []

    class Recorder(InlineThread):
        def __init__(self, target=None, daemon=None, **kw):
            created.append((target, daemon))

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", Recorder)
    isolated_webui.runs.start_scheduler()
    assert created == [(isolated_webui.runs._scheduler_loop, True)]


# --- scheduler -------------------------------------------------------------

class Stop(BaseException):
    """Raised by the fake sleep to end the otherwise endless scheduler loop."""


@pytest.fixture
def clock(isolated_webui, monkeypatch):
    """Drive _scheduler_loop tick by tick: yields (set_time, run), where run() takes the
    schedule and the list of fake 'now' values, one per tick, and returns the start log."""
    starts = []

    class FakeDateTime(datetime):
        current = None

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(isolated_webui.runs, "datetime", FakeDateTime)
    monkeypatch.setattr(isolated_webui.runs, "_start_backup_run", lambda: starts.append(FakeDateTime.current) or True)
    isolated_webui.runs._scheduler_state["last_run_minute"] = None
    monkeypatch.setattr(isolated_webui.runs, "_config_stamp", object)  # config.json changes every tick

    def run(schedule, ticks, load=None):
        ticks = iter(ticks)
        monkeypatch.setattr(isolated_webui.runs, "load_config", load or (lambda: {"cron_schedule": schedule}))

        def fake_sleep(seconds):
            assert seconds == isolated_webui.runs._SCHEDULER_INTERVAL_SECONDS
            try:
                FakeDateTime.current = next(ticks)
            except StopIteration:
                raise Stop

        monkeypatch.setattr(isolated_webui.runs.time, "sleep", fake_sleep)
        FakeDateTime.current = next(ticks)
        with pytest.raises(Stop):
            isolated_webui.runs._scheduler_loop()
        return starts

    return run


def at(day, hour, minute, second=0):
    return datetime(2026, 1, day, hour, minute, second)


def test_the_scheduler_starts_a_run_once_per_matching_minute(clock):
    starts = clock("0 3 * * *", [at(5, 2, 59, 40), at(5, 3, 0, 0), at(5, 3, 0, 20), at(5, 3, 0, 40), at(5, 3, 1, 0), at(6, 3, 0, 5)])
    assert starts == [at(5, 3, 0, 0), at(6, 3, 0, 5)]


def test_the_scheduler_ignores_an_invalid_schedule(clock):
    assert clock("not a cron", [at(5, 3, 0), at(5, 3, 1)]) == []


def test_the_scheduler_survives_a_failing_tick(clock):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("config unreadable")
        return {"cron_schedule": "* * * * *"}

    assert clock(None, [at(5, 3, 0), at(5, 3, 1), at(5, 3, 2)], load=flaky) == [at(5, 3, 1), at(5, 3, 2)]


def test_the_scheduler_re_reads_the_schedule_every_tick(clock):
    schedules = iter(["0 3 * * *", "0 3 * * *", "5 3 * * *", "5 3 * * *"])
    starts = clock(None, [at(5, 3, 0), at(5, 3, 1), at(5, 3, 5), at(5, 3, 6)], load=lambda: {"cron_schedule": next(schedules)})
    assert starts == [at(5, 3, 0), at(5, 3, 5)]


def test_the_scheduler_reads_the_config_only_when_it_changes(isolated_webui, monkeypatch):
    loads = []
    monkeypatch.setattr(isolated_webui.runs, "load_config", lambda: loads.append(1) or {"cron_schedule": "0 4 * * *"})
    monkeypatch.setitem(isolated_webui.runs._scheduler_state, "config_stamp", None)
    path = isolated_webui.runs.config_store.CONFIG_PATH
    with open(path, "w") as f:
        f.write("{}")
    assert isolated_webui.runs._scheduled_cron() == isolated_webui.runs._scheduled_cron() == "0 4 * * *"
    assert len(loads) == 1
    with open(path, "w") as f:
        f.write('{"cron_schedule": "0 4 * * *"}')
    isolated_webui.runs._scheduled_cron()
    assert len(loads) == 2


def test_the_scheduler_does_not_retry_a_minute_skipped_because_a_run_was_active(isolated_webui, monkeypatch, clock):
    attempts = []
    monkeypatch.setattr(isolated_webui.runs, "_start_backup_run", lambda: attempts.append(1) or False)
    clock("* * * * *", [at(5, 3, 0, 0), at(5, 3, 0, 20), at(5, 3, 0, 40)])
    assert attempts == [1]


def test_a_run_that_cannot_start_for_lack_of_disk_is_reported_in_one_line(inline, authed_client, monkeypatch, caplog):
    def disk_full(cfg, **kw):
        raise OSError(errno.ENOSPC, "No space left on device", "/tmp/backuparr-run-x")

    monkeypatch.setattr(inline.runs, "run_backup", disk_full)
    with caplog.at_level(logging.INFO):
        assert authed_client.post("/api/backup/run", json={}).status_code == 200
    state = status(authed_client)
    assert state["failed"] == ["backup run stopped: no space left on the device (/tmp/backuparr-run-x)"]
    stopped = [r for r in caplog.records if "backup run stopped" in r.getMessage() and r.levelno == logging.ERROR]
    assert len(stopped) == 1 and stopped[0].exc_info is None


class _StopLoop(Exception):
    pass


def _ticks(inline, monkeypatch, errors):
    """Runs the scheduler loop once per entry: an exception to raise from
    load_config, or None for a healthy tick."""
    queue = list(errors)

    def load_config():
        error = queue.pop(0)
        if error:
            raise error
        return {"cron_schedule": "0 3 * * *"}

    def sleep(seconds):
        if not queue:
            raise _StopLoop

    monkeypatch.setattr(inline.runs, "load_config", load_config)
    monkeypatch.setattr(inline.runs, "_config_stamp", object)
    monkeypatch.setattr(inline.runs.time, "sleep", sleep)
    with pytest.raises(_StopLoop):
        inline.runs._scheduler_loop()


def test_a_broken_config_is_reported_once_not_every_tick(inline, monkeypatch, caplog):
    broken = inline.ConfigError("config.json isn't valid JSON (line 3) - fix it")
    with caplog.at_level(logging.INFO):
        _ticks(inline, monkeypatch, [broken, broken, broken])
    lines = [r for r in caplog.records if "scheduler tick failed" in r.getMessage()]
    assert len(lines) == 1 and lines[0].exc_info is None
    assert lines[0].getMessage() == "scheduler tick failed - config.json isn't valid JSON (line 3) - fix it"


def test_the_scheduler_reports_a_problem_again_after_it_recovers(inline, monkeypatch, caplog):
    broken = inline.ConfigError("config.json isn't valid JSON")
    with caplog.at_level(logging.INFO):
        _ticks(inline, monkeypatch, [broken, None, broken])
    assert len([r for r in caplog.records if "scheduler tick failed" in r.getMessage()]) == 2


def test_an_unexpected_scheduler_error_keeps_its_traceback(inline, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _ticks(inline, monkeypatch, [KeyError("surprise")])
    (line,) = [r for r in caplog.records if "scheduler tick failed" in r.getMessage()]
    assert line.exc_info and line.exc_info[0] is KeyError


def test_saving_settings_to_a_read_only_volume_explains_itself(authed_client, inline, monkeypatch, caplog):
    def read_only(cfg):
        raise PermissionError(errno.EACCES, "Permission denied", "/config/backuparr/config.json")

    monkeypatch.setattr(inline.settings, "save_config", read_only)
    with caplog.at_level(logging.INFO):
        response = authed_client.post("/api/config", json={"retention_days": 3})
    assert response.status_code == 500
    assert response.get_json()["error"].startswith("permission denied - Backuparr runs as PUID:PGID")
    (line,) = [r for r in caplog.records if "POST /api/config failed" in r.getMessage()]
    assert line.exc_info is None and "(/config/backuparr/config.json)" in line.getMessage()


def test_a_corrupt_config_file_is_explained_not_a_stack_trace(authed_client, inline, monkeypatch, caplog):
    def corrupt():
        raise inline.ConfigError("config.json isn't valid JSON (line 1) - fix it")

    monkeypatch.setattr(inline.settings, "load_config", corrupt)
    with caplog.at_level(logging.INFO):
        response = authed_client.get("/api/config")
    assert response.status_code == 500 and "isn't valid JSON" in response.get_json()["error"]
    assert not any(r.exc_info for r in caplog.records if "GET /api/config failed" in r.getMessage())


def test_a_backup_waits_for_a_running_restore(inline, authed_client, monkeypatch):
    started = []
    monkeypatch.setattr(inline.runs, "run_backup", lambda cfg, **kw: started.append(1) or ([], []))
    monkeypatch.setitem(inline.runs.RESTORE_RUN_STATE, "running", True)
    response = authed_client.post("/api/backup/run", json={})
    assert response.status_code == 409 and "a restore is running" in response.get_json()["error"]
    assert started == [] and inline.runs.RUN_STATE["running"] is False


def test_the_scheduler_skips_its_run_during_a_restore(inline, monkeypatch, caplog):
    monkeypatch.setattr(inline.runs, "run_backup", lambda cfg, **kw: ([], []))
    monkeypatch.setitem(inline.runs.RESTORE_RUN_STATE, "running", True)
    monkeypatch.setattr(inline.runs, "load_config", lambda: {"cron_schedule": "* * * * *"})
    monkeypatch.setitem(inline.runs._scheduler_state, "last_run_minute", None)

    def stop(_seconds):
        raise SystemExit
    monkeypatch.setattr(inline.runs.time, "sleep", stop)
    with pytest.raises(SystemExit):
        inline.runs._scheduler_loop()
    assert "a restore is in progress" in caplog.text and inline.runs.RUN_STATE["running"] is False
