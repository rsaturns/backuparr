"""Pinned self-signed certificates (#153), against a real local HTTPS server."""
import datetime
import http.server
import ssl
import threading

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from apps import cert_pin
from apps.cert_pin import CertificateUntrusted


def self_signed(tmp_path, name):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path, cert.public_bytes(serialization.Encoding.DER)


@pytest.fixture
def server(tmp_path):
    """An HTTPS server whose certificate can be swapped; counts requests."""
    state = {"requests": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def use(name):
        cert, key, der = self_signed(tmp_path, name)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        state["context"] = context
        return cert_pin.describe(der)

    original_get_request = httpd.get_request

    def get_request():
        sock, addr = original_get_request()
        return state["context"].wrap_socket(sock, server_side=True), addr

    httpd.get_request = get_request
    state["use"] = use
    state["first"] = use("firewall.lab")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"https://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()


def session(trusted=None):
    return cert_pin.pin(requests.Session(), "opnsense", trusted)


def trusted(details):
    return {k: details[k] for k in ("sha256", "subject", "not_after")}


def test_an_untrusted_certificate_is_refused_with_its_details_and_nothing_sent(server):
    with pytest.raises(CertificateUntrusted) as raised:
        session().get(f"{server['url']}/api/core/backup/providers", auth=("key", "secret"), timeout=5)
    error = raised.value
    assert error.changed is False and error.trusted is None
    assert error.certificate["sha256"] == server["first"]["sha256"]
    assert error.certificate["subject"] == "firewall.lab" and error.certificate["self_signed"] is True
    assert "isn't trusted. Check it and trust it in Settings" in str(error)
    assert server["requests"] == []


def test_the_trusted_certificate_is_accepted_without_a_matching_host_name(server):
    response = session(trusted(server["first"])).get(f"{server['url']}/x", auth=("key", "secret"), timeout=5)
    assert response.text == "ok" and server["requests"] == [("/x", "Basic a2V5OnNlY3JldA==")]


def test_a_changed_certificate_stops_before_anything_is_sent(server):
    pinned = trusted(server["first"])
    new = server["use"]("firewall.lab")  # a fresh self-signed certificate, same name
    with pytest.raises(CertificateUntrusted) as raised:
        session(pinned).get(f"{server['url']}/x", auth=("key", "secret"), timeout=5)
    error = raised.value
    assert error.changed is True and error.trusted == pinned
    assert error.certificate["sha256"] == new["sha256"] != pinned["sha256"]
    assert str(error) == "opnsense: the firewall's certificate changed. Check it and trust the new one to resume backups."
    assert server["requests"] == []
    # Trusting the new one resumes.
    assert session(trusted(new)).get(f"{server['url']}/y", timeout=5).text == "ok"


def test_plain_http_is_untouched(server):
    adapter = session().get_adapter("http://example.invalid/")
    assert not isinstance(adapter, cert_pin.PinnedAdapter)


@pytest.mark.parametrize("value, ok", [
    (None, True), ("", True), ({}, True),
    ({"sha256": "a" * 64, "subject": "fw", "not_after": "2027-01-01T00:00:00+00:00"}, True),
    ({"sha256": "A" * 64, "subject": "fw", "not_after": "x"}, False),
    ({"sha256": "a" * 63, "subject": "fw", "not_after": "x"}, False),
    ({"sha256": "a" * 64, "subject": "fw"}, False),
    ({"sha256": "a" * 64, "subject": "fw", "not_after": "x", "extra": 1}, False),
    ("a" * 64, False),
])
def test_a_stored_trusted_cert_must_be_well_formed(value, ok):
    assert cert_pin.valid_trusted_cert(value) is ok


# A protocol requests doesn't speak (Home Assistant's WebSocket, #155) gets a
# socket checked by the same rules before anything is sent on it.
def test_a_socket_to_an_untrusted_certificate_is_refused_before_anything_is_sent(server):
    with pytest.raises(CertificateUntrusted) as raised:
        cert_pin.connect_socket(server["url"], "homeassistant", None, timeout=5)
    assert raised.value.certificate["sha256"] == server["first"]["sha256"] and not raised.value.changed
    assert str(raised.value) == ("homeassistant: Home Assistant's certificate isn't trusted. Check it and trust it "
                                 "in Settings to back it up.")
    assert server["requests"] == []


def test_a_socket_to_the_trusted_certificate_carries_the_request(server):
    sock = cert_pin.connect_socket(server["url"], "homeassistant", trusted(server["first"]), timeout=5)
    try:
        sock.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        answer = b""
        while chunk := sock.recv(4096):
            answer += chunk
    finally:
        sock.close()
    assert answer.startswith(b"HTTP/1.0 200") and server["requests"] == [("/ws", None)]


def test_a_socket_to_a_changed_certificate_is_refused(server):
    pinned = trusted(server["first"])
    new = server["use"]("homeassistant.lab")
    with pytest.raises(CertificateUntrusted) as raised:
        cert_pin.connect_socket(server["url"], "homeassistant", pinned, timeout=5)
    assert raised.value.changed and raised.value.certificate["sha256"] == new["sha256"]
    assert server["requests"] == []


def test_a_plain_socket_for_http(server):
    sock = cert_pin.connect_socket(server["url"].replace("https://", "http://"), "homeassistant", None, timeout=5)
    assert not hasattr(sock, "getpeercert")
    sock.close()
