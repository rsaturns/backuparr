import io
from pathlib import Path
import zipfile

import pytest
import requests

from apps.plex import PlexApp, PlexError
from backup import build_app
from config_store import restore_supported


EXPORT_NAME = 'databaseBackup.db3fa58294-9e85-4bd7-ac6d-8da54a567d7e'


def archive(name=EXPORT_NAME, data=b'SQLite format 3\0fixture database'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zf:
        zf.writestr(name, data)
    return out.getvalue()


class Session:
    def __init__(self, body=None, status=200, error=None, prefs=None, routes=None):
        self.body, self.status, self.error = body, status, error
        self.prefs, self.routes = prefs, routes or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.status
        if url.endswith('/identity'):
            body = b'<MediaContainer version="1.43.3.10896-test" machineIdentifier="fixture-server"/>'
        elif url.endswith('/:/prefs'):
            body = self.prefs if self.prefs is not None else b'<MediaContainer><Setting id="FriendlyName" value="Fixture"/></MediaContainer>'
        elif url.endswith('/library/sections'):
            body = self.routes.get('/library/sections', b'<MediaContainer size="0"/>')
        elif '/library/' in url:
            body = self.routes[url.split('/base', 1)[1]]
            if callable(body):
                body = body(kwargs.get('params', {}))
        else:
            body = self.body if self.body is not None else archive()
        if isinstance(body, tuple):
            response.status_code, body = body
        response._content = body
        response.headers['Content-Type'] = 'image/png' if '/thumb/' in url else 'application/xml'
        response._content_consumed = True
        return response


def driver(**kwargs):
    app = PlexApp('http://plex:32400/base/', 'private-plex-token')
    app.session = Session(**kwargs)
    return app


def test_connection_requires_owner_settings_without_creating_backup():
    app = driver(prefs=b'<MediaContainer><Setting id="FriendlyName" value="Media"/></MediaContainer>')
    assert 'owner access' in app.test_connection()
    assert [url for url, _ in app.session.calls] == ['http://plex:32400/base/:/prefs']
    assert not app.session.calls[0][1]['allow_redirects']


@pytest.mark.parametrize('name', [
    EXPORT_NAME,
    'Databases/' + EXPORT_NAME,
    'Databases\\' + EXPORT_NAME,
    'databaseBackup.db',
    'Databases/databaseBackup.db',
    r'Databases\databaseBackup.db',
    'com.plexapp.plugins.library.db',
    'Databases/com.plexapp.plugins.library.db',
])
def test_native_database_export_kept_intact(tmp_path, name):
    body = archive(name=name)
    app = driver(body=body)
    path = Path(app.backup(str(tmp_path)))
    with zipfile.ZipFile(path) as zf:
        assert zf.read("databases.zip") == body
        assert b"Fixture" in zf.read("server-preferences.xml")
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(app.session.calls) == 4
    url, kwargs = app.session.calls[1]
    assert url.endswith('/diagnostics/databases')
    assert 'private-plex-token' not in str(app.session.calls)
    assert kwargs['stream'] and not kwargs['allow_redirects']


@pytest.mark.parametrize('status', [301, 302, 307, 401, 403, 404, 500])
def test_http_failures_never_produce_backups(tmp_path, status):
    with pytest.raises(PlexError):
        driver(status=status).backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('body', [
    b'<html>sign in</html>', b'', archive('other.db'),
    archive('com.plexapp.plugins.library.blobs.db'),
    archive('databaseBackup.db-unrelated'),
    archive(data=b'not a database'), archive()[:-40],
])
def test_invalid_archives_are_removed(tmp_path, body):
    with pytest.raises(PlexError):
        driver(body=body).backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('body', [b'<html/>', b'<MediaContainer/>', b'bad xml'])
def test_false_connection_success_is_rejected(body):
    with pytest.raises(PlexError):
        driver(prefs=body).test_connection()


def test_download_failure_is_cleaned_and_sanitized(tmp_path, monkeypatch):
    def interrupted(self, **kwargs):
        yield b'partial'
        raise requests.ConnectionError('private-plex-token')
    monkeypatch.setattr(requests.Response, 'iter_content', interrupted)
    with pytest.raises(PlexError) as exc:
        driver().backup(str(tmp_path))
    assert 'private-plex-token' not in str(exc.value)
    assert exc.value.__suppress_context__
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('url', ['ftp://plex', 'http://user:pass@plex', 'http://plex/?token=secret', 'http://plex:bad', None])
def test_bad_urls_fail_before_network(url):
    with pytest.raises(PlexError):
        PlexApp(url, 'token')


def test_factory_and_token_storage(authed_client, isolated_webui, monkeypatch):
    app = build_app('plex', {'url': 'http://plex:32400', 'api_key': 'private-plex-token'})
    assert app.session.headers['X-Plex-Token'] == 'private-plex-token'
    assert restore_supported('plex')
    html = authed_client.get('/').data
    assert b'Plex token' in html and b'Library databases including' in html
    cfg = {'enabled': True, 'url': 'http://plex:32400', 'api_key': 'private-plex-token'}
    assert authed_client.post('/api/config', json={'apps': {'plex': cfg}}).status_code == 200
    assert authed_client.get('/api/config').json['apps']['plex']['api_key'] == 'private-plex-token'
    assert 'private-plex-token' not in Path(isolated_webui.CONFIG_PATH).read_text()
    monkeypatch.setattr(PlexApp, 'test_connection', lambda self: 'Plex reachable')
    assert authed_client.post('/api/test/plex', json=cfg).json['ok']


@pytest.mark.parametrize('name', [EXPORT_NAME, 'databaseBackup.db', 'com.plexapp.plugins.library.db'])
def test_database_export_preserves_watched_unwatched_and_progress_for_multiple_users(tmp_path, name):
    import sqlite3

    original = tmp_path / 'original.db'
    with sqlite3.connect(original) as db:
        db.execute('CREATE TABLE metadata_item_settings (account_id INT, guid TEXT, view_count INT, view_offset INT)')
        rows = [(1, 'plex://movie/one', 1, 0), (2, 'plex://movie/one', 0, 0),
                (2, 'plex://movie/two', 0, 123000)]
        db.executemany('INSERT INTO metadata_item_settings VALUES (?, ?, ?, ?)', rows)
    output = driver(body=archive(name=name, data=original.read_bytes())).backup(str(tmp_path / 'output'))
    with zipfile.ZipFile(output) as zf:
        restored = tmp_path / 'com.plexapp.plugins.library.db'
        with zipfile.ZipFile(io.BytesIO(zf.read('databases.zip'))) as database:
            restored.write_bytes(database.read(name))
        assert b'databaseBackup.db' in zf.read('RESTORE.txt')
    with sqlite3.connect(restored) as db:
        assert db.execute('SELECT * FROM metadata_item_settings').fetchall() == rows


def test_ambiguous_library_databases_are_rejected(tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zf:
        for name in ('databaseBackup.db', 'com.plexapp.plugins.library.db'):
            zf.writestr(name, b'SQLite format 3\0fixture database')
    with pytest.raises(PlexError, match='multiple Plex library databases'):
        driver(body=out.getvalue()).backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_settings_metadata_artwork_and_external_image_limits(tmp_path):
    import json
    image = b"\x89PNG\r\n\x1a\nfixture"
    def pages(params):
        if params["type"] == 18:
            return b'<MediaContainer totalSize="0" offset="0"/>'
        return b'<MediaContainer totalSize="1" offset="0"><Video ratingKey="7" thumb="/library/metadata/7/thumb/42" art="https://external.example/private.jpg"/></MediaContainer>'
    app = driver(routes={
        '/library/sections': b'<MediaContainer><Directory key="1" type="movie" title="Movies"/></MediaContainer>',
        '/library/sections/1/prefs': b'<MediaContainer><Setting id="language" value="cs"/></MediaContainer>',
        '/library/sections/1/all': pages,
        '/library/metadata/7/thumb/42': image,
    })
    with zipfile.ZipFile(app.backup(str(tmp_path))) as z:
        assert b'value="cs"' in z.read('library-preferences/1.xml')
        assert b'ratingKey="7"' in z.read('metadata/1/1-0.xml')
        assert z.read('artwork/0') == image
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['artwork_count'] == 1
        assert manifest['unavailable_artwork'] == ['https://external.example/private.jpg']
    assert all(url.startswith('http://plex:32400/base/') for url, _ in app.session.calls)
    assert list(tmp_path.iterdir()) == [tmp_path / 'plex-backup.zip']


def test_incomplete_metadata_discards_entire_export(tmp_path):
    app = driver(routes={
        '/library/sections': b'<MediaContainer><Directory key="1" type="movie"/></MediaContainer>',
        '/library/sections/1/prefs': b'<MediaContainer/>',
        '/library/sections/1/all': b'<MediaContainer totalSize="5" offset="0"/>',
    })
    with pytest.raises(PlexError):
        app.backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('status', [404, 401, 403, 500, 302])
def test_missing_artwork_is_reported_but_other_download_errors_fail(tmp_path, status):
    import json

    def pages(params):
        if params['type'] == 18:
            return b'<MediaContainer totalSize="0" offset="0"/>'
        return b'<MediaContainer totalSize="1" offset="0"><Video ratingKey="7" thumb="/library/metadata/7/thumb/42"/></MediaContainer>'

    app = driver(routes={
        '/library/sections': b'<MediaContainer><Directory key="1" type="movie"/></MediaContainer>',
        '/library/sections/1/prefs': b'<MediaContainer/>',
        '/library/sections/1/all': pages,
        '/library/metadata/7/thumb/42': (status, b'not found'),
    })
    if status == 404:
        with zipfile.ZipFile(app.backup(str(tmp_path))) as z:
            assert z.read('databases.zip') == archive()
            assert 'metadata/1/1-0.xml' in z.namelist()
            manifest = json.loads(z.read('manifest.json'))
            assert manifest['unavailable_artwork'] == ['/library/metadata/7/thumb/42']
            assert manifest['artwork_count'] == 0
    else:
        with pytest.raises(PlexError):
            app.backup(str(tmp_path))
        assert not list(tmp_path.iterdir())
