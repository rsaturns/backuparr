"""Backup runs, the in-process scheduler and the run-tracking helpers."""
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

    def fake_run_backup(cfg, on_progress=None, should_cancel=None):
        on_progress(1, 2, "radarr")
        on_progress(2, 2, "sonarr")
        seen["cancel"] = should_cancel
        logging.getLogger("backuparr").warning("hello from the run")
        return ["radarr"], ["sonarr: boom"]

    monkeypatch.setattr(inline, "run_backup", fake_run_backup)
    monkeypatch.setattr(inline, "notify", lambda url, message, **kw: notified.append((url, message)))
    authed_client.post("/api/config", json={"notify_url": "https://ntfy.example/t"})

    response = authed_client.post("/api/backup/run")
    assert response.status_code == 200 and response.get_json() == {"started": True}
    state = status(authed_client)
    assert state["running"] is False
    assert (state["ok"], state["failed"]) == (["radarr"], ["sonarr: boom"])
    assert (state["current_app"], state["current_index"], state["total_apps"]) == ("sonarr", 2, 2)
    assert state["started_at"] and state["finished_at"]
    assert any("hello from the run" in line for line in state["log"])
    assert notified == [("https://ntfy.example/t", backup.format_run_message(["radarr"], ["sonarr: boom"]))]
    assert seen["cancel"] == inline.RUN_CANCEL_EVENT.is_set


def test_the_run_log_handler_is_removed_afterwards(inline, authed_client, monkeypatch):
    monkeypatch.setattr(inline, "run_backup", lambda cfg, **kw: ([], []))
    before = list(logging.getLogger("backuparr").handlers)
    authed_client.post("/api/backup/run")
    assert logging.getLogger("backuparr").handlers == before


