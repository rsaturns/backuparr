"""Encrypts backup archives with the install's encryption key (#147), the
same key that protects the secrets in config.json and Backuparr's own backup.

Any archive can be encrypted the same way, whatever the app, so a new app
needs no code here: backup.py encrypts what the driver made, and restores,
History downloads and the startup backfill decrypt it again.

The format is streamed in chunks, so a multi-GB archive (Plex, Home
Assistant) never has to sit whole in memory:

    header  = MAGIC (8) | salt (16) | chunk size (4, big-endian) | key check (16)
    chunk i = AES-256-GCM(plaintext[i], nonce = i (11 bytes) | last (1 byte),
                          associated data = header)

Each file gets its own random salt. HKDF-SHA256 turns the install key (32
random bytes, all of it used) and the salt into that file's AES-256 key and a
16-byte key check, so a different key is told apart from a damaged file
before any chunk is read. The last chunk is flagged in its nonce: a file cut
short at a chunk boundary, or with anything added after it, fails to decrypt.
The format is written up in docs/security.md, so a backup can be opened
without Backuparr."""
import hashlib
import hmac
import io
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import secrets_crypto
from apps.base import AppError

MAGIC = b"BKPARR\x00\x01"  # "BKPARR", a zero byte, format 1
SUFFIX = ".enc"
SALT_SIZE = 16
CHECK_SIZE = 16
TAG_SIZE = 16
CHUNK_SIZE = 1024 * 1024
_MAX_CHUNK_SIZE = 64 * 1024 * 1024  # a header asking for more is damaged
_HEADER_SIZE = len(MAGIC) + SALT_SIZE + 4 + CHECK_SIZE
_INFO = b"backuparr archive v1"


class ArchiveCryptoError(AppError):
    pass


class WrongKeyError(ArchiveCryptoError):
    """The archive was encrypted with another key, or the key given isn't one."""


# Backuparr's own backup from before 3.0 was one Fernet token. Every token
# starts with version byte 0x80 and a 64-bit timestamp whose top bytes are
# still zero, which base64 writes as "gAAAAA" - enough to name the file
# without any code that could decrypt it (#162).
_PRE_3_PREFIX = b"gAAAAA"
PRE_3_SELF_BACKUP = "this backup is from before Backuparr 3.0. Restore it with Backuparr 2.5.6, then update."

_WRONG_KEY = "this backup was encrypted with a different encryption key - enter that key to use it"
_DAMAGED = "the encrypted backup is damaged or incomplete"


def encrypted_name(name):
    return name + SUFFIX


def plain_name(name):
    """The archive's name before it was encrypted: radarr_<ts>.zip.enc ->
    radarr_<ts>.zip."""
    return name[:-len(SUFFIX)] if name.endswith(SUFFIX) else name


def is_encrypted_name(name):
    return name.endswith(SUFFIX)


def _key_material(key):
    try:
        return secrets_crypto.key_bytes(key)
    except ValueError:
        raise WrongKeyError("that isn't a Backuparr encryption key - it's 44 characters ending in =") from None


def _derive(key, salt):
    """(AES-256 key, key check) for one file."""
    out = HKDF(algorithm=hashes.SHA256(), length=32 + CHECK_SIZE, salt=salt, info=_INFO).derive(_key_material(key))
    return out[:32], out[32:]


def _nonce(index, last):
    return index.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def _read_full(src, size):
    """Reads size bytes, or fewer only at the end of the file."""
    parts, got = [], 0
    while got < size:
        part = src.read(size - got)
        if not part:
            break
        parts.append(part)
        got += len(part)
    return b"".join(parts)


