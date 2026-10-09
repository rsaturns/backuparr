import json
from pathlib import Path

import pytest
import requests

from apps.plex import PlexApp, PlexError
from apps.plex_restore import PlexRestoreAgent


IDENTITY = {'version': '1.43.3-test', 'machine_identifier': 'test-server'}


def response(status, body):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result._content_consumed = True
    return result


@pytest.fixture
def plex(monkeypatch):
    instance = PlexApp('http://plex:32400', 'plex-secret')
    monkeypatch.setattr(instance, 'identity', lambda: dict(IDENTITY))
    return instance


def test_client_streams_authenticated_archive_and_polls(tmp_path, monkeypatch, plex):
    agent = PlexRestoreAgent('http://agent:8991', 'agent-secret')
    archive = tmp_path / 'backup.zip'
    archive.write_bytes(b'fixture')
    calls = []
    replies = iter([response(200, {'api_version': 1, 'server': IDENTITY}),
                    response(202, {'phase': 'queued'}),
                    response(200, {'phase': 'complete', 'message': 'Plex restored'})])

    def request(method, url, **kwargs):
        if method == 'PUT':
            assert kwargs['data'].read() == b'fixture'
            assert kwargs['headers']['X-Plex-Token'] == 'plex-secret'
            assert kwargs['headers']['X-Plex-Machine-Identifier'] == 'test-server'
        assert not kwargs['allow_redirects']
        calls.append((method, url))
        return next(replies)

    monkeypatch.setattr(agent.session, 'request', request)
    result = agent.restore(str(archive), plex)
    assert result['message'] == 'Plex restored'
    assert agent.session.headers['Authorization'] == 'Bearer agent-secret'
    assert [method for method, _ in calls] == ['GET', 'PUT', 'GET']
    assert calls[1][1] == calls[2][1]


def test_lost_upload_response_never_submits_again(tmp_path, monkeypatch, plex):
    agent = PlexRestoreAgent('http://agent:8991', 'agent-secret')
    archive = tmp_path / 'backup.zip'
    archive.write_bytes(b'fixture')
    calls = []

    def request(method, url, **kwargs):
        calls.append(method)
        if method == 'PUT':
            raise requests.ConnectionError('secret-detail')
        if url.endswith('/status'):
            return response(200, {'api_version': 1, 'server': IDENTITY})
        return response(200, {'phase': 'complete'})

    monkeypatch.setattr(agent.session, 'request', request)
    assert agent.restore(str(archive), plex)['agent_job']
    assert calls.count('PUT') == 1


@pytest.mark.parametrize('phase', ['failed', 'rolled_back', 'recovery_failed'])
def test_failed_and_rolled_back_jobs_are_not_reported_as_success(tmp_path, monkeypatch, plex, phase):
    agent = PlexRestoreAgent('http://agent:8991', 'agent-secret')
    archive = tmp_path / 'backup.zip'
    archive.write_bytes(b'fixture')
    replies = iter([{'api_version': 1, 'server': IDENTITY}, {'phase': 'queued'},
                    {'phase': phase, 'error': 'original data retained'}])
    monkeypatch.setattr(agent, 'request', lambda *args, **kw: next(replies))
    with pytest.raises(PlexError, match='original data retained'):
        agent.restore(str(archive), plex)


def test_agent_redirect_is_never_followed(monkeypatch):
    agent = PlexRestoreAgent('http://agent:8991', 'agent-secret')
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        result = response(302, {})
        result.headers['Location'] = 'http://other.example'
        return result

    monkeypatch.setattr(agent.session, 'request', request)
    with pytest.raises(PlexError, match='redirect refused'):
        agent.request('GET', '/v1/status')
    assert len(calls) == 1 and calls[0]['allow_redirects'] is False


def test_settings_encrypt_agent_token_and_restore_requires_it(authed_client, isolated_webui):
    cfg = {'url': 'http://plex:32400', 'api_key': 'plex-secret', 'enabled': True,
           'restore_agent_url': 'http://agent:8991', 'restore_agent_token': 'agent-secret'}
    assert authed_client.post('/api/config', json={'apps': {'plex': cfg}}).status_code == 200
    assert authed_client.get('/api/config').json['apps']['plex']['restore_agent_token'] == 'agent-secret'
    text = Path(isolated_webui.CONFIG_PATH).read_text()
    assert 'agent-secret' not in text and 'plex-secret' not in text
    assert authed_client.post('/api/config', json={'apps': {'plex': {'restore_agent_token': ''}}}).status_code == 200
    result = authed_client.post('/api/restore/local/plex', json={'confirm': True})
    assert result.status_code == 400 and 'restore agent' in result.json['error']


def test_agent_test_route_requires_login_and_does_not_restore(authed_client, monkeypatch):
    calls = []
    monkeypatch.setattr(PlexRestoreAgent, 'test_connection', lambda self, plex: calls.append(plex.url) or 'Verified')
    data = {'url': 'http://plex:32400', 'api_key': 'plex-secret',
            'restore_agent_url': 'http://agent:8991', 'restore_agent_token': 'agent-secret'}
    result = authed_client.post('/api/plex/restore-agent/test', json=data)
    assert result.json == {'ok': True, 'message': 'Verified'}
    assert calls == ['http://plex:32400']
    authed_client.post('/api/logout')
    assert authed_client.post('/api/plex/restore-agent/test', json=data).status_code == 401


def test_plex_restore_route_reports_agent_result(inline, authed_client, monkeypatch, tmp_path):
    import restore_actions
    cfg = {'url': 'http://plex:32400', 'api_key': 'plex-secret', 'enabled': True,
           'restore_agent_url': 'http://agent:8991', 'restore_agent_token': 'agent-secret'}
    authed_client.post('/api/config', json={'apps': {'plex': cfg}})
    work = tmp_path / 'restore'
    work.mkdir()
    monkeypatch.setattr(restore_actions, 'fetch_backup', lambda *a: (str(work), str(work / 'x.zip'), 'plex_1.zip'))
    monkeypatch.setattr(PlexRestoreAgent, 'restore', lambda self, archive, plex: {'agent_job': 'id', 'message': 'Database restored; settings not applied'})
    assert authed_client.post('/api/restore/local/plex', json={'confirm': True}).status_code == 200
    result = authed_client.get('/api/restore/status').json
    assert result['ok'] and result['summary']['agent_job'] == 'id'
    assert 'settings not applied' in result['message']
    assert not work.exists()
