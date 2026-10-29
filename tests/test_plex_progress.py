import logging
import threading
from types import SimpleNamespace

import pytest
import requests

import apps.plex as plex_module
from apps.plex import PlexError
from test_driver_plex import archive, driver, plex_folder


def messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == 'backuparr.apps.plex']


def test_phase_progress_does_not_log_credentials_or_settings(tmp_path, caplog):
    app = driver(str(plex_folder(tmp_path / 'plex')))
    with caplog.at_level(logging.INFO, logger='backuparr'):
        app.backup(str(tmp_path / 'work'))
    output = messages(caplog)
    expected = ['checking server identity', 'waiting for Plex', 'downloading database export',
                'database downloaded', 'backed up the library database']
    cursor = 0
    for fragment in expected:
        cursor = next(i for i in range(cursor, len(output)) if fragment in output[i]) + 1
    for secret in ('private-plex-token', 'PlexOnlineToken', 'http://plex:32400'):
        assert secret not in '\n'.join(output)


def test_database_reports_waiting_before_request_and_throttles_byte_progress(tmp_path, monkeypatch, caplog):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(plex_module, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    app = driver(str(plex_folder(tmp_path / 'plex')), body=archive())
    get = app.session.get

    def request(url, **kwargs):
        if url.endswith('/diagnostics/databases'):
            assert any('waiting for Plex to prepare' in line for line in messages(caplog))
        return get(url, **kwargs)

    def chunks(response, **kwargs):
        for offset in range(0, len(response.content), 20):
            clock.now += 1
            yield response.content[offset:offset + 20]

    monkeypatch.setattr(app.session, 'get', request)
    monkeypatch.setattr(requests.Response, 'iter_content', chunks)
    with caplog.at_level(logging.INFO, logger='backuparr'):
        app.backup(str(tmp_path / 'work'))
    periodic = [line for line in messages(caplog) if 'database download:' in line]
    assert len(periodic) == int(clock.now // 10)
    assert 1 < len(periodic) < clock.now
    assert all('MiB received' in line for line in periodic)


def test_failed_export_does_not_claim_archive_is_ready(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger='backuparr'), pytest.raises(PlexError):
        driver(str(plex_folder(tmp_path / 'plex')), body=b'not a zip').backup(str(tmp_path / 'work'))
    output = messages(caplog)
    assert any('database downloaded' in line for line in output)
    assert not any('backed up the library database' in line for line in output)
    assert not list((tmp_path / 'work').glob('*'))


def test_progress_reaches_live_web_status_before_backup_finishes(authed_client, isolated_webui, monkeypatch, tmp_path):
    app = driver(str(plex_folder(tmp_path / 'plex')))
    entered, release = threading.Event(), threading.Event()
    get = app.session.get

    def request(url, **kwargs):
        if url.endswith('/diagnostics/databases'):
            entered.set()
            assert release.wait(5), 'test did not release the database request'
        return get(url, **kwargs)

    monkeypatch.setattr(app.session, 'get', request)
    monkeypatch.setattr(isolated_webui, 'run_backup', lambda *a, **kw: (app.backup(str(tmp_path / 'backup')) and ['plex'], []))
    monkeypatch.setattr(isolated_webui, 'notify', lambda *a: None)
    monkeypatch.setattr(logging.getLogger('backuparr.apps.plex'), 'level', logging.INFO)
    # Keep the real worker thread, HTTP status route and live log handler.
    try:
        assert authed_client.post('/api/backup/run', json={}).status_code == 200
        assert entered.wait(5)
        status = authed_client.get('/api/backup/status').json
        assert status['running'] is True
        assert any('waiting for Plex to prepare' in line for line in status['log'])
        assert not any('backed up the library database' in line for line in status['log'])
    finally:
        release.set()
        # Join only this run's worker, without depending on arbitrary sleeps.
        for thread in threading.enumerate():
            if thread.name.endswith('(_run_tracked)'):
                thread.join(timeout=5)
    status = authed_client.get('/api/backup/status').json
    assert status['running'] is False and status['ok'] == ['plex']
    assert any('backed up the library database' in line for line in status['log'])
