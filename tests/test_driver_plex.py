import io
import json
import os
from pathlib import Path
import sqlite3
import zipfile

import pytest
import requests

import backup
from apps import plex
from apps.plex import LIBRARY_DB, MANIFEST, PREFERENCES, PlexApp, PlexError
from backup import build_app
from config_store import restore_supported


EXPORT_NAME = 'databaseBackup.db3fa58294-9e85-4bd7-ac6d-8da54a567d7e'
SERVER_ID = 'fixture-server'


def database(path, schema=20240718114400, rows=((1, 'plex://movie/one', 1, 0),)):
    """A stand-in library database: Plex's migration table plus watch state."""
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT)')
        db.execute('DELETE FROM schema_migrations')
        db.execute('INSERT INTO schema_migrations VALUES (?)', (str(schema),))
        db.execute('CREATE TABLE IF NOT EXISTS metadata_item_settings (account_id INT, guid TEXT, view_count INT, view_offset INT)')
        db.execute('DELETE FROM metadata_item_settings')
        db.executemany('INSERT INTO metadata_item_settings VALUES (?, ?, ?, ?)', rows)
    db.close()
    return Path(path)


def watch_state(path):
    db = sqlite3.connect(path)
    try:
        return db.execute('SELECT * FROM metadata_item_settings ORDER BY account_id, guid').fetchall()
    finally:
        db.close()


def archive(name=EXPORT_NAME, data=None):
    if data is None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            data = database(Path(tmp, 'fixture.db')).read_bytes()
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zf:
        zf.writestr(name, data)
    return out.getvalue()


class Session:
    """Answers Plex's routes; records every request."""

    def __init__(self, body=None, status=200, error=None, prefs=None, running=True):
        self.body, self.status, self.error, self.prefs, self.running = body, status, error, prefs, running
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.status
        if url.endswith('/identity'):
            if not self.running:
                raise requests.ConnectionError('refused')
            body = f'<MediaContainer version="1.43.5.11079-test" machineIdentifier="{SERVER_ID}"/>'.encode()
        elif url.endswith('/:/prefs'):
            body = self.prefs if self.prefs is not None else b'<MediaContainer><Setting id="FriendlyName" value="Fixture"/></MediaContainer>'
        else:
            body = self.body if self.body is not None else archive()
        response._content = body
        response._content_consumed = True
        return response


def driver(data_dir=None, **kwargs):
    app = PlexApp('http://plex:32400/base/', 'private-plex-token', data_dir=data_dir)
    session = Session(**kwargs)
    session.headers = app.session.headers
    app.session = session
    return app


def plex_folder(root, server_id=SERVER_ID, name='Lab'):
    """A stopped Plex's "Plex Media Server" folder."""
    (root / 'Plug-in Support/Databases').mkdir(parents=True)
    (root / 'Metadata/Movies').mkdir(parents=True)
    (root / PREFERENCES).write_text(
        f'<Preferences MachineIdentifier="m-{server_id}" ProcessedMachineIdentifier="{server_id}" '
        f'FriendlyName="{name}" PlexOnlineToken="secret"/>')
    database(root / 'Plug-in Support/Databases' / LIBRARY_DB, rows=((1, 'plex://movie/one', 0, 0),))
    (root / 'Plug-in Support/Databases/com.plexapp.plugins.library.blobs.db').write_bytes(b'blobs')
    return root


@pytest.fixture
def folder(tmp_path):
    return plex_folder(tmp_path / 'Plex Media Server')


def make_backup(app, tmp_path):
    out = tmp_path / 'plex_20261028_030000.zip'
    backup.zip_dir(app.backup(str(tmp_path / 'work')), str(out))
    return str(out)


# --- connection --------------------------------------------------------------

def test_connection_requires_owner_settings_and_keeps_the_token_out_of_urls(folder):
    app = driver(str(folder))
    assert app.test_connection() == 'plex 1.43.5.11079-test reachable, owner access OK, data folder OK'
    assert [url for url, _ in app.session.calls] == ['http://plex:32400/base/:/prefs', 'http://plex:32400/base/identity']
    assert all(not kwargs['allow_redirects'] for _, kwargs in app.session.calls)
    assert 'private-plex-token' not in str(app.session.calls)


@pytest.mark.parametrize('body', [b'<html/>', b'<MediaContainer/>', b'bad xml'])
def test_false_connection_success_is_rejected(folder, body):
    with pytest.raises(PlexError):
        driver(str(folder), prefs=body).test_connection()


