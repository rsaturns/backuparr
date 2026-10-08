"""Web UI for Backuparr: configure apps/API keys, trigger backups, browse
history, and restore - all from the browser instead of editing
docker-compose env vars by hand.
"""
import copy
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from croniter import croniter
from flask import Flask, after_this_request, jsonify, redirect, render_template, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix

import auth_store
import destination_util
import discovery
import gdrive_oauth
import dropbox_oauth
import onedrive_oauth
import rclone_util
import restore_actions as ra
import secrets_crypto
from apps.prowlarr import ProwlarrApp
from webui.plex_auth import PlexAuthError, PlexLogin
from backup import build_app, describe_failure, format_run_message, humanize_error, log_failure, notify, run_backup
from config_store import (
    APP_META,
    APP_NAMES,
    CONFIG_PATH,
    ConfigError,
    DEFAULT_APP,
    DEST_EDITABLE_FIELDS,
    DEST_NAMES,
    DESTINATION_META,
    app_meta,
    destination_meta,
    key_required,
    load_config,
    restore_supported,
    save_config,
)

# Fields an override in the restore request is allowed to replace for a given
# app - url/api_key plus whatever that app's own extra_fields declare (e.g.
# bazarr's basic-auth username/password). Never lets an override touch
# anything outside this set.
def _restore_override_fields(app_name):
    meta = app_meta(app_name) or {}
    return {"url", "api_key"} | {f["name"] for f in meta.get("extra_fields", [])}

app = Flask(__name__)
log = logging.getLogger("backuparr.webui")

# Read once - baked in at build time, can't change at runtime.
_VERSION_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
try:
    with open(_VERSION_PATH) as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "dev"

def _load_or_create_secret_key():
    """Session-signing key, persisted so sessions survive restarts."""
    path = os.environ.get("BACKUPARR_SECRET_KEY_PATH", "/config/backuparr/secret_key")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    key_dir = os.path.dirname(path)
    os.makedirs(key_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=key_dir, prefix=".secret_key.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        os.remove(tmp_path)
        raise
    return key


# Off by default - the documented deployment is plain HTTP on a trusted
# LAN, where a Secure-only cookie would just break login. Set this if
# Backuparr sits behind your own TLS-terminating reverse proxy - it also
# makes the app trust that proxy's X-Forwarded-Proto/-For, so redirect
# URIs built for OAuth (Google Drive) come out https:// instead of the
# http:// this container actually listens on, and login-lockout tracking
# sees the real client IP instead of the proxy's.
_BEHIND_HTTPS_PROXY = os.environ.get("BACKUPARR_FORCE_HTTPS", "").lower() in ("1", "true", "yes")
# Deployment setting: read at startup, never controlled by a request or config.json.
_AUTH_DISABLED = os.environ.get("BACKUPARR_DISABLE_AUTH", "").lower() in ("1", "true", "yes")
if _AUTH_DISABLED:
    log.warning("BACKUPARR_DISABLE_AUTH is set: local authentication is DISABLED. "
                "Anyone who can reach this port has full access - make sure an "
                "authenticating reverse proxy protects it and direct access is blocked.")
if _BEHIND_HTTPS_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_for=1)

app.secret_key = _load_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_BEHIND_HTTPS_PROXY,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


# Scripts only from this origin plus Google's, which the Drive folder picker
# loads; there are no inline scripts. Inline styles stay allowed. connect-src
# includes GitHub's API for the footer's new-version check.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://apis.google.com https://www.gstatic.com",
    "style-src 'self' 'unsafe-inline' https://www.gstatic.com",
    "img-src 'self' data: https://*.gstatic.com https://*.googleusercontent.com https://*.google.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    "connect-src 'self' https://*.googleapis.com https://api.github.com",
    "frame-src https://*.google.com https://content.googleapis.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


@app.after_request
def _security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    # API responses carry API keys and backups: never let a browser or proxy cache them.
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

# CSRF state for the OAuth callback, no session store needed. state -> ts
_OAUTH_STATE = {}
_OAUTH_STATE_TTL = 600

# Login lockout tracking. ip -> (failure_count, last_failure_ts)
_LOGIN_FAILURES = {}
_LOGIN_FAILURE_TTL = 3600
_LOGIN_LOCKOUT_THRESHOLD = 5
_LOGIN_LOCKOUT_BASE_SECONDS = 5
_LOGIN_LOCKOUT_MAX_SECONDS = 300


def _login_lockout_remaining(ip):
    now = time.time()
    for key, (_, last_fail) in list(_LOGIN_FAILURES.items()):
        if now - last_fail > _LOGIN_FAILURE_TTL:
            del _LOGIN_FAILURES[key]
    count, last_fail = _LOGIN_FAILURES.get(ip, (0, 0))
    if count < _LOGIN_LOCKOUT_THRESHOLD:
        return 0
    lockout = min(_LOGIN_LOCKOUT_BASE_SECONDS * (2 ** (count - _LOGIN_LOCKOUT_THRESHOLD)), _LOGIN_LOCKOUT_MAX_SECONDS)
    return max(0, lockout - (now - last_fail))


def _login_record_failure(ip):
    count, _ = _LOGIN_FAILURES.get(ip, (0, 0))
    _LOGIN_FAILURES[ip] = (count + 1, time.time())


# ---------------------------------------------------------------- auth ----
# Session-cookie login, single admin account created via the setup screen
# (see auth_store.py).
_PUBLIC_PATHS = {"/api/logout", "/api/reset"}
# Every local-auth API route. Keep in sync with the /api/setup, /api/login,
# /api/logout and /api/reset routes: any listed here is refused with 403 when
# auth is disabled, and an auth route missing from this set would stay reachable.
_AUTH_API_PATHS = {"/api/setup", "/api/login", "/api/logout", "/api/reset"}


