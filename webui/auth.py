"""First-run setup, login, logout and reset: the single admin account the
web UI is gated behind (see auth_store.py), and the session key."""
import base64
import binascii
import hmac
import logging
import os
import secrets
import threading
import time

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session

import atomic_file
import auth_store
import config_store
import destination_util
import manifest
import runlog
import secrets_crypto
import self_restore
from config_store import CONFIG_PATH, load_config
from webui.services import DISCOVERY_JOBS, PLEX_LOGINS
from webui.version import VERSION


bp = Blueprint("auth", __name__)
log = logging.getLogger("backuparr.webui")


# Deployment setting: read at startup, never controlled by a request or config.json.
_AUTH_DISABLED = os.environ.get("BACKUPARR_DISABLE_AUTH", "").lower() in ("1", "true", "yes")

SECRET_KEY_PATH = os.environ.get("BACKUPARR_SECRET_KEY_PATH", "/config/backuparr/secret_key")
MIN_PASSWORD_LENGTH = 8


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
_AUTH_API_PATHS = {"/api/setup", "/api/setup/restore", "/api/login", "/api/logout", "/api/reset", "/api/password"}


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


# Home Assistant's read-only status routes (#170) take a bearer token of
# their own, checked in webui/ha.py, never this login - so they skip it, and
# BACKUPARR_DISABLE_AUTH doesn't open them.
_HA_STATUS_PATH = "/api/ha/status"


@bp.before_app_request
def _check_auth():
    if request.path.startswith("/static/"):
        return None
    if request.method == "GET" and (request.path == _HA_STATUS_PATH or request.path.startswith(f"{_HA_STATUS_PATH}/")):
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

    if request.path in ("/setup", "/api/setup", "/api/setup/restore"):
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
    return render_template("setup.html", version=VERSION, needs_key=config_store.holds_settings())


def _existing_settings_key_error(key):
    """With no login but settings already held (#123), only this install's
    encryption key may replace the login: None when that's given, else the
    error response. A wrong key counts towards the login lockout."""
    if not config_store.holds_settings():
        return None
    ip = request.remote_addr or "unknown"
    wait = _login_lockout_remaining(ip)
    if wait:
        return jsonify({"error": f"Too many failed attempts - try again in {int(wait) + 1}s"}), 429
    if not isinstance(key, str) or not key.strip():
        return jsonify({"error": "Backuparr already has settings - enter your encryption key to keep them",
                        "key_required": True}), 400
    if not hmac.compare_digest(key.strip().encode("utf-8"), secrets_crypto.current_key().encode("utf-8")):
        _login_record_failure(ip)
        return jsonify({"error": "That isn't this Backuparr's encryption key", "key_required": True}), 403
    return None


@bp.post("/api/setup")
def api_setup():
    username, password = _credentials_from_request()
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
    data = request.get_json(force=True, silent=True)
    error = _existing_settings_key_error(data.get("key") if isinstance(data, dict) else None)
    if error:
        return error
    replacing = config_store.holds_settings()
    auth_store.set_credentials(username, password)
    if replacing:
        # A replaced login: sessions from before it stop working, and the
        # key was just typed in, so it counts as saved.
        try:
            config_store.acknowledge_secrets_key()
        except (config_store.ConfigError, OSError) as exc:
            log.warning("setup: couldn't record the encryption key as saved (%s)", exc)
        _new_session_key()
        session.clear()
    session.permanent = True
    session["authed"] = True
    return jsonify({"ok": True})


# ----------------------------------------------- restore Backuparr itself ----
# From its own encrypted backup (#111): on the setup page until the first
# account exists, or from Settings with the admin password. The archive comes
# as base64 in JSON, so the JSON-only guard against cross-site requests holds.


