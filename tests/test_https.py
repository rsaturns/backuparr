"""HTTPS on its own port (#177): the certificate, proxy headers, cookies per
scheme, the HTTPS only switch, Backuparr's own backup, and the server."""
import asyncio
import http.client
import json
import os
import socket
import ssl
import stat
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from conftest import PASSWORD
import tls
from webui.forwarded import ForwardedHeaders

HTTPS = {"base_url": "https://localhost"}


def _load(path):
    with open(path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


# --------------------------------------------------------- certificate ----
def test_first_start_makes_a_private_self_signed_certificate_apple_accepts():
    cert = tls.ensure()
    assert not cert.own and cert.error is None
    for path in (tls.CERT_PATH, tls.KEY_PATH):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    made = _load(tls.CERT_PATH)
    # Apple's rules for any TLS certificate: <= 825 days, SHA-256, a DNS name, serverAuth.
    assert made.not_valid_after_utc - made.not_valid_before_utc <= timedelta(days=825)
    assert isinstance(made.signature_hash_algorithm, hashes.SHA256)
    names = made.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert ExtendedKeyUsageOID.SERVER_AUTH in made.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert made.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert cert.fingerprint == tls.fingerprint(made) and len(cert.fingerprint.split(":")) == 32


def test_the_certificate_is_kept_across_starts():
    first = tls.ensure()
    assert tls.ensure().fingerprint == first.fingerprint


def test_renewal_keeps_the_key_so_the_pin_still_matches(monkeypatch):
    tls.ensure()
    first = _load(tls.CERT_PATH)
    later = datetime.now(timezone.utc) + tls.VALIDITY - timedelta(days=10)
    monkeypatch.setattr(tls, "_now", lambda: later)
    renewed = tls.ensure()
    assert renewed.fingerprint != tls.fingerprint(first)
    assert tls.public_key_pin(_load(tls.CERT_PATH)) == tls.public_key_pin(first)


def test_an_unreadable_certificate_is_made_again():
    tls.ensure()
    with open(tls.CERT_PATH, "w") as f:
        f.write("not a certificate")
    assert tls.ensure().fingerprint == tls.fingerprint(_load(tls.CERT_PATH))


def _own_pair(tmp_path, days=365, key=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    algorithm = None if isinstance(key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)) else hashes.SHA256()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "backuparr.example.com")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + timedelta(days=days))
            .sign(key, algorithm))
    cert_path, key_path = tmp_path / "own.crt", tmp_path / "own.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert, str(cert_path), str(key_path)


def test_your_own_certificate_is_used_as_is(tmp_path, monkeypatch):
    own, cert_path, key_path = _own_pair(tmp_path)
    monkeypatch.setenv("BACKUPARR_TLS_CERT", cert_path)
    monkeypatch.setenv("BACKUPARR_TLS_KEY", key_path)
    cert = tls.ensure()
    assert cert.own and cert.cert_path == cert_path and cert.fingerprint == tls.fingerprint(own)
    assert not os.path.exists(tls.CERT_PATH)


@pytest.mark.parametrize("problem, message", [
    ("only_cert", "set both"),
    ("missing", "couldn't read"),
    ("mismatch", "doesn't belong"),
    ("garbage", "aren't a PEM"),
])
def test_a_certificate_of_yours_that_cant_be_used_falls_back_and_says_why(tmp_path, monkeypatch, problem, message):
    _, cert_path, key_path = _own_pair(tmp_path)
    if problem == "only_cert":
        monkeypatch.setenv("BACKUPARR_TLS_CERT", cert_path)
    else:
        if problem == "missing":
            key_path = str(tmp_path / "nope.key")
        elif problem == "mismatch":
            (tmp_path / "other").mkdir()
            _, _, key_path = _own_pair(tmp_path / "other")
        elif problem == "garbage":
            (tmp_path / "own.crt").write_text("garbage")
        monkeypatch.setenv("BACKUPARR_TLS_CERT", cert_path)
        monkeypatch.setenv("BACKUPARR_TLS_KEY", key_path)
    cert = tls.ensure()
    assert not cert.own and message in cert.error
    assert cert.cert_path == tls.CERT_PATH and os.path.exists(tls.CERT_PATH)