@app.before_request
def _check_auth():
    if request.path.startswith("/static/"):
        return None

    if _AUTH_DISABLED:
        if request.path in ("/login", "/setup"):
            return redirect("/")
        if request.path in _AUTH_API_PATHS:
            return jsonify({"error": "local authentication is disabled"}), 403
        return None

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
    return None


@app.get("/setup")
def setup_page():
    return render_template("setup.html", version=VERSION)


@app.post("/api/setup")
def api_setup():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    auth_store.set_credentials(username, password)
    session.permanent = True
    session["authed"] = True
    return jsonify({"ok": True})


@app.get("/login")
def login_page():
    return render_template("login.html", version=VERSION)


@app.post("/api/login")
def api_login():
    ip = request.remote_addr or "unknown"
    wait = _login_lockout_remaining(ip)
    if wait:
        return jsonify({"error": f"Too many failed attempts - try again in {int(wait) + 1}s"}), 429

    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not auth_store.verify_password(username, password):
        _login_record_failure(ip)
        return jsonify({"error": "Incorrect username or password"}), 401
    _LOGIN_FAILURES.pop(ip, None)
    session.permanent = True
    session["authed"] = True
    return jsonify({"ok": True})


@app.post("/api/logout")
def api_logout():
    session.clear()
    _discard_plex_logins(request.cookies.get(PLEX_OWNER_COOKIE))
    response = jsonify({"ok": True})
    response.delete_cookie(DISCOVERY_OWNER_COOKIE)
    response.delete_cookie(PLEX_OWNER_COOKIE)
    return response


# Deliberately public - it's the forgot-password recovery path. Gated by
# the typed phrase, checked server-side too.
RESET_CONFIRM_PHRASE = "i-want-to-reset-and-delete-files"


@app.post("/api/reset")
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
        shutil.rmtree(local_backup_dir, ignore_errors=True)

    rclone_conf_path = os.environ.get("RCLONE_CONFIG", "/config/backuparr/rclone.conf")
    rclone_pass_path = os.environ.get("RCLONE_CONFIG_PASS_FILE", "/config/backuparr/rclone.pass")
    secret_key_path = os.environ.get("BACKUPARR_SECRET_KEY_PATH", "/config/backuparr/secret_key")
    # secret_key too, so it invalidates any other already-logged-in session.
    for path in (CONFIG_PATH, rclone_conf_path, rclone_pass_path, auth_store.AUTH_PATH, secret_key_path, secrets_crypto.KEY_PATH):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------- run state -----
RUN_LOCK = threading.Lock()
RUN_CANCEL_EVENT = threading.Event()
RUN_STATE = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": [],
    "failed": [],
    "log": [],
    "current_app": None,
    "current_index": 0,
    "total_apps": 0,
    "cancel_requested": False,
}


class _ListLogHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(self.format(record))


def _run_tracked(state, work, *args):
    """Runs work(*args) with a temporary log handler feeding state["log"],
    then marks the run finished. `state["running"]` must already be True -
    only _start_tracked_run() (which sets it under a lock) should trigger
    this, via the background thread it starts."""
    runner_logger = logging.getLogger("backuparr")
    handler = _ListLogHandler(state["log"])
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    runner_logger.addHandler(handler)
    try:
        work(*args)
    finally:
        runner_logger.removeHandler(handler)
        state["running"] = False
        state["finished_at"] = datetime.now(timezone.utc).isoformat()


def _start_tracked_run(state, lock, reset_fields, work, *args, before_start=None):
    """Starts work(*args) in a background thread unless state["running"] is
    already True. reset_fields is applied to `state` right before the
    thread starts (only once we know a run will actually happen);
    before_start, if given, runs under the same lock just before that -
    for setup that must not fire when a run is already in progress (e.g.
    clearing a cancel flag that might belong to that other run). Returns
    whether it started."""
    with lock:
        if state["running"]:
            return False
        if before_start:
            before_start()
        state.update(reset_fields)
        state["running"] = True
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        state["finished_at"] = None
    threading.Thread(target=_run_tracked, args=(state, work, *args), daemon=True).start()
    return True


def _backup_work():
    backup_logger = logging.getLogger("backuparr")

    def _progress(index, total, name):
        RUN_STATE["current_app"] = name
        RUN_STATE["current_index"] = index
        RUN_STATE["total_apps"] = total

    try:
        cfg = load_config()
        ok, failed = run_backup(cfg, on_progress=_progress, should_cancel=RUN_CANCEL_EVENT.is_set)
        RUN_STATE["ok"] = ok
        RUN_STATE["failed"] = failed
        notify(cfg.get("notify_url"), format_run_message(ok, failed))
    except Exception as exc:  # not a per-app failure: the run itself couldn't proceed
        message = describe_failure(exc)
        RUN_STATE["failed"] = [f"backup run stopped: {message}" if message else f"unexpected error: {exc}"]
        log_failure(backup_logger, "backup run stopped", exc)


def _start_backup_run():
    """Starts a backup run unless one's already in progress. Returns
    whether it started. Shared by the manual endpoint and the scheduler."""
    return _start_tracked_run(
        RUN_STATE,
        RUN_LOCK,
        {
            "ok": [],
            "failed": [],
            "log": [],
            "current_app": None,
            "current_index": 0,
            "total_apps": 0,
            "cancel_requested": False,
        },
        _backup_work,
        before_start=RUN_CANCEL_EVENT.clear,
    )


