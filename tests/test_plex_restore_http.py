"""Exercise the production HTTP stack before it can buffer an upload."""
import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import time

import pytest

from plex_restore_agent.__main__ import create_server
from plex_restore_agent.__main__ import HeaderLimitedRequest
from plex_restore_agent.server import create_app
from test_plex_restore_agent import AGENT_TOKEN, DATABASE, JOB, backup, manager


@pytest.fixture
def http_server(manager):
    server = create_server(create_app(manager, AGENT_TOKEN), '127.0.0.1', 0)
    server.prepare()
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def http_agent(http_server):
    return http_server.socket.getsockname()


def partial_put(address, headers, body=b''):
    """Require a final response without sending the rest of the declared body."""
    with socket.create_connection(address, timeout=3) as connection:
        request = ('PUT /v1/restores/' + JOB + ' HTTP/1.1\r\nHost: localhost\r\n'
                   'Content-Type: application/zip\r\n' + headers + '\r\n').encode()
        connection.sendall(request + body)
        response = http.client.HTTPResponse(connection)
        response.begin()  # Also consumes a possible interim 100 Continue.
        result = response.status, response.read()
        assert response.will_close
        return result


@pytest.mark.parametrize('authorization', ['', 'Authorization: Bearer wrong-token\r\n'])
@pytest.mark.parametrize('framing', ['Content-Length: 4194304\r\n', 'Transfer-Encoding: chunked\r\n'])
@pytest.mark.parametrize('expect', ['', 'Expect: 100-continue\r\n'])
def test_unauthenticated_headers_are_rejected_without_waiting_for_body(
        http_agent, manager, authorization, framing, expect):
    # The client asks for keep-alive implicitly and never sends the body.
    status, body = partial_put(http_agent, authorization + framing + expect)
    assert status == 401
    assert json.loads(body)['error'] == 'Agent authentication failed'
    assert list(manager.state_dir.iterdir()) == [manager.state_dir / '.backuparr-restore.lock']
    assert manager.docker.calls == []


def test_unauthorized_partial_body_is_not_spooled(http_agent, manager, monkeypatch):
    def no_spooling(*args, **kwargs):
        pytest.fail('The HTTP server must not spool unauthenticated request bodies')

    monkeypatch.setattr(tempfile, 'TemporaryFile', no_spooling)
    monkeypatch.setattr(tempfile, 'SpooledTemporaryFile', no_spooling)
    status, _ = partial_put(http_agent, 'Content-Length: 4194304\r\n', b'x' * 8192)
    assert status == 401
    assert manager.read_job(JOB) is None


@pytest.mark.parametrize('reason,expected', [('limit', 413), ('chunked', 411),
                                           ('concurrent', 409), ('duplicate', 200)])
def test_authenticated_admission_checks_do_not_drain_rejected_uploads(
        http_agent, manager, reason, expected):
    headers = ('Authorization: Bearer ' + AGENT_TOKEN + '\r\n'
               'X-Plex-Token: plex-token\r\nX-Plex-Machine-Identifier: fixture-server\r\n')
    if reason == 'chunked':
        headers += 'Transfer-Encoding: chunked\r\n'
    else:
        size = manager.max_bytes + 1 if reason == 'limit' else 4194304
        headers += f'Content-Length: {size}\r\n'
    if reason == 'concurrent':
        manager.lock.acquire()
    elif reason == 'duplicate':
        (manager.state_dir / JOB).mkdir()
        manager.save({'id': JOB, 'phase': 'complete'})
    try:
        assert partial_put(http_agent, headers)[0] == expected
    finally:
        if reason == 'concurrent':
            manager.lock.release()
    assert manager.docker.calls == []


def test_authenticated_upload_restores_and_polling_survives_connection_close(
        http_agent, manager, backup):
    headers = {'Authorization': 'Bearer ' + AGENT_TOKEN, 'Content-Type': 'application/zip',
               'X-Plex-Token': 'plex-token', 'X-Plex-Machine-Identifier': 'fixture-server'}
    connection = http.client.HTTPConnection(*http_agent, timeout=3)
    try:
        connection.request('PUT', '/v1/restores/' + JOB, body=backup, headers=headers)
        response = connection.getresponse()
        assert response.status == 202
        assert json.loads(response.read())['phase'] == 'queued'
        deadline = time.monotonic() + 5
        while True:
            connection.request('GET', '/v1/restores/' + JOB, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            phase = json.loads(response.read())['phase']
            if phase == 'complete':
                break
            assert phase not in ('failed', 'rolled_back', 'recovery_failed')
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        connection.close()
    with sqlite3.connect(manager.database_dir / DATABASE) as db:
        assert db.execute('SELECT view_count FROM metadata_item_settings WHERE account_id=1').fetchone() == (5,)


def test_interrupted_authenticated_upload_never_stops_plex(http_agent, manager):
    with socket.create_connection(http_agent, timeout=3) as connection:
        connection.sendall((
            'PUT /v1/restores/' + JOB + ' HTTP/1.1\r\nHost: localhost\r\n'
            'Authorization: Bearer ' + AGENT_TOKEN + '\r\n'
            'X-Plex-Token: plex-token\r\nX-Plex-Machine-Identifier: fixture-server\r\n'
            'Content-Type: application/zip\r\nContent-Length: 4194304\r\n\r\npartial'
        ).encode())
        connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection)
        response.begin()
        body = response.read()
        assert response.status == 503
        assert json.loads(body)['error'] == 'Archive upload was interrupted'
    assert manager.read_job(JOB)['phase'] == 'failed'
    assert not (manager.state_dir / JOB / 'upload.zip').exists()
    assert not manager.lock.locked()
    assert not manager.recovery_blocked()
    assert manager.docker.calls == []


