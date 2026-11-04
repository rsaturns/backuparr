"""Backup and restore runs (one at a time, in the background), the backup
routes, and the in-process scheduler."""
import logging
import os
import threading
import time
from datetime import datetime, timezone

from croniter import croniter
from flask import Blueprint, jsonify, request

import backup
import config_store
from backup import describe_failure, format_run_message, log_failure, notify, run_backup
from config_store import enabled_apps, load_config


bp = Blueprint("runs", __name__)
log = logging.getLogger("backuparr.webui")


# ---------------------------------------------------------- run state -----
RUN_LOCK = threading.Lock()
RUN_CANCEL_EVENT = threading.Event()
RUN_STATE = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": [],
    "failed": [],
    "log": [],
    "current_app": None,
    "current_index": 0,
    "total_apps": 0,
    "cancel_requested": False,
}


RESTORE_RUN_STATE = {
    "running": False,
    "app": None,
    "file": None,
    "started_at": None,
    "finished_at": None,
    "ok": False,
    "message": None,
    "summary": None,
    "error": None,
    "log": [],
}


class _ListLogHandler(logging.Handler):
    """Keeps a run's newest lines for the UI, which is sent the whole list
    on every status poll; backup.log keeps them all."""
    MAX_LINES = 500

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(self.format(record))
        if len(self.sink) > self.MAX_LINES:
            del self.sink[:-self.MAX_LINES]


def _run_tracked(state, work, *args):
    """Runs work(*args) with a temporary log handler feeding state["log"],
    then marks the run finished. `state["running"]` must already be True -
    only _start_tracked_run() (which sets it under a lock) should trigger
    this, via the background thread it starts."""
    runner_logger = logging.getLogger("backuparr")
    handler = _ListLogHandler(state["log"])
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    runner_logger.addHandler(handler)
    try:
        work(*args)
    finally:
        runner_logger.removeHandler(handler)
        state["running"] = False
        state["finished_at"] = datetime.now(timezone.utc).isoformat()


def _start_tracked_run(state, lock, reset_fields, work, *args, before_start=None, blocked_by=None):
    """Starts work(*args) in a background thread unless state["running"] is
    already True. reset_fields is applied to `state` right before the
    thread starts (only once we know a run will actually happen);
    before_start, if given, runs under the same lock just before that -
    for setup that must not fire when a run is already in progress (e.g.
    clearing a cancel flag that might belong to that other run). It doesn't
    start while the blocked_by run state is running either - backups and
    restores never overlap, so callers share one lock. Returns whether it
    started."""
    with lock:
        if state["running"] or (blocked_by is not None and blocked_by["running"]):
            return False
        if before_start:
            before_start()
        state.update(reset_fields)
        state["running"] = True
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        state["finished_at"] = None
    threading.Thread(target=_run_tracked, args=(state, work, *args), daemon=True).start()
    return True


def _backup_work(only=None):
    backup_logger = logging.getLogger("backuparr")

    def _progress(index, total, name):
        RUN_STATE["current_app"] = name
        RUN_STATE["current_index"] = index
        RUN_STATE["total_apps"] = total

    try:
        cfg = load_config()
        ok, failed = run_backup(cfg, on_progress=_progress, should_cancel=RUN_CANCEL_EVENT.is_set, only=only)
        RUN_STATE["ok"] = ok
        RUN_STATE["failed"] = failed
        notify(cfg.get("notify_url"), format_run_message(ok, failed))
    except Exception as exc:  # not a per-app failure: the run itself couldn't proceed
        message = describe_failure(exc)
        RUN_STATE["failed"] = [f"backup run stopped: {message}" if message else f"unexpected error: {exc}"]
        log_failure(backup_logger, "backup run stopped", exc)


def _start_backup_run(only=None):
    """Starts a backup run unless one's already in progress - of every
    enabled app, or of just the `only` app or instance id. Returns whether
    it started. Shared by the manual endpoint and the scheduler."""
    return _start_tracked_run(
        RUN_STATE,
        RUN_LOCK,
        {
            "ok": [],
            "failed": [],
            "log": [],
            "current_app": None,
            "current_index": 0,
            "total_apps": 0,
            "cancel_requested": False,
        },
        _backup_work,
        only,
        before_start=RUN_CANCEL_EVENT.clear,
        blocked_by=RESTORE_RUN_STATE,
    )


