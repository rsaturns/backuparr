"""The storage estimate: how many backups of each app a destination keeps,
checked against what retention really keeps, and the room they need, from
the backup record (manifest)."""
import copy
import threading
from datetime import datetime, timedelta

import pytest
from croniter import croniter

import backup
import config_store
import manifest
import rclone_util
import storage_estimate

NOW = datetime(2026, 11, 23, 12, 0)


@pytest.mark.parametrize("schedule, days, runs", [
    ("0 3 * * *", 7, 7),         # the default: one a day for a week
    ("0 3 * * *", 30, 30),
    ("0 */6 * * *", 7, 28),
    ("0 3 * * 1-5", 7, 5),
    ("0 3 * * 1-5", 10, 8),      # after Friday's run: this week's five and last Wednesday-Friday
    ("0 3 * * 0", 30, 5),
    ("0 3 1 * *", 7, 1),         # monthly: only the newest
    ("0 3 1,15 * *", 20, 2),
])
def test_runs_kept_after_a_run(schedule, days, runs):
    assert storage_estimate.kept_runs(schedule, days, NOW) == (runs, False)


@pytest.mark.parametrize("schedule, days", [
    ("0 3 * * *", 7), ("0 */6 * * *", 7), ("0 3 * * 1-5", 10), ("0 3 1,15 * *", 20), ("30 2 * * 0,3", 9),
])
def test_the_estimate_matches_what_retention_keeps(monkeypatch, schedule, days):
    """Weeks of scheduled runs through the real apply_retention, in a run's
    order - the apps' uploads, retention from the run's start, then
    Backuparr's own - each started a different number of seconds into its
    minute: the most
    backups of an app it leaves after a run is kept_runs, one more is there
    while a run uploads, and Backuparr's own never has more than kept_runs."""
    files = []
    monkeypatch.setattr(backup.rclone_util, "lsjson", lambda root, recursive=False, include=(): [
        {"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False} for p in files])
    monkeypatch.setattr(backup.rclone_util, "delete_file", lambda path: files.remove(path.removeprefix("dest:/")))

    def count(app):
        return sum(1 for p in files if p.startswith(f"{app}/"))

    runs = croniter(schedule, NOW - timedelta(days=days + 1))
    most_after = most_during = most_own = 0
    tick = 0
    while (when := runs.get_next(datetime)) <= NOW + timedelta(days=storage_estimate.ANCHOR_DAYS):
        # The scheduler starts a run some seconds into its minute, a different
        # number each time; a quick run's retention follows a few seconds later.
        tick += 1
        stamp = f"{when + timedelta(seconds=(tick * 7) % 20):%Y%m%d_%H%M%S}"
        files.append(f"radarr/radarr_{stamp}.zip")
        if when >= NOW:
            most_during = max(most_during, count("radarr"))
        backup.apply_retention("dest:", days, now=backup.retention_now(stamp))
        files.append(f"backuparr/backuparr_{stamp}.zip")
        if when >= NOW:
            most_after = max(most_after, count("radarr"))
            most_own = max(most_own, count("backuparr"))
    assert storage_estimate.kept_runs(schedule, days, NOW) == (most_after, False)
    assert most_during == most_after + 1
    assert most_own == most_after


@pytest.mark.parametrize("schedule", ["", "not cron", "0 3 * *", "61 3 * * *", "0 0 30 2 *"])
def test_a_schedule_that_isnt_valid_or_never_runs_has_no_estimate(schedule):
    assert storage_estimate.kept_runs(schedule, 7, NOW) == (None, False)


def test_a_schedule_with_more_runs_than_it_counts_says_so():
    assert storage_estimate.kept_runs("* * * * *", 30, NOW) == (storage_estimate.MAX_RUNS, True)
    assert storage_estimate.kept_runs("* * * * *", 7, NOW) == (7 * 24 * 60, False)


def test_any_number_of_days_settings_takes_is_fine():
    assert storage_estimate.kept_runs("0 3 1 1 *", 10 ** 9, NOW) == (100, False)  # a century of yearly runs
    assert storage_estimate.kept_runs("0 3 * * *", 10 ** 9, NOW) == (storage_estimate.MAX_RUNS, True)


def _record(app, stamp, size, dests=("local",)):
    manifest.record(app, f"{app}_{stamp}.zip", list(dests), app_version="1", sha256="a", size=size, backuparr_version="3")


@pytest.fixture
def recorded():
    _record("radarr", "20261121_030000", 100)
    _record("radarr", "20261122_030000", 300)    # the largest is counted, not the newest
    _record("radarr", "20261123_030000", 200, ("local", "dropbox"))
    _record("sonarr", "20261123_030000", 50)
    _record("backuparr", "20261123_030000", 10)
    _record("lidarr", "20261001_030000", 70)     # switched off: its newest stays, on Local only
    _record("lidarr", "20261002_030000", 40)


# radarr 300 + sonarr 50 copies + 1 times, Backuparr's own 10 copies times (it's
# uploaded after retention), and Lidarr's newest where it is.
def _needed(runs, kept_elsewhere=0, apps=350, own=10):
    return apps * (runs + 1) + own * runs + kept_elsewhere


FORM = {"local": (True, None), "dropbox": (True, 3), "gdrive": (False, None), "onedrive": (False, 14)}


def test_settings_estimate(recorded):
    result = storage_estimate.estimate("0 3 * * *", 7, FORM, ["radarr", "sonarr", "backuparr", "prowlarr"], NOW)
    assert result == {
        "per_run": 300 + 50 + 10,
        "missing": 1,  # prowlarr
        "default": {"days": 7, "copies": 8, "capped": False, "needed": _needed(7, 40)},
        "destinations": {
            "local": {"days": 7, "copies": 8, "capped": False, "needed": _needed(7, 40)},
            "dropbox": {"days": 3, "copies": 4, "capped": False, "needed": _needed(3)},  # no Lidarr there
            "gdrive": {"days": 7, "copies": 8, "capped": False, "needed": _needed(7)},   # off: holds nothing that counts
            "onedrive": {"days": 14, "copies": 15, "capped": False, "needed": _needed(14)},
        },
    }


def test_the_default_hint_counts_the_most_any_destination_using_it_keeps(recorded):
    _record("lidarr", "20261003_030000", 25, ("dropbox",))
    form = {"local": (True, 30), "dropbox": (True, None), "gdrive": (True, None)}
    result = storage_estimate.estimate("0 3 * * *", 7, form, ["radarr", "sonarr", "backuparr"], NOW)
    assert result["destinations"]["local"]["needed"] == _needed(30, 40)
    assert result["destinations"]["dropbox"]["needed"] == _needed(7, 25)
    assert result["default"]["needed"] == _needed(7, 25)  # Local has its own retention
    form["local"] = (True, None)
    assert storage_estimate.estimate("0 3 * * *", 7, form, ["radarr"], NOW)["default"]["needed"] == (
        300 * 8 + 50 + 10 + 40)  # Local's Sonarr, Backuparr's own and Lidarr: not in the run, newest kept


def test_backups_on_a_destination_thats_off_dont_count():
    """Retention never lists a destination that's off, so its entries in the
    record are never tidied: its old backups mustn't count forever."""
    _record("plex", "20260901_030000", 5000, ("gdrive",))   # with metadata, to Drive, now off
    _record("plex", "20261123_030000", 450)
    _record("lidarr", "20260901_030000", 900, ("gdrive",))  # switched off, and only on Drive
    form = {"local": (True, None), "gdrive": (False, None)}
    result = storage_estimate.estimate("0 3 * * *", 7, form, ["plex", "sonarr"], NOW)
    assert result["per_run"] == 450 and result["missing"] == 1
    assert result["destinations"]["local"]["needed"] == result["destinations"]["gdrive"]["needed"] == 450 * 8
    form["gdrive"] = (True, None)
    result = storage_estimate.estimate("0 3 * * *", 7, form, ["plex", "sonarr"], NOW)
    assert result["per_run"] == 5000
    assert result["destinations"]["gdrive"]["needed"] == 5000 * 8 + 900
    cfg = _cfg(self_backup=False)
    cfg["apps"]["plex"]["enabled"] = True
    assert storage_estimate.overview(cfg, NOW)["destinations"]["local"]["used"] == 450


def test_backuparrs_own_backup_keeps_one_copy_fewer():
    _record("backuparr", "20261123_030000", 10)
    result = storage_estimate.estimate("0 3 * * *", 7, {"local": (True, None)}, ["backuparr"], NOW)
    assert result["default"] == {"days": 7, "copies": 8, "capped": False, "needed": 10 * 7}


def test_settings_estimate_with_no_backups_or_no_schedule():
    assert storage_estimate.estimate("0 3 * * *", 7, {"local": (True, None)}, ["radarr"], NOW) == {
        "per_run": 0, "missing": 1, "default": {"days": 7, "copies": 8, "capped": False, "needed": 0},
        "destinations": {"local": {"days": 7, "copies": 8, "capped": False, "needed": 0}}}
    assert storage_estimate.estimate("bad", 7, {"local": (True, None)}, ["radarr"], NOW)["default"] == {
        "days": 7, "copies": None, "capped": False, "needed": None}


def test_a_record_entry_that_isnt_a_kept_backup_is_left_out():
    _record("radarr", "20261122_030000", 300)
    entries = copy.deepcopy(manifest.backups())
    entries["radarr/radarr_20261123_030000.zip"] = {"app": "radarr", "file": "radarr_20261123_030000.zip",
                                                     "destinations": [], "size": 999}  # on no destination
    entries["sonarr/sonarr_20261123_030000.zip"] = {"app": "sonarr", "file": "sonarr_20261123_030000.zip",
                                                     "destinations": ["local"], "size": "big"}
    entries["../x/x_20261123_030000.zip"] = {"app": "../x", "file": "x_20261123_030000.zip",
                                             "destinations": ["local"], "size": 5}
    manifest._write(entries)
    result = storage_estimate.estimate("0 3 * * *", 7, {"local": (True, None)}, ["radarr", "sonarr"], NOW)
    assert result["per_run"] == 300 and result["missing"] == 1
    assert result["default"]["needed"] == 300 * 8


def _cfg(**overrides):
    cfg = copy.deepcopy(config_store.DEFAULTS)
    for name in ("radarr", "sonarr", "prowlarr"):
        cfg["apps"][name]["enabled"] = True
    cfg["destinations"]["dropbox"].update(enabled=True, retention_days=3)
    cfg.update(overrides)
    return cfg


def test_overview(recorded):
    result = storage_estimate.overview(_cfg(), NOW)
    assert result == {
        "per_run": 300 + 50 + 10,
        "missing": ["prowlarr"],
        "destinations": {
            "local": {"days": 7, "copies": 8, "capped": False, "needed": _needed(7, 40),
                      "used": 100 + 300 + 200 + 50 + 10 + 70 + 40},
            "dropbox": {"days": 3, "copies": 4, "capped": False, "needed": _needed(3), "used": 200},
        },
    }


def test_overview_without_backuparrs_own_backup(recorded):
    result = storage_estimate.overview(_cfg(self_backup=False), NOW)
    assert result["per_run"] == 300 + 50
    # Backuparr's own backups are still there: like a switched-off app's, its newest is kept.
    assert result["destinations"]["local"]["needed"] == _needed(7, 40 + 10, own=0)


@pytest.mark.parametrize("output, expected", [
    ('{"total": 1000, "used": 400, "free": 600}', {"total": 1000, "free": 600}),
    ('{"used": 400, "free": 600}', {"total": None, "free": 600}),     # no quota
    ('{"total": true, "free": -1}', {"total": None, "free": None}),
    ("[]", {"total": None, "free": None}),
    ("", {"total": None, "free": None}),
])
def test_about_reads_the_space(monkeypatch, output, expected):
    calls = []
    monkeypatch.setattr(rclone_util, "_run", lambda args, **kwargs: calls.append((args, kwargs)) or output)
    assert rclone_util.about("dropbox:") == expected
    assert calls == [(["about", "--json", "dropbox:"], {"timeout": rclone_util.ABOUT_TIMEOUT})]


# ------------------------------------------------------------- the routes ----
def test_overview_route(authed_client, isolated_webui):
    _record("backuparr", "20261123_030000", 10)
    result = authed_client.get("/api/storage").get_json()
    assert result["per_run"] == 10 and result["missing"] == []
    assert result["destinations"]["local"]["used"] == 10
    assert result["destinations"]["local"]["copies"] == 8


def test_estimate_route(authed_client):
    _record("radarr", "20261123_030000", 300)
    response = authed_client.post("/api/storage/estimate", json={
        "cron_schedule": "0 */6 * * *", "retention_days": 7,
        "destinations": {"local": {"enabled": True, "retention_days": 2},
                         "dropbox": {"enabled": False, "retention_days": None}},
        "apps": ["radarr", "radarr-4k", "radarr"]})
    assert response.status_code == 200
    assert response.get_json() == {"per_run": 300, "missing": 1, "default": {
        "days": 7, "copies": 29, "capped": False, "needed": 300 * 29}, "destinations": {
        "local": {"days": 2, "copies": 9, "capped": False, "needed": 300 * 9},
        "dropbox": {"days": 7, "copies": 29, "capped": False, "needed": 300 * 29}}}


_OK = {"cron_schedule": "0 3 * * *", "retention_days": 7, "destinations": {}, "apps": []}
_DESTINATIONS_ERROR = "destinations must map each destination to enabled and retention_days"


@pytest.mark.parametrize("change, error", [
    ({"cron_schedule": None}, "cron_schedule must be text"),
    ({"cron_schedule": 5}, "cron_schedule must be text"),
    ({"retention_days": None}, "retention_days must be a positive number"),
    ({"retention_days": 0}, "retention_days must be a positive number"),
    ({"retention_days": "7"}, "retention_days must be a positive number"),
    ({"retention_days": True}, "retention_days must be a positive number"),
    ({"destinations": None}, _DESTINATIONS_ERROR),
    ({"destinations": ["local"]}, _DESTINATIONS_ERROR),
    ({"destinations": {"nowhere": {"enabled": True, "retention_days": None}}}, _DESTINATIONS_ERROR),
    ({"destinations": {"local": True}}, _DESTINATIONS_ERROR),
    ({"destinations": {"local": {"retention_days": None}}}, _DESTINATIONS_ERROR),
    ({"destinations": {"local": {"enabled": "yes", "retention_days": None}}}, _DESTINATIONS_ERROR),
    ({"destinations": {"local": {"enabled": True, "retention_days": 0}}}, _DESTINATIONS_ERROR),
    ({"destinations": {"local": {"enabled": True, "retention_days": "3"}}}, _DESTINATIONS_ERROR),
    ({"apps": None}, "apps must be a list of app ids"),
    ({"apps": ["../etc"]}, "apps must be a list of app ids"),
    ({"apps": [5]}, "apps must be a list of app ids"),
    ({"apps": ["radarr"] * 1001}, "apps must be a list of app ids"),
])
def test_estimate_route_checks_its_body(authed_client, change, error):
    response = authed_client.post("/api/storage/estimate", json={**_OK, **change})
    assert response.status_code == 400 and response.get_json() == {"error": error}


def test_space_route(authed_client, isolated_webui, monkeypatch):
    roots = []
    monkeypatch.setattr(isolated_webui.backups.rclone_util, "about",
                        lambda root: roots.append(root) or {"total": 1000, "free": 600})
    response = authed_client.get("/api/storage/local/space")
    assert response.status_code == 200 and response.get_json() == {"total": 1000, "free": 600}
    assert roots == [isolated_webui.backups.destination_util.DEFAULT_LOCAL_DIR]


def test_space_route_for_a_destination_that_isnt_on(authed_client):
    assert authed_client.get("/api/storage/nowhere/space").status_code == 404
    assert authed_client.get("/api/storage/dropbox/space").status_code == 400


def test_space_route_when_rclone_fails(authed_client, isolated_webui, monkeypatch):
    def fail(root):
        raise rclone_util.RcloneError("rclone about failed: couldn't connect", "couldn't connect")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "about", fail)
    response = authed_client.get("/api/storage/local/space")
    assert response.status_code == 502 and "couldn't connect" in response.get_json()["error"]


def test_space_is_asked_once_a_minute(authed_client, isolated_webui, monkeypatch):
    backups = isolated_webui.backups
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(backups.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(backups.rclone_util, "about", lambda root: calls.append(root) or {"total": 1000, "free": 600})
    for _ in range(3):
        assert authed_client.get("/api/storage/local/space").get_json() == {"total": 1000, "free": 600}
    assert len(calls) == 1
    clock[0] += backups._SPACE_TTL
    authed_client.get("/api/storage/local/space")
    assert len(calls) == 2
    backups._destination_space("local", "/elsewhere")  # Local storage's folder changed
    assert calls[-1] == "/elsewhere" and len(calls) == 3


def test_a_failure_is_kept_for_a_minute_too(authed_client, isolated_webui, monkeypatch):
    calls = []

    def fail(root):
        calls.append(root)
        raise rclone_util.RcloneError("rclone about failed: couldn't connect", "couldn't connect")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "about", fail)
    for _ in range(2):
        response = authed_client.get("/api/storage/local/space")
        assert response.status_code == 502 and "couldn't connect" in response.get_json()["error"]
    assert len(calls) == 1


def test_two_views_at_once_start_rclone_once(isolated_webui, monkeypatch):
    backups = isolated_webui.backups
    started, release, calls, answers = threading.Event(), threading.Event(), [], []

    def about(root):
        calls.append(root)
        started.set()
        release.wait(5)
        return {"total": 1000, "free": 600}

    monkeypatch.setattr(backups.rclone_util, "about", about)
    threads = [threading.Thread(target=lambda: answers.append(backups._destination_space("local", "/x"))) for _ in range(2)]
    threads[0].start()
    assert started.wait(5)
    threads[1].start()  # waits for the first one's answer
    release.set()
    for thread in threads:
        thread.join(5)
    assert calls == ["/x"] and answers == [{"total": 1000, "free": 600}] * 2
