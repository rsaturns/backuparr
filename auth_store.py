"""The single admin account Backuparr's web UI is gated behind.

Kept in its own file (not folded into config.json) on purpose: resetting a
forgotten password is then just "delete this file and restart" without
touching any app/destination settings, and it never round-trips through the
generic config load/save path.

Password hashing is Argon2id (via argon2-cffi) - OWASP's current primary
recommendation over scrypt/bcrypt/PBKDF2, since its hybrid data-dependent/
-independent memory access resists both GPU cracking and timing side-
channels. PasswordHasher()'s defaults (m=64MiB, t=3, p=4) are already one
of OWASP's listed acceptable configurations, so nothing here overrides them.
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
    # Unique tmp filename, not a fixed "<path>.tmp" - see config_store.save_config
    # for the concurrent-write collision this avoids.
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
    # Compare both in constant time so a wrong username can't be
    # distinguished from a wrong password by response timing - hashing a
    # password is the expensive part, so a plain `==` on the username alone
    # would let an attacker probe for valid usernames faster.
    valid_user = hmac.compare_digest(username, data.get("username", ""))
    try:
        _hasher.verify(data.get("password_hash", ""), password)
        valid_pass = True
    except (VerifyMismatchError, InvalidHashError):
        valid_pass = False
    return valid_user and valid_pass
