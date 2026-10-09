import io
import json
import zipfile

import pytest
import requests

from apps.plex import PlexApp, PlexError
from test_driver_plex import driver


def response(status, location=None, body=b'picture'):
    result = requests.Response()
    result.status_code = status
    result._content = body
    result._content_consumed = True
    result.headers['Content-Type'] = 'image/png'
    if location is not None:
        result.headers['Location'] = location
    return result


@pytest.mark.parametrize('location', ['/photo/actual', 'https://plex.example/photo/actual', '../actual'])
def test_same_origin_redirects_keep_token_and_do_not_repeat_query(monkeypatch, location):
    app = PlexApp('https://plex.example', 'private-token')
    calls = []
    replies = iter([response(302, location), response(200)])

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(replies)

    monkeypatch.setattr(app.session, 'get', get)
    assert app._get('/library/metadata/7/thumb/42', params={'a': 1}).content == b'picture'
    assert len(calls) == 2 and calls[1][0].startswith('https://plex.example/')
    assert 'params' not in calls[1][1]
    assert app.session.headers['X-Plex-Token'] == 'private-token'
    assert all(not kwargs['allow_redirects'] for _, kwargs in calls)


@pytest.mark.parametrize('location', [
    'https://external.example/art?X-Plex-Token=do-not-log', '//external.example/art',
    'http://plex.example/art', 'https://user:pass@plex.example/art',
    'https://plex.example:8443/art', r'\\external.example\art',
])
def test_cross_origin_redirect_never_sends_a_second_request(monkeypatch, location):
    app = PlexApp('https://plex.example', 'private-token')
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return response(302, location)

    monkeypatch.setattr(app.session, 'get', get)
    with pytest.raises(PlexError, match='/diagnostics/databases') as caught:
        app._get('/diagnostics/databases')
    assert len(calls) == 1
    assert 'do-not-log' not in str(caught.value) and 'private-token' not in str(caught.value)
    assert app._get('/library/metadata/7/thumb/42', missing_ok=True) is None
    assert len(calls) == 2  # one initial request per call, zero cross-host calls


def test_redirect_loop_is_bounded(monkeypatch):
    app = PlexApp('https://plex.example', 'private-token')
    calls = []
    monkeypatch.setattr(app.session, 'get', lambda url, **kw: (calls.append(url), response(302, '/loop'))[1])
    with pytest.raises(PlexError, match='too many redirects'):
        app._get('/diagnostics/databases')
    assert len(calls) == 6


def test_external_artwork_redirect_is_recorded_without_losing_database_backup(tmp_path, monkeypatch):
    def pages(params):
        if params['type'] == 18:
            return b'<MediaContainer totalSize="0" offset="0"/>'
        return b'<MediaContainer totalSize="1" offset="0"><Video ratingKey="7" thumb="/library/metadata/7/thumb/42"/></MediaContainer>'
    app = driver(routes={
        '/library/sections': b'<MediaContainer><Directory key="1" type="movie"/></MediaContainer>',
        '/library/sections/1/prefs': b'<MediaContainer/>', '/library/sections/1/all': pages,
    })
    get = app.session.get

    def redirected(url, **kwargs):
        if '/thumb/' in url:
            return response(302, 'https://cdn.example/image')
        return get(url, **kwargs)

    monkeypatch.setattr(app.session, 'get', redirected)
    with zipfile.ZipFile(app.backup(str(tmp_path))) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['unavailable_artwork'] == ['/library/metadata/7/thumb/42']
        assert manifest['server']['machine_identifier'] == 'fixture-server'
        assert zipfile.is_zipfile(io.BytesIO(archive.read('databases.zip')))
