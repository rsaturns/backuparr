"""First-run setup, login, logout and reset: the single admin account the
web UI is gated behind (see auth_store.py), and the session key."""
import logging
import os
import secrets
import threading
import time

from flask import Blueprint, jsonify, redirect, render_template, request, session

import atomic_file
import auth_store
import config_store
import destination_util
import manifest
import runlog
import secrets_crypto
from config_store import CONFIG_PATH, load_config
from webui.services import DISCOVERY_JOBS, PLEX_LOGINS
from webui.version import VERSION


bp = Blueprint("auth", __name__)
log = logging.getLogger("backuparr.webui")


# Deployment setting: read at startup, never controlled by a request or config.json.
_AUTH_DISABLED = os.environ.get("BACKUPARR_DISABLE_AUTH", "").lower() in ("1", "true", "yes")

SECRET_KEY_PATH = os.environ.get("BACKUPARR_SECRET_KEY_PATH", "/config/backuparr/secret_key")


def _load_or_create_secret_key():
    """Session-signing key, persisted so sessions survive restarts."""
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    atomic_file.write_private(SECRET_KEY_PATH, key)
    return key


# Login lockout tracking. ip -> (failure_count, last failure's time.monotonic())
_LOGIN_FAILURES = {}
_LOGIN_FAILURES_LOCK = threading.Lock()
_LOGIN_FAILURE_TTL = 3600
_LOGIN_LOCKOUT_THRESHOLD = 5
_LOGIN_LOCKOUT_BASE_SECONDS = 5
_LOGIN_LOCKOUT_MAX_SECONDS = 300


def _login_lockout_remaining(ip):
    now = time.monotonic()
    with _LOGIN_FAILURES_LOCK:
        for key, (_, last_fail) in list(_LOGIN_FAILURES.items()):
            if now - last_fail > _LOGIN_FAILURE_TTL:
                del _LOGIN_FAILURES[key]
        count, last_fail = _LOGIN_FAILURES.get(ip, (0, 0))
    if count < _LOGIN_LOCKOUT_THRESHOLD:
        return 0
    lockout = min(_LOGIN_LOCKOUT_BASE_SECONDS * (2 ** (count - _LOGIN_LOCKOUT_THRESHOLD)), _LOGIN_LOCKOUT_MAX_SECONDS)
    return max(0, lockout - (now - last_fail))


def _login_record_failure(ip):
    with _LOGIN_FAILURES_LOCK:
        count, _ = _LOGIN_FAILURES.get(ip, (0, 0))
        _LOGIN_FAILURES[ip] = (count + 1, time.monotonic())


# ---------------------------------------------------------------- auth ----
# Session-cookie login, single admin account created via the setup screen
# (see auth_store.py).
_PUBLIC_PATHS = {"/api/logout", "/api/reset"}
# Every local-auth API route. Keep in sync with the /api/setup, /api/login,
# /api/logout and /api/reset routes: any listed here is refused with 403 when
# auth is disabled, and an auth route missing from this set would stay reachable.
_AUTH_API_PATHS = {"/api/setup", "/api/login", "/api/logout", "/api/reset"}


@bp.before_app_request
def _require_json_for_changes():
    """State-changing API calls must send JSON. A form, or a simple request
    from another site, can only send form or text/plain bodies - and a
    cross-site JSON request needs a CORS preflight this app never grants -
    so this blocks cross-site request forgery, including against the public
    reset and first-run setup routes."""
    if request.method in ("POST", "PUT", "PATCH") and request.path.startswith("/api/") and not request.is_json:
        return jsonify({"error": "send the request as JSON (Content-Type: application/json)"}), 415
    return None


