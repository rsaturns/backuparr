"""Backuparr's HTTPS certificate (#177).

Backuparr serves HTTPS on its own port next to plain HTTP. On first start
it makes a self-signed certificate: an EC P-256 key and a certificate valid
for 825 days, the longest Apple devices accept for any TLS certificate.
30 days before it runs out a new certificate is made with the same key, so
Home Assistant's pin (on the public key) keeps working. Both live in
/config/backuparr/tls/ as private files, and Backuparr's own backup carries
them, so a restored install keeps its certificate.

BACKUPARR_TLS_CERT and BACKUPARR_TLS_KEY name your own PEM files instead.
Backuparr never changes those; if they can't be used, it says why on its
card and in the log and serves its own certificate meanwhile, so the web UI
stays reachable."""
import hashlib
import ipaddress
import logging
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import config_store
from atomic_file import write_private

log = logging.getLogger("backuparr.tls")

TLS_DIR = os.environ.get("BACKUPARR_TLS_DIR") or os.path.join(os.path.dirname(config_store.CONFIG_PATH), "tls")
CERT_PATH = os.path.join(TLS_DIR, "cert.pem")
KEY_PATH = os.path.join(TLS_DIR, "key.pem")
VALIDITY = timedelta(days=825)
RENEW_BEFORE = timedelta(days=30)

_lock = threading.Lock()
_listeners = []


class Certificate(NamedTuple):
    cert_path: str
    key_path: str
    own: bool  # the user's own files, from BACKUPARR_TLS_CERT/_KEY
    fingerprint: str  # SHA-256 of the certificate, as browsers show it
    expires: datetime
    error: str | None  # why the user's own files aren't in use


def _now():
    return datetime.now(timezone.utc)


def fingerprint(cert):
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def public_key_pin(cert):
    """SHA-256 of the certificate's public key, as hex: what Home Assistant pins."""
    der = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _load_key():
    try:
        return serialization.load_pem_private_key(_read(KEY_PATH), password=None)
    except FileNotFoundError:
        pass
    except (ValueError, TypeError) as exc:
        log.warning("tls: %s can't be read (%s); making a new key", KEY_PATH, exc)
    key = ec.generate_private_key(ec.SECP256R1())
    write_private(KEY_PATH, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    log.info("tls: made a new key for Backuparr's HTTPS certificate")
    return key


def _names():
    names = {"localhost", "backuparr"}
    try:
        names.add(socket.gethostname())
    except OSError:
        pass
    return [x509.DNSName(n) for n in sorted(n for n in names if n)] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]


def _make_certificate(key):
    """A self-signed certificate Apple devices accept: SHA-256, a DNS name in
    subjectAltName, serverAuth, and no more than 825 days."""
    now = _now() - timedelta(hours=1)  # a client clock a little behind still accepts it
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Backuparr")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + VALIDITY)
            .add_extension(x509.SubjectAlternativeName(_names()), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    write_private(CERT_PATH, cert.public_bytes(serialization.Encoding.PEM))
    log.info("tls: made Backuparr's HTTPS certificate, valid until %s (SHA-256 %s)",
             cert.not_valid_after_utc.date(), fingerprint(cert))
    return cert


def _same_key(cert, key):
    return cert.public_key().public_numbers() == key.public_key().public_numbers()


def _own_files():
    cert_path = os.environ.get("BACKUPARR_TLS_CERT", "").strip()
    key_path = os.environ.get("BACKUPARR_TLS_KEY", "").strip()
    return (cert_path, key_path) if cert_path or key_path else None


def _check_own(cert_path, key_path):
    """The user's certificate, or why it can't be used."""
    if not cert_path or not key_path:
        return None, "set both BACKUPARR_TLS_CERT and BACKUPARR_TLS_KEY"
    try:
        certs = x509.load_pem_x509_certificates(_read(cert_path))
        key = serialization.load_pem_private_key(_read(key_path), password=None)
    except OSError as exc:
        return None, f"couldn't read {exc.filename}: {exc.strerror}"
    except (ValueError, TypeError):
        return None, "the files aren't a PEM certificate and an unencrypted PEM key"
    if not _same_key(certs[0], key):
        return None, "the key doesn't belong to the certificate"
    if certs[0].not_valid_after_utc <= _now():
        log.warning("tls: your certificate %s expired on %s", cert_path, certs[0].not_valid_after_utc.date())
    return certs[0], None


def _own_signed():
    """Backuparr's own certificate, made or renewed if needed."""
    key = _load_key()
    try:
        cert = x509.load_pem_x509_certificate(_read(CERT_PATH))
    except FileNotFoundError:
        cert = None
    except ValueError:
        log.warning("tls: %s can't be read; making a new certificate", CERT_PATH)
        cert = None
    if cert is None or not _same_key(cert, key) or cert.not_valid_after_utc - _now() < RENEW_BEFORE:
        cert = _make_certificate(key)
    return cert


def ensure():
    """The certificate to serve, made or renewed when needed."""
    with _lock:
        error = None
        own = _own_files()
        if own:
            cert, error = _check_own(*own)
            if cert is not None:
                return Certificate(own[0], own[1], True, fingerprint(cert), cert.not_valid_after_utc, None)
            log.error("tls: your certificate isn't used - %s. Serving Backuparr's own meanwhile.", error)
        cert = _own_signed()
        return Certificate(CERT_PATH, KEY_PATH, False, fingerprint(cert), cert.not_valid_after_utc, error)


# ------------------------------------------- Backuparr's own backup ----
BACKUP_NAMES = ("tls-key.pem", "tls-cert.pem")


def backup_files():
    """{name: bytes} of Backuparr's own key and certificate, for its backup."""
    files = {}
    for name, path in zip(BACKUP_NAMES, (KEY_PATH, CERT_PATH)):
        try:
            files[name] = _read(path)
        except FileNotFoundError:
            pass
    return files


def restore_files(key_pem, cert_pem):
    """Puts back a key and certificate from Backuparr's own backup, if they
    are a matching pair, and has the server use them."""
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
        cert = x509.load_pem_x509_certificate(cert_pem)
    except (ValueError, TypeError):
        log.warning("tls: the backup's certificate can't be read; keeping this one")
        return False
    if not _same_key(cert, key):
        log.warning("tls: the backup's certificate and key don't match; keeping this one")
        return False
    with _lock:
        write_private(KEY_PATH, key_pem)
        write_private(CERT_PATH, cert_pem)
    reload()
    return True


# --------------------------------------------------------- listeners ----
def on_change(callback):
    """callback(Certificate) runs when the certificate files change."""
    _listeners.append(callback)


def reload():
    """Checks the certificate (renewing it when due) and tells the server."""
    cert = ensure()
    for callback in list(_listeners):
        try:
            callback(cert)
        except Exception:
            log.exception("tls: couldn't load the new certificate")
    return cert
