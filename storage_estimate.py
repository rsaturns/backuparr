"""How much room backups take on a destination: the Overview's Storage
tiles and the hints under Settings' retention fields.

A run uploads each app's backup, and Backuparr's own, to every destination,
and retention keeps an app's backups there for the destination's retention
days (always its newest). So a destination holds, of each app, the scheduled
runs in its retention window - and one more while a run uploads, before
retention deletes the oldest. An app's size is its largest backup still kept,
so a backup that grows is covered; an app that's switched off, or a removed
instance, still has its newest backup kept, so that counts too. An app with
no backup yet isn't counted.

Sizes come from the backup record (manifest.py), so nothing is listed or
downloaded. Manual runs aren't in the schedule: one adds a copy until it
ages out.
"""
from datetime import datetime, timedelta

from croniter import croniter, CroniterBadDateError

import config_store
import destination_util
import manifest

# The runs counted at most. A schedule every minute kept for weeks has more,
# and an estimate that large needs no precision ("over 20,000 backups").
MAX_RUNS = 20_000
# Run times checked from now: five weeks holds each weekday's run at least
# twice, so a weekday-only schedule's busiest window is in it.
ANCHOR_DAYS = 35
# Settings takes any positive number of days; a window this long (a century)
# keeps the dates in range.
MAX_DAYS = 36_500


def run_apps(cfg):
    """What a scheduled run backs up: each enabled app and instance, and
    Backuparr itself while its card is on."""
    return [*config_store.enabled_apps(cfg), *(["backuparr"] if cfg.get("self_backup", True) is not False else [])]


def kept_runs(schedule, days, now=None):
    """(runs, capped): the most scheduled runs a destination keeps an app's
    backups from right after a run - those in (run - days, run] - over the
    runs of the next ANCHOR_DAYS, or the next run when there's none sooner
    (a weekday-only schedule keeps more after Friday's run than after
    Monday's). (None, False) for a schedule that isn't valid or never runs.
    capped: there are more than MAX_RUNS."""
    if not config_store.valid_schedule(schedule):
        return None, False
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    window = timedelta(days=min(int(days), MAX_DAYS))
    end = now + timedelta(days=ANCHOR_DAYS)
    times = []
    try:
        runs = croniter(schedule, now - window)
        while len(times) < MAX_RUNS:
            when = runs.get_next(datetime)
            if when > end and times and times[-1] >= now:
                break  # past ANCHOR_DAYS, with a run from now on (a yearly one may be months away)
            times.append(when)
    except CroniterBadDateError:  # a date that never comes, such as 30 February
        pass
    if not times:
        return None, False
    most = oldest = 0
    for i, when in enumerate(times):
        if when < now:
            continue  # its window starts before the times walked
        # Retention, after the run, deletes what's older than days: a run
        # exactly days earlier is gone.
        while times[oldest] <= when - window:
            oldest += 1
        most = max(most, i - oldest + 1)
    if not most:  # MAX_RUNS ran out before reaching now
        return MAX_RUNS, True
    return most, False


def _sizes(apps):
    """(largest, kept_elsewhere, used): each app in apps' largest backup still
    kept; the newest backup of each other app or instance, which retention
    keeps; and the size of every backup on each destination."""
    largest, newest, used = {}, {}, {}
    for entry in manifest.backups().values():
        app, name, size, dests = entry.get("app"), entry.get("file"), entry.get("size"), entry.get("destinations")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not dests or not isinstance(name, str):
            continue
        if not destination_util.is_backup_folder(app or ""):
            continue
        for dest_id in dests:
            used[dest_id] = used.get(dest_id, 0) + size
        if app in apps:
            largest[app] = max(largest.get(app, 0), size)
        elif app not in newest or name > newest[app][0]:  # same prefix, so the timestamp decides
            newest[app] = (name, size)
    return largest, sum(size for _name, size in newest.values()), used


def estimate(schedule, days_list, apps, now=None):
    """Settings' hints: for each number of days, the most backups of each app
    a destination holds and the room they need, for these apps and this
    schedule. missing: how many of the apps have no backup yet."""
    largest, kept_elsewhere, _used = _sizes(set(apps))
    per_run = sum(largest.values())
    estimates = {}
    for days in days_list:
        runs, capped = kept_runs(schedule, days, now)
        copies = None if runs is None else runs + 1
        estimates[str(days)] = {
            "copies": copies,
            "capped": capped,
            "needed": None if copies is None else per_run * copies + kept_elsewhere,
        }
    return {"per_run": per_run, "missing": sum(1 for app in apps if app not in largest), "estimates": estimates}


def overview(cfg, now=None):
    """The Overview's Storage tiles: what a run uploads, the apps with no
    backup yet and, for each enabled destination, its retention, the most
    backups of each app it holds, the room they need and what Backuparr's
    backups use there now."""
    apps = run_apps(cfg)
    largest, kept_elsewhere, used = _sizes(set(apps))
    per_run = sum(largest.values())
    schedule = cfg.get("cron_schedule", config_store.DEFAULTS["cron_schedule"])
    destinations = {}
    for dest_id in config_store.enabled_destinations(cfg):
        days = config_store.retention_days(cfg, dest_id)
        runs, capped = kept_runs(schedule, days, now)
        copies = None if runs is None else runs + 1
        destinations[dest_id] = {
            "days": days,
            "copies": copies,
            "capped": capped,
            "needed": None if copies is None else per_run * copies + kept_elsewhere,
            "used": used.get(dest_id, 0),
        }
    return {
        "per_run": per_run,
        "missing": [app for app in apps if app not in largest],
        "destinations": destinations,
    }