@pytest.mark.parametrize('drip', [False, True])
def test_four_incomplete_headers_cannot_hold_all_workers(http_server, http_agent, monkeypatch, drip):
    http_server.header_timeout = 0.5
    entered = 0
    entered_lock = threading.Lock()
    all_entered = threading.Event()
    stop_dripping = threading.Event()
    parse_request = HeaderLimitedRequest.parse_request

    def count_headers(request):
        nonlocal entered
        with entered_lock:
            entered += 1
            if entered == 4:
                all_entered.set()
        return parse_request(request)

    monkeypatch.setattr(HeaderLimitedRequest, 'parse_request', count_headers)
    connections = []

    def drip_headers():
        while not stop_dripping.wait(0.03):
            for connection in connections:
                try:
                    connection.sendall(b'x')
                except OSError:
                    pass

    thread = threading.Thread(target=drip_headers, daemon=True)
    try:
        for _ in range(4):
            connection = socket.create_connection(http_agent, timeout=2)
            connections.append(connection)
            connection.sendall(b'GET /v1/health HTTP/1.1\r\nHost: localhost\r\nX-Slow: ')
        assert all_entered.wait(2)
        if drip:
            thread.start()
        # This request queues behind all four occupied workers. A byte drip
        # must not extend the header deadline like an inactivity timeout would.
        client = http.client.HTTPConnection(*http_agent, timeout=2)
        try:
            client.request('GET', '/v1/health', headers={'Authorization': 'Bearer ' + AGENT_TOKEN})
            response = client.getresponse()
            assert response.status == 200
            response.read()
        finally:
            client.close()
    finally:
        stop_dripping.set()
        if drip:
            thread.join(2)
        for connection in connections:
            connection.close()


@pytest.mark.parametrize('partial', ['request-line', 'authenticated-headers'])
def test_header_deadline_closes_incomplete_requests_without_admitting_upload(
        http_server, http_agent, manager, partial):
    http_server.header_timeout = 0.3
    with socket.create_connection(http_agent, timeout=2) as connection:
        data = 'PUT /v1/restores/' + JOB + ' HTTP/1.'
        if partial == 'authenticated-headers':
            data += ('1\r\nHost: localhost\r\nAuthorization: Bearer ' + AGENT_TOKEN + '\r\n'
                     'Content-Type: application/zip\r\nContent-Length: 100\r\n'
                     'X-Plex-Token: plex-token\r\nX-Plex-Machine-Identifier: fixture-server\r\n')
        connection.sendall(data.encode())
        # No terminating CRLF and no upload body. Cheroot rejects the truncated
        # headers once the deadline interrupts the read, then closes the socket.
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 400
        response.read()
        assert connection.recv(1) == b''
    assert manager.read_job(JOB) is None
    assert manager.docker.calls == []


def test_accepted_upload_can_pause_longer_than_header_deadline(http_server, http_agent, manager, backup):
    http_server.header_timeout = http_server.timeout = 0.3
    http_server.upload_timeout = 3
    with socket.create_connection(http_agent, timeout=3) as connection:
        headers = ('PUT /v1/restores/' + JOB + ' HTTP/1.1\r\nHost: localhost\r\n'
                   'Authorization: Bearer ' + AGENT_TOKEN + '\r\n'
                   'X-Plex-Token: plex-token\r\nX-Plex-Machine-Identifier: fixture-server\r\n'
                   'Content-Type: application/zip\r\n' + f'Content-Length: {len(backup)}\r\n\r\n')
        connection.sendall(headers.encode() + backup[:32])
        deadline = time.monotonic() + 2
        while manager.read_job(JOB) is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.6)
        connection.sendall(backup[32:])
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 202
        response.read()
    deadline = time.monotonic() + 3
    while manager.read_job(JOB)['phase'] != 'complete':
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert not manager.recovery_blocked()
