"""The storage estimate: how many backups of each app a destination keeps,
checked against what retention really keeps, and the room they need, from
the backup record (manifest)."""
import copy
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
    """Weeks of scheduled runs through the real apply_retention: the most
    backups it leaves after a run is kept_runs, and one more is there while
    a run uploads."""
    files = []
    monkeypatch.setattr(backup.rclone_util, "lsjson", lambda root, recursive=False, include=(): [
        {"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False} for p in files])
    monkeypatch.setattr(backup.rclone_util, "delete_file", lambda path: files.remove(path.removeprefix("dest:/")))
    runs = croniter(schedule, NOW - timedelta(days=days + 1))
    most_after = most_during = 0
    while (when := runs.get_next(datetime)) <= NOW + timedelta(days=storage_estimate.ANCHOR_DAYS):
        files.append(f"radarr/radarr_{when:%Y%m%d_%H%M%S}.zip")
        if when >= NOW:
            most_during = max(most_during, len(files))
        backup.apply_retention("dest:", days, now=when + timedelta(minutes=2))  # after the run's uploads
        if when >= NOW:
            most_after = max(most_after, len(files))
    assert storage_estimate.kept_runs(schedule, days, NOW) == (most_after, False)
    assert most_during == most_after + 1


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
    _record("lidarr", "20261001_030000", 70)     # switched off: its newest stays
    _record("lidarr", "20261002_030000", 40)


def test_settings_estimate(recorded):
    result = storage_estimate.estimate("0 3 * * *", [7, 30], ["radarr", "sonarr", "backuparr", "prowlarr"], NOW)
    per_run = 300 + 50 + 10
    assert result == {
        "per_run": per_run,
        "missing": 1,  # prowlarr
        "estimates": {
            "7": {"copies": 8, "capped": False, "needed": per_run * 8 + 40},
            "30": {"copies": 31, "capped": False, "needed": per_run * 31 + 40},
        },
    }


def test_settings_estimate_with_no_backups_or_no_schedule():
    assert storage_estimate.estimate("0 3 * * *", [7], ["radarr"], NOW) == {
        "per_run": 0, "missing": 1, "estimates": {"7": {"copies": 8, "capped": False, "needed": 0}}}
    assert storage_estimate.estimate("bad", [7], ["radarr"], NOW)["estimates"] == {
        "7": {"copies": None, "capped": False, "needed": None}}


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
    result = storage_estimate.estimate("0 3 * * *", [7], ["radarr", "sonarr"], NOW)
    assert result["per_run"] == 300 and result["missing"] == 1
    assert result["estimates"]["7"]["needed"] == 300 * 8


def _cfg(**overrides):
    cfg = copy.deepcopy(config_store.DEFAULTS)
    for name in ("radarr", "sonarr", "prowlarr"):
        cfg["apps"][name]["enabled"] = True
    cfg["destinations"]["dropbox"].update(enabled=True, retention_days=3)
    cfg.update(overrides)
    return cfg


def test_overview(recorded):
    result = storage_estimate.overview(_cfg(), NOW)
    per_run = 300 + 50 + 10
    assert result == {
        "per_run": per_run,
        "missing": ["prowlarr"],
        "destinations": {
            "local": {"days": 7, "copies": 8, "capped": False, "needed": per_run * 8 + 40,
                      "used": 100 + 300 + 200 + 50 + 10 + 70 + 40},
            "dropbox": {"days": 3, "copies": 4, "capped": False, "needed": per_run * 4 + 40, "used": 200},
        },
    }


def test_overview_without_backuparrs_own_backup(recorded):
    result = storage_estimate.overview(_cfg(self_backup=False), NOW)
    assert result["per_run"] == 300 + 50
    # Backuparr's own backups are still there: like a switched-off app's, its newest is kept.
    assert result["destinations"]["local"]["needed"] == (300 + 50) * 8 + 40 + 10


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
        "cron_schedule": "0 */6 * * *", "retention_days": [7, 7, 2], "apps": ["radarr", "radarr-4k", "backuparr"]})
    assert response.status_code == 200
    assert response.get_json() == {"per_run": 300, "missing": 2, "estimates": {
        "7": {"copies": 29, "capped": False, "needed": 300 * 29},
        "2": {"copies": 9, "capped": False, "needed": 300 * 9}}}


@pytest.mark.parametrize("body, error", [
    ({"retention_days": [7], "apps": []}, "cron_schedule must be text"),
    ({"cron_schedule": 5, "retention_days": [7], "apps": []}, "cron_schedule must be text"),
    ({"cron_schedule": "0 3 * * *", "apps": []}, "retention_days must be a list of positive numbers"),
    ({"cron_schedule": "0 3 * * *", "retention_days": [0], "apps": []}, "retention_days must be a list of positive numbers"),
    ({"cron_schedule": "0 3 * * *", "retention_days": ["7"], "apps": []}, "retention_days must be a list of positive numbers"),
    ({"cron_schedule": "0 3 * * *", "retention_days": [True], "apps": []}, "retention_days must be a list of positive numbers"),
    ({"cron_schedule": "0 3 * * *", "retention_days": list(range(1, 12)), "apps": []},
     "retention_days must be a list of positive numbers"),
    ({"cron_schedule": "0 3 * * *", "retention_days": [7]}, "apps must be a list of app ids"),
    ({"cron_schedule": "0 3 * * *", "retention_days": [7], "apps": ["../etc"]}, "apps must be a list of app ids"),
    ({"cron_schedule": "0 3 * * *", "retention_days": [7], "apps": [5]}, "apps must be a list of app ids"),
    ({"cron_schedule": "0 3 * * *", "retention_days": [7], "apps": ["radarr"] * 1001}, "apps must be a list of app ids"),
])
def test_estimate_route_checks_its_body(authed_client, body, error):
    response = authed_client.post("/api/storage/estimate", json=body)
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