def _cancel_backup_run():
    """Requests cancellation of the in-progress run, if any. run_backup()
    only checks between apps/uploads, so this stops the run at the next
    safe point rather than instantly. Returns whether a run was running."""
    if not RUN_STATE["running"]:
        return False
    RUN_STATE["cancel_requested"] = True
    RUN_CANCEL_EVENT.set()
    return True


# ------------------------------------------------------------ scheduler ----
# In-process, not an external cron daemon - re-reads cron_schedule from
# config.json every tick, so a Settings change applies immediately.
_SCHEDULER_INTERVAL_SECONDS = 20
_scheduler_state = {"last_run_minute": None, "last_error": None}


def _scheduler_loop():
    while True:
        try:
            schedule = load_config().get("cron_schedule", "0 3 * * *")
            now = datetime.now().replace(second=0, microsecond=0)
            # Dedup: a matching minute is seen on more than one ~20s tick.
            if (
                _scheduler_state["last_run_minute"] != now
                and croniter.is_valid(schedule)
                and croniter.match(schedule, now)
            ):
                _scheduler_state["last_run_minute"] = now
                if _start_backup_run():
                    log.info("scheduler: starting backup run (schedule %r, %s)", schedule, now.isoformat())
        except Exception as exc:
            # A persistent problem (unreadable config.json, say) would
            # repeat every tick; report each distinct one once.
            problem = describe_failure(exc) or repr(exc)
            if _scheduler_state["last_error"] != problem:
                _scheduler_state["last_error"] = problem
                log_failure(log, "scheduler tick failed", exc)
        else:
            _scheduler_state["last_error"] = None
        time.sleep(_SCHEDULER_INTERVAL_SECONDS)


def start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True).start()


# --------------------------------------------------------------- errors ----
# Safety net for failures no route handles itself: a full or read-only
# volume when settings are saved, an unreadable or hand-broken config.json.
@app.errorhandler(OSError)
@app.errorhandler(ConfigError)
def _operational_error(exc):
    message = describe_failure(exc)
    if message is None:  # an unfamiliar OS error: keep the stack for the bug report
        log.error("%s %s failed", request.method, request.path, exc_info=exc)
        return jsonify({"error": str(exc)}), 500
    log.error("%s %s failed - %s", request.method, request.path, message)
    return jsonify({"error": message}), 500


# --------------------------------------------------------------- pages ----
@app.get("/")
def index():
    # Prowlarr and the apps it discovers lead the Settings list, side by side.
    lead = ("prowlarr", "radarr", "sonarr", "sabnzbd")
    apps = sorted(APP_META, key=lambda m: lead.index(m["id"]) if m["id"] in lead else len(lead))
    return render_template("index.html", app_meta=apps, destination_meta=DESTINATION_META, version=VERSION,
                           auth_disabled=_AUTH_DISABLED)


# -------------------------------------------------------------- config ----
@app.get("/api/config")
def api_get_config():
    return jsonify(load_config())


@app.get("/api/meta")
def api_meta():
    return jsonify(APP_META)


def _validate_config(data, cfg):
    if not isinstance(data, dict):
        return "expected a JSON object"
    for key in ("apps", "destinations"):
        section = data.get(key, {})
        if not isinstance(section, dict) or not all(isinstance(entry, dict) for entry in section.values()):
            return f"{key} must be an object of objects"
    if "retention_days" in data:
        try:
            if isinstance(data["retention_days"], bool) or int(data["retention_days"]) < 1:
                return "retention_days must be a positive number"
        except (TypeError, ValueError):
            return "retention_days must be a number"
    if "cron_schedule" in data:
        schedule = str(data["cron_schedule"])
        if len(schedule.split()) != 5:
            return "cron_schedule must be 5 space-separated fields (minute hour day month weekday)"
        if not croniter.is_valid(schedule):
            return "cron_schedule is not a valid cron expression"
    for name, app_data in data.get("apps", {}).items():
        if name not in APP_NAMES:
            return f"unknown app: {name}"
        meta = app_meta(name)
        if app_data.get("enabled") and meta["status"] != "available":
            return f"{meta['label']} isn't available yet"
        if app_data.get("enabled") and not app_data.get("url"):
            return f"{name}: a URL is required to enable it"
        if app_data.get("enabled") and key_required(name) and not app_data.get("api_key"):
            return f"{name}: {meta.get('key_label', 'an API key')} is required to enable it"
    for name, dest_data in data.get("destinations", {}).items():
        if name not in DEST_NAMES:
            return f"unknown destination: {name}"
        meta = destination_meta(name)
        if dest_data.get("enabled") and meta["status"] != "available":
            return f"{meta['label']} isn't available yet"
        if name == "gdrive" and dest_data.get("enabled") and not dest_data.get("client_id"):
            return "Google Drive: a Client ID is required to enable it (paste it in first, then Connect)"
        if name == "onedrive" and dest_data.get("enabled") and not cfg["destinations"]["onedrive"].get("token"):
            return "OneDrive: connect it first (paste a token from `rclone authorize onedrive`) before enabling"
        if name == "dropbox" and dest_data.get("enabled") and not cfg["destinations"]["dropbox"].get("token"):
            return "Dropbox: connect it first (paste a token from `rclone authorize dropbox`) before enabling"
    return None


@app.post("/api/config")
def api_set_config():
    data = request.get_json(force=True, silent=True)
    cfg = load_config()
    error = _validate_config(data, cfg)
    if error:
        return jsonify({"error": error}), 400

    for key in ("retention_days", "cron_schedule", "notify_url", "bazarr_backup_dir"):
        if key in data:
            cfg[key] = data[key]
    for name in APP_NAMES:
        if name in data.get("apps", {}):
            incoming = data["apps"][name]
            cfg["apps"][name].update({k: v for k, v in incoming.items() if k in DEFAULT_APP})
    for name in DEST_NAMES:
        if name in data.get("destinations", {}):
            incoming = data["destinations"][name]
            cfg["destinations"][name].update({k: v for k, v in incoming.items() if k in DEST_EDITABLE_FIELDS[name]})

    save_config(cfg)
    destination_util.sync(cfg)
    return jsonify({"ok": True})


