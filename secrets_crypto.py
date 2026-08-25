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
import tempfile

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("backuparr.secrets_crypto")

PREFIX = "enc:v1:"

KEY_PATH = os.environ.get("BACKUPARR_SECRETS_KEY_PATH", "/config/backuparr/secrets.key")
_fernet = None


def _load_or_create_key():
    env_key = os.environ.get("BACKUPARR_SECRETS_KEY")
    if env_key:
        return env_key.encode("ascii")
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    key_dir = os.path.dirname(KEY_PATH)
    os.makedirs(key_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=key_dir, prefix=".secrets.key.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, KEY_PATH)
    except BaseException:
        os.remove(tmp_path)
        raise
    return key


def _get_fernet():
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(value):
    """Encrypts a single string for storage. Empty values pass through."""
    if not value:
        return value
    token = _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return PREFIX + token


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