@pytest.mark.parametrize('status', [401, 403])
def test_a_rejected_token_asks_for_the_owners(status):
    with pytest.raises(PlexError, match="use the server owner's Plex token"):
        driver(status=status).test_connection()


def test_the_data_folder_must_be_set_exist_and_be_plexs(folder, tmp_path, monkeypatch):
    with pytest.raises(PlexError, match='set the data folder'):
        driver().test_connection()
    with pytest.raises(PlexError, match="doesn't exist in this container"):
        driver(str(tmp_path / 'missing')).test_connection()
    with pytest.raises(PlexError, match="doesn't look like Plex's data folder"):
        driver(str(folder / 'Plug-in Support')).test_connection()
    monkeypatch.setattr(plex.os, 'access', lambda path, mode: mode != os.W_OK)
    assert driver(str(folder)).test_connection().endswith("; data folder is read-only, so backups work but restores won't")


def test_another_servers_folder_is_the_wrong_mount(folder):
    (folder / 'Preferences.xml').write_text('<Preferences ProcessedMachineIdentifier="other-server"/>')
    with pytest.raises(PlexError, match="holds another Plex server's data .*other-server"):
        driver(str(folder)).test_connection()


@pytest.mark.parametrize('url', ['ftp://plex', 'http://user:pass@plex', 'http://plex/?token=secret', 'http://plex:bad', None])
def test_bad_urls_fail_before_network(url):
    with pytest.raises(PlexError):
        PlexApp(url, 'token')


# --- backup ------------------------------------------------------------------

@pytest.mark.parametrize('name', [
    EXPORT_NAME, 'Databases/' + EXPORT_NAME, 'Databases\\' + EXPORT_NAME, 'databaseBackup.db',
    'com.plexapp.plugins.library.db', 'Databases/com.plexapp.plugins.library.db',
])
def test_backup_holds_plexs_database_export_and_its_settings_file(folder, tmp_path, name):
    rows = [(1, 'plex://movie/one', 1, 0), (2, 'plex://movie/one', 0, 0), (2, 'plex://movie/two', 0, 123000)]
    data = database(tmp_path / 'exported.db', rows=rows).read_bytes()
    app = driver(str(folder), body=archive(name=name, data=data))
    with zipfile.ZipFile(make_backup(app, tmp_path)) as zf:
        assert sorted(zf.namelist()) == sorted([MANIFEST, LIBRARY_DB, PREFERENCES])
        assert zf.read(LIBRARY_DB) == data
        assert zf.read(PREFERENCES) == (folder / PREFERENCES).read_bytes()
        assert json.loads(zf.read(MANIFEST)) == {'plex_version': '1.43.5.11079-test', 'machine_identifier': SERVER_ID,
                                                 'schema_version': 20240718114400}
    url, kwargs = app.session.calls[1]
    assert url.endswith('/diagnostics/databases') and kwargs['stream'] and not kwargs['allow_redirects']
    # Every user's watch state comes along, not only the token owner's.
    assert watch_state(tmp_path / 'exported.db') == rows


@pytest.mark.parametrize('status', [301, 302, 307, 401, 403, 404, 500])
def test_http_failures_never_produce_backups(folder, tmp_path, status):
    with pytest.raises(PlexError):
        driver(str(folder), status=status).backup(str(tmp_path / 'work'))
    assert not list((tmp_path / 'work').glob('*'))


@pytest.mark.parametrize('body', [
    b'<html>sign in</html>', b'', archive('other.db'),
    archive('com.plexapp.plugins.library.blobs.db'),
    archive('databaseBackup.db-unrelated'),
    archive(data=b'not a database'), archive()[:-40],
])
def test_invalid_exports_are_refused_and_removed(folder, tmp_path, body):
    with pytest.raises(PlexError):
        driver(str(folder), body=body).backup(str(tmp_path / 'work'))
    assert not list((tmp_path / 'work').glob('*'))