@app.post("/api/test/<app_name>")
def api_test(app_name):
    if app_name not in APP_NAMES:
        return jsonify({"ok": False, "message": "unknown app"}), 404
    meta = app_meta(app_name)
    if meta["status"] != "available":
        return jsonify({"ok": False, "message": f"{meta['label']} isn't available yet"})
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("url"):
        return jsonify({"ok": False, "message": "URL is required"}), 400
    if key_required(app_name) and not data.get("api_key"):
        return jsonify({"ok": False, "message": f"{meta.get('key_label', 'API key')} is required"}), 400

    app_cfg = copy.deepcopy(DEFAULT_APP)
    app_cfg.update(data)
    try:
        instance = build_app(app_name, app_cfg)
        message = instance.test_connection()
        return jsonify({"ok": True, "message": message})
    except Exception as exc:
        return jsonify({"ok": False, "message": humanize_error(exc)})


# ------------------------------------------------------- Plex sign-in ----
PLEX_OWNER_COOKIE = "backuparr_plex_login"
PLEX_LOGINS = {}
PLEX_LOGINS_LOCK = threading.Lock()
PLEX_START_LOCK = threading.Lock()


def _purge_plex_logins_locked():
    for job_id, (_, login) in list(PLEX_LOGINS.items()):
        if time.monotonic() >= login.expires_at:
            del PLEX_LOGINS[job_id]


def _discard_plex_logins(owner):
    with PLEX_LOGINS_LOCK:
        for job_id, (job_owner, _) in list(PLEX_LOGINS.items()):
            if job_owner == owner:
                del PLEX_LOGINS[job_id]


@app.post("/api/plex/auth")
def api_plex_auth_start():
    # JSON-only, with no CORS allowance: a cross-origin form cannot start login.
    if not isinstance(request.get_json(silent=True), dict):
        return jsonify({"error": "A JSON request is required."}), 400
    if not PLEX_START_LOCK.acquire(blocking=False):
        return jsonify({"error": "Plex sign-in is starting. Try again in a moment."}), 429
    try:
        owner = request.cookies.get(PLEX_OWNER_COOKIE) or secrets.token_urlsafe(24)
        _discard_plex_logins(owner)
        with PLEX_LOGINS_LOCK:
            _purge_plex_logins_locked()
            if len(PLEX_LOGINS) >= 20:
                return jsonify({"error": "Too many pending Plex sign-ins. Try again later."}), 429
        login = PlexLogin(VERSION)
        job_id = secrets.token_urlsafe(24)
        with PLEX_LOGINS_LOCK:
            PLEX_LOGINS[job_id] = (owner, login)
        response = jsonify({"job_id": job_id, "auth_url": login.auth_url, "expires_in": login.expires_in})
        # Separate from Flask's refreshing session cookie, as with discovery.
        response.set_cookie(PLEX_OWNER_COOKIE, owner, httponly=True,
                            secure=app.config["SESSION_COOKIE_SECURE"], samesite="Strict")
        return response, 201
    except PlexAuthError as exc:
        return jsonify({"error": str(exc)}), exc.status
    finally:
        PLEX_START_LOCK.release()


@app.route("/api/plex/auth/<job_id>", methods=["GET", "DELETE"])
def api_plex_auth_status(job_id):
    with PLEX_LOGINS_LOCK:
        _purge_plex_logins_locked()
        job = PLEX_LOGINS.get(job_id)
        if not job or job[0] != request.cookies.get(PLEX_OWNER_COOKIE):
            return jsonify({"error": "Plex sign-in expired or was cancelled. Click Get Plex token to try again."}), 404
        if request.method == "DELETE":
            del PLEX_LOGINS[job_id]
            return jsonify({"ok": True})
    try:
        result = job[1].poll()
        with PLEX_LOGINS_LOCK:
            if PLEX_LOGINS.get(job_id) is not job:
                return jsonify({"error": "Plex sign-in was cancelled."}), 404
        return jsonify(result)
    except PlexAuthError as exc:
        return jsonify({"error": str(exc)}), exc.status


# Discovery results contain API keys: they live in memory for a few minutes,
# visible only to the browser that started the job, and are purged on the next
# discovery request.
DISCOVERY_RUN_LOCK = threading.Lock()
DISCOVERY_JOBS_LOCK = threading.Lock()
DISCOVERY_JOBS = {}
DISCOVERY_RESULT_TTL = 600
DISCOVERY_OWNER_COOKIE = "backuparr_discovery"


def _purge_discovery_locked():
    cutoff = time.time() - DISCOVERY_RESULT_TTL
    expired = [j for j, job in DISCOVERY_JOBS.items() if job.get("finished", float("inf")) < cutoff]
    for job_id in expired:
        del DISCOVERY_JOBS[job_id]


def _forget_discovery(job_id):
    with DISCOVERY_JOBS_LOCK:
        DISCOVERY_JOBS.pop(job_id, None)


