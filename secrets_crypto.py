"""Encrypts the actual secret values inside config.json at rest (app API
keys, Bazarr's basic-auth password, Google Drive's OAuth client secret and
refresh token, OneDrive's token blob) - transparently, at the
load_config()/save_config() boundary in config_store.py, so nothing else
in the codebase needs to know this happens.

The key is a Fernet key (AES-128-CBC + HMAC, authenticated), generated once
and persisted in its own file - separate from config.json, and separate
from auth.json/secret_key too, since scheduled backups run unattended with
nobody logged in and still need to decrypt these values on every run.
There is no way to derive this key from the admin login password without
breaking that (see auth_store.py's docstring for the same reasoning
applied to session cookies).

This protects against a *partial* leak - config.json alone ending up in a
support bundle, a misconfigured backup, or a repurposed drive - not a full
compromise of the same volume the key file also lives on. For that,
override BACKUPARR_SECRETS_KEY with a key kept off the volume entirely
(e.g. a Docker secret), instead of relying on the auto-generated file.
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
    """Encrypts a single string for storage. Empty values are left as-is -
    nothing to protect, and it keeps config.json from filling up with
    encrypted-empty-string noise for every unused field."""
    if not value:
        return value
    token = _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt(value):
    """Reverses encrypt(). A value without the enc:v1: prefix is treated as
    legacy plaintext (written before this existed) and returned unchanged -
    config_store.save_config() re-encrypts it the next time it runs, so
    there's no separate migration step to run by hand.

    If the key has changed (lost secrets.key, or BACKUPARR_SECRETS_KEY
    pointed at a different key than whatever encrypted this value), the
    field can't be recovered - logs a warning and returns "" rather than
    crashing whatever request or cron run touched it, so a lost key locks
    out only the specific fields it can't decrypt, not the whole app."""
    if not value or not value.startswith(PREFIX):
        return value
    token = value[len(PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        log.warning("could not decrypt a stored secret - the encryption key may have changed; treating it as unset")
        return ""
