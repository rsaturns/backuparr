from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading

import pytest

from healthcheck import check_health


@contextmanager
def local_server(host, status=200):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/login"
            self.send_response(status)
            if status == 302:
                # The probe must not follow this redirect.
                self.send_header("Location", "http://unreachable.invalid/")
            self.end_headers()

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    try:
        server = Server((host, 0), Handler)
    except OSError:
        if ":" in host:
            pytest.skip("IPv6 loopback is unavailable")
        raise
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("setting,host", [
    (None, "127.0.0.1"), ("", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"),
    ("127.0.0.1", "127.0.0.1"), ("127.0.0.2", "127.0.0.2"),
    ("::", "::1"), ("::1", "::1"),
])
def test_healthcheck_uses_the_configured_listener(monkeypatch, setting, host):
    if setting is None:
        monkeypatch.delenv("WEBUI_HOST", raising=False)
    else:
        monkeypatch.setenv("WEBUI_HOST", setting)
    # A local probe must not go through an inherited outbound proxy.
    monkeypatch.setenv("http_proxy", "http://unreachable.invalid:1234")
    monkeypatch.setenv("HTTP_PROXY", "http://unreachable.invalid:1234")
    with local_server(host) as port:
        monkeypatch.setenv("WEBUI_PORT", str(port))
        assert check_health()


@pytest.mark.parametrize("status,healthy", [(302, True), (401, False), (500, False)])
def test_healthcheck_handles_login_redirects_and_errors(monkeypatch, status, healthy):
    monkeypatch.setenv("WEBUI_HOST", "127.0.0.1")
    with local_server("127.0.0.1", status) as port:
        monkeypatch.setenv("WEBUI_PORT", str(port))
        assert check_health() is healthy


def test_healthcheck_fails_for_the_wrong_bind_address(monkeypatch):
    with local_server("127.0.0.2") as port:
        monkeypatch.setenv("WEBUI_HOST", "127.0.0.1")
        monkeypatch.setenv("WEBUI_PORT", str(port))
        assert not check_health()
