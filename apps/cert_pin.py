"""Trusting an app's self-signed HTTPS certificate by its fingerprint (#153).

Firewalls (and other appliances) usually serve their web UI with a
self-signed certificate, which normal verification refuses. Instead of an
option that skips the check - and would send the admin login to whatever
answers - the card pins the certificate the user trusted:

- A certificate from a trusted CA is checked as usual; nothing is pinned, so
  a renewed Let's Encrypt certificate keeps working.
- One that normal verification refuses raises CertificateUntrusted with the
  certificate's details (SHA-256 fingerprint, name, expiry). Test connection
  shows them; Trust saves {"sha256", "subject", "not_after"} as the card's
  trusted_cert.
- From then on, that certificate is accepted by its fingerprint, which
  urllib3 checks right after the TLS handshake, before a byte of the request
  is sent. A different one (a new self-signed certificate, a rotated one, or
  something else answering) raises CertificateUntrusted with changed=True:
  nothing reaches a host with an unexpected certificate.

Only the leaf certificate's fingerprint is compared, so the host name in
the URL doesn't have to match the certificate's (appliances rarely do).
"""
import hashlib
import re
import socket
import ssl
import warnings
from urllib.parse import urlsplit

import certifi
import requests
from cryptography import x509
from cryptography.x509.oid import NameOID
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning

from apps.base import AppError

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
CHANGED_TEXT = "{whose} certificate changed. Check it and trust the new one to resume backups."
UNTRUSTED_TEXT = "{whose} certificate isn't trusted. Check it and trust it in Settings to back it up."
# Whose certificate the messages name, by app type; the rest are firewalls.
_WHOSE = {"homeassistant": "Home Assistant's", "unifi": "UniFi's", "unraid": "Unraid's", "bitwarden": "Bitwarden's",
          "synology": "DSM's", "qnap": "QNAP's"}
_FIREWALLS = "the firewall's"


class CertificateUntrusted(AppError):
    """The app's certificate failed normal verification and isn't the one the
    card trusts. certificate: the presented one's details (or None if it
    couldn't be read); trusted: the card's trusted_cert, if any."""

    def __init__(self, app, certificate, trusted=None):
        self.certificate = certificate
        self.trusted = trusted
        self.changed = bool(trusted)
        text = CHANGED_TEXT if self.changed else UNTRUSTED_TEXT
        super().__init__(f"{app}: {text.format(whose=_WHOSE.get(app, _FIREWALLS))}")


def valid_trusted_cert(value):
    """A card's trusted_cert as stored: {"sha256": 64 hex, "subject": str,
    "not_after": str}, or None/empty for none."""
    if value in (None, "", {}):
        return True
    return (isinstance(value, dict) and set(value) == {"sha256", "subject", "not_after"}
            and isinstance(value["sha256"], str) and bool(_FINGERPRINT.fullmatch(value["sha256"]))
            and all(isinstance(value[k], str) and len(value[k]) <= 500 for k in ("subject", "not_after")))


def describe(der):
    """The details Test connection shows for a certificate (DER bytes)."""
    cert = x509.load_der_x509_certificate(der)

    def name(n):
        cn = n.get_attributes_for_oid(NameOID.COMMON_NAME)
        return cn[0].value if cn else n.rfc4514_string()

    return {
        "sha256": hashlib.sha256(der).hexdigest(),
        "subject": str(name(cert.subject))[:500],
        "issuer": str(name(cert.issuer))[:500],
        "not_after": cert.not_valid_after_utc.isoformat(timespec="seconds"),
        "self_signed": cert.subject == cert.issuer,
    }


def fetch_certificate(url, timeout=10):
    """The certificate the server at an https URL presents, read without
    verifying it and without sending anything but the TLS handshake."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        server_name = None if _is_ip(host) else host
        with context.wrap_socket(sock, server_hostname=server_name) as tls:
            return tls.getpeercert(binary_form=True)


def _same_certificate(tls, trusted):
    return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() == trusted["sha256"]


def connect_socket(url, app, trusted=None, timeout=10):
    """A connected socket to the http(s) URL's host, for a protocol requests
    doesn't speak (Home Assistant's WebSocket API). For https it's TLS that a
    CA vouches for, or the trusted certificate by its fingerprint - the same
    rules as PinnedAdapter, checked before a byte of the protocol is sent.
    Raises CertificateUntrusted."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme != "https":
        return socket.create_connection((host, port), timeout=timeout)
    trusted = trusted if trusted and valid_trusted_cert(trusted) else None
    if trusted:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls = context.wrap_socket(socket.create_connection((host, port), timeout=timeout),
                                  server_hostname=None if _is_ip(host) else host)
        if _same_certificate(tls, trusted):
            return tls
        tls.close()  # Another certificate: fine if a CA vouches for it, else refused.
    context = ssl.create_default_context(cafile=certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        return context.wrap_socket(raw, server_hostname=host)
    except ssl.SSLCertVerificationError:
        raw.close()
        try:
            presented = describe(fetch_certificate(url, timeout))
        except (OSError, ValueError):
            presented = None
        raise CertificateUntrusted(app, presented, trusted) from None
    except BaseException:
        raw.close()
        raise


def _is_ip(host):
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
        return True
    except OSError:
        return False


def _verify_failed(exc):
    text = str(exc)
    return ("CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text
            or ("hostname" in text.lower() and "match" in text.lower()))


def _fingerprint_mismatch(exc):
    return "Fingerprints did not match" in str(exc)


class _FingerprintAdapter(HTTPAdapter):
    """Accepts exactly one certificate, by its SHA-256 fingerprint, checked
    after the handshake and before the request is sent."""

    def __init__(self, sha256, **kwargs):
        self._sha256 = sha256
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs.update(assert_fingerprint=self._sha256, assert_hostname=False)
        return super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        # The chain isn't verified (it's self-signed); the fingerprint is,
        # and urllib3 refuses the connection before sending if it differs.
        kwargs["verify"] = False
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            return super().send(request, **kwargs)


class PinnedAdapter(HTTPAdapter):
    """Normal verification, falling back to the card's trusted certificate."""

    def __init__(self, app, trusted=None, **kwargs):
        super().__init__(**kwargs)
        self.app = app
        self.trusted = trusted or None
        self._pinned = _FingerprintAdapter(self.trusted["sha256"]) if self.trusted else None

    def send(self, request, **kwargs):
        if self._pinned is not None:
            try:
                return self._pinned.send(request, **kwargs)
            except requests.exceptions.SSLError as exc:
                if not _fingerprint_mismatch(exc):
                    raise
                # Another certificate: fine if a CA vouches for it, else refused.
        try:
            return super().send(request, **kwargs)
        except requests.exceptions.SSLError as exc:
            if not _verify_failed(exc) and not self._pinned:
                raise
            raise CertificateUntrusted(self.app, self._presented(request.url), self.trusted) from None

    def _presented(self, url):
        try:
            return describe(fetch_certificate(url))
        except (OSError, ValueError):
            return None

    def close(self):
        if self._pinned is not None:
            self._pinned.close()
        super().close()


def pin(session, app, trusted=None):
    """Mounts PinnedAdapter for https on the session."""
    session.mount("https://", PinnedAdapter(app, trusted if valid_trusted_cert(trusted) else None))
    return session