def _discovery_work(job_id, url, api_key):
    def progress(message):
        with DISCOVERY_JOBS_LOCK:
            if job_id in DISCOVERY_JOBS:
                DISCOVERY_JOBS[job_id]["message"] = message

    try:
        instance = ProwlarrApp(url, api_key, strict_redirects=True)
        result = discovery.discover_prowlarr(instance, progress)
        outcome = {"state": "completed", "message": "Discovery complete.", "result": result}
    except discovery.DiscoveryError as exc:
        outcome = {"state": "failed", "message": str(exc)}
    except Exception:
        # Remote error text can carry URLs or credentials, so it is never surfaced.
        outcome = {"state": "failed", "message": "Could not discover services. Check Prowlarr's URL, API key and that it is reachable from Backuparr."}
    finally:
        with DISCOVERY_JOBS_LOCK:
            if job_id in DISCOVERY_JOBS:
                DISCOVERY_JOBS[job_id].update(outcome, finished=time.time())
        DISCOVERY_RUN_LOCK.release()


def _discovery_response(data, status=200):
    response = jsonify(data)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/discovery/prowlarr")
def api_discovery_prowlarr():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not discovery.valid_service_url(data.get("url")):
        return _discovery_response({"error": "A valid Prowlarr URL starting with http:// or https:// is required."}, 400)
    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        return _discovery_response({"error": "Prowlarr's API key is required."}, 400)
    if not DISCOVERY_RUN_LOCK.acquire(blocking=False):
        return _discovery_response({"error": "Discovery is already running. Try again when it finishes."}, 409)
    # A separate cookie: a stale concurrent response must not clobber the refreshing session cookie.
    owner = request.cookies.get(DISCOVERY_OWNER_COOKIE) or secrets.token_urlsafe(24)
    job_id = secrets.token_urlsafe(24)
    with DISCOVERY_JOBS_LOCK:
        _purge_discovery_locked()
        DISCOVERY_JOBS[job_id] = {"owner": owner, "state": "running", "message": "Connecting to Prowlarr..."}
    try:
        threading.Thread(target=_discovery_work, args=(job_id, data["url"].strip(), api_key.strip()), daemon=True).start()
    except RuntimeError:
        _forget_discovery(job_id)
        DISCOVERY_RUN_LOCK.release()
        return _discovery_response({"error": "Could not start discovery. Try again."}, 503)
    response = _discovery_response({"job_id": job_id}, 202)
    response.set_cookie(DISCOVERY_OWNER_COOKIE, owner, httponly=True,
                        secure=app.config["SESSION_COOKIE_SECURE"], samesite="Strict")
    return response


@app.route("/api/discovery/prowlarr/<job_id>", methods=["GET", "DELETE"])
def api_discovery_status(job_id):
    with DISCOVERY_JOBS_LOCK:
        _purge_discovery_locked()
        job = DISCOVERY_JOBS.get(job_id)
        if not job or job["owner"] != request.cookies.get(DISCOVERY_OWNER_COOKIE):
            return _discovery_response({"error": "Discovery results expired. Run discovery again."}, 404)
        if request.method == "DELETE":
            if job["state"] == "running":
                return _discovery_response({"error": "Discovery is still running."}, 409)
            DISCOVERY_JOBS.pop(job_id)
            return _discovery_response({"ok": True})
        return _discovery_response({key: value for key, value in job.items() if key not in ("owner", "finished")})


@app.post("/api/test-notify")
def api_test_notify():
    data = request.get_json(force=True, silent=True) or {}
    notify_url = data.get("notify_url")
    if not notify_url:
        return jsonify({"ok": False, "message": "Notify URL is required"}), 400
    try:
        notify(notify_url, "Backuparr test notification - if you see this, your Notify URL is working.", raise_on_error=True)
        return jsonify({"ok": True, "message": "sent - check your notification target"})
    except Exception as exc:
        return jsonify({"ok": False, "message": humanize_error(exc)})


# --------------------------------------------------------- destinations ----
@app.get("/api/destinations")
def api_destinations():
    return jsonify(DESTINATION_META)


def _not_connected_message(dest_id, label, request_data):
    """Test only checks the saved connection, so a token that's pasted but not
    yet connected (the UI sends token_pasted) gets a pointer to Connect."""
    if request_data.get("token_pasted"):
        return f"The pasted token isn't used until you click Connect {label} - connect first, then test"
    return f"Not connected yet - paste a token from `rclone authorize {dest_id}` first"


