from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import threading

import pytest

import tls
from healthcheck import check_health


@contextmanager
def local_server(host, status=200, https=False):
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
    if https:
        cert = tls.ensure()
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert.cert_path, cert.key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def both(monkeypatch, host, http_status=200, https_status=200):
    with local_server(host, http_status) as http_port, local_server(host, https_status, https=True) as https_port:
        monkeypatch.setenv("WEBUI_PORT", str(http_port))
        monkeypatch.setenv("WEBUI_HTTPS_PORT", str(https_port))
        yield http_port, https_port


@pytest.fixture(autouse=True)
def no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUPARR_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("BACKUPARR_HTTPS_ONLY", raising=False)


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
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://unreachable.invalid:1234")
    with both(monkeypatch, host):
        assert check_health()


@pytest.mark.parametrize("status,healthy", [(302, True), (401, False), (500, False)])
def test_healthcheck_handles_login_redirects_and_errors(monkeypatch, status, healthy):
    monkeypatch.setenv("WEBUI_HOST", "127.0.0.1")
    with both(monkeypatch, "127.0.0.1", http_status=status, https_status=status):
        assert check_health() is healthy


def test_healthcheck_fails_for_the_wrong_bind_address(monkeypatch):
    with both(monkeypatch, "127.0.0.2"):
        monkeypatch.setenv("WEBUI_HOST", "127.0.0.1")
        assert not check_health()


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _only(monkeypatch, https):
    """Just one of the two servers answering."""
    monkeypatch.setenv("WEBUI_HOST", "127.0.0.1")
    return local_server("127.0.0.1", https=https)


def test_https_down_is_unhealthy(monkeypatch):
    with _only(monkeypatch, https=False) as port:
        monkeypatch.setenv("WEBUI_PORT", str(port))
        monkeypatch.setenv("WEBUI_HTTPS_PORT", str(_closed_port()))
        assert not check_health()


def test_http_down_is_unhealthy_unless_https_only(tmp_path, monkeypatch):
    with _only(monkeypatch, https=True) as port:
        monkeypatch.setenv("WEBUI_HTTPS_PORT", str(port))
        monkeypatch.setenv("WEBUI_PORT", str(_closed_port()))
        assert not check_health()
        (tmp_path / "config.json").write_text(json.dumps({"https_only": True}))
        assert check_health()
        monkeypatch.setenv("BACKUPARR_HTTPS_ONLY", "false")
        assert not check_health()
        (tmp_path / "config.json").write_text(json.dumps({"https_only": False}))
        monkeypatch.setenv("BACKUPARR_HTTPS_ONLY", "true")
        assert check_health()