@bp.before_app_request
def _check_auth():
    if request.path.startswith("/static/"):
        return None

    if _AUTH_DISABLED:
        if request.path in ("/login", "/setup"):
            return redirect("/")
        if request.path in _AUTH_API_PATHS:
            return jsonify({"error": "local authentication is disabled"}), 403
        return _require_saved_key()

    if request.path in _PUBLIC_PATHS:
        return None

    has_creds = auth_store.has_credentials()

    if request.path in ("/setup", "/api/setup"):
        if has_creds:
            return redirect("/login") if request.path == "/setup" else (jsonify({"error": "already set up"}), 403)
        return None

    if request.path in ("/login", "/api/login"):
        if not has_creds:
            return redirect("/setup") if request.path == "/login" else (jsonify({"error": "not set up yet"}), 400)
        if request.path == "/login" and session.get("authed"):
            return redirect("/")
        return None

    if not has_creds:
        return redirect("/setup")
    if not session.get("authed"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect("/login")
    return _require_saved_key()


# ------------------------------------------------------- encryption key ----
# The key that encrypts the secrets in config.json and Backuparr's own
# backup is shown until the user confirms they've saved it (#110), and never
# again. Until then every other API call is refused; scheduled backups run.
_KEY_PATHS = {"/api/secrets-key", "/api/secrets-key/acknowledge", "/api/logout"}


def _require_saved_key():
    if request.path.startswith("/api/") and request.path not in _KEY_PATHS and not config_store.secrets_key_acknowledged():
        return jsonify({"error": "save your encryption key first - reload the page to see it", "key_required": True}), 403
    return None


@bp.get("/api/secrets-key")
def api_secrets_key():
    if config_store.secrets_key_acknowledged():
        return jsonify({"acknowledged": True})
    return jsonify({"acknowledged": False, "key": secrets_crypto.current_key(),
                    "from_environment": bool(os.environ.get("BACKUPARR_SECRETS_KEY"))})


@bp.post("/api/secrets-key/acknowledge")
def api_secrets_key_acknowledge():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"error": "confirm must be true"}), 400
    config_store.acknowledge_secrets_key()
    return jsonify({"ok": True})


def _credentials_from_request():
    """The username and password of a setup or login request, as strings -
    anything else (a number, a list) counts as missing."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return "", ""
    username, password = data.get("username"), data.get("password")
    return (username.strip() if isinstance(username, str) else "",
            password if isinstance(password, str) else "")


@bp.get("/setup")
def setup_page():
    return render_template("setup.html", version=VERSION)


@bp.post("/api/setup")
def api_setup():
    username, password = _credentials_from_request()
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    auth_store.set_credentials(username, password)
    session.permanent = True
    session["authed"] = True
    return jsonify({"ok": True})


@bp.get("/login")
def login_page():
    return render_template("login.html", version=VERSION)


@bp.post("/api/login")
def api_login():
    ip = request.remote_addr or "unknown"
    wait = _login_lockout_remaining(ip)
    if wait:
        return jsonify({"error": f"Too many failed attempts - try again in {int(wait) + 1}s"}), 429

    username, password = _credentials_from_request()
    if not auth_store.verify_password(username, password):
        _login_record_failure(ip)
        return jsonify({"error": "Incorrect username or password"}), 401
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.pop(ip, None)
    session.permanent = True
    session["authed"] = True
    return jsonify({"ok": True})


@bp.post("/api/logout")
def api_logout():
    session.clear()
    PLEX_LOGINS.discard_owner(request.cookies.get(PLEX_LOGINS.cookie))
    response = jsonify({"ok": True})
    response.delete_cookie(DISCOVERY_JOBS.cookie)
    response.delete_cookie(PLEX_LOGINS.cookie)
    return response


# Deliberately public - it's the forgot-password recovery path. Gated by
# the typed phrase, checked server-side too.
RESET_CONFIRM_PHRASE = "i-want-to-reset-and-delete-files"


@bp.post("/api/reset")
def api_reset():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("confirm") != RESET_CONFIRM_PHRASE:
        return jsonify({"error": "confirmation phrase didn't match"}), 400

    # Resolve the local backup dir before config.json (which may record a
    # custom path) is deleted.
    local_backup_dir = None
    try:
        cfg = load_config()
        local_backup_dir = destination_util.local_root(cfg["destinations"]["local"])
    except Exception:
        pass

    if local_backup_dir and os.path.isdir(local_backup_dir):
        # Only Backuparr's own archives: the folder may hold other files.
        # Every instance-shaped folder, so removed instances' backups go too.
        # Reset is how a forgotten password is recovered, so a backup folder
        # it can't clear (a stale mount, no permission) doesn't stop it.
        try:
            folders = [name for name in os.listdir(local_backup_dir) if destination_util.is_backup_folder(name)]
            destination_util.remove_local_backups(local_backup_dir, folders)
        except OSError as exc:
            log.warning("reset: couldn't remove the local backups in %s (%s) - delete them by hand if needed",
                        local_backup_dir, exc.strerror or type(exc).__name__)

    rclone_conf_path = os.environ.get("RCLONE_CONFIG", "/config/backuparr/rclone.conf")
    rclone_pass_path = os.environ.get("RCLONE_CONFIG_PASS_FILE", "/config/backuparr/rclone.pass")
    # secret_key too, so it invalidates any other already-logged-in session.
    for path in (CONFIG_PATH, rclone_conf_path, rclone_pass_path, auth_store.AUTH_PATH, SECRET_KEY_PATH, secrets_crypto.KEY_PATH):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    manifest.clear()
    runlog.clear()
    secrets_crypto.forget_key()
    config_store.forget_secrets_key_acknowledged()

    session.clear()
    return jsonify({"ok": True})