@pytest.mark.parametrize("make_key", [
    lambda: ed25519.Ed25519PrivateKey.generate(),
    lambda: ed448.Ed448PrivateKey.generate(),
    lambda: rsa.generate_private_key(65537, 2048),
], ids=["ed25519", "ed448", "rsa"])
def test_your_own_certificate_can_have_any_key_type(tmp_path, monkeypatch, make_key):
    own, cert_path, key_path = _own_pair(tmp_path, key=make_key())
    monkeypatch.setenv("BACKUPARR_TLS_CERT", cert_path)
    monkeypatch.setenv("BACKUPARR_TLS_KEY", key_path)
    cert = tls.ensure()
    assert cert.own and cert.error is None and cert.fingerprint == tls.fingerprint(own)


def test_a_certificate_of_yours_that_cant_be_checked_still_falls_back(tmp_path, monkeypatch):
    _, cert_path, key_path = _own_pair(tmp_path)
    monkeypatch.setenv("BACKUPARR_TLS_CERT", cert_path)
    monkeypatch.setenv("BACKUPARR_TLS_KEY", key_path)

    def odd(*_):
        raise RuntimeError("something new")
    monkeypatch.setattr(tls, "_same_key", odd)
    monkeypatch.setattr(tls, "_own_signed", lambda: tls._make_certificate(tls._load_key()))
    cert = tls.ensure()
    assert not cert.own and "can't be checked" in cert.error


def test_a_certificate_of_yours_openssl_refuses_gives_way_to_backuparrs_own(tmp_path, monkeypatch):
    from webui import serve
    _, cert_path, key_path = _own_pair(tmp_path, key=rsa.generate_private_key(65537, 1024))
    monkeypatch.setenv("BACKUPARR_TLS_CERT", cert_path)
    monkeypatch.setenv("BACKUPARR_TLS_KEY", key_path)
    assert tls.ensure().own  # it reads fine; OpenSSL won't serve a 1024-bit key
    context, cert = serve._ssl_context(tls.ensure())
    assert not cert.own and cert.cert_path == tls.CERT_PATH
    assert "HTTPS can't use it" in cert.error and "KEY_TOO_SMALL" in cert.error
    assert tls.ensure().error == cert.error  # the card keeps saying why
    _, cert_path, key_path = _own_pair(tmp_path)  # replaced with a usable pair
    assert tls.ensure().own


def test_restore_files_needs_a_matching_pair_and_tells_the_server():
    tls.ensure()
    pair = tls.backup_files()
    seen = []
    tls.on_change(seen.append)
    os.remove(tls.CERT_PATH)
    os.remove(tls.KEY_PATH)
    tls.ensure()  # a new install's own certificate
    assert tls.restore_files(pair["tls-key.pem"], pair["tls-cert.pem"])
    assert tls.backup_files() == pair
    assert seen and seen[-1].fingerprint == tls.fingerprint(x509.load_pem_x509_certificate(pair["tls-cert.pem"]))
    other_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    assert not tls.restore_files(other_key, pair["tls-cert.pem"])
    assert tls.backup_files() == pair


# ------------------------------------------------------ proxy headers ----
def _environ(remote, **headers):
    environ = {"REMOTE_ADDR": remote, "wsgi.url_scheme": "http", "HTTP_HOST": "backuparr:8990"}
    environ.update({f"HTTP_{k.upper()}": v for k, v in headers.items()})
    return environ


def _seen(middleware, environ):
    middleware(environ, None)
    return environ["wsgi.url_scheme"], environ["HTTP_HOST"], environ["REMOTE_ADDR"]


FORWARDED = {"x_forwarded_proto": "https", "x_forwarded_host": "backups.example.com", "x_forwarded_for": "1.2.3.4, 10.0.0.9"}


def test_plain_http_uses_no_proxy_headers():
    middleware = ForwardedHeaders(lambda e, s: None, enabled=False, trusted_proxy="")
    assert _seen(middleware, _environ("10.0.0.5", **FORWARDED)) == ("http", "backuparr:8990", "10.0.0.5")


def test_https_proxy_alone_never_trusts_a_forwarded_client_address():
    # Anyone reaching the port directly could forge X-Forwarded-For to dodge the login lockout.
    middleware = ForwardedHeaders(lambda e, s: None, enabled=True, trusted_proxy="")
    assert _seen(middleware, _environ("10.0.0.5", **FORWARDED)) == ("https", "backups.example.com", "10.0.0.5")