def encrypt_stream(src, dst, key=None):
    """Encrypts the readable binary src into dst. key is the install's key
    when None, or a key's text. Returns the plaintext's SHA-256 (hex)."""
    salt = os.urandom(SALT_SIZE)
    aes_key, check = _derive(key, salt)
    header = MAGIC + salt + struct.pack(">I", CHUNK_SIZE) + check
    aes = AESGCM(aes_key)
    digest = hashlib.sha256()
    dst.write(header)
    index = 0
    current = _read_full(src, CHUNK_SIZE)
    while True:
        following = _read_full(src, CHUNK_SIZE)
        last = not following
        digest.update(current)
        dst.write(aes.encrypt(_nonce(index, last), current, header))
        if last:
            return digest.hexdigest()
        current, index = following, index + 1


def decrypt_stream(src, dst=None, key=None):
    """Decrypts src into dst; with dst None it only checks the file. Returns
    the plaintext's SHA-256 (hex). Raises WrongKeyError or ArchiveCryptoError.
    A chunk is written only once it has been authenticated, but a damaged file
    can still leave the chunks before the damage in dst - callers discard it."""
    header = _read_full(src, _HEADER_SIZE)
    if len(header) < _HEADER_SIZE or not header.startswith(MAGIC):
        raise ArchiveCryptoError("this isn't a backup encrypted by Backuparr")
    salt = header[len(MAGIC):len(MAGIC) + SALT_SIZE]
    (chunk_size,) = struct.unpack(">I", header[len(MAGIC) + SALT_SIZE:len(MAGIC) + SALT_SIZE + 4])
    if not 0 < chunk_size <= _MAX_CHUNK_SIZE:
        raise ArchiveCryptoError(_DAMAGED)
    aes_key, check = _derive(key, salt)
    if not hmac.compare_digest(check, header[-CHECK_SIZE:]):
        raise WrongKeyError(_WRONG_KEY)
    aes = AESGCM(aes_key)
    digest = hashlib.sha256()
    sealed_size = chunk_size + TAG_SIZE
    index = 0
    current = _read_full(src, sealed_size)
    while True:
        following = _read_full(src, sealed_size)
        last = not following
        if len(current) < TAG_SIZE or (not last and len(current) != sealed_size):
            raise ArchiveCryptoError(_DAMAGED)
        try:
            plain = aes.decrypt(_nonce(index, last), current, header)
        except InvalidTag:
            raise ArchiveCryptoError(_DAMAGED) from None
        digest.update(plain)
        if dst is not None:
            dst.write(plain)
        if last:
            return digest.hexdigest()
        current, index = following, index + 1


def encrypt_file(src_path, dst_path, key=None):
    """Writes the encrypted copy of src_path to dst_path. Returns the
    plaintext's SHA-256 (hex)."""
    try:
        with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
            return encrypt_stream(src, dst, key)
    except BaseException:
        _remove(dst_path)
        raise


def decrypt_file(src_path, dst_path, key=None):
    """Writes the decrypted copy of src_path to dst_path, removed again if it
    fails. Returns the plaintext's SHA-256 (hex)."""
    try:
        with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
            return decrypt_stream(src, dst, key)
    except BaseException:
        _remove(dst_path)
        raise


def check_file(path, plain_sha256, key=None):
    """Raises ArchiveCryptoError unless path decrypts to a plaintext with
    that SHA-256: the check before an encrypted backup is uploaded."""
    with open(path, "rb") as src:
        if decrypt_stream(src, None, key) != plain_sha256:
            raise ArchiveCryptoError("the encrypted backup didn't decrypt to the archive that was made")


def is_encrypted(path):
    """True when the file at path starts with this format's header."""
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def is_encrypted_bytes(data):
    return data[:len(MAGIC)] == MAGIC


def is_pre_3_self_backup(data):
    """True for the first bytes of Backuparr's own backup in its pre-3.0
    format, which 3.0 no longer opens."""
    return data[:len(_PRE_3_PREFIX)] == _PRE_3_PREFIX


def encrypt_bytes(data, key=None):
    out = io.BytesIO()
    encrypt_stream(io.BytesIO(data), out, key)
    return out.getvalue()


def decrypt_bytes(data, key=None):
    out = io.BytesIO()
    decrypt_stream(io.BytesIO(data), out, key)
    return out.getvalue()


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
