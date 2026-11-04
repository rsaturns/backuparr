"""Each app's log and result for every backup run, kept in run_logs.json
beside config.json so they survive restarts (#109). The Overview tiles
read each app's latest result from here, and the log panel on the Overview
and in History shows one app's run.

A run's record: its id ("<app>_<YYYYMMDD_HHMMSS>", the archive's name
without its extension), when it started and finished, the archive's file
name once it was made, whether it worked and why not, the destinations it
was for with each one's result, and its log lines. Records older than
retention_days go, always keeping each app's latest, as for backups."""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import atomic_file
import config_store

log = logging.getLogger("backuparr.runlog")

SCHEMA = 1
MAX_LINES = 500
MAX_LINE_LENGTH = 2000
_lock = threading.RLock()


def path():
    return os.path.join(os.path.dirname(config_store.CONFIG_PATH), "run_logs.json")


def _read():
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        bad = path() + ".bad"
        try:
            os.replace(path(), bad)
        except OSError:
            pass
        log.warning("run logs: %s couldn't be read (%s), moved to %s and started again",
                    path(), getattr(exc, "strerror", None) or type(exc).__name__, bad)
        return {}
    runs = data.get("runs") if isinstance(data, dict) else None
    return runs if isinstance(runs, dict) else {}


def _write(runs):
    atomic_file.write_private(path(), json.dumps({"schema": SCHEMA, "runs": runs}, separators=(",", ":")))


class Capture(logging.Handler):
    """Collects the log lines one app's backup writes. Only lines from the
    thread that started it count, so a web request or the manifest backfill
    logging meanwhile doesn't end up in the app's log."""

    def __init__(self):
        super().__init__()
        self.thread = threading.get_ident()
        self.lines = []
        self.dropped = 0
        self.logger = logging.getLogger("backuparr")

    def start(self):
        self.logger.addHandler(self)
        return self

    def stop(self):
        self.logger.removeHandler(self)

    def emit(self, record):
        if record.thread != self.thread:
            return
        if len(self.lines) >= MAX_LINES:
            self.dropped += 1
            return
        message = record.getMessage()
        if record.exc_info and record.levelno >= logging.ERROR:
            message += " (details are in the container log)"
        self.lines.append({
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "message": message[:MAX_LINE_LENGTH],
        })

    def result(self):
        if self.dropped:
            return self.lines + [{"time": self.lines[-1]["time"], "level": "WARNING",
                                  "message": f"{self.dropped} more line(s) not kept"}]
        return self.lines


def save(record):
    """Adds one app's run. A run log that can't be written is logged, not
    raised: the backup itself is done."""
    try:
        with _lock:
            runs = _read()
            app_runs = [r for r in runs.get(record["app"], []) if r.get("id") != record["id"]]
            app_runs.append(record)
            app_runs.sort(key=lambda r: r["id"])
            runs[record["app"]] = app_runs
            _write(runs)
    except OSError as exc:
        log.warning("%s: couldn't save this run's log: %s", record["app"], exc.strerror or type(exc).__name__)


def _summary(record):
    return {k: v for k, v in record.items() if k != "lines"}


def latest():
    """{app: its latest run, without the log lines}."""
    with _lock:
        return {app: _summary(app_runs[-1]) for app, app_runs in _read().items() if app_runs}


def all_runs():
    """{app: [every kept run, oldest first, without the log lines]}."""
    with _lock:
        return {app: [_summary(r) for r in app_runs] for app, app_runs in _read().items()}


def get(app, run_id):
    with _lock:
        return next((r for r in _read().get(app, []) if r.get("id") == run_id), None)


def forget_file(app, file):
    """The backup was deleted from every destination: its run log goes too."""
    with _lock:
        runs = _read()
        kept = [r for r in runs.get(app, []) if r.get("file") != file]
        if len(kept) != len(runs.get(app, [])):
            runs[app] = kept
            if not kept:
                del runs[app]
            _write(runs)


def prune(retention_days, now=None):
    """Drops each app's runs older than retention_days, keeping its latest.
    Age comes from the run's timestamp, in this container's local time, as
    for backups."""
    cutoff = ((now or datetime.now()) - timedelta(days=int(retention_days))).strftime("%Y%m%d_%H%M%S")
    try:
        with _lock:
            runs = _read()
            changed = False
            for app, app_runs in runs.items():
                kept = [r for r in app_runs[:-1] if r["id"][len(app) + 1:] >= cutoff] + app_runs[-1:]
                if len(kept) != len(app_runs):
                    runs[app] = kept
                    changed = True
            if changed:
                _write(runs)
    except OSError as exc:
        log.warning("run logs: couldn't remove old ones: %s", exc.strerror or type(exc).__name__)


def clear():
    """Reset: the run logs go with the rest of Backuparr's config."""
    with _lock:
        try:
            os.remove(path())
        except FileNotFoundError:
            pass
