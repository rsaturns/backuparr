import copy
import errno
import io
import json
from pathlib import Path
import sqlite3
import threading
import zipfile

import pytest
import requests

from plex_restore_agent.archive import stage_database
from plex_restore_agent.docker import AgentError
from plex_restore_agent.restore import DATABASE, DATABASE_FILES, RestoreManager
from plex_restore_agent.server import create_app
from plex_restore_agent import restore as restore_module


IDENTITY = {'version': '1.43.3.10896-test', 'machine_identifier': 'fixture-server'}
AGENT_TOKEN = 'agent-test-token-with-at-least-32-characters'
JOB = 'a' * 32


def database(path, watched):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata_items (id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE library_sections (id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE metadata_item_settings (account_id INTEGER, view_count INTEGER, view_offset INTEGER)')
        db.executemany('INSERT INTO metadata_item_settings VALUES (?, ?, ?)', [(1, watched, 0), (2, 0, 123000)])
    return path.read_bytes()


def export(db_bytes, identity=IDENTITY, names=None):
    native = io.BytesIO()
    with zipfile.ZipFile(native, 'w') as archive:
        for name in names or ['databaseBackup.db3fa58294-9e85-4bd7-ac6d-8da54a567d7e']:
            archive.writestr(name, db_bytes)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({
            'format': 'backuparr-plex-export', 'format_version': 1,
            'database_archive': 'databases.zip', 'server': identity,
        }))
        archive.writestr('databases.zip', native.getvalue())
    return out.getvalue()


class FakeDocker:
    def __init__(self, root, state):
        self.target = {
            'Id': 'c' * 64, 'Name': '/plex',
            'Config': {'Labels': {'io.backuparr.plex-restore': 'true'}},
            'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}},
            'State': {'Running': True, 'Paused': False, 'Restarting': False},
            'Mounts': [{'Destination': '/config', 'Source': '/host/plex', 'Type': 'bind', 'RW': True}],
        }
        self.agent = {'Id': 'd' * 64, 'Mounts': [
            {'Destination': str(root), 'Source': '/host/plex', 'Type': 'bind', 'RW': True},
            {'Destination': str(state), 'Source': '/host/state', 'Type': 'volume', 'RW': True},
        ]}
        self.calls = []
        self.starts = 0

    def inspect(self, container):
        return copy.deepcopy(self.agent if container == 'agent' else self.target)

    def assert_stopped(self, container):
        if self.target['State']['Running']:
            raise AgentError('still running')

    def stop(self, container):
        self.calls.append(('stop', container))
        self.target['State']['Running'] = False

    def start(self, container):
        self.calls.append(('start', container))
        self.starts += 1
        self.target['State']['Running'] = True

    def restart_policy(self, container, policy):
        self.calls.append(('policy', dict(policy)))
        self.target['HostConfig']['RestartPolicy'] = dict(policy)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    root = tmp_path / 'plex'
    folder = root / 'Plug-in Support' / 'Databases'
    folder.mkdir(parents=True)
    state = tmp_path / 'state'
    state.mkdir()
    database(folder / DATABASE, 0)
    docker = FakeDocker(root, state)
    instance = RestoreManager(
        docker, container='plex', agent_container='agent', database_dir=folder,
        container_database_dir='/config/Plug-in Support/Databases',
        state_dir=state, plex_url='http://plex:32400', max_bytes=1024**3, health_timeout=0,
    )
    monkeypatch.setattr(instance, 'plex_identity', lambda *args, **kwargs: dict(IDENTITY))
    yield instance
    instance.close()


@pytest.fixture
def backup(tmp_path):
    return export(database(tmp_path / 'new.db', 5))


def run_restore(manager, backup, expected='fixture-server'):
    path = manager.state_dir / JOB
    path.mkdir()
    (path / 'upload.zip').write_bytes(backup)
    job = {'id': JOB, 'phase': 'queued'}
    manager.save(job)
    assert manager.lock.acquire(False)
    manager.execute(job, 'plex-test-token', expected)
    return manager.read_job(JOB)


def test_restore_keeps_watched_progress_ownership_and_originals(manager, backup):
    original = (manager.database_dir / DATABASE).read_bytes()
    original_stat = (manager.database_dir / DATABASE).stat()
    job = run_restore(manager, backup)
    assert job['phase'] == 'complete'
    assert (manager.state_dir / JOB / 'rollback' / DATABASE).read_bytes() == original
    assert (manager.state_dir / JOB / 'rollback' / DATABASE).stat().st_mode & 0o777 == 0o600
    restored_stat = (manager.database_dir / DATABASE).stat()
    assert (restored_stat.st_uid, restored_stat.st_gid, restored_stat.st_mode) == (
        original_stat.st_uid, original_stat.st_gid, original_stat.st_mode)
    with sqlite3.connect(manager.database_dir / DATABASE) as db:
        assert db.execute('SELECT * FROM metadata_item_settings').fetchall() == [(1, 5, 0), (2, 0, 123000)]
    assert manager.docker.target['HostConfig']['RestartPolicy']['Name'] == 'unless-stopped'
    assert manager.docker.calls[0] == ('policy', {'Name': 'no', 'MaximumRetryCount': 0})
    assert manager.docker.target['State']['Running']
    assert not (manager.state_dir / JOB / 'upload.zip').exists()


@pytest.mark.parametrize('identity,fragment', [
    (None, 'no Plex version'),
    ({'version': IDENTITY['version'], 'machine_identifier': 'another-server'}, 'another Plex'),
    ({'version': '1.40.0-test', 'machine_identifier': 'fixture-server'}, 'version differs'),
])
def test_legacy_wrong_server_and_wrong_version_never_stop_plex(manager, tmp_path, identity, fragment):
    job = run_restore(manager, export(database(tmp_path / 'incoming.db', 1), identity))
    assert job['phase'] == 'failed' and fragment in job['error']
    assert manager.docker.calls == []


@pytest.mark.parametrize('change,fragment', [
    ('label', 'labelled'), ('mount', 'does not match'), ('readonly', 'writable'),
    ('paused', 'running normally'), ('stopped', 'running normally'), ('self', 'separate container'),
    ('no-state-volume', 'not covered'), ('nested-mount', 'Nested mounts'),
])
def test_unsafe_target_never_stops_plex(manager, backup, change, fragment):
    docker = manager.docker
    if change == 'label':
        docker.target['Config']['Labels'] = {}
    elif change == 'mount':
        docker.target['Mounts'][0]['Source'] = '/host/another-plex'
    elif change == 'readonly':
        docker.target['Mounts'][0]['RW'] = False
    elif change == 'paused':
        docker.target['State']['Paused'] = True
    elif change == 'stopped':
        docker.target['State']['Running'] = False
    elif change == 'self':
        docker.agent['Id'] = docker.target['Id']
    elif change == 'no-state-volume':
        docker.agent['Mounts'].pop()
    elif change == 'nested-mount':
        docker.target['Mounts'].append({'Destination': '/config/Plug-in Support/Databases/' + DATABASE,
                                        'Source': '/host/file', 'Type': 'bind', 'RW': True})
    job = run_restore(manager, backup)
    assert job['phase'] == 'failed' and fragment in job['error']
    assert docker.calls == []


def test_different_backuparr_target_never_stops_plex(manager, backup):
    job = run_restore(manager, backup, expected='wrong-backuparr-server')
    assert job['phase'] == 'failed' and 'different Plex servers' in job['error']
    assert manager.docker.calls == []


def test_failed_health_check_rolls_back_every_database_companion(manager, backup, monkeypatch):
    originals = {DATABASE: (manager.database_dir / DATABASE).read_bytes()}
    for name in DATABASE_FILES[1:]:
        originals[name] = ('original-' + name).encode()
        (manager.database_dir / name).write_bytes(originals[name])

    def identity(*args, **kwargs):
        if manager.docker.starts == 1:
            raise AgentError('not healthy')
        return IDENTITY

    monkeypatch.setattr(manager, 'plex_identity', identity)
    job = run_restore(manager, backup)
    assert job['phase'] == 'rolled_back' and 'did not become ready' in job['cause']
    for name, content in originals.items():
        assert (manager.database_dir / name).read_bytes() == content
    assert manager.docker.starts == 2
    assert manager.docker.target['HostConfig']['RestartPolicy']['Name'] == 'unless-stopped'


def test_no_diagnostic_write_can_reactivate_a_completed_rollback(manager, backup, monkeypatch):
    write_json = restore_module.write_json
    rollback_saved = False
    later_writes = []

    def no_writes_after_rollback(path, value):
        nonlocal rollback_saved
        if rollback_saved:
            later_writes.append(value['phase'])
            if len(later_writes) == 1:
                raise OSError(errno.EIO, 'simulated failure after durable rollback')
        write_json(path, value)
        rollback_saved |= value['phase'] == 'rolled_back'

    def identity(*args, **kwargs):
        if manager.docker.starts == 1:
            raise AgentError('not healthy')
        return IDENTITY

    with monkeypatch.context() as patch:
        patch.setattr(manager, 'plex_identity', identity)
        patch.setattr(restore_module, 'write_json', no_writes_after_rollback)
        job = run_restore(manager, backup)
    assert job['phase'] == 'rolled_back'
    assert 'did not become ready' in job['cause']
    assert rollback_saved and later_writes == []
    assert not manager.recovery_blocked()
    assert manager.docker.target['State']['Running']
    # Plex is back in use; restarting the agent must preserve newer progress.
    with sqlite3.connect(manager.database_dir / DATABASE) as db:
        db.execute('UPDATE metadata_item_settings SET view_count=9 WHERE account_id=1')
    calls = list(manager.docker.calls)
    manager.recover()
    assert manager.docker.calls == calls
    with sqlite3.connect(manager.database_dir / DATABASE) as db:
        assert db.execute('SELECT view_count FROM metadata_item_settings WHERE account_id=1').fetchone() == (9,)


def test_failed_rollback_commit_still_requires_recovery(manager, backup, monkeypatch):
    write_json = restore_module.write_json

    def fail_rollback_commit(path, value):
        if value['phase'] == 'rolled_back':
            raise OSError(errno.EIO, 'simulated failure before durable rollback')
        write_json(path, value)

    def identity(*args, **kwargs):
        if manager.docker.starts == 1:
            raise AgentError('not healthy')
        return IDENTITY

    with monkeypatch.context() as patch:
        patch.setattr(manager, 'plex_identity', identity)
        patch.setattr(restore_module, 'write_json', fail_rollback_commit)
        job = run_restore(manager, backup)
    assert job['phase'] == 'recovery_failed'
    assert 'did not become ready' in job['cause']
    assert manager.recovery_blocked()
    manager.recover()
    assert manager.read_job(JOB)['phase'] == 'rolled_back'
    assert not manager.recovery_blocked()


@pytest.mark.parametrize('crash_point', ['stopping', 'snapshot', 'replace', 'start'])
def test_crash_recovery_uses_journal_and_does_not_leave_plex_stopped(manager, backup, monkeypatch, crash_point):
    class Crash(BaseException):
        pass

    original = (manager.database_dir / DATABASE).read_bytes()
    with monkeypatch.context() as patch:
        if crash_point == 'stopping':
            original_fn = manager.docker.stop
            def crash(*args):
                original_fn(*args)
                raise Crash()
            patch.setattr(manager.docker, 'stop', crash)
        elif crash_point == 'snapshot':
            original_fn = manager.snapshot
            def crash(*args):
                original_fn(*args)
                raise Crash()
            patch.setattr(manager, 'snapshot', crash)
        elif crash_point == 'replace':
            original_fn = manager.replace
            def crash(*args):
                original_fn(*args)
                raise Crash()
            patch.setattr(manager, 'replace', crash)
        else:
            patch.setattr(manager.docker, 'start', lambda *args: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            run_restore(manager, backup)
    assert manager.read_job(JOB)['phase'] not in ('complete', 'failed', 'rolled_back')
    manager.recover()
    assert manager.read_job(JOB)['phase'] == 'rolled_back'
    assert (manager.database_dir / DATABASE).read_bytes() == original
    assert manager.docker.target['State']['Running']
    assert manager.docker.target['HostConfig']['RestartPolicy']['Name'] == 'unless-stopped'


def test_failed_recovery_blocks_new_restores_and_can_be_retried(manager, backup, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(manager.docker, 'start', lambda *a: (_ for _ in ()).throw(AgentError('Docker down')))
        job = run_restore(manager, backup)
    assert job['phase'] == 'recovery_failed' and manager.recovery_blocked()
    assert (manager.state_dir / JOB / 'rollback' / DATABASE).is_file()
    assert manager.docker.target['HostConfig']['RestartPolicy']['Name'] == 'no'
    manager.recover()
    assert manager.read_job(JOB)['phase'] == 'rolled_back'
    assert not manager.recovery_blocked()


@pytest.mark.parametrize('error_number', [errno.ENOSPC, errno.EIO])
def test_unwritable_journal_blocks_later_restore_until_restart_recovery(
        manager, backup, monkeypatch, tmp_path, error_number):
    from conftest import InlineThread
    original = (manager.database_dir / DATABASE).read_bytes()
    write_json = restore_module.write_json
    broken = False

    def fail_after_replacement(path, value):
        nonlocal broken
        broken |= value['phase'] == 'starting'
        if broken:
            raise OSError(error_number, 'simulated journal failure')
        write_json(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(restore_module, 'write_json', fail_after_replacement)
        job = run_restore(manager, backup)
    assert job['phase'] == 'applying'  # Even recording recovery_failed failed.
    assert not manager.lock.locked()
    assert manager.active_job is None
    assert manager.recovery_blocked()
    assert (manager.state_dir / JOB / 'rollback' / DATABASE).read_bytes() == original

    client = create_app(manager, AGENT_TOKEN).test_client()
    headers = {'Authorization': 'Bearer ' + AGENT_TOKEN, 'X-Plex-Token': 'plex-token',
               'X-Plex-Machine-Identifier': IDENTITY['machine_identifier']}
    assert client.get('/v1/health', headers=headers).status_code == 503
    # Freeing space and manually starting Plex must not allow a newer restore
    # that would be overwritten when this old journal is recovered later.
    manager.docker.start(manager.container)
    second_backup = export(database(tmp_path / 'second.db', 9))
    response = client.put('/v1/restores/' + 'b' * 32, data=second_backup,
                          content_type='application/zip', headers=headers)
    assert response.status_code == 409
    assert manager.read_job('b' * 32) is None

    # Drop process-local state as on a real restart; the unfinished journal
    # alone must still block new work before recovery.
    manager.journal_failed = False
    assert manager.recovery_blocked()
    manager.recover()
    assert manager.read_job(JOB)['phase'] == 'rolled_back'
    assert (manager.database_dir / DATABASE).read_bytes() == original
    assert client.get('/v1/health', headers=headers).status_code == 200
    monkeypatch.setattr(threading, 'Thread', InlineThread)
    assert client.put('/v1/restores/' + 'b' * 32, data=second_backup,
                      content_type='application/zip', headers=headers).status_code == 202
    assert manager.read_job('b' * 32)['phase'] == 'complete'
    manager.recover()
    with sqlite3.connect(manager.database_dir / DATABASE) as db:
        assert db.execute('SELECT view_count FROM metadata_item_settings WHERE account_id=1').fetchone() == (9,)


def test_running_restore_is_healthy_but_abandoned_job_blocks(manager, backup, monkeypatch):
    replace = manager.replace

    def check_active(*args):
        assert manager.active_job == JOB
        assert not manager.recovery_blocked()
        replace(*args)

    monkeypatch.setattr(manager, 'replace', check_active)
    assert run_restore(manager, backup)['phase'] == 'complete'
    manager.save(manager.read_job(JOB), 'applying')
    assert manager.recovery_blocked()


def test_unreadable_journal_fails_closed(manager):
    folder = manager.state_dir / JOB
    folder.mkdir()
    (folder / 'job.json').write_text('{truncated')
    assert manager.recovery_blocked()
    client = create_app(manager, AGENT_TOKEN).test_client()
    headers = {'Authorization': 'Bearer ' + AGENT_TOKEN}
    assert client.get('/v1/health', headers=headers).status_code == 503
    assert client.get('/v1/restores/' + JOB, headers=headers).status_code == 503


def test_initial_journal_failure_releases_lock_and_retains_storage_block(manager, backup, monkeypatch):
    def unwritable(*args):
        raise OSError(errno.ENOSPC, 'simulated full state volume')

    monkeypatch.setattr(restore_module, 'write_json', unwritable)
    client = create_app(manager, AGENT_TOKEN).test_client()
    headers = {'Authorization': 'Bearer ' + AGENT_TOKEN, 'X-Plex-Token': 'plex-token',
               'X-Plex-Machine-Identifier': IDENTITY['machine_identifier']}
    assert client.put('/v1/restores/' + JOB, data=backup, content_type='application/zip',
                      headers=headers).status_code == 503
    assert not manager.lock.locked()
    assert manager.active_job is None
    assert manager.recovery_blocked()
    assert manager.docker.calls == []


def test_restore_refuses_symlink_without_modifying_target(manager, backup, tmp_path):
    path = manager.database_dir / DATABASE
    outside = tmp_path / 'outside.db'
    path.rename(outside)
    path.symlink_to(outside)
    job = run_restore(manager, backup)
    assert job['phase'] == 'failed' and 'symlink' in job['error']
    assert manager.docker.calls == []


@pytest.mark.parametrize('bad', [b'not a zip', b'PK truncated', export(b'SQLite format 3\0not-a-real-database')])
def test_bad_archives_never_stop_plex(manager, bad):
    job = run_restore(manager, bad)
    assert job['phase'] == 'failed' and manager.docker.calls == []


def test_ambiguous_database_is_rejected(manager, tmp_path):
    body = export(database(tmp_path / 'new.db', 1), names=['databaseBackup.db', DATABASE])
    job = run_restore(manager, body)
    assert job['phase'] == 'failed' and 'exactly one' in job['error']


def test_archive_paths_never_choose_extraction_destination(tmp_path):
    contents = database(tmp_path / 'source.db', 1)
    archive = tmp_path / 'export.zip'
    archive.write_bytes(export(contents, names=['../../outside/databaseBackup.db']))
    stage = tmp_path / 'staged'
    stage.mkdir()
    assert stage_database(archive, stage, 1024**3) == IDENTITY
    assert (stage / 'incoming.db').read_bytes() == contents
    assert not (tmp_path.parent / 'outside').exists()


def test_upload_auth_idempotency_and_no_persisted_tokens(manager, backup, monkeypatch):
    from conftest import InlineThread
    monkeypatch.setattr(threading, 'Thread', InlineThread)
    client = create_app(manager, AGENT_TOKEN).test_client()
    headers = {'Authorization': 'Bearer ' + AGENT_TOKEN,
               'X-Plex-Token': 'private-plex-token', 'X-Plex-Machine-Identifier': 'fixture-server'}
    assert client.get('/v1/status').status_code == 401
    assert client.put('/v1/restores/' + JOB, data=backup, content_type='application/zip').status_code == 401
    assert manager.docker.calls == []
    response = client.put('/v1/restores/' + JOB, data=backup, content_type='application/zip', headers=headers)
    assert response.status_code == 202
    status = client.get('/v1/restores/' + JOB, headers=headers)
    assert status.json['phase'] == 'complete'
    calls = list(manager.docker.calls)
    assert client.put('/v1/restores/' + JOB, data=backup, content_type='application/zip', headers=headers).status_code == 200
    assert manager.docker.calls == calls
    on_disk = (manager.state_dir / JOB / 'job.json').read_text()
    assert 'private-plex-token' not in on_disk and AGENT_TOKEN not in on_disk
    assert status.headers['Cache-Control'] == 'no-store'


def test_upload_rejects_concurrent_jobs_wrong_types_and_limit(manager, backup):
    client = create_app(manager, AGENT_TOKEN).test_client()
    headers = {'Authorization': 'Bearer ' + AGENT_TOKEN, 'X-Plex-Token': 'plex-key',
               'X-Plex-Machine-Identifier': 'fixture-server'}
    assert client.put('/v1/restores/' + JOB, data=backup, headers=headers).status_code == 415
    assert client.put('/v1/restores/not-a-job', data=backup, headers=headers).status_code == 400
    manager.lock.acquire()
    try:
        assert client.put('/v1/restores/' + JOB, data=backup, content_type='application/zip', headers=headers).status_code == 409
    finally:
        manager.lock.release()
    manager.max_bytes = 10
    assert client.put('/v1/restores/' + JOB, data=backup, content_type='application/zip', headers=headers).status_code == 413
    assert manager.docker.calls == []


def test_second_agent_cannot_acquire_same_directory_lock(manager):
    with pytest.raises(AgentError, match='Another Plex restore agent'):
        RestoreManager(manager.docker, container='plex', agent_container='agent',
                       database_dir=manager.database_dir, container_database_dir=manager.container_database_dir,
                       state_dir=manager.state_dir, plex_url=manager.plex_url, max_bytes=manager.max_bytes)


def test_live_identity_is_compared_with_processed_preferences_identifier(manager, monkeypatch):
    prefs = manager.database_dir.parent.parent / 'Preferences.xml'
    prefs.write_text('<Preferences MachineIdentifier="native-uuid" ProcessedMachineIdentifier="fixture-server"/>')

    def get(url, **kwargs):
        assert kwargs['allow_redirects'] is False
        result = requests.Response()
        result.status_code = 200
        result._content_consumed = True
        result._content = (b'<MediaContainer version="1.43.3.10896-test" machineIdentifier="fixture-server"/>'
                           if url.endswith('/identity') else b'<MediaContainer size="0"/>')
        return result

    monkeypatch.setattr(requests, 'get', get)
    assert RestoreManager.plex_identity(manager, 'plex-token', check_library=True) == IDENTITY
    prefs.write_text('<Preferences MachineIdentifier="native-uuid" ProcessedMachineIdentifier="different-server"/>')
    with pytest.raises(AgentError, match='different servers'):
        RestoreManager.plex_identity(manager)
