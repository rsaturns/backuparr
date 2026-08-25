"""The single admin account Backuparr's web UI is gated behind.

Kept in its own file (not folded into config.json) on purpose: resetting a
forgotten password is then just "delete this file and restart" without
touching any app/destination settings, and it never round-trips through the
generic config load/save path.

Password hashing is Werkzeug's generate_password_hash/check_password_hash -
already a Flask dependency, no extra requirement - which defaults to a
salted scrypt hash.
"""
import hmac
import json
import os
import tempfile

from werkzeug.security import check_password_hash, generate_password_hash

AUTH_PATH = os.environ.get("BACKUPARR_AUTH", "/config/backuparr/auth.json")


def has_credentials():
    return os.path.exists(AUTH_PATH)


def _load():
    with open(AUTH_PATH) as f:
        return json.load(f)


def set_credentials(username, password):
    data = {"username": username, "password_hash": generate_password_hash(password)}
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
    valid_pass = check_password_hash(data.get("password_hash", ""), password)
    return valid_user and valid_pass
