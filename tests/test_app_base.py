"""apps.base: what every app driver shares."""
from types import SimpleNamespace

from apps import base


def test_poll_until_returns_the_first_result_and_reports_progress(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(base, "time", SimpleNamespace(monotonic=lambda: now[0],
                                                      sleep=lambda s: now.__setitem__(0, now[0] + s)))
    answers, reports = iter([None] * 12 + ["done"]), []
    assert base.poll_until(lambda: next(answers), 60, 2, reports.append) == "done"
    assert reports == [10, 20]
    assert base.poll_until(lambda: None, 5, 2) is None and now[0] == 30