def test_an_export_with_two_library_databases_is_refused(folder, tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zf:
        for name in ('databaseBackup.db', 'com.plexapp.plugins.library.db'):
            zf.writestr(name, archive())
    with pytest.raises(PlexError, match='exactly one library database'):
        driver(str(folder), body=out.getvalue()).backup(str(tmp_path / 'work'))
    assert not list((tmp_path / 'work').glob('*'))


def test_download_failure_is_cleaned_and_sanitized(folder, tmp_path, monkeypatch):
    def interrupted(self, **kwargs):
        yield b'partial'
        raise requests.ConnectionError('private-plex-token')
    monkeypatch.setattr(requests.Response, 'iter_content', interrupted)
    with pytest.raises(PlexError) as exc:
        driver(str(folder)).backup(str(tmp_path / 'work'))
    assert 'private-plex-token' not in str(exc.value) and exc.value.__suppress_context__
    assert not list((tmp_path / 'work').glob('*'))


def test_version_change_during_the_export_discards_the_backup(folder, tmp_path, monkeypatch):
    app = driver(str(folder))
    identities = iter([{'version': '1.43.3', 'machine_identifier': SERVER_ID},
                       {'version': '1.43.4', 'machine_identifier': SERVER_ID}])
    monkeypatch.setattr(app, 'identity', lambda: next(identities))
    with pytest.raises(PlexError, match='changed during the database export'):
        app.backup(str(tmp_path / 'work'))
    assert not list((tmp_path / 'work').glob('*'))


def test_another_servers_folder_is_never_backed_up(folder, tmp_path):
    (folder / PREFERENCES).write_text('<Preferences ProcessedMachineIdentifier="other-server"/>')
    with pytest.raises(PlexError, match="holds another Plex server's data"):
        driver(str(folder)).backup(str(tmp_path / 'work'))


def test_a_settings_file_plex_was_rewriting_fails_the_backup(folder, tmp_path, monkeypatch):
    real = plex.shutil.copyfile
    monkeypatch.setattr(plex.shutil, 'copyfile', lambda src, dst: (real(src, dst), Path(dst).write_text('<Preferences Machine'))[0])
    with pytest.raises(PlexError, match="can't read Preferences.xml"):
        driver(str(folder)).backup(str(tmp_path / 'work'))
    assert not list((tmp_path / 'work').glob('*'))


def test_factory_settings_and_token_storage(authed_client, isolated_webui, monkeypatch):
    app = build_app('plex', {'url': 'http://plex:32400', 'api_key': 'private-plex-token', 'data_dir': '/mnt/plex'})
    assert app.session.headers['X-Plex-Token'] == 'private-plex-token' and app.data_dir == '/mnt/plex'
    assert restore_supported('plex')
    assert b'Plex token' in authed_client.get('/').data
    cfg = {'enabled': True, 'url': 'http://plex:32400', 'api_key': 'private-plex-token', 'data_dir': '/mnt/plex'}
    assert authed_client.post('/api/config', json={'apps': {'plex': cfg}}).status_code == 200
    assert authed_client.get('/api/config').json['apps']['plex']['api_key'] == 'private-plex-token'
    assert 'private-plex-token' not in Path(isolated_webui.CONFIG_PATH).read_text()
    monkeypatch.setattr(PlexApp, 'test_connection', lambda self: 'Plex reachable')
    assert authed_client.post('/api/test/plex', json=cfg).json['ok']


# --- restore -----------------------------------------------------------------

def stopped(folder):
    return driver(str(folder), running=False)


@pytest.fixture
def source(tmp_path):
    """The folder of the server the backups come from."""
    return plex_folder(tmp_path / 'source')


def test_restore_writes_the_backups_database_and_settings_over_plexs(folder, source, tmp_path):
    rows = [(1, 'plex://movie/one', 1, 0), (2, 'plex://movie/two', 0, 123000)]
    archive_path = make_backup(driver(str(source), body=archive(data=database(tmp_path / 'e.db', rows=rows).read_bytes())), tmp_path)
    (folder / PREFERENCES).write_text('<Preferences ProcessedMachineIdentifier="fixture-server" FriendlyName="Changed"/>')
    target = folder / 'Plug-in Support/Databases' / LIBRARY_DB
    os.chmod(target, 0o640)
    (folder / 'Plug-in Support/Databases' / f'{LIBRARY_DB}-shm').write_bytes(b'stale')
    stopped(folder).restore_from_file(archive_path)
    assert watch_state(target) == rows
    assert (folder / PREFERENCES).read_bytes() == (source / PREFERENCES).read_bytes()
    assert target.stat().st_mode & 0o777 == 0o640  # Rewritten in place.
    assert not (folder / 'Plug-in Support/Databases' / f'{LIBRARY_DB}-shm').exists()
    assert (folder / 'Plug-in Support/Databases/com.plexapp.plugins.library.blobs.db').read_bytes() == b'blobs'


def test_a_rebuilt_server_gets_its_old_identity_back(source, tmp_path):
    # A fresh install has its own id; restored, it is the backed-up server
    # again, so plex.tv still knows it and shared users keep their access.
    rebuilt = plex_folder(tmp_path / 'rebuilt', server_id='brand-new-install', name='New')
    archive_path = make_backup(driver(str(source)), tmp_path)
    stopped(rebuilt).restore_from_file(archive_path)
    restored = (rebuilt / PREFERENCES).read_text()
    assert f'ProcessedMachineIdentifier="{SERVER_ID}"' in restored and 'brand-new-install' not in restored


def test_restore_is_refused_while_plex_has_its_databases_open(folder, source, tmp_path):
    archive_path = make_backup(driver(str(source)), tmp_path)
    (folder / 'Plug-in Support/Databases/com.plexapp.plugins.library.blobs.db-wal').write_bytes(b'')
    with pytest.raises(PlexError, match=r'stop Plex before restoring - its databases are in use \(found com.plexapp.plugins.library.blobs.db-wal\)'):
        stopped(folder).restore_from_file(archive_path)
    assert watch_state(folder / 'Plug-in Support/Databases' / LIBRARY_DB) == [(1, 'plex://movie/one', 0, 0)]


def test_restore_is_refused_while_plex_answers(folder, source, tmp_path):
    archive_path = make_backup(driver(str(source)), tmp_path)
    with pytest.raises(PlexError, match="it's still answering at http://plex:32400/base"):
        driver(str(folder)).restore_from_file(archive_path)


def test_a_proxy_in_front_of_a_stopped_plex_counts_as_stopped(folder, source, tmp_path):
    archive_path = make_backup(driver(str(source)), tmp_path)
    app = driver(str(folder), status=502)
    app.restore_from_file(archive_path)
    assert watch_state(folder / 'Plug-in Support/Databases' / LIBRARY_DB) == [(1, 'plex://movie/one', 1, 0)]


def test_a_backup_from_a_newer_plex_is_refused(folder, source, tmp_path):
    newer = database(tmp_path / 'newer.db', schema=20991231000000).read_bytes()
    archive_path = make_backup(driver(str(source), body=archive(data=newer)), tmp_path)
    with pytest.raises(PlexError, match='newer Plex than the one installed .*update Plex first'):
        stopped(folder).restore_from_file(archive_path)


def test_an_older_backup_is_restored_for_plex_to_upgrade(folder, source, tmp_path):
    older = database(tmp_path / 'older.db', schema=20200101000000, rows=((1, 'plex://movie/old', 1, 0),)).read_bytes()
    archive_path = make_backup(driver(str(source), body=archive(data=older)), tmp_path)
    stopped(folder).restore_from_file(archive_path)
    assert watch_state(folder / 'Plug-in Support/Databases' / LIBRARY_DB) == [(1, 'plex://movie/old', 1, 0)]


def test_restore_needs_the_data_folder_and_write_access(folder, source, tmp_path, monkeypatch):
    archive_path = make_backup(driver(str(source)), tmp_path)
    with pytest.raises(PlexError, match='set the data folder'):
        driver(running=False).restore_from_file(archive_path)
    monkeypatch.setattr(plex.os, 'access', lambda path, mode: mode != os.W_OK)
    with pytest.raises(PlexError, match="aren't writable - mount the data folder read-write"):
        stopped(folder).restore_from_file(archive_path)


def test_restore_refuses_a_file_that_isnt_a_plex_backup(folder, tmp_path):
    other = tmp_path / 'plex_x.zip'
    other.write_bytes(archive())  # Plex's raw export, not a Backuparr archive.
    with pytest.raises(PlexError, match="isn't a Plex backup made by Backuparr"):
        stopped(folder).restore_from_file(str(other))


# --- connection failures -------------------------------------------------------

def unused_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.mark.parametrize('case, expected', [
    ('dns', "couldn't resolve hostname 'plex' - check the URL"),
    ('refused', 'connection refused by'),
    ('tls', 'TLS/SSL error talking to plex.example:32400 - check http:// vs https://'),
])
def test_a_failed_connection_says_why(request, folder, monkeypatch, case, expected):
    # Plex's usual host networking leaves http://plex:32400 unresolvable from
    # Backuparr's network: the message has to say why, like for other apps.
    if case == 'dns':
        request.getfixturevalue('dns_down')
        url = 'http://plex:32400'
    elif case == 'refused':
        url = f'http://127.0.0.1:{unused_port()}'
    else:
        url = 'https://plex.example:32400'

        def bad_certificate(*args, **kwargs):
            raise requests.exceptions.SSLError("hostname 'plex.example' doesn't match '*.abc.plex.direct'")
        monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', bad_certificate)
    app = PlexApp(url, 'private-plex-token', data_dir=str(folder))
    with pytest.raises(requests.ConnectionError) as caught:
        app.test_connection()
    message = backup.humanize_error(caught.value, url, app='plex')
    assert expected in message and 'private-plex-token' not in message


# --- plex.direct addresses -------------------------------------------------------

@pytest.mark.parametrize('host, address', [
    ('192-168-2-25.abc123def.plex.direct', '192.168.2.25'),
    ('10-0-0-1.ABC123.plex.direct', '10.0.0.1'),
    ('192-168-2-256.abc123.plex.direct', None),
    ('192.168.2.25', None), ('plex', None), ('evil-plex.direct', None), (None, None),
])
def test_a_plex_direct_name_spells_out_its_address(host, address):
    assert plex.plex_direct_address(host) == address


@pytest.fixture(scope='module')
def plex_tls(tmp_path_factory):
    """A Plex stand-in on 127.0.0.1 with a CA-signed *.abc123.plex.direct
    certificate - like Plex's own, which is never issued for its IP."""
    import datetime
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    root = tmp_path_factory.mktemp('tls')
    now = datetime.datetime.now(datetime.timezone.utc)

    def certificate(subject, issuer, key, signer, extensions):
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=1)))
        for extension, critical in extensions:
            builder = builder.add_extension(extension, critical=critical)
        return builder.sign(signer, hashes.SHA256())

    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test CA')])
    ca = certificate(ca_name, ca_name, ca_key, ca_key, [
        (x509.BasicConstraints(ca=True, path_length=None), True),
        (x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                       encipher_only=False, decipher_only=False), True),
        (x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)])
    leaf = certificate(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '*.abc123.plex.direct')]), ca_name,
                       leaf_key, ca_key, [
                           (x509.SubjectAlternativeName([x509.DNSName('*.abc123.plex.direct')]), False),
                           (x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)])
    (root / 'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (root / 'leaf.pem').write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (root / 'leaf.key').write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            body = (f'<MediaContainer version="1.43.5" machineIdentifier="{SERVER_ID}">'
                    '<Setting id="FriendlyName" value="Lab"/></MediaContainer>').encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / 'leaf.pem', root / 'leaf.key')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {'port': server.server_address[1], 'ca': str(root / 'ca.pem'), 'received': received}
    server.shutdown()