def _cancel_backup_run():
    """Requests cancellation of the in-progress run, if any. run_backup()
    only checks between apps/uploads, so this stops the run at the next
    safe point rather than instantly. Returns whether a run was running."""
    if not RUN_STATE["running"]:
        return False
    RUN_STATE["cancel_requested"] = True
    RUN_CANCEL_EVENT.set()
    return True


# ------------------------------------------------------------ scheduler ----
# In-process, not an external cron daemon - re-reads cron_schedule whenever
# config.json changes, so a Settings change applies on the next tick.
_SCHEDULER_INTERVAL_SECONDS = 20
_scheduler_state = {"last_run_minute": None, "last_error": None, "config_stamp": None, "schedule": None}


def _config_stamp():
    """Changes whenever config.json does; None while it doesn't exist."""
    try:
        stat = os.stat(config_store.CONFIG_PATH)
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def _scheduled_cron():
    """cron_schedule, without decrypting the whole config every tick."""
    stamp = _config_stamp()
    if stamp is None or stamp != _scheduler_state["config_stamp"]:
        _scheduler_state["schedule"] = load_config().get("cron_schedule", "0 3 * * *")
        _scheduler_state["config_stamp"] = stamp
    return _scheduler_state["schedule"]


def _scheduler_loop():
    while True:
        try:
            schedule = _scheduled_cron()
            now = datetime.now().replace(second=0, microsecond=0)
            # Dedup: a matching minute is seen on more than one ~20s tick.
            if (
                _scheduler_state["last_run_minute"] != now
                and croniter.is_valid(schedule)
                and croniter.match(schedule, now)
            ):
                _scheduler_state["last_run_minute"] = now
                if _start_backup_run():
                    log.info("scheduler: starting backup run (schedule %r, %s)", schedule, now.isoformat())
                elif RESTORE_RUN_STATE["running"]:
                    log.warning("scheduler: skipped the %s backup run - a restore is in progress", now.isoformat())
        except Exception as exc:
            # A persistent problem (unreadable config.json, say) would
            # repeat every tick; report each distinct one once.
            problem = describe_failure(exc) or repr(exc)
            if _scheduler_state["last_error"] != problem:
                _scheduler_state["last_error"] = problem
                log_failure(log, "scheduler tick failed", exc)
        else:
            _scheduler_state["last_error"] = None
        time.sleep(_SCHEDULER_INTERVAL_SECONDS)


def start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True).start()


# ------------------------------------------------------------- backups ----
@bp.post("/api/backup/run")
def api_backup_run():
    data = request.get_json(force=True, silent=True)
    # No "app" (or null) backs up every enabled app; otherwise just that one.
    only = data.get("app") if isinstance(data, dict) else None
    if only is not None and (not isinstance(only, str) or only not in enabled_apps(load_config())):
        return jsonify({"error": "that app isn't enabled - pick an enabled app, or All apps"}), 400
    if not _start_backup_run(only):
        if RESTORE_RUN_STATE["running"]:
            return jsonify({"error": "a restore is running - start the backup when it finishes"}), 409
        return jsonify({"error": "a backup is already running"}), 409
    return jsonify({"started": True})


@bp.post("/api/backup/cancel")
def api_backup_cancel():
    if not _cancel_backup_run():
        return jsonify({"error": "no backup is running"}), 409
    return jsonify({"cancelling": True})


def _tail_lines(path, count=200, chunk=64 * 1024):
    """The last `count` lines of a file, read back from its end."""
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        data = b""
        while pos > 0 and data.count(b"\n") <= count:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.split(b"\n")
    if lines and not lines[-1]:
        lines.pop()
    # Past count+1 newlines the first, possibly partial, line falls outside.
    return [line.decode("utf-8", "replace") for line in lines[-count:]]


@bp.get("/api/backup/status")
def api_backup_status():
    state = dict(RUN_STATE)
    # The UI shows the run's own log while it runs, backup.log's tail after.
    state["log_tail"] = []
    if not state["running"]:
        try:
            state["log_tail"] = _tail_lines(backup.LOG_FILE)
        except OSError:
            pass
    return jsonify(state)
