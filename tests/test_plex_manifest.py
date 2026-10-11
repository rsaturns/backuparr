"""Compatibility and bounds for backup metadata in small and large libraries."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from plex_restore_agent.docker import AgentError
from plex_restore_agent.manifest import read_manifest
from test_driver_plex import archive, driver
from test_plex_restore_agent import DATABASE, IDENTITY, backup, database, manager, run_restore


MANIFEST = {'format': 'backuparr-plex-export', 'format_version': 1,
            'database_archive': 'databases.zip', 'server': IDENTITY}


def replace_manifest(backup, manifest):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(backup)) as source, zipfile.ZipFile(
            output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, manifest if name == 'manifest.json' else source.read(name))
    return output.getvalue()


@pytest.mark.parametrize('legacy', [False, True])
def test_large_missing_artwork_backup_restores_watched_state(manager, tmp_path, legacy):
    # Reproduce the review: the old inline list exceeds 1 MiB even though the
    # complete compressed export is small and its database is valid.
    def pages(params):
        total = 12000 if params['type'] == 1 else 0
        start = params['X-Plex-Container-Start']
        stop = min(start + params['X-Plex-Container-Size'], total)
        items = ''.join(
            f'<Video ratingKey="{key}" thumb="/library/metadata/{key}/thumb/1760227200" '
            f'art="/library/metadata/{key}/art/1760227200"/>'
            for key in range(start + 1, stop + 1))
        return f'<MediaContainer totalSize="{total}" offset="{start}">{items}</MediaContainer>'.encode()

    app = driver(body=archive(data=database(tmp_path / 'source.db', 5)), routes={
        '/library/sections': b'<MediaContainer><Directory key="1" type="movie"/></MediaContainer>',
        '/library/sections/1/prefs': b'<MediaContainer/>',
        '/library/sections/1/all': pages,
    })
    get = app.session.get

    def missing(url, **kwargs):
        if '/library/metadata/' in url:
            app.session.routes[url.split('/base', 1)[1]] = (404, b'')
        return get(url, **kwargs)

    app.session.get = missing
    content = Path(app.backup(str(tmp_path / 'export'))).read_bytes()
    with zipfile.ZipFile(io.BytesIO(content)) as exported:
        manifest = json.loads(exported.read('manifest.json'))
        unavailable = exported.read(manifest['unavailable_artwork_file'])
        assert len(unavailable) > 1024**2
        assert len(json.loads(unavailable)) == manifest['unavailable_artwork_count'] == 24000
        assert exported.getinfo('manifest.json').file_size < 4096
    if legacy:
        del manifest['unavailable_artwork_file'], manifest['unavailable_artwork_count']
        # Put the large list before the required fields, so a streaming reader
        # cannot stop parsing as soon as it has encountered the server identity.
        manifest = dict(unavailable_artwork=json.loads(unavailable), **manifest)
        content = replace_manifest(content, json.dumps(manifest, indent=2))
    job = run_restore(manager, content)
    assert job['phase'] == 'complete'
    with closing(sqlite3.connect(manager.database_dir / DATABASE)) as db:
        assert db.execute('SELECT * FROM metadata_item_settings').fetchall() == [(1, 5, 0), (2, 0, 123000)]


@pytest.mark.parametrize('suffix', [b' trailing garbage', b'{}', b'\xff', b','])
def test_invalid_manifest_tail_never_stops_plex(manager, backup, suffix):
    content = replace_manifest(backup, json.dumps(MANIFEST).encode() + suffix)
    job = run_restore(manager, content)
    assert job['phase'] == 'failed'
    assert 'manifest' in job['error']
    assert manager.docker.calls == []


def test_truncated_ignored_artwork_list_never_stops_plex(manager, backup):
    manifest = json.dumps(MANIFEST)[:-1] + ',"unavailable_artwork":["unfinished"'
    job = run_restore(manager, replace_manifest(backup, manifest))
    assert job['phase'] == 'failed' and 'manifest' in job['error']
    assert manager.docker.calls == []


def test_expanded_manifest_limit_is_checked_before_stopping_plex(manager, backup):
    manager.max_bytes = 1024**2
    manifest = dict(MANIFEST, unavailable_artwork=['https://example.test/image'] * 50000)
    content = replace_manifest(backup, json.dumps(manifest))
    assert len(content) < manager.max_bytes
    job = run_restore(manager, content)
    assert job['phase'] == 'failed' and 'manifest is too large' in job['error']
    assert manager.docker.calls == []


@pytest.mark.parametrize('field,value', [
    ('unavailable_artwork', ['x' * (2 * 1024**2)]),
    ('unknown', 'x' * (2 * 1024**2)),
], ids=['legacy-url', 'unknown-field'])
def test_single_huge_json_value_is_bounded(field, value):
    source = io.BytesIO(json.dumps(dict(MANIFEST, **{field: value})).encode())
    with pytest.raises(AgentError, match='oversized JSON value'):
        read_manifest(source, 20 * 1024**3)
    assert source.tell() <= 2 * 1024**2  # Abort before accumulating the whole value.


def test_manifest_stream_enforces_actual_read_limit():
    source = io.BytesIO(json.dumps(dict(MANIFEST, unavailable_artwork=['url'] * 100000)).encode())
    with pytest.raises(AgentError, match='configured size limit'):
        read_manifest(source, 128 * 1024)


def test_deeply_nested_ignored_values_are_rejected():
    source = io.BytesIO(b'{"ignored":' + b'[' * 100 + b'0' + b']' * 100 + b'}')
    with pytest.raises(AgentError, match='nested too deeply'):
        read_manifest(source, 1024**2)


@pytest.mark.parametrize('document', [
    b'{"server":{},"server":{}}',
    b'{"server":{"version":"one","version":"two"}}',
    b'{"format":"one","format":"two"}',
])
def test_ambiguous_restore_fields_are_rejected(document):
    with pytest.raises(AgentError, match='Duplicate'):
        read_manifest(io.BytesIO(document), 1024**2)


def test_early_rejection_closes_parser_without_consuming_rest_of_manifest():
    document = b'{"format":1,"format":2,"unavailable_artwork":[' + b'"url",' * 100000 + b'"last"]}'
    source = io.BytesIO(document)
    with pytest.raises(AgentError, match='Duplicate'):
        read_manifest(source, 1024**2)
    assert source.tell() < len(document)


def test_unknown_nested_and_dotted_keys_cannot_replace_restore_metadata():
    document = dict(MANIFEST, **{'server.version': 'wrong', 'unknown': {'server': {'version': 'wrong'}}})
    assert read_manifest(io.BytesIO(json.dumps(document).encode()), 1024**2) == MANIFEST


def test_corrupt_manifest_crc_is_rejected_before_stopping_plex(manager, backup):
    # ZIP_STORED permits a same-length edit that leaves valid JSON but stale CRC.
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(backup)) as source, zipfile.ZipFile(output, 'w') as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
    content = output.getvalue().replace(b'fixture-server', b'fixture-serveX')
    job = run_restore(manager, content)
    assert job['phase'] == 'failed' and manager.docker.calls == []