@pytest.fixture
def lookups(monkeypatch):
    """Records every name lookup; plex.direct names fail, as behind a router
    with DNS-rebinding protection."""
    import socket
    seen, real = [], socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        seen.append(host)
        if str(host).endswith('plex.direct'):
            raise socket.gaierror(-2, 'Name does not resolve')
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    return seen


def test_a_plex_direct_url_works_without_dns_and_still_verifies_the_certificate(plex_tls, lookups, folder):
    app = PlexApp(f"https://127-0-0-1.abc123.plex.direct:{plex_tls['port']}", 'private-plex-token', data_dir=str(folder))
    app.session.verify = plex_tls['ca']
    assert app.test_connection() == 'plex 1.43.5 reachable, owner access OK, data folder OK'
    assert not any(str(host).endswith('plex.direct') for host in lookups)


def test_another_servers_plex_direct_name_fails_verification(plex_tls, lookups, folder):
    app = PlexApp(f"https://127-0-0-1.fff999.plex.direct:{plex_tls['port']}", 'private-plex-token', data_dir=str(folder))
    app.session.verify = plex_tls['ca']
    with pytest.raises(requests.exceptions.SSLError):
        app.test_connection()


def test_https_to_plexs_ip_explains_the_plex_direct_address(plex_tls, folder):
    app = PlexApp(f"https://127.0.0.1:{plex_tls['port']}", 'private-plex-token', data_dir=str(folder))
    app.session.verify = plex_tls['ca']
    with pytest.raises(PlexError, match=rf"isn't issued for 127.0.0.1 - use its https://127-0-0-1.<id>.plex.direct:{plex_tls['port']} address \(Get Plex token fills it in\)"):
        app.test_connection()


def test_service_headers_reach_plex_at_its_plex_direct_address(plex_tls, lookups, folder):
    url = f"https://127-0-0-1.abc123.plex.direct:{plex_tls['port']}"
    cfg = {'url': url, 'api_key': 'private-plex-token', 'data_dir': str(folder),
           'request_headers': [{'name': 'X-Proxy-Token', 'value': 'proxy-secret'}]}
    app = build_app('plex', cfg)
    app.session.verify = plex_tls['ca']
    plex_tls['received'].clear()
    app.test_connection()
    assert plex_tls['received'] and all(h['X-Proxy-Token'] == 'proxy-secret' and h['X-Plex-Token'] == 'private-plex-token'
                                        for h in plex_tls['received'])
