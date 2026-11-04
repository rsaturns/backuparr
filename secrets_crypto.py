"""Encrypts secret values inside config.json at rest, transparently, at
the load_config()/save_config() boundary in config_store.py.

The key is a Fernet key, generated once and persisted in its own file -
separate from config.json and auth.json/secret_key. It can't be derived
from the admin login password: scheduled backups run unattended with
nobody logged in and still need to decrypt these values.

Protects against a *partial* leak (config.json alone, without the key
file). Override BACKUPARR_SECRETS_KEY to keep the key off the volume
entirely for stronger protection.
"""
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

import atomic_file

log = logging.getLogger("backuparr.secrets_crypto")

PREFIX = "enc:v1:"

KEY_PATH = os.environ.get("BACKUPARR_SECRETS_KEY_PATH", "/config/backuparr/secrets.key")
_fernet = None
_key = None  # the key _fernet was made with


def _load_or_create_key():
    env_key = os.environ.get("BACKUPARR_SECRETS_KEY")
    if env_key:
        return env_key.encode("ascii")
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    atomic_file.write_private(KEY_PATH, key)
    return key


def _get_fernet():
    global _fernet, _key
    if _fernet is None:
        _key = _load_or_create_key()
        _fernet = Fernet(_key)
    return _fernet


def forget_key():
    """Reset deleted the key file: the next use makes a new key, instead of
    carrying on with the old one, which nothing could read after a restart."""
    global _fernet, _key
    _fernet = _key = None


def encrypt(value):
    """Encrypts a single string for storage. Empty values pass through."""
    if not value:
        return value
    token = _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return PREFIX + token


def undecryptable(value):
    """True for a stored encrypted value the current key can't open - one
    written with another key, kept as is until the right key is back."""
    if not value or not value.startswith(PREFIX):
        return False
    try:
        _get_fernet().decrypt(value[len(PREFIX):].encode("ascii"))
        return False
    except InvalidToken:
        return True


def decrypt(value):
    """Reverses encrypt(). A value without the enc:v1: prefix is legacy
    plaintext, returned unchanged - save_config() re-encrypts it next run.

    If the key has changed, the field can't be recovered - logs a
    warning and returns "" instead of crashing, so a lost key only locks
    out the fields it can't decrypt."""
    if not value or not value.startswith(PREFIX):
        return value
    token = value[len(PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        log.warning("could not decrypt a stored secret - the encryption key may have changed; treating it as unset")
        return ""


def current_key():
    """The key in use, as text: shown once so the user can keep it (#110).
    It unlocks every stored secret and Backuparr's own backup - never log it."""
    _get_fernet()
    return _key.decode("ascii")


def encrypt_bytes(data):
    """Encrypts a whole file (Backuparr's own backup) with the key."""
    return _get_fernet().encrypt(data)


def decrypt_bytes(token, key=None):
    """Reverses encrypt_bytes(), with this install's key or the one given.
    Raises InvalidToken for a wrong key or a damaged or altered file."""
    fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key) if key else _get_fernet()
    return fernet.decrypt(token)