@app.post("/api/test-destination/<dest_id>")
def api_test_destination(dest_id):
    if dest_id not in DEST_NAMES:
        return jsonify({"ok": False, "message": "unknown destination"}), 404

    cfg = load_config()
    dest_cfg = dict(cfg["destinations"].get(dest_id, {}))
    data = request.get_json(force=True, silent=True) or {}
    dest_cfg.update({k: v for k, v in data.items() if k in DEST_EDITABLE_FIELDS.get(dest_id, set())})

    try:
        if dest_id == "local":
            path = destination_util.local_root(dest_cfg)
            probe = os.path.join(path, ".backuparr-write-test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return jsonify({"ok": True, "message": f"{path} is writable"})

        if dest_id == "gdrive":
            if not dest_cfg.get("refresh_token"):
                return jsonify({"ok": False, "message": "Not connected yet - click Connect Google Drive first"})
            cfg["destinations"]["gdrive"] = dest_cfg
            destination_util.sync(cfg)
            root = destination_util.remote_root("gdrive", dest_cfg)
            rclone_util.check_remote(root)
            folder = dest_cfg.get("folder_name") or "My Drive (root)"
            return jsonify({"ok": True, "message": f"connected, backing up to \"{folder}\""})

        if dest_id == "onedrive":
            if not dest_cfg.get("token"):
                return jsonify({"ok": False, "message": _not_connected_message("onedrive", "OneDrive", data)})
            cfg["destinations"]["onedrive"] = dest_cfg
            destination_util.sync(cfg)
            root = destination_util.remote_root("onedrive", dest_cfg)
            rclone_util.check_remote(root)
            return jsonify({"ok": True, "message": "connected, backing up to your OneDrive app folder"})

        if dest_id == "dropbox":
            if not dest_cfg.get("token"):
                return jsonify({"ok": False, "message": _not_connected_message("dropbox", "Dropbox", data)})
            cfg["destinations"]["dropbox"] = dest_cfg
            destination_util.sync(cfg)
            root = destination_util.remote_root("dropbox", dest_cfg)
            rclone_util.check_remote(root)
            return jsonify({"ok": True, "message": f"connected, backing up to the {dropbox_oauth.BACKUP_FOLDER} folder in your Dropbox"})

        return jsonify({"ok": False, "message": f"{dest_id} is not available yet"})
    except (rclone_util.RcloneError, destination_util.DestinationError, OSError) as exc:
        return jsonify({"ok": False, "message": str(exc)})


# ------------------------------------------------------------- backups ----
@app.post("/api/backup/run")
def api_backup_run():
    if not _start_backup_run():
        return jsonify({"error": "a backup is already running"}), 409
    return jsonify({"started": True})


@app.post("/api/backup/cancel")
def api_backup_cancel():
    if not _cancel_backup_run():
        return jsonify({"error": "no backup is running"}), 409
    return jsonify({"cancelling": True})


@app.get("/api/backup/status")
def api_backup_status():
    tail = []
    log_path = os.path.join(os.environ.get("BACKUPARR_LOG_DIR", "/var/log/backuparr"), "backup.log")
    try:
        with open(log_path) as f:
            tail = f.readlines()[-200:]
    except OSError:
        pass
    state = dict(RUN_STATE)
    state["log_tail"] = [line.rstrip("\n") for line in tail]
    return jsonify(state)


def _destination_root_or_error(cfg, dest_id):
    """Returns (remote_root, None) or (None, (json_response, status))."""
    if dest_id not in DEST_NAMES:
        return None, (jsonify({"error": "unknown destination"}), 404)
    dest_cfg = cfg["destinations"].get(dest_id, {})
    if not dest_cfg.get("enabled"):
        return None, (jsonify({"error": f"{dest_id} is not enabled"}), 400)
    try:
        destination_util.sync(cfg)
        return destination_util.remote_root(dest_id, dest_cfg), None
    except destination_util.DestinationError as exc:
        return None, (jsonify({"error": str(exc)}), 400)


@app.get("/api/history/<dest_id>")
def api_history(dest_id):
    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    # One recursive listing of the whole destination instead of one rclone
    # call per app - much faster against remote destinations (Drive/OneDrive).
    history = {name: [] for name in APP_NAMES if app_meta(name)["status"] == "available"}
    for entry in rclone_util.lsjson(root, recursive=True):
        if entry.get("IsDir"):
            continue
        app_name, _, _ = entry["Path"].partition("/")
        if app_name in history:
            history[app_name].append({"name": entry["Name"], "size": entry["Size"], "mod_time": entry["ModTime"]})
    for entries in history.values():
        entries.sort(key=lambda e: e["mod_time"], reverse=True)
    return jsonify(history)


@app.delete("/api/history/<dest_id>/<app_name>/<filename>")
def api_history_delete(dest_id, app_name, filename):
    if app_name not in APP_NAMES:
        return jsonify({"error": "unknown app"}), 404
    if not ra.SAFE_FILENAME.match(filename):
        return jsonify({"error": "invalid filename"}), 400

    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error

    try:
        rclone_util.delete_file(f"{root}/{app_name}/{filename}")
    except rclone_util.RcloneError as exc:
        return jsonify({"error": humanize_error(exc)}), 500
    return jsonify({"ok": True})


@app.get("/api/history/<dest_id>/<app_name>/<filename>/download")
def api_history_download(dest_id, app_name, filename):
    if app_name not in APP_NAMES:
        return jsonify({"error": "unknown app"}), 404
    if not ra.SAFE_FILENAME.match(filename):
        return jsonify({"error": "invalid filename"}), 400

    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error

    tmp_dir = tempfile.mkdtemp(prefix="backuparr-dl-")
    local_path = os.path.join(tmp_dir, filename)
    try:
        rclone_util.copyto(f"{root}/{app_name}/{filename}", local_path)
    except rclone_util.RcloneError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": humanize_error(exc)}), 500

    @after_this_request
    def _cleanup(response):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return response

    return send_file(local_path, as_attachment=True, download_name=filename)


# ------------------------------------------------------------- restore ----
# Shares _start_tracked_run()/_run_tracked() with the backup run state above:
# a lock guarding one-at-a-time, a background thread, and live log output the
# UI polls for instead of blocking on one long request.
RESTORE_LOCK = threading.Lock()
RESTORE_RUN_STATE = {
    "running": False,
    "app": None,
    "file": None,
    "started_at": None,
    "finished_at": None,
    "ok": False,
    "message": None,
    "summary": None,
    "error": None,
    "log": [],
}


def _restore_work(app_name, root, app_cfg, data, bazarr_backup_dir):
    tmp_dir = None
    try:
        log.info("restore: fetching %s backup...", app_name)
        tmp_dir, local_zip, filename = ra.fetch_backup(root, app_name, data.get("file"))
        RESTORE_RUN_STATE["file"] = filename

        if app_name in ra.UPLOAD_RESTORE_APPS:
            log.info("restore: uploading backup to %s, it will restart...", app_name)
            ra.restore_app(app_name, app_cfg, tmp_dir, local_zip)
            RESTORE_RUN_STATE["message"] = f"{app_name} restore uploaded, app is restarting"

        elif app_name == "bazarr":
            log.info("restore: triggering bazarr restore...")
            ra.restore_app(app_name, app_cfg, tmp_dir, local_zip, bazarr_backup_dir=bazarr_backup_dir)
            RESTORE_RUN_STATE["message"] = "bazarr restore triggered, app is restarting"

        elif app_name == "tdarr":
            log.info("restore: restoring tdarr collections...")
            ra.restore_app(app_name, app_cfg, tmp_dir, local_zip)
            RESTORE_RUN_STATE["message"] = "tdarr restore complete"

        elif app_name == "tautulli":
            log.info("restore: restoring tautulli...")
            result = ra.restore_app(app_name, app_cfg, tmp_dir, local_zip)
            RESTORE_RUN_STATE["summary"] = result["summary"]
            RESTORE_RUN_STATE["message"] = (
                "tautulli config staged, finish it in Tautulli"
                if result["summary"].get("config_staged")
                else "tautulli restore uploaded"
            )

        elif app_name == "sabnzbd":
            passwords = data.get("passwords", {})

            def password_prompt(name, _server):
                return passwords.get(name) or None

            log.info("restore: restoring sabnzbd config...")
            result = ra.restore_app(app_name, app_cfg, tmp_dir, local_zip, sabnzbd_password_prompt=password_prompt)
            RESTORE_RUN_STATE["summary"] = result["summary"]
            RESTORE_RUN_STATE["message"] = "sabnzbd restore complete"

        RESTORE_RUN_STATE["ok"] = True
    except Exception as exc:
        log_failure(log, f"restore failed for {app_name}", exc, app_cfg.get("url"), app=app_name)
        RESTORE_RUN_STATE["error"] = humanize_error(exc, app_cfg.get("url"))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _start_restore_run(app_name, root, app_cfg, data, bazarr_backup_dir):
    """Starts a restore run unless one's already in progress. Returns
    whether it started."""
    return _start_tracked_run(
        RESTORE_RUN_STATE,
        RESTORE_LOCK,
        {
            "app": app_name,
            "file": None,
            "ok": False,
            "message": None,
            "summary": None,
            "error": None,
            "log": [],
        },
        _restore_work,
        app_name,
        root,
        app_cfg,
        data,
        bazarr_backup_dir,
    )


@app.get("/api/restore/status")
def api_restore_status():
    return jsonify(RESTORE_RUN_STATE)


@app.get("/api/restore/<dest_id>/<app_name>/backups")
def api_restore_backups(dest_id, app_name):
    if app_name not in APP_NAMES:
        return jsonify({"error": "unknown app"}), 404
    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    try:
        files = ra.list_backups(root, app_name)
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 500
    return jsonify({"files": list(reversed(files))})


@app.post("/api/restore/<dest_id>/sabnzbd/preview")
def api_restore_sabnzbd_preview(dest_id):
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    tmp_dir = None
    try:
        tmp_dir, local_zip, filename = ra.fetch_backup(root, "sabnzbd", data.get("file"))
        config = ra.load_sabnzbd_config(tmp_dir, local_zip)
        servers = ra.sabnzbd_server_summary(config)
        return jsonify({"file": filename, "servers": servers})
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 500
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/restore/<dest_id>/<app_name>")
def api_restore(dest_id, app_name):
    if app_name not in APP_NAMES:
        return jsonify({"error": "unknown app"}), 404
    if not restore_supported(app_name):
        return jsonify({"error": f"{app_name} does not support automated restore - see the README"}), 400

    data = request.get_json(force=True, silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "confirm must be true"}), 400

    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    app_cfg = cfg["apps"].get(app_name, {})

    # One-off target override for this restore only - never written back to
    # config.json. Lets you point a restore at a throwaway/test instance
    # without touching the app's configured connection in Settings.
    override = data.get("override")
    if override and not isinstance(override, dict):
        return jsonify({"error": "override must be an object"}), 400
    if override:
        allowed = _restore_override_fields(app_name)
        app_cfg = dict(app_cfg)
        for field in allowed:
            value = override.get(field)
            if value:
                app_cfg[field] = value

    if not app_cfg.get("url") or (key_required(app_name) and not app_cfg.get("api_key")):
        return jsonify({"error": f"{app_name} is not configured"}), 400

    bazarr_backup_dir = None
    if app_name == "bazarr":
        bazarr_backup_dir = data.get("bazarr_backup_dir") or cfg.get("bazarr_backup_dir")
        if not bazarr_backup_dir:
            return jsonify({"error": "bazarr_backup_dir is not configured"}), 400

    if not _start_restore_run(app_name, root, app_cfg, data, bazarr_backup_dir):
        return jsonify({"error": "a restore is already running"}), 409
    return jsonify({"started": True})


