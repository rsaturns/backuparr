"""Web UI for arr-backup: configure apps/API keys, trigger backups, browse
history, and restore - all from the browser instead of editing
docker-compose env vars by hand.
"""
import copy
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

import rclone_util
import restore_actions as ra
from backup import build_app, notify, run_backup
from config_store import APP_META, APP_NAMES, DEFAULT_APP, key_required, load_config, save_config

app = Flask(__name__)
log = logging.getLogger("arr-backup.webui")

CRONTAB_PATH = "/etc/crontabs/root"
CRON_MARKER_COMMENT = "# arr-backup schedule - managed by the web UI, do not edit by hand"


# ---------------------------------------------------------------- auth ----
def _auth_required():
    return bool(os.environ.get("WEBUI_USERNAME")) and bool(os.environ.get("WEBUI_PASSWORD"))


@app.before_request
def _check_auth():
    if not _auth_required():
        return None
    auth = request.authorization
    if not auth or auth.username != os.environ["WEBUI_USERNAME"] or auth.password != os.environ["WEBUI_PASSWORD"]:
        return ("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="arr-backup"'})
    return None


# ------------------------------------------------------------- crontab ----
def write_crontab(schedule):
    try:
        with open(CRONTAB_PATH, "w") as f:
            f.write(f"{CRON_MARKER_COMMENT}\n")
            f.write(f"{schedule} cd /app && python3 backup.py >> /proc/1/fd/1 2>> /proc/1/fd/2\n")
    except OSError as exc:
        log.warning("failed to write crontab: %s", exc)


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
    backup_logger = logging.getLogger("arr-backup")
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
            notify(notify_url, f"arr-backup OK: {', '.join(ok) or 'none'}")
        else:
            notify(notify_url, f"arr-backup FAILED: {'; '.join(failed)} | OK: {', '.join(ok) or 'none'}")
    except Exception as exc:  # unexpected crash, not a per-app failure
        RUN_STATE["failed"] = [f"unexpected error: {exc}"]
        backup_logger.exception("backup run crashed")
    finally:
        backup_logger.removeHandler(handler)
        RUN_STATE["running"] = False
        RUN_STATE["finished_at"] = datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------- pages ----
@app.get("/")
def index():
    return render_template("index.html", app_meta=APP_META)


# -------------------------------------------------------------- config ----
@app.get("/api/config")
def api_get_config():
    return jsonify(load_config())


@app.get("/api/meta")
def api_meta():
    return jsonify(APP_META)


def _validate_config(data):
    if "retention_days" in data:
        try:
            if int(data["retention_days"]) < 1:
                return "retention_days must be a positive number"
        except (TypeError, ValueError):
            return "retention_days must be a number"
    if "cron_schedule" in data:
        if len(str(data["cron_schedule"]).split()) != 5:
            return "cron_schedule must be 5 space-separated fields (minute hour day month weekday)"
    if "rclone_remote" in data and data["rclone_remote"] and ":" not in data["rclone_remote"]:
        return "rclone_remote should look like 'gdrive:some/path'"
    for name, app_data in data.get("apps", {}).items():
        if name not in APP_NAMES:
            return f"unknown app: {name}"
        if app_data.get("enabled") and not app_data.get("url"):
            return f"{name}: a URL is required to enable it"
        if app_data.get("enabled") and key_required(name) and not app_data.get("api_key"):
            return f"{name}: an API key is required to enable it"
    return None


@app.post("/api/config")
def api_set_config():
    data = request.get_json(force=True, silent=True) or {}
    error = _validate_config(data)
    if error:
        return jsonify({"error": error}), 400

    cfg = load_config()
    for key in ("rclone_remote", "retention_days", "cron_schedule", "notify_url", "bazarr_backup_dir"):
        if key in data:
            cfg[key] = data[key]
    for name in APP_NAMES:
        if name in data.get("apps", {}):
            incoming = data["apps"][name]
            cfg["apps"][name].update({k: v for k, v in incoming.items() if k in DEFAULT_APP})

    save_config(cfg)
    write_crontab(cfg["cron_schedule"])
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
        return jsonify({"ok": False, "message": str(exc)})


@app.post("/api/test-rclone")
def api_test_rclone():
    data = request.get_json(force=True, silent=True) or {}
    remote = data.get("rclone_remote", "")
    if not remote:
        return jsonify({"ok": False, "message": "rclone_remote is required"}), 400
    try:
        rclone_util.check_remote(remote)
        return jsonify({"ok": True, "message": "remote reachable"})
    except rclone_util.RcloneError as exc:
        return jsonify({"ok": False, "message": str(exc)})


# ------------------------------------------------------------- backups ----
@app.post("/api/backup/run")
def api_backup_run():
    with RUN_LOCK:
        if RUN_STATE["running"]:
            return jsonify({"error": "a backup is already running"}), 409
        RUN_STATE.update(
            {"running": True, "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None, "ok": [], "failed": [], "log": []}
        )
    threading.Thread(target=_do_run, daemon=True).start()
    return jsonify({"started": True})


@app.get("/api/backup/status")
def api_backup_status():
    tail = []
    log_path = os.path.join(os.environ.get("ARR_BACKUP_LOG_DIR", "/var/log/arr-backup"), "backup.log")
    try:
        with open(log_path) as f:
            tail = f.readlines()[-200:]
    except OSError:
        pass
    state = dict(RUN_STATE)
    state["log_tail"] = [line.rstrip("\n") for line in tail]
    return jsonify(state)


@app.get("/api/history")
def api_history():
    cfg = load_config()
    remote = cfg.get("rclone_remote")
    if not remote:
        return jsonify({})
    history = {}
    for name in APP_NAMES:
        entries = rclone_util.lsjson(f"{remote.rstrip('/')}/{name}/")
        history[name] = sorted(
            [{"name": e["Name"], "size": e["Size"], "mod_time": e["ModTime"]} for e in entries],
            key=lambda e: e["mod_time"],
            reverse=True,
        )
    return jsonify(history)


# Backup filenames are always generated as <app>_<timestamp>.zip (see
# backup.py); reject anything that doesn't look like that instead of passing
# a user-supplied string into a remote path, since some rclone backends
# (e.g. a local-disk remote) do honor ".." traversal.
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")


@app.delete("/api/history/<app_name>/<filename>")
def api_history_delete(app_name, filename):
    if app_name not in APP_NAMES:
        return jsonify({"error": "unknown app"}), 404
    if not _SAFE_FILENAME.match(filename):
        return jsonify({"error": "invalid filename"}), 400

    cfg = load_config()
    remote = cfg.get("rclone_remote")
    if not remote:
        return jsonify({"error": "rclone_remote is not configured"}), 400

    remote_path = f"{remote.rstrip('/')}/{app_name}/{filename}"
    try:
        rclone_util.delete_file(remote_path)
    except rclone_util.RcloneError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


# ------------------------------------------------------------- restore ----
@app.get("/api/restore/<app_name>/backups")
def api_restore_backups(app_name):
    cfg = load_config()
    remote = cfg.get("rclone_remote")
    if not remote:
        return jsonify({"error": "rclone_remote is not configured"}), 400
    try:
        files = ra.list_backups(remote, app_name)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"files": list(reversed(files))})


