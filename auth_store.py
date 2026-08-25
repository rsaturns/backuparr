"""The single admin account Backuparr's web UI is gated behind. Kept in
its own file, separate from config.json.

Password hashing is Argon2id (via argon2-cffi), OWASP's current primary
recommendation. PasswordHasher()'s defaults (m=64MiB, t=3, p=4) already
match one of OWASP's listed acceptable configurations.
"""
import hmac
import json
import os
import tempfile

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

AUTH_PATH = os.environ.get("BACKUPARR_AUTH", "/config/backuparr/auth.json")
_hasher = PasswordHasher()


def has_credentials():
    return os.path.exists(AUTH_PATH)


def _load():
    with open(AUTH_PATH) as f:
        return json.load(f)


def set_credentials(username, password):
    data = {"username": username, "password_hash": _hasher.hash(password)}
    auth_dir = os.path.dirname(AUTH_PATH)
    os.makedirs(auth_dir, exist_ok=True)
    # Unique tmp filename avoids concurrent-write collisions.
    fd, tmp_path = tempfile.mkstemp(dir=auth_dir, prefix=".auth.json.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, AUTH_PATH)
    except BaseException:
        os.remove(tmp_path)
        raise


def verify_password(username, password):
    if not has_credentials():
        return False
    data = _load()
    # Constant-time compare so a wrong username can't be timed against a
    # wrong password.
    valid_user = hmac.compare_digest(username, data.get("username", ""))
    try:
        _hasher.verify(data.get("password_hash", ""), password)
        valid_pass = True
    except (VerifyMismatchError, InvalidHashError):
        valid_pass = False
    return valid_user and valid_pass
