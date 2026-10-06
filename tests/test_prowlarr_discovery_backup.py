"""The command echo Prowlarr gives back decides whether discovery owns a backup."""
import requests

from apps.prowlarr import ProwlarrApp


def instance(echoed):
    app = ProwlarrApp("http://prowlarr:9696", "key")
    sent = {}

    def trigger_backup(request_headers=None, **kwargs):
        sent.update(request_headers)
        return {"status": "completed", "body": {"clientUserAgent": echoed(request_headers["User-Agent"])}}

    app.trigger_backup = trigger_backup
    return app, sent


def test_owns_the_backup_when_the_marker_is_echoed():
    app, sent = instance(lambda marker: marker)
    assert app.trigger_discovery_backup() is True
    assert sent["User-Agent"].startswith("BackuparrDiscovery/")


def test_does_not_own_a_backup_started_by_someone_else():
    app, _ = instance(lambda marker: "BackuparrDiscovery/another-caller")
    assert app.trigger_discovery_backup() is False


def test_markers_are_unique_per_call():
    app, sent = instance(lambda marker: marker)
    app.trigger_discovery_backup()
    first = sent["User-Agent"]
    app.trigger_discovery_backup()
    assert sent["User-Agent"] != first


def test_missing_or_null_body_is_not_ownership():
    app = ProwlarrApp("http://prowlarr:9696", "key")
    for command in ({}, {"body": None}, {"body": {}}):
        app.trigger_backup = lambda request_headers=None, c=command, **kw: c
        assert app.trigger_discovery_backup() is False