def test_an_unexpected_crash_is_reported_not_raised(inline, authed_client, monkeypatch):
    def crash(cfg, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(inline, "run_backup", crash)
    assert authed_client.post("/api/backup/run").status_code == 200
    state = status(authed_client)
    assert state["failed"] == ["unexpected error: kaboom"] and state["running"] is False and state["finished_at"]


def test_a_second_run_is_refused_while_one_is_in_progress(inline, authed_client, monkeypatch):
    started = []
    monkeypatch.setattr(inline, "run_backup", lambda cfg, **kw: started.append(1) or ([], []))
    inline.RUN_STATE["running"] = True
    response = authed_client.post("/api/backup/run")
    assert response.status_code == 409 and "already running" in response.get_json()["error"]
    assert started == []


def test_each_run_starts_from_a_clean_state(inline, authed_client, monkeypatch):
    results = iter([(["radarr"], []), ([], ["sonarr: x"])])
    monkeypatch.setattr(inline, "run_backup", lambda cfg, **kw: next(results))
    authed_client.post("/api/backup/run")
    authed_client.post("/api/backup/run")
    state = status(authed_client)
    assert (state["ok"], state["failed"]) == ([], ["sonarr: x"])


# --- cancelling ------------------------------------------------------------

def test_cancel_needs_a_run_in_progress(inline, authed_client):
    response = authed_client.post("/api/backup/cancel")
    assert response.status_code == 409 and not inline.RUN_CANCEL_EVENT.is_set()


def test_cancel_flags_the_running_run_and_the_next_run_clears_it(inline, authed_client, monkeypatch):
    inline.RUN_STATE["running"] = True
    response = authed_client.post("/api/backup/cancel")
    assert response.get_json() == {"cancelling": True}
    assert inline.RUN_CANCEL_EVENT.is_set() and inline.RUN_STATE["cancel_requested"] is True

    inline.RUN_STATE["running"] = False
    observed = []
    monkeypatch.setattr(inline, "run_backup", lambda cfg, should_cancel=None, **kw: observed.append(should_cancel()) or ([], []))
    authed_client.post("/api/backup/run")
    assert observed == [False] and inline.RUN_STATE["cancel_requested"] is False


def test_a_refused_start_does_not_clear_a_pending_cancel(inline, authed_client):
    inline.RUN_STATE["running"] = True
    inline.RUN_CANCEL_EVENT.set()
    assert authed_client.post("/api/backup/run").status_code == 409
    assert inline.RUN_CANCEL_EVENT.is_set()


# --- status log tail -------------------------------------------------------

def test_status_includes_the_last_200_log_lines(inline, authed_client):
    os.makedirs(os.environ["BACKUPARR_LOG_DIR"], exist_ok=True)
    with open(os.path.join(os.environ["BACKUPARR_LOG_DIR"], "backup.log"), "w") as f:
        f.writelines(f"line {i}\n" for i in range(300))
    tail = status(authed_client)["log_tail"]
    assert len(tail) == 200 and tail[0] == "line 100" and tail[-1] == "line 299"


def test_status_copes_with_a_missing_log_file(inline, authed_client):
    assert status(authed_client)["log_tail"] == []


# --- run helpers -----------------------------------------------------------

def test_run_tracked_always_marks_the_run_finished(isolated_webui):
    state = {"running": True, "finished_at": None, "log": []}

    def work():
        logging.getLogger("backuparr").warning("inside work")
        raise ValueError("work failed")

    with pytest.raises(ValueError):
        isolated_webui._run_tracked(state, work)
    assert state["running"] is False and state["finished_at"]
    assert any("inside work" in line for line in state["log"])
    assert not any(isinstance(h, isolated_webui._ListLogHandler) for h in logging.getLogger("backuparr").handlers)


def test_start_tracked_run_applies_resets_only_when_it_really_starts(inline):
    state = {"running": False, "started_at": None, "finished_at": "old", "items": ["stale"], "log": []}
    ran, order = [], []
    lock = threading.Lock()
    assert inline._start_tracked_run(state, lock, {"items": []}, lambda: ran.append(state["items"][:]), before_start=lambda: order.append("before"))
    assert ran == [[]] and order == ["before"] and state["started_at"] and state["running"] is False

    state.update(running=True, items=["keep"])
    assert not inline._start_tracked_run(state, lock, {"items": []}, lambda: ran.append("never"), before_start=lambda: order.append("again"))
    assert state["items"] == ["keep"] and order == ["before"]


def test_scheduler_thread_is_a_daemon(isolated_webui, monkeypatch):
    created = []

    class Recorder(InlineThread):
        def __init__(self, target=None, daemon=None, **kw):
            created.append((target, daemon))

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", Recorder)
    isolated_webui.start_scheduler()
    assert created == [(isolated_webui._scheduler_loop, True)]


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

    monkeypatch.setattr(isolated_webui, "datetime", FakeDateTime)
    monkeypatch.setattr(isolated_webui, "_start_backup_run", lambda: starts.append(FakeDateTime.current) or True)
    isolated_webui._scheduler_state["last_run_minute"] = None

    def run(schedule, ticks, load=None):
        ticks = iter(ticks)
        monkeypatch.setattr(isolated_webui, "load_config", load or (lambda: {"cron_schedule": schedule}))

        def fake_sleep(seconds):
            assert seconds == isolated_webui._SCHEDULER_INTERVAL_SECONDS
            try:
                FakeDateTime.current = next(ticks)
            except StopIteration:
                raise Stop

        monkeypatch.setattr(isolated_webui.time, "sleep", fake_sleep)
        FakeDateTime.current = next(ticks)
        with pytest.raises(Stop):
            isolated_webui._scheduler_loop()
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


def test_the_scheduler_does_not_retry_a_minute_skipped_because_a_run_was_active(isolated_webui, monkeypatch, clock):
    attempts = []
    monkeypatch.setattr(isolated_webui, "_start_backup_run", lambda: attempts.append(1) or False)
    clock("* * * * *", [at(5, 3, 0, 0), at(5, 3, 0, 20), at(5, 3, 0, 40)])
    assert attempts == [1]
