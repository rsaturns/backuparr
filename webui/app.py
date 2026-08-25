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

import auth_store
import destination_util
import gdrive_oauth
import onedrive_oauth
import rclone_util
import restore_actions as ra
import secrets_crypto
from backup import build_app, humanize_error, notify, run_backup
from config_store import (
    APP_META,
    APP_NAMES,
    CONFIG_PATH,
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

app = Flask(__name__)
log = logging.getLogger("backuparr.webui")

# Baked into the image at build time (see Dockerfile) - read once here
# rather than on every page load, since it can't change for the lifetime of
# a running container.
_VERSION_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
try:
    with open(_VERSION_PATH) as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "dev"

def _load_or_create_secret_key():
    """Random bytes for signing session cookies - generated once, persisted
    on the same volume as everything else so sessions survive container
    restarts, not regenerated per-process (which would log everyone out on
    every deploy)."""
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


app.secret_key = _load_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


@app.after_request
def _security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response

# Single in-flight OAuth attempt is all a personal, single-admin tool needs -
# CSRF-protects the callback without a real session store. (state -> ts)
_OAUTH_STATE = {}
_OAUTH_STATE_TTL = 600

# Throttles repeated /api/login attempts per source IP - Argon2id's own cost
# adds some friction, but nothing else stops sustained guessing. In-memory,
# same as _OAUTH_STATE above: ip -> (failure_count, last_failure_ts).
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
# Login is required by default, via a session cookie set after a one-time
# setup screen creates the single admin account (see auth_store.py). This
# used to also support HTTP Basic Auth via WEBUI_USERNAME/WEBUI_PASSWORD env
# vars as an alternative - removed once the setup screen existed, since it's
# a strictly better path for everyone the env vars were meant to serve too
# (no plaintext credential sitting in a compose file to manage/rotate).
_PUBLIC_PATHS = {"/api/logout", "/api/reset"}


@app.before_request
def _check_auth():
    if request.path.startswith("/static/"):
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
    return jsonify({"ok": True})


# Deliberately reachable with no auth at all - it's the recovery path for a
# forgotten password, so it can't require the password to trigger. The typed
# phrase (checked here, not just in the UI) is the only thing gating it, so
# it can't be hit by a stray/blind POST.
RESET_CONFIRM_PHRASE = "i-want-to-reset-and-delete-files"


@app.post("/api/reset")
def api_reset():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("confirm") != RESET_CONFIRM_PHRASE:
        return jsonify({"error": "confirmation phrase didn't match"}), 400

    # Resolve the local backup dir (which may be a custom path) before
    # config.json - the only place that custom path is recorded - is gone.
    local_backup_dir = None
    try:
        cfg = load_config()
        local_backup_dir = destination_util.local_root(cfg["destinations"]["local"])
    except Exception:
        pass  # no usable config yet - nothing to look up, that's fine

    if local_backup_dir and os.path.isdir(local_backup_dir):
        shutil.rmtree(local_backup_dir, ignore_errors=True)

    rclone_conf_path = os.environ.get("RCLONE_CONFIG", "/config/backuparr/rclone.conf")
    rclone_pass_path = os.environ.get("RCLONE_CONFIG_PASS_FILE", "/config/backuparr/rclone.pass")
    secret_key_path = os.environ.get("BACKUPARR_SECRET_KEY_PATH", "/config/backuparr/secret_key")
    # secret_key too, not just auth.json - it signs session cookies, so
    # deleting it invalidates any session that was already logged in at the
    # moment of reset, not just the one that triggered it. secrets.key and
    # rclone.pass too - config.json/rclone.conf are about to be deleted
    # anyway, but leaving either encryption key behind would be a loose end
    # if a stale copy of the file it protects ever resurfaces.
    for path in (CONFIG_PATH, rclone_conf_path, rclone_pass_path, auth_store.AUTH_PATH, secret_key_path, secrets_crypto.KEY_PATH):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------- run state -----
RUN_LOCK = threading.Lock()
RUN_STATE = {"running": False, "started_at": None, "finished_at": None, "ok": [], "failed": [], "log": []}


class _ListLogHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(self.format(record))


def _do_run():
    backup_logger = logging.getLogger("backuparr")
    handler = _ListLogHandler(RUN_STATE["log"])
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    backup_logger.addHandler(handler)
    try:
        cfg = load_config()
        ok, failed = run_backup(cfg)
        RUN_STATE["ok"] = ok
        RUN_STATE["failed"] = failed
        notify_url = cfg.get("notify_url")
        if not failed:
            notify(notify_url, f"Backuparr OK: {', '.join(ok) or 'none'}")
        else:
            notify(notify_url, f"Backuparr FAILED: {'; '.join(failed)} | OK: {', '.join(ok) or 'none'}")
    except Exception as exc:  # unexpected crash, not a per-app failure
        RUN_STATE["failed"] = [f"unexpected error: {exc}"]
        backup_logger.exception("backup run crashed")
    finally:
        backup_logger.removeHandler(handler)
        RUN_STATE["running"] = False
        RUN_STATE["finished_at"] = datetime.now(timezone.utc).isoformat()


def _start_backup_run():
    """Starts a backup run in a background thread unless one is already in
    progress. Returns True if it started, False otherwise. Shared by the
    manual "Run backup now" endpoint and the scheduler below, so both go
    through the same lock/state bookkeeping."""
    with RUN_LOCK:
        if RUN_STATE["running"]:
            return False
        RUN_STATE.update(
            {"running": True, "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None, "ok": [], "failed": [], "log": []}
        )
    threading.Thread(target=_do_run, daemon=True).start()
    return True


# ------------------------------------------------------------ scheduler ----
# Runs in-process instead of via an external cron daemon (see entrypoint.sh)
# so a schedule change made in Settings takes effect on the next tick, not
# the next restart - cron_schedule is re-read fresh from config.json every
# time, with no separate crontab file to go stale.
_SCHEDULER_INTERVAL_SECONDS = 20
_scheduler_state = {"last_run_minute": None}


def _scheduler_loop():
    while True:
        try:
            schedule = load_config().get("cron_schedule", "0 3 * * *")
            now = datetime.now().replace(second=0, microsecond=0)
            # Dedup guard: the ~20s poll interval means a single matching
            # minute is seen on more than one tick, so only fire once per
            # minute actually matched.
            if (
                _scheduler_state["last_run_minute"] != now
                and croniter.is_valid(schedule)
                and croniter.match(schedule, now)
            ):
                _scheduler_state["last_run_minute"] = now
                if _start_backup_run():
                    log.info("scheduler: starting backup run (schedule %r, %s)", schedule, now.isoformat())
        except Exception:
            log.exception("scheduler tick failed")
        time.sleep(_SCHEDULER_INTERVAL_SECONDS)


def start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True).start()


# --------------------------------------------------------------- pages ----
@app.get("/")
def index():
    return render_template("index.html", app_meta=APP_META, destination_meta=DESTINATION_META, version=VERSION)


# -------------------------------------------------------------- config ----
@app.get("/api/config")
def api_get_config():
    return jsonify(load_config())


@app.get("/api/meta")
def api_meta():
    return jsonify(APP_META)


def _validate_config(data, cfg):
    if "retention_days" in data:
        try:
            if int(data["retention_days"]) < 1:
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
            return f"{name}: an API key is required to enable it"
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
    return None


@app.post("/api/config")
def api_set_config():
    data = request.get_json(force=True, silent=True) or {}
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
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("url"):
        return jsonify({"ok": False, "message": "URL is required"}), 400
    if key_required(app_name) and not data.get("api_key"):
        return jsonify({"ok": False, "message": "API key is required"}), 400

    app_cfg = copy.deepcopy(DEFAULT_APP)
    app_cfg.update(data)
    try:
        instance = build_app(app_name, app_cfg)
        message = instance.test_connection()
        return jsonify({"ok": True, "message": message})
    except Exception as exc:
        return jsonify({"ok": False, "message": humanize_error(exc)})


# --------------------------------------------------------- destinations ----
@app.get("/api/destinations")
def api_destinations():
    return jsonify(DESTINATION_META)


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
                return jsonify({"ok": False, "message": "Not connected yet - paste a token from `rclone authorize onedrive` first"})
            cfg["destinations"]["onedrive"] = dest_cfg
            destination_util.sync(cfg)
            root = destination_util.remote_root("onedrive", dest_cfg)
            rclone_util.check_remote(root)
            return jsonify({"ok": True, "message": "connected, backing up to your OneDrive app folder"})

        return jsonify({"ok": False, "message": f"{dest_id} is not available yet"})
    except (rclone_util.RcloneError, destination_util.DestinationError, OSError) as exc:
        return jsonify({"ok": False, "message": str(exc)})


# ------------------------------------------------------------- backups ----
@app.post("/api/backup/run")
def api_backup_run():
    if not _start_backup_run():
        return jsonify({"error": "a backup is already running"}), 409
    return jsonify({"started": True})


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
    history = {}
    for name in APP_NAMES:
        entries = rclone_util.lsjson(f"{root}/{name}/")
        history[name] = sorted(
            [{"name": e["Name"], "size": e["Size"], "mod_time": e["ModTime"]} for e in entries],
            key=lambda e: e["mod_time"],
            reverse=True,
        )
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
        return jsonify({"error": str(exc)}), 500
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
        return jsonify({"error": str(exc)}), 500

    @after_this_request
    def _cleanup(response):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return response

    return send_file(local_path, as_attachment=True, download_name=filename)


# ------------------------------------------------------------- restore ----
@app.get("/api/restore/<dest_id>/<app_name>/backups")
def api_restore_backups(dest_id, app_name):
    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    try:
        files = ra.list_backups(root, app_name)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
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
        return jsonify({"error": str(exc)}), 500
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
    if not app_cfg.get("url") or (key_required(app_name) and not app_cfg.get("api_key")):
        return jsonify({"error": f"{app_name} is not configured"}), 400

    tmp_dir = None
    try:
        tmp_dir, local_zip, filename = ra.fetch_backup(root, app_name, data.get("file"))

        if app_name in ra.UPLOAD_RESTORE_APPS:
            ra.restore_upload_app(app_name, app_cfg, local_zip)
            return jsonify({"ok": True, "message": f"{app_name} restore uploaded, app is restarting", "file": filename})

        if app_name == "bazarr":
            backup_dir = data.get("bazarr_backup_dir") or cfg.get("bazarr_backup_dir")
            if not backup_dir:
                return jsonify({"error": "bazarr_backup_dir is not configured"}), 400
            ra.restore_bazarr(app_cfg, local_zip, backup_dir)
            return jsonify({"ok": True, "message": "bazarr restore triggered, app is restarting", "file": filename})

        if app_name == "tdarr":
            ra.restore_tdarr(app_cfg, tmp_dir, local_zip)
            return jsonify({"ok": True, "message": "tdarr restore complete", "file": filename})

        if app_name == "tautulli":
            summary = ra.restore_tautulli(app_cfg, tmp_dir, local_zip)
            return jsonify({"ok": True, "message": "tautulli restore uploaded", "file": filename, "summary": summary})

        if app_name == "sabnzbd":
            config = ra.load_sabnzbd_config(tmp_dir, local_zip)
            passwords = data.get("passwords", {})

            def password_prompt(name, _server):
                return passwords.get(name) or None

            summary = ra.restore_sabnzbd(app_cfg, config, password_prompt)
            return jsonify({"ok": True, "file": filename, "summary": summary})

    except Exception as exc:
        log.exception("restore failed for %s", app_name)
        return jsonify({"error": str(exc)}), 500
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


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
    except gdrive_oauth.GDriveOAuthError as exc:
        log.exception("gdrive oauth exchange failed")
        return _redirect_with_error(str(exc))

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
    except gdrive_oauth.GDriveOAuthError as exc:
        return jsonify({"error": str(exc)}), 400


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
    """Takes the token blob the user pasted from a locally-run
    `rclone authorize onedrive`, validates it, and does one Graph API call
    to resolve the app folder's id/drive_id/drive_type - everything
    sync_rclone_remote needs. No redirect/callback dance of our own since
    the OAuth exchange already happened wherever the user ran that
    command."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        token_json, access_token = onedrive_oauth.parse_token_blob(data.get("token_blob", ""))
        approot = onedrive_oauth.approot_metadata(access_token)
    except onedrive_oauth.OneDriveOAuthError as exc:
        return jsonify({"error": str(exc)}), 400

    cfg = load_config()
    onedrive_cfg = cfg["destinations"]["onedrive"]
    onedrive_cfg["token"] = token_json
    onedrive_cfg["drive_id"] = approot["parentReference"]["driveId"]
    onedrive_cfg["drive_type"] = approot["parentReference"]["driveType"]
    onedrive_cfg["item_id"] = approot["id"]
    onedrive_cfg["enabled"] = True
    save_config(cfg)
    # force=True: this is an explicit (re)connect with a freshly pasted
    # token, so it should win over whatever's already in rclone.conf -
    # unlike the routine sync() path, which deliberately leaves an existing
    # token alone (see sync_rclone_remote's docstring).
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


# ------------------------------------------------------------- startup ----
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("WEBUI_PORT", 8990)), debug=False)