@app.post("/api/restore/sabnzbd/preview")
def api_restore_sabnzbd_preview():
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    remote = cfg.get("rclone_remote")
    tmp_dir = None
    try:
        tmp_dir, local_zip, filename = ra.fetch_backup(remote, "sabnzbd", data.get("file"))
        config = ra.load_sabnzbd_config(tmp_dir, local_zip)
        servers = ra.sabnzbd_server_summary(config)
        return jsonify({"file": filename, "servers": servers})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/restore/<app_name>")
def api_restore(app_name):
    if app_name not in APP_NAMES:
        return jsonify({"error": "unknown app"}), 404

    data = request.get_json(force=True, silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "confirm must be true"}), 400

    cfg = load_config()
    remote = cfg.get("rclone_remote")
    if not remote:
        return jsonify({"error": "rclone_remote is not configured"}), 400
    app_cfg = cfg["apps"].get(app_name, {})
    if not app_cfg.get("url") or (key_required(app_name) and not app_cfg.get("api_key")):
        return jsonify({"error": f"{app_name} is not configured"}), 400

    tmp_dir = None
    try:
        tmp_dir, local_zip, filename = ra.fetch_backup(remote, app_name, data.get("file"))

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


# ------------------------------------------------------------- startup ----
def init():
    cfg = load_config()
    write_crontab(cfg.get("cron_schedule", "0 3 * * *"))


init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("WEBUI_PORT", 8990)), debug=False)
