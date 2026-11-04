"""A destination's backups: History (list, delete, download) and Restore."""
import logging
import os
import shutil
import tempfile

from flask import after_this_request, Blueprint, jsonify, request, send_file

import destination_util
import manifest
import rclone_util
import restore_actions as ra
from apps.http_headers import app_connection_config
from backup import humanize_error, log_failure
from config_store import APP_NAMES, DEST_NAMES, instance_ids, instance_type, key_required, load_config, restore_supported
from webui.runs import _start_tracked_run, RESTORE_RUN_STATE, RUN_LOCK, RUN_STATE
from webui.settings import _missing_required_field, _restore_override_fields


bp = Blueprint("backups", __name__)
log = logging.getLogger("backuparr.webui")


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


@bp.get("/api/history/<dest_id>")
def api_history(dest_id):
    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    # One recursive listing of the whole destination instead of one rclone
    # call per app - much faster against remote destinations (Drive/OneDrive).
    # Keyed by instance id; a removed instance's backups are listed while any remain.
    # The app version comes from the manifest; "recorded" is False for a
    # backup the startup backfill hasn't reached yet.
    history = {name: [] for name in instance_ids(cfg)}
    try:
        entries = rclone_util.lsjson(root, recursive=True)
    except rclone_util.RcloneError as exc:
        return jsonify({"error": humanize_error(exc)}), 502
    recorded = manifest.backups()
    for entry in entries:
        if entry.get("IsDir"):
            continue
        app_name, _, _ = entry["Path"].partition("/")
        if app_name in history or (instance_type(app_name) and app_name not in APP_NAMES):
            details = recorded.get(manifest.key(app_name, entry["Name"]))
            history.setdefault(app_name, []).append({
                "name": entry["Name"], "size": entry["Size"], "mod_time": entry["ModTime"],
                "app_version": details.get("app_version") if details else None, "recorded": details is not None,
            })
    for entries in history.values():
        entries.sort(key=lambda e: e["mod_time"], reverse=True)
    return jsonify(history)


@bp.delete("/api/history/<dest_id>/<app_name>/<filename>")
def api_history_delete(dest_id, app_name, filename):
    if not instance_type(app_name):
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
    manifest.remove(app_name, filename, dest_id)
    return jsonify({"ok": True})


@bp.get("/api/history/<dest_id>/<app_name>/<filename>/download")
def api_history_download(dest_id, app_name, filename):
    if not instance_type(app_name):
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


def _restore_work(app_name, root, app_cfg, data, bazarr_backup_dir):
    tmp_dir = None
    try:
        log.info("restore: fetching %s backup...", app_name)
        tmp_dir, local_zip, filename = ra.fetch_backup(root, app_name, data.get("file"))
        RESTORE_RUN_STATE["file"] = filename

        passwords = data.get("passwords", {})

        def password_prompt(name, _server):
            return passwords.get(name) or None

        log.info("restore: %s...", ra.restore_step(app_name))
        result = ra.restore_app(app_name, app_cfg, tmp_dir, local_zip, bazarr_backup_dir=bazarr_backup_dir,
                                sabnzbd_password_prompt=password_prompt)
        RESTORE_RUN_STATE["summary"] = result.get("summary")
        RESTORE_RUN_STATE["message"] = ra.restore_message(app_name, result)
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
        RUN_LOCK,  # one lock: a backup and a restore never run at once
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
        blocked_by=RUN_STATE,
    )


@bp.get("/api/restore/status")
def api_restore_status():
    return jsonify(RESTORE_RUN_STATE)


@bp.get("/api/restore/<dest_id>/<app_name>/backups")
def api_restore_backups(dest_id, app_name):
    if not instance_type(app_name):
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


@bp.post("/api/restore/<dest_id>/sabnzbd/preview")
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


@bp.post("/api/restore/<dest_id>/<app_name>")
def api_restore(dest_id, app_name):
    if not instance_type(app_name):
        return jsonify({"error": "unknown app"}), 404
    if not restore_supported(app_name):
        return jsonify({"error": f"{app_name} does not support automated restore - see its page in the docs"}), 400

    data = request.get_json(force=True, silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "confirm must be true"}), 400

    cfg = load_config()
    if app_name not in cfg["apps"]:
        return jsonify({"error": f"{app_name} is not configured"}), 400
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    app_cfg = app_connection_config(cfg, app_name)

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
    missing = _missing_required_field(app_name, app_cfg)
    if missing:
        return jsonify({"error": f"{app_name}: {missing} is not configured"}), 400

    bazarr_backup_dir = None
    if app_name == "bazarr":
        bazarr_backup_dir = data.get("bazarr_backup_dir") or cfg.get("bazarr_backup_dir")
        if not bazarr_backup_dir:
            return jsonify({"error": "bazarr_backup_dir is not configured"}), 400

    if not _start_restore_run(app_name, root, app_cfg, data, bazarr_backup_dir):
        if RUN_STATE["running"]:
            return jsonify({"error": "a backup is running - restore when it finishes"}), 409
        return jsonify({"error": "a restore is already running"}), 409
    return jsonify({"started": True})