# ------------------------------------------------------- gdrive oauth ----
def _gdrive_redirect_uri():
    return request.host_url.rstrip("/") + "/api/destinations/gdrive/oauth/callback"


def _redirect_with_error(message):
    return redirect("/?gdrive_error=" + urllib.parse.quote(str(message)))


def _oauth_state_new():
    now = time.time()
    for s, ts in list(_OAUTH_STATE.items()):
        if now - ts > _OAUTH_STATE_TTL:
            del _OAUTH_STATE[s]
    state = secrets.token_urlsafe(24)
    _OAUTH_STATE[state] = now
    return state


def _oauth_state_consume(state):
    ts = _OAUTH_STATE.pop(state, None)
    return ts is not None and (time.time() - ts) <= _OAUTH_STATE_TTL


@app.get("/api/destinations/gdrive/oauth/start")
def api_gdrive_oauth_start():
    cfg = load_config()
    gdrive_cfg = cfg["destinations"]["gdrive"]
    if not gdrive_cfg.get("client_id") or not gdrive_cfg.get("client_secret"):
        return _redirect_with_error("Save a Client ID and Client Secret first")
    state = _oauth_state_new()
    url = gdrive_oauth.build_auth_url(gdrive_cfg["client_id"], _gdrive_redirect_uri(), state)
    return redirect(url)


