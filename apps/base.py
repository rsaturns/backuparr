"""What the app drivers share."""
import time

_PROGRESS_EVERY = 10  # seconds between "still waiting" log lines


class AppError(RuntimeError):
    """A driver failure whose message was written for the user: it starts
    with "<app>: " and holds no URL or credential."""


def poll_until(check, timeout_s, interval, on_wait=None):
    """Calls check() until it returns something other than None and returns
    that, or None once timeout_s has passed. check() always runs at least
    once. on_wait(seconds), if given, is called about every 10 s of waiting,
    for a progress log line."""
    start = time.monotonic()
    next_report = _PROGRESS_EVERY
    while True:
        result = check()
        if result is not None:
            return result
        waited = time.monotonic() - start
        if waited >= timeout_s:
            return None
        time.sleep(interval)
        waited = time.monotonic() - start
        if on_wait and waited >= next_report:
            on_wait(int(waited))
            next_report += _PROGRESS_EVERY