def test_a_named_proxy_is_the_only_source_of_forwarded_headers():
    middleware = ForwardedHeaders(lambda e, s: None, enabled=True, trusted_proxy="172.18.0.5")
    assert _seen(middleware, _environ("172.18.0.5", **FORWARDED)) == ("https", "backups.example.com", "10.0.0.9")
    assert _seen(middleware, _environ("10.0.0.7", **FORWARDED)) == ("http", "backuparr:8990", "10.0.0.7")


def test_a_bogus_forwarded_proto_is_ignored():
    middleware = ForwardedHeaders(lambda e, s: None, enabled=True, trusted_proxy="")
    assert _seen(middleware, _environ("10.0.0.5", x_forwarded_proto="javascript"))[0] == "http"


# ------------------------------------------------------------- cookies ----
def test_session_cookie_is_plain_over_http_and_secure_host_only_over_https(isolated_webui):
    client = isolated_webui.app.test_client()
    assert client.post("/api/setup", json={"username": "admin", "password": PASSWORD}).status_code == 200
    plain = client.get_cookie("session")
    assert plain is not None and not plain.secure
    assert client.post("/api/login", json={"username": "admin", "password": PASSWORD}, **HTTPS).status_code == 200
    secure = client.get_cookie("__Host-session", domain="localhost")
    assert secure is not None and secure.secure
    # Each scheme keeps its own login: HTTP still works after signing in over HTTPS.
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/config", **HTTPS).status_code == 200


def test_behind_a_tls_proxy_the_session_cookie_keeps_its_name_and_is_secure(isolated_webui, monkeypatch):
    monkeypatch.setattr(isolated_webui.cookies, "BEHIND_HTTPS_PROXY", True)
    client = isolated_webui.app.test_client()
    client.post("/api/setup", json={"username": "admin", "password": PASSWORD}, **HTTPS)
    cookie = client.get_cookie("session", domain="localhost")
    assert cookie is not None and cookie.secure


# -------------------------------------------------------------- routes ----
def _on(listener):
    return {"environ_base": {"backuparr.listener": listener}}


def test_https_status_shows_the_certificate(authed_client):
    data = authed_client.get("/api/https").get_json()
    assert data["port"] == 8991 and data["http_port"] == 8990
    assert data["fingerprint"] == tls.ensure().fingerprint
    assert data["own_certificate"] is False and data["certificate_error"] is None
    assert data["https_only"] is False and data["https_only_locked"] is False and data["on_https"] is False
    assert authed_client.get("/api/https", **_on("https")).get_json()["on_https"] is True


def test_https_only_can_only_be_turned_on_from_the_https_port(authed_client, isolated_webui, monkeypatch):
    calls = []
    monkeypatch.setattr(isolated_webui.https.serve, "set_http_open", calls.append)
    response = authed_client.post("/api/https/only", json={"enabled": True}, **_on("http"))
    assert response.status_code == 409 and "port 8991" in response.get_json()["error"]
    assert authed_client.get("/api/config").get_json()["https_only"] is False

    https = {"base_url": "https://localhost", **_on("https")}
    authed_client.post("/api/login", json={"username": "admin", "password": PASSWORD}, **https)
    # HTTPS through a proxy on the HTTP port doesn't count: only the port it came in on.
    response = authed_client.post("/api/https/only", json={"enabled": True}, base_url="https://localhost", **_on("http"))
    assert response.status_code == 409 and calls == []
    assert authed_client.post("/api/https/only", json={"enabled": True}, **https).status_code == 200
    assert calls == [False]
    assert authed_client.get("/api/https", **https).get_json()["https_only"] is True
    # Turning it off works from anywhere that can still reach Backuparr.
    assert authed_client.post("/api/https/only", json={"enabled": False}, **https).status_code == 200
    assert calls == [False, True]


