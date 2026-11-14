"""The single admin account Backuparr's web UI is gated behind. Kept in
its own file, separate from config.json.

Password hashing is Argon2id (via argon2-cffi), OWASP's current primary
recommendation. PasswordHasher()'s defaults (m=64MiB, t=3, p=4) already
match one of OWASP's listed acceptable configurations.
"""
import hmac
import json
import os
import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

import atomic_file

AUTH_PATH = os.environ.get("BACKUPARR_AUTH", "/config/backuparr/auth.json")
_hasher = PasswordHasher()
# Each hash takes 64 MiB. Sign-ins sent at once would otherwise all be
# hashed together - up to one per request thread, about 1 GiB - so at most
# MAX_HASHING run at a time and the rest wait their turn (#228).
MAX_HASHING = 2
_HASHING = threading.BoundedSemaphore(MAX_HASHING)


def has_credentials():
    return os.path.exists(AUTH_PATH)


def _load():
    with open(AUTH_PATH) as f:
        return json.load(f)


def username():
    """The admin's username, or None without a readable account."""
    try:
        return _load().get("username")
    except (OSError, ValueError, AttributeError):
        return None


def set_credentials(username, password):
    with _HASHING:
        password_hash = _hasher.hash(password)
    data = {"username": username, "password_hash": password_hash}
    atomic_file.write_private(AUTH_PATH, json.dumps(data, indent=2))


def verify_password(username, password):
    if not has_credentials():
        return False
    data = _load()
    # Constant-time compare so a wrong username can't be timed against a
    # wrong password. Bytes, because compare_digest refuses non-ASCII str.
    valid_user = hmac.compare_digest(
        username.encode("utf-8"), data.get("username", "").encode("utf-8"))
    try:
        with _HASHING:
            _hasher.verify(data.get("password_hash", ""), password)
        valid_pass = True
    except (VerifyMismatchError, InvalidHashError):
        valid_pass = False
    return valid_user and valid_pass