@app.get("/api/destinations/gdrive/oauth/callback")
def api_gdrive_oauth_callback():
    error = request.args.get("error")
    if error:
        return _redirect_with_error(error)

    state = request.args.get("state", "")
    code = request.args.get("code")
    if not code or not _oauth_state_consume(state):
        return _redirect_with_error("invalid or expired authorization request, try connecting again")

    cfg = load_config()
    gdrive_cfg = cfg["destinations"]["gdrive"]
    try:
        tokens = gdrive_oauth.exchange_code(
            gdrive_cfg["client_id"], gdrive_cfg["client_secret"], _gdrive_redirect_uri(), code
        )
    except Exception as exc:
        # Broad on purpose: a network failure here raises a raw requests
        # exception, not just GDriveOAuthError.
        log_failure(log, "gdrive oauth exchange failed", exc, gdrive_oauth.TOKEN_URL)
        return _redirect_with_error(humanize_error(exc, gdrive_oauth.TOKEN_URL))

    gdrive_cfg["refresh_token"] = tokens["refresh_token"]
    gdrive_cfg["enabled"] = True
    save_config(cfg)
    destination_util.sync(cfg)
    return redirect("/?gdrive=connected")


@app.post("/api/destinations/gdrive/access-token")
def api_gdrive_access_token():
    cfg = load_config()
    try:
        token = gdrive_oauth.get_access_token(cfg["destinations"]["gdrive"])
        return jsonify({"access_token": token})
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 400


@app.post("/api/destinations/gdrive/folder")
def api_gdrive_folder():
    data = request.get_json(force=True, silent=True) or {}
    folder_id = data.get("folder_id", "")
    folder_name = data.get("folder_name", "")
    if not folder_id:
        return jsonify({"error": "folder_id is required"}), 400

    cfg = load_config()
    gdrive_cfg = cfg["destinations"]["gdrive"]
    if not gdrive_cfg.get("refresh_token"):
        return jsonify({"error": "Google Drive is not connected"}), 400
    gdrive_cfg["folder_id"] = folder_id
    gdrive_cfg["folder_name"] = folder_name
    save_config(cfg)
    destination_util.sync(cfg)
    return jsonify({"ok": True})


@app.post("/api/destinations/gdrive/disconnect")
def api_gdrive_disconnect():
    cfg = load_config()
    cfg["destinations"]["gdrive"].update({
        "enabled": False, "refresh_token": "", "folder_id": "", "folder_name": "",
    })
    save_config(cfg)
    destination_util.sync(cfg)
    return jsonify({"ok": True})


# --------------------------------------------------------- onedrive ----
@app.post("/api/destinations/onedrive/connect")
def api_onedrive_connect():
    """Validates the pasted `rclone authorize onedrive` token and resolves
    the app folder's drive_id/drive_type via one Graph API call."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        token_json, access_token = onedrive_oauth.parse_token_blob(data.get("token_blob", ""))
        approot = onedrive_oauth.approot_metadata(access_token)
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 400

    cfg = load_config()
    onedrive_cfg = cfg["destinations"]["onedrive"]
    onedrive_cfg["token"] = token_json
    onedrive_cfg["drive_id"] = approot["parentReference"]["driveId"]
    onedrive_cfg["drive_type"] = approot["parentReference"]["driveType"]
    onedrive_cfg["item_id"] = approot["id"]
    onedrive_cfg["enabled"] = True
    save_config(cfg)
    # force=True: fresh token should win over whatever's already stored.
    onedrive_oauth.sync_rclone_remote(onedrive_cfg, force=True)
    return jsonify({"ok": True})


@app.post("/api/destinations/onedrive/disconnect")
def api_onedrive_disconnect():
    cfg = load_config()
    cfg["destinations"]["onedrive"].update({
        "enabled": False, "token": "", "drive_id": "", "drive_type": "", "item_id": "",
    })
    save_config(cfg)
    destination_util.sync(cfg)
    return jsonify({"ok": True})


# ---------------------------------------------------------- dropbox ----
@app.post("/api/destinations/dropbox/connect")
def api_dropbox_connect():
    """Validates the pasted `rclone authorize dropbox` token against
    Dropbox before saving it."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        token_json, access_token = dropbox_oauth.parse_token_blob(data.get("token_blob", ""))
        dropbox_oauth.verify_access_token(access_token)
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 400

    cfg = load_config()
    dropbox_cfg = cfg["destinations"]["dropbox"]
    dropbox_cfg["token"] = token_json
    dropbox_cfg["enabled"] = True
    save_config(cfg)
    # force=True: fresh token should win over whatever's already stored.
    dropbox_oauth.sync_rclone_remote(dropbox_cfg, force=True)
    return jsonify({"ok": True})


@app.post("/api/destinations/dropbox/disconnect")
def api_dropbox_disconnect():
    cfg = load_config()
    cfg["destinations"]["dropbox"].update({"enabled": False, "token": ""})
    save_config(cfg)
    destination_util.sync(cfg)
    return jsonify({"ok": True})


# ------------------------------------------------------------- startup ----
start_scheduler()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("WEBUI_HOST") or "0.0.0.0",
        port=int(os.environ.get("WEBUI_PORT", 8990)),
        debug=False,
    )
