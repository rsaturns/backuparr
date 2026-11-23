"""The cron schedule: what Settings accepts, and that every schedule the
Settings picker writes runs when it says (#166 moved croniter to 7.0)."""
from datetime import datetime, timedelta

import pytest
from croniter import croniter

from config_store import valid_schedule


@pytest.mark.parametrize("schedule", ["0 3 * * *", "30 2 * * 1", "0 */6 * * *", "*/15 1-5 * * 1-5", "0 3 1 * *"])
def test_valid_schedules(schedule):
    assert valid_schedule(schedule)


@pytest.mark.parametrize("schedule", [
    None, 3, "", "* * *", "0 0 3 * * *", "61 * * * *", "a b c d e",
    # Refused since croniter 7.0, though 6.x took them:
    "٥ * * * *",          # an Arabic-Indic digit
    "0 0 * * *",          # a no-break space between fields
    "0 0 * * 1#7", "0 0 * * 1#0",  # an out-of-range nth weekday
])
def test_invalid_schedules(schedule):
    assert not valid_schedule(schedule)


# What the picker in app.js (applyPickerToCron) can write: every N hours from
# its list, and daily or weekly (0-6, Sunday first) at any HH:MM.
HOURLY_STEPS = (1, 2, 3, 4, 6, 8, 12)
# A week starting on a Sunday, crossing a leap day.
WEEK = datetime(2028, 2, 27)


def _fires_only_at(schedule, expected):
    """croniter.match - what the scheduler asks once a minute - is true at
    each expected minute and false a minute either side of it."""
    assert valid_schedule(schedule)
    for t in expected:
        assert croniter.match(schedule, t), (schedule, t)
        for other in (t - timedelta(minutes=1), t + timedelta(minutes=1)):
            if other not in expected:
                assert not croniter.match(schedule, other), (schedule, other)


@pytest.mark.parametrize("step", HOURLY_STEPS)
def test_every_n_hours_runs_on_the_hour_every_n_hours(step):
    schedule = f"0 */{step} * * *"
    expected = [WEEK + timedelta(days=d, hours=h) for d in range(2) for h in range(0, 24, step)]
    _fires_only_at(schedule, expected)
    hours = [WEEK + timedelta(hours=h) for h in range(24)]
    assert [t.hour for t in hours if croniter.match(schedule, t)] == list(range(0, 24, step))


@pytest.mark.parametrize("hour", range(24))
def test_daily_runs_once_a_day_at_its_time(hour):
    for minute in range(60):
        _fires_only_at(f"{minute} {hour} * * *", [WEEK + timedelta(days=d, hours=hour, minutes=minute) for d in (0, 2, 3)])


@pytest.mark.parametrize("weekday", range(7))
def test_weekly_runs_on_its_day_only(weekday):
    # WEEK is a Sunday, so day `weekday` of it is cron weekday `weekday`.
    for hour in range(24):
        for minute in range(60):
            schedule = f"{minute} {hour} * * {weekday}"
            day = WEEK + timedelta(days=weekday, hours=hour, minutes=minute)
            assert valid_schedule(schedule)
            assert croniter.match(schedule, day) and croniter.match(schedule, day + timedelta(days=7)), schedule
            assert not croniter.match(schedule, day + timedelta(minutes=1)), schedule
            assert not croniter.match(schedule, day + timedelta(days=1 + (hour + minute) % 6)), schedule