def test_https_only_set_by_the_environment_locks_the_switch(authed_client, isolated_webui, monkeypatch):
    monkeypatch.setenv("BACKUPARR_HTTPS_ONLY", "true")
    data = authed_client.get("/api/https").get_json()
    assert data["https_only"] is True and data["https_only_locked"] is True
    response = authed_client.post("/api/https/only", json={"enabled": False}, **_on("https"))
    assert response.status_code == 409 and "BACKUPARR_HTTPS_ONLY" in response.get_json()["error"]


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}])
def test_https_only_needs_a_boolean(authed_client, body):
    assert authed_client.post("/api/https/only", json=body, **_on("https")).status_code == 400


def test_save_never_changes_https_only(authed_client):
    authed_client.post("/api/config", json={"https_only": True})
    assert authed_client.get("/api/config").get_json()["https_only"] is False


@pytest.mark.parametrize("value, expected", [
    ("true", True), ("1", True), ("YES", True), ("false", False), ("0", False), ("off", False), ("", None), ("maybe", None),
])
def test_https_only_env_values(monkeypatch, value, expected):
    from webui import serve
    monkeypatch.setenv("BACKUPARR_HTTPS_ONLY", value)
    assert serve.https_only_env() is expected


def test_a_broken_config_doesnt_stop_the_server_starting(monkeypatch, caplog):
    import config_store
    from webui import serve

    def broken():
        raise config_store.ConfigError("config.json isn't valid JSON - fix it, or delete the file")
    monkeypatch.setattr(serve, "load_config", broken)
    monkeypatch.delenv("BACKUPARR_HTTPS_ONLY", raising=False)
    assert serve._https_only_at_start() is False  # HTTP opens, so the UI can say what's wrong
    assert "isn't valid JSON" in caplog.text
    monkeypatch.setenv("BACKUPARR_HTTPS_ONLY", "true")
    assert serve._https_only_at_start() is True  # the environment needs no config.json


# ------------------------------------------------ Backuparr's own backup ----
def test_own_backup_carries_the_certificate_and_restore_keeps_https_only(authed_client, monkeypatch):
    import config_store
    import self_backup
    import self_restore
    import secrets_crypto
    cfg = config_store.load_config()
    cfg["https_only"] = True
    config_store.save_config(cfg)
    tls.ensure()
    pair = tls.backup_files()
    monkeypatch.setattr(self_backup.rclone_util, "config_dump", lambda: {})
    members = self_backup.open_archive(self_backup.build())
    assert members["tls-key.pem"] == pair["tls-key.pem"] and members["tls-cert.pem"] == pair["tls-cert.pem"]

    # A new install, HTTP still open there, with a certificate of its own.
    os.remove(tls.CERT_PATH)
    os.remove(tls.KEY_PATH)
    tls.ensure()
    cfg = config_store.load_config()
    cfg["https_only"] = False
    config_store.save_config(cfg)
    monkeypatch.setattr(self_restore.rclone_util, "config_dump", lambda: {})
    with open(secrets_crypto.KEY_PATH) as f:
        self_restore.apply(members, f.read())
    assert tls.backup_files() == pair
    with open(config_store.CONFIG_PATH) as f:
        assert json.load(f)["https_only"] is False


# -------------------------------------------------------------- server ----
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port, tls_on, path="/login"):
    context = ssl._create_unverified_context()
    connection = (http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=context) if tls_on
                  else http.client.HTTPConnection("127.0.0.1", port, timeout=5))
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.fixture
def running_server(monkeypatch):
    """webui.serve's Server on two free ports, around a tiny WSGI app that
    reports which port a request came in on."""
    from webui import serve
    http_port, https_port = _free_port(), _free_port()
    monkeypatch.setenv("WEBUI_HOST", "127.0.0.1")
    monkeypatch.setenv("WEBUI_PORT", str(http_port))
    monkeypatch.setenv("WEBUI_HTTPS_PORT", str(https_port))
    monkeypatch.setattr(serve, "https_only", lambda cfg=None: False)

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [f"{environ['backuparr.listener']} {environ['wsgi.url_scheme']}".encode()]

    server = serve.Server(app)
    thread = threading.Thread(target=lambda: asyncio.run(server.main()), daemon=True)
    thread.start()
    for _ in range(100):
        try:
            _get(https_port, True)
            _get(http_port, False)
            break
        except OSError:
            time.sleep(0.05)
    yield server, http_port, https_port
    server.loop.call_soon_threadsafe(server.stopping.set)
    thread.join(10)