def _restore_upload():
    """(archive bytes, key, None) from the request, or (None, None, error)."""
    data = request.get_json(force=True, silent=True)
    archive = data.get("archive") if isinstance(data, dict) else None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(archive, str) or not archive or not isinstance(key, str) or not key.strip():
        return None, None, (jsonify({"error": "Choose Backuparr's backup file and enter its encryption key"}), 400)
    try:
        return base64.b64decode(archive, validate=True), key, None
    except (binascii.Error, ValueError):
        return None, None, (jsonify({"error": "The backup file couldn't be read"}), 400)


def _restore(archive, key):
    """Restores, then replaces the session key so everyone signs in again."""
    from webui import runs  # imported here: runs is loaded after this module

    with runs.RUN_LOCK:  # a scheduled run waits until the restore is done
        if runs.busy():
            return jsonify({"error": "A backup or restore is running - restore Backuparr when it finishes"}), 409
        try:
            members = self_restore.prepare(archive, key)
        except self_restore.SelfRestoreError as exc:
            return jsonify({"error": str(exc).removeprefix("backuparr: ")}), 400
        warnings = self_restore.apply(members, key)
    _new_session_key()
    session.clear()
    return jsonify({"ok": True, "warnings": warnings})


@bp.post("/api/setup/restore")
def api_setup_restore():
    archive, key, error = _restore_upload()
    # On an install that already holds settings, the backup must be one made
    # with this install's key (#123) - a restore can't replace them otherwise.
    return error or _existing_settings_key_error(key) or _restore(archive, key)


@bp.post("/api/backuparr/restore")
def api_backuparr_restore():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"error": "Confirm that this replaces Backuparr's settings and login first"}), 400
    if not _AUTH_DISABLED:
        ip = request.remote_addr or "unknown"
        wait = _login_lockout_remaining(ip)
        if wait:
            return jsonify({"error": f"Too many failed attempts - try again in {int(wait) + 1}s"}), 429
        password = data.get("password")
        if not isinstance(password, str) or not auth_store.verify_password(auth_store.username(), password):
            _login_record_failure(ip)
            return jsonify({"error": "That isn't the admin password"}), 403
    archive, key, error = _restore_upload()
    return error or _restore(archive, key)


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


def _new_session_key():
    """Replaces the key sessions are signed with, so every existing session
    cookie stops working."""
    try:
        os.remove(SECRET_KEY_PATH)
    except FileNotFoundError:
        pass
    current_app.secret_key = _load_or_create_secret_key()


@bp.post("/api/password")
def api_change_password():
    """Changes the admin password (#120). A wrong current password counts
    towards the login lockout; every other session is signed out, and this
    one stays signed in."""
    ip = request.remote_addr or "unknown"
    wait = _login_lockout_remaining(ip)
    if wait:
        return jsonify({"error": f"Too many failed attempts - try again in {int(wait) + 1}s"}), 429
    data = request.get_json(force=True, silent=True)
    data = data if isinstance(data, dict) else {}
    current, new, confirm = (data.get(k) for k in ("current_password", "new_password", "confirm_password"))
    if not all(isinstance(v, str) for v in (current, new, confirm)):
        return jsonify({"error": "Enter the current password and the new one twice"}), 400
    if not auth_store.verify_password(auth_store.username(), current):
        _login_record_failure(ip)
        return jsonify({"error": "The current password isn't right"}), 403
    if len(new) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"The new password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
    if new != confirm:
        return jsonify({"error": "The new passwords don't match"}), 400
    auth_store.set_credentials(auth_store.username(), new)
    _new_session_key()
    session.clear()
    session.permanent = True
    session["authed"] = True  # re-signed with the new key in this response
    return jsonify({"ok": True})


@bp.post("/api/logout")
def api_logout():
    session.clear()
    response = jsonify({"ok": True})
    PLEX_LOGINS.forget_browser(response)
    DISCOVERY_JOBS.forget_browser(response)
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
    except Exception as exc:  # reset still goes ahead: it's how a lost password is recovered
        why = str(exc) if isinstance(exc, (config_store.ConfigError, OSError)) else type(exc).__name__
        log.warning("reset: couldn't find the Local storage folder (%s), so its backups stay - "
                    "delete them by hand if needed", why)

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
