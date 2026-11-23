"""How much room backups take on a destination: the Overview's Storage
tiles and the hints under Settings' retention fields.

A run uploads each app's backup to every destination, then retention keeps
an app's backups there for the destination's retention days (always its
newest), then Backuparr's own backup is uploaded. So a destination holds, of
each app, the scheduled runs in its retention window - and one more while a
run uploads, before retention deletes the oldest; Backuparr's own isn't
uploaded until after it. An app's size is its largest backup still kept, so
a backup that grows is covered; an app that's switched off, or a removed
instance, still has its newest backup kept on each destination, so that
counts there too. An app with no backup yet isn't counted.

Sizes come from the backup record (manifest.py), so nothing is listed or
downloaded - and only its backups on enabled destinations count: retention
never lists a destination that's off, so its entries are never tidied.
Manual runs aren't in the schedule: one adds a copy until it ages out.
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
SELF = destination_util.SELF_BACKUP_FOLDER


def kept_runs(schedule, days, now=None):
    """(runs, capped): the most scheduled runs a destination keeps an app's
    backups from right after a run - those in (run - days, run], to the
    minute - over the runs of the next ANCHOR_DAYS, or the next run when
    there's none sooner (a weekday-only schedule keeps more after Friday's
    run than after Monday's). (None, False) for a schedule that isn't valid
    or never runs. capped: there are more than MAX_RUNS."""
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


def _record(apps, enabled):
    """What the backup record says, counting only backups on the enabled
    destinations: (largest, kept_elsewhere, used) - each app in apps' largest
    backup still kept; per destination, the newest backup of each other app
    or instance on it, which retention keeps; and per destination, the size
    of every backup on it."""
    largest, newest, used = {}, {}, {}
    for entry in manifest.backups().values():
        app, name, size = entry.get("app"), entry.get("file"), entry.get("size")
        dests = [d for d in entry.get("destinations") or [] if d in enabled]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not dests or not isinstance(name, str):
            continue
        if not destination_util.is_backup_folder(app or ""):
            continue
        if app in apps:
            largest[app] = max(largest.get(app, 0), size)
        for dest_id in dests:
            used[dest_id] = used.get(dest_id, 0) + size
            # The same prefix, so the timestamp decides which is newest.
            if app not in apps and ((dest_id, app) not in newest or name > newest[dest_id, app][0]):
                newest[dest_id, app] = (name, size)
    kept_elsewhere = {}
    for (dest_id, _app), (_name, size) in newest.items():
        kept_elsewhere[dest_id] = kept_elsewhere.get(dest_id, 0) + size
    return largest, kept_elsewhere, used


def _room(largest, runs, kept_elsewhere):
    """The room a destination needs at most: each app's largest backup times
    runs + 1 copies, the next run's upload landing before retention deletes
    the oldest - Backuparr's own times runs, uploaded after retention - and
    what it keeps of the apps that are off. None with no runs."""
    if runs is None:
        return None
    own = largest.get(SELF, 0)
    return (sum(largest.values()) - own) * (runs + 1) + own * runs + kept_elsewhere


def _estimates(schedule, largest, now):
    """destination(days, kept_elsewhere): one destination's estimate, each
    number of days' runs counted once."""
    runs_for = {}

    def destination(days, kept_elsewhere):
        if days not in runs_for:
            runs_for[days] = kept_runs(schedule, days, now)
        runs, capped = runs_for[days]
        return {"days": days, "copies": None if runs is None else runs + 1, "capped": capped,
                "needed": _room(largest, runs, kept_elsewhere)}

    return destination


def estimate(schedule, default_days, destinations, apps, now=None):
    """Settings' hints, for what's on the form: destinations is {dest_id:
    (enabled, its own retention days or None)}. For each destination, and
    for the default retention, the most backups of each app it holds and the
    room they need - the default's for the enabled destinations that use it
    (the most any of them keeps of the apps that are off). missing: how many
    of the apps have no backup yet."""
    enabled = {dest_id for dest_id, (on, _days) in destinations.items() if on}
    largest, kept_elsewhere, _used = _record(set(apps), enabled)
    destination = _estimates(schedule, largest, now)
    using_default = [dest_id for dest_id in enabled if destinations[dest_id][1] is None]
    return {
        "per_run": sum(largest.values()),
        "missing": sum(1 for app in apps if app not in largest),
        "default": destination(default_days, max((kept_elsewhere.get(d, 0) for d in using_default), default=0)),
        "destinations": {dest_id: destination(days or default_days, kept_elsewhere.get(dest_id, 0))
                         for dest_id, (_on, days) in destinations.items()},
    }


def overview(cfg, now=None):
    """The Overview's Storage tiles: what a run uploads, the apps with no
    backup yet and, for each enabled destination, its retention, the most
    backups of each app it holds, the room they need and what Backuparr's
    backups use there now."""
    apps = config_store.run_app_ids(cfg)
    enabled = config_store.enabled_destinations(cfg)
    largest, kept_elsewhere, used = _record(set(apps), set(enabled))
    destination = _estimates(cfg.get("cron_schedule", config_store.DEFAULTS["cron_schedule"]), largest, now)
    return {
        "per_run": sum(largest.values()),
        "missing": [app for app in apps if app not in largest],
        "destinations": {
            dest_id: {**destination(config_store.retention_days(cfg, dest_id), kept_elsewhere.get(dest_id, 0)),
                      "used": used.get(dest_id, 0)}
            for dest_id in enabled
        },
    }