def test_server_answers_on_both_ports_and_tags_them(running_server):
    _, http_port, https_port = running_server
    assert _get(https_port, True) == (200, b"https https")
    assert _get(http_port, False) == (200, b"http http")


def test_idle_connections_dont_hold_up_https(running_server):
    _, _, https_port = running_server
    idle = [socket.create_connection(("127.0.0.1", https_port)) for _ in range(5)]
    started = time.monotonic()
    assert _get(https_port, True)[0] == 200
    assert time.monotonic() - started < 2
    for sock in idle:
        sock.close()


def test_http_closes_and_opens_again_without_a_restart(running_server):
    server, http_port, https_port = running_server
    server.set_http_open(False)
    for _ in range(100):
        try:
            _get(http_port, False)
            time.sleep(0.05)
        except OSError:
            break
    with pytest.raises(OSError):
        _get(http_port, False)
    assert _get(https_port, True)[0] == 200
    server.set_http_open(True)
    for _ in range(100):
        try:
            assert _get(http_port, False)[0] == 200
            break
        except OSError:
            time.sleep(0.05)
    assert server.http_open()


def _wait_for_http(port, open_, tries=100):
    for _ in range(tries):
        try:
            _get(port, False)
            if open_:
                return True
        except OSError:
            if not open_:
                return True
        time.sleep(0.05)
    return False


def test_http_turned_back_on_while_it_is_still_closing_opens_again(running_server):
    server, http_port, _ = running_server
    # Keep a request in flight so the old server takes a while to close.
    busy = socket.create_connection(("127.0.0.1", http_port))
    busy.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
    server.set_http_open(False)
    server.set_http_open(True)
    busy.close()
    time.sleep(0.5)
    assert _wait_for_http(http_port, True) and server.http_open()


def test_http_turned_off_again_while_it_is_closing_stays_closed(running_server):
    server, http_port, _ = running_server
    for open_ in (False, True, False):
        server.set_http_open(open_)
    time.sleep(0.5)
    assert _wait_for_http(http_port, False)
    time.sleep(0.5)
    assert _wait_for_http(http_port, False, tries=1) and not server.http_open()


def _taken_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


@pytest.mark.parametrize("https_only, http_taken, keeps_running", [
    (False, False, True),  # HTTP is still a way in
    (True, False, False),  # HTTPS only: no way in at all
    (False, True, False),  # neither port opens
])
def test_https_that_cant_start_leaves_http_serving_when_it_can(monkeypatch, https_only, http_taken, keeps_running):
    from webui import serve
    taken = _taken_port()
    other = _taken_port() if http_taken else None
    http_port = other.getsockname()[1] if other else _free_port()
    monkeypatch.setenv("WEBUI_HOST", "127.0.0.1")
    monkeypatch.setenv("WEBUI_PORT", str(http_port))
    monkeypatch.setenv("WEBUI_HTTPS_PORT", str(taken.getsockname()[1]))
    monkeypatch.setattr(serve, "https_only", lambda cfg=None: https_only)

    def app(environ, start_response):
        start_response("200 OK", [])
        return [environ["backuparr.listener"].encode()]

    server = serve.Server(app)
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("ok", asyncio.run(server.main())), daemon=True)
    thread.start()
    try:
        if keeps_running:
            assert _wait_for_http(http_port, True) and _get(http_port, False) == (200, b"http")
            thread.join(1)
            assert thread.is_alive()
            server.loop.call_soon_threadsafe(server.stopping.set)
        thread.join(10)
        assert not thread.is_alive() and result["ok"] is keeps_running
    finally:
        taken.close()
        if other:
            other.close()


def _served_fingerprint(port):
    pem = ssl.get_server_certificate(("127.0.0.1", port))
    return tls.fingerprint(x509.load_pem_x509_certificate(pem.encode()))


def test_a_new_certificate_is_served_without_a_restart(running_server):
    server, _, https_port = running_server
    seen = lambda: _served_fingerprint(https_port)  # noqa: E731
    before = seen()
    os.remove(tls.CERT_PATH)
    tls.on_change(server._use_certificate)
    after = tls.reload().fingerprint
    for _ in range(50):
        if seen() == after:
            break
        time.sleep(0.05)
    assert after != before and seen() == after
