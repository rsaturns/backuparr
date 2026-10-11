import logging
import threading
from types import SimpleNamespace

import pytest
import requests

import apps.plex as plex_module
from apps.plex import PlexError
from test_driver_plex import archive, driver


def messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == 'backuparr.apps.plex']


def test_phase_progress_and_pagination_do_not_log_credentials_or_library_contents(tmp_path, caplog):
    def pages(params):
        if params['type'] == 18:
            return b'<MediaContainer totalSize="0" offset="0"/>'
        start = params['X-Plex-Container-Start']
        items = ''.join(f'<Video ratingKey="{key}" title="private-movie-title"/>'
                        for key in range(start, min(start + 500, 501)))
        return f'<MediaContainer totalSize="501" offset="{start}">{items}</MediaContainer>'.encode()

    app = driver(routes={
        '/library/sections': b'<MediaContainer><Directory key="1" type="movie" title="private-library-name"/></MediaContainer>',
        '/library/sections/1/prefs': b'<MediaContainer><Setting id="private-setting" value="private-setting-value"/></MediaContainer>',
        '/library/sections/1/all': pages,
    })
    with caplog.at_level(logging.INFO, logger='backuparr'):
        app.backup(str(tmp_path))
    output = messages(caplog)
    expected = ['checking server identity', 'server identity verified', 'waiting for Plex',
                'downloading database', 'database downloaded', 'database archive validated',
                'adding database export', 'exporting server settings', 'server settings exported',
                'library list exported (1 libraries)', 'settings exported',
                'movies metadata read 500/501', 'movies metadata read 501/501',
                'collections metadata read 0/0', 'library 1/1 (ID 1) exported',
                'downloading artwork (0 files', 'artwork export complete: 0 saved',
                'finalizing archive', 'backup archive ready']
    cursor = 0
    for fragment in expected:
        cursor = next(i for i in range(cursor, len(output)) if fragment in output[i]) + 1
    for secret in ('private-plex-token', 'private-library-name', 'private-movie-title',
                   'private-setting-value', 'http://plex:32400'):
        assert secret not in '\n'.join(output)


def test_database_reports_waiting_before_request_and_throttles_byte_progress(tmp_path, monkeypatch, caplog):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(plex_module, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    app = driver(body=archive(data=b'SQLite format 3\0' + b'x' * 300))
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
        app.backup(str(tmp_path))
    periodic = [line for line in messages(caplog) if 'database download:' in line]
    assert len(periodic) == int(clock.now // 10)
    assert 1 < len(periodic) < clock.now
    assert all('MiB received' in line for line in periodic)


def test_artwork_progress_includes_skipped_files_and_inflight_bytes(tmp_path, monkeypatch, caplog):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(plex_module, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    art_path = '/library/metadata/7/thumb/42?X-Plex-Token=private-query-token'

    def pages(params):
        if params['type'] == 18:
            return b'<MediaContainer totalSize="0" offset="0"/>'
        return (f'<MediaContainer totalSize="1" offset="0"><Video ratingKey="7" thumb="{art_path}" '
                'art="/library/metadata/8/thumb/missing" banner="https://external.example/private-image"/></MediaContainer>').encode()

    app = driver(routes={
        '/library/sections': b'<MediaContainer><Directory key="1" type="movie"/></MediaContainer>',
        '/library/sections/1/prefs': b'<MediaContainer/>',
        '/library/sections/1/all': pages,
        art_path: b'picture', '/library/metadata/8/thumb/missing': (404, b'not found'),
    })
    get = app.session.get

    def request(url, **kwargs):
        if '/thumb/' in url:
            clock.now += 11
        return get(url, **kwargs)

    monkeypatch.setattr(app.session, 'get', request)
    with caplog.at_level(logging.INFO, logger='backuparr'):
        app.backup(str(tmp_path))
    output = messages(caplog)
    assert any('artwork: 0/2 processed, 0 saved' in line for line in output)
    assert any('artwork: 2/2 processed, 1 saved' in line for line in output)
    assert any('artwork export complete: 1 saved, 2 unavailable or external' in line for line in output)
    assert not any('private-query-token' in line or 'external.example' in line for line in output)


def test_failed_export_does_not_claim_archive_is_ready(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger='backuparr'), pytest.raises(PlexError):
        driver(body=b'not a zip').backup(str(tmp_path))
    output = messages(caplog)
    assert any('validating archive' in line for line in output)
    assert not any('archive validated' in line or 'archive ready' in line for line in output)
    assert not list(tmp_path.iterdir())


def test_progress_reaches_live_web_status_before_backup_finishes(authed_client, isolated_webui, monkeypatch, tmp_path):
    app = driver()
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
        assert authed_client.post('/api/backup/run').status_code == 200
        assert entered.wait(5)
        status = authed_client.get('/api/backup/status').json
        assert status['running'] is True
        assert any('waiting for Plex to prepare' in line for line in status['log'])
        assert not any('archive ready' in line for line in status['log'])
    finally:
        release.set()
        # Join only this run's worker, without depending on arbitrary sleeps.
        for thread in threading.enumerate():
            if thread.name.endswith('(_run_tracked)'):
                thread.join(timeout=5)
    status = authed_client.get('/api/backup/status').json
    assert status['running'] is False and status['ok'] == ['plex']
    assert any('backup archive ready' in line for line in status['log'])
