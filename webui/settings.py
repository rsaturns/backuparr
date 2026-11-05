"""Settings: the config API, its validation, and the app and notification tests."""
import logging

from croniter import croniter
from flask import Blueprint, jsonify, request

import destination_util
from apps.http_headers import headers_for_app, RequestHeadersError, validate_request_headers
from backup import build_app, humanize_error, notify
from config_store import app_meta, APP_NAMES, DEFAULT_APP, DEFAULT_INSTANCE, DEST_EDITABLE_FIELDS, DEST_NAMES, instance_ids, instance_type, key_required, load_config, MAX_EXTRA_INSTANCES, MULTI_INSTANCE_APPS, save_config


bp = Blueprint("settings", __name__)
log = logging.getLogger("backuparr.webui")


# Fields an override in the restore request is allowed to replace for a given
# app - url/api_key plus whatever that app's own extra_fields declare (e.g.
# bazarr's basic-auth username/password). Never lets an override touch
# anything outside this set.
def _restore_override_fields(app_name):
    meta = app_meta(app_name) or {}
    return {"url", "api_key"} | {f["name"] for f in meta.get("extra_fields", [])}


def _missing_required_field(app_name, app_data):
    """Label of the first required extra field (e.g. Jellyfin's backups
    folder) left empty, or None."""
    meta = app_meta(app_name) or {}
    return next((f["label"] for f in meta.get("extra_fields", []) if f.get("required") and not app_data.get(f["name"])), None)

# -------------------------------------------------------------- config ----
@bp.get("/api/config")
def api_get_config():
    cfg = load_config()
    # The UI only needs to know a destination's OAuth token is there. Only the
    # connect and disconnect routes write it, so it never has to come back.
    for dest, remote in destination_util.REMOTES.items():
        cfg["destinations"][dest][remote.TOKEN_FIELD] = bool(cfg["destinations"][dest][remote.TOKEN_FIELD])
    return jsonify(cfg)


def _instances_after(data, cfg, unsaved=()):
    """The instance ids config.json will hold once `data`'s apps are applied:
    an extra instance is added by sending it, removed by sending null.
    `unsaved` adds well-formed ids the Settings form holds but hasn't saved
    yet, for a Test or discovery run before Save."""
    ids = instance_ids(cfg)
    for name, app_data in data.get("apps", {}).items():
        if app_data is None:
            ids = [i for i in ids if i != name]
        elif name not in ids:
            ids.append(name)
    return ids + [i for i in dict.fromkeys(unsaved) if instance_type(i) and i not in ids]


def _validate_instances(data, cfg):
    for name, app_data in data.get("apps", {}).items():
        # instance_type() only accepts extra ids for MULTI_INSTANCE_APPS, so
        # "prowlarr-2" is an unknown app here.
        if not instance_type(name):
            return f"unknown app: {name}"
        if name in APP_NAMES:
            if app_data is None:
                return f"{name} can't be removed - disable it instead"
            if not isinstance(app_data, dict):
                return "apps must be an object of objects"
            continue
        if app_data is None:
            if name not in cfg["apps"]:
                return f"unknown instance: {name}"
            continue
        if not isinstance(app_data, dict):
            return "apps must be an object of objects"
        if name not in cfg["apps"] and name in cfg["removed_instances"]:
            return f"{name} belonged to a removed instance whose backups are kept - add this one under another name"
        display = app_data.get("name", cfg["apps"].get(name, {}).get("name", ""))
        if not isinstance(display, str) or not display.strip() or len(display) > 64:
            return f"{name}: a name of 1-64 characters is required"
    ids = _instances_after(data, cfg)
    for kind in MULTI_INSTANCE_APPS:
        if sum(1 for i in ids if instance_type(i) == kind and i != kind) > MAX_EXTRA_INSTANCES:
            return f"{app_meta(kind)['label']} can have at most {MAX_EXTRA_INSTANCES} extra instances"
    return None


def _validate_config(data, cfg):
    if not isinstance(data, dict):
        return "expected a JSON object"
    for key in ("apps", "destinations"):
        if not isinstance(data.get(key, {}), dict):
            return f"{key} must be an object of objects"
    if not all(isinstance(entry, dict) for entry in data.get("destinations", {}).values()):
        return "destinations must be an object of objects"
    error = _validate_instances(data, cfg)
    if error:
        return error
    # Checked against the instances there will be, so a header can't be left
    # scoped to a removed one.
    headers = data.get("request_headers", cfg.get("request_headers", []))
    removed = {name for name, app_data in data.get("apps", {}).items() if app_data is None}
    for index, row in enumerate(headers if isinstance(headers, list) else [], 1):
        scope = row.get("apps") if isinstance(row, dict) else None
        gone = sorted(removed & set(scope)) if isinstance(scope, list) and all(isinstance(i, str) for i in scope) else []
        if gone:
            return f"Request header {index} is limited to {gone[0]}, which is being removed - change the apps it goes to first"
    try:
        validate_request_headers(headers, _instances_after(data, cfg))
    except RequestHeadersError as exc:
        return str(exc)
    if "retention_days" in data:
        try:
            if isinstance(data["retention_days"], bool) or int(data["retention_days"]) < 1:
                return "retention_days must be a positive number"
        except (TypeError, ValueError):
            return "retention_days must be a number"
    if "self_backup" in data and not isinstance(data["self_backup"], bool):
        return "self_backup must be true or false"
    if "cron_schedule" in data:
        schedule = str(data["cron_schedule"])
        if len(schedule.split()) != 5:
            return "cron_schedule must be 5 space-separated fields (minute hour day month weekday)"
        if not croniter.is_valid(schedule):
            return "cron_schedule is not a valid cron expression"
    for name, app_data in data.get("apps", {}).items():
        if app_data is None:
            continue
        meta = app_meta(name)
        if app_data.get("enabled") and not app_data.get("url"):
            return f"{name}: a URL is required to enable it"
        if app_data.get("enabled") and key_required(name) and not app_data.get("api_key"):
            return f"{name}: {meta.get('key_label', 'an API key')} is required to enable it"
        missing = app_data.get("enabled") and _missing_required_field(name, app_data)
        if missing:
            return f"{name}: {missing} is required to enable it"
    for name, dest_data in data.get("destinations", {}).items():
        if name not in DEST_NAMES:
            return f"unknown destination: {name}"
        if name == "gdrive" and dest_data.get("enabled") and not dest_data.get("client_id"):
            return "Google Drive: a Client ID is required to enable it (paste it in first, then Connect)"
        if name == "onedrive" and dest_data.get("enabled") and not cfg["destinations"]["onedrive"].get("token"):
            return "OneDrive: connect it first (paste a token from `rclone authorize onedrive`) before enabling"
        if name == "dropbox" and dest_data.get("enabled") and not cfg["destinations"]["dropbox"].get("token"):
            return "Dropbox: connect it first (paste a token from `rclone authorize dropbox`) before enabling"
    return None


@bp.post("/api/config")
def api_set_config():
    data = request.get_json(force=True, silent=True)
    cfg = load_config()
    error = _validate_config(data, cfg)
    if error:
        return jsonify({"error": error}), 400

    for key in ("retention_days", "cron_schedule", "notify_url", "bazarr_backup_dir", "request_headers", "self_backup"):
        if key in data:
            cfg[key] = data[key]
    for name, incoming in data.get("apps", {}).items():
        if incoming is None:
            cfg["apps"].pop(name, None)
            if name not in cfg["removed_instances"]:
                cfg["removed_instances"].append(name)
            continue
        extra = name not in APP_NAMES
        if extra and name not in cfg["apps"]:
            cfg["apps"][name] = dict(DEFAULT_INSTANCE)
        fields = set(DEFAULT_INSTANCE if extra else DEFAULT_APP) | {f["name"] for f in app_meta(name).get("extra_fields", [])}
        cfg["apps"][name].update({k: v for k, v in incoming.items() if k in fields})
    for name in DEST_NAMES:
        if name in data.get("destinations", {}):
            incoming = data["destinations"][name]
            cfg["destinations"][name].update({k: v for k, v in incoming.items() if k in DEST_EDITABLE_FIELDS[name]})

    save_config(cfg)
    destination_util.sync(cfg)
    return jsonify({"ok": True})


def _header_scope_ids(rows):
    """Every id request-header rows are scoped to, valid or not."""
    rows = rows if isinstance(rows, list) else []
    scopes = [row.get("apps") for row in rows if isinstance(row, dict)]
    return [i for scope in scopes if isinstance(scope, list) for i in scope if isinstance(i, str)]


@bp.post("/api/test/<app_name>")
def api_test(app_name):
    # Any well-formed id: an instance can be tested before it's saved.
    if not instance_type(app_name):
        return jsonify({"ok": False, "message": "unknown app"}), 404
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "message": "expected a JSON object"}), 400
    if not data.get("url"):
        return jsonify({"ok": False, "message": "URL is required"}), 400
    if key_required(app_name) and not data.get("api_key"):
        return jsonify({"ok": False, "message": f"{app_meta(app_name).get('key_label', 'API key')} is required"}), 400

    app_cfg = dict(DEFAULT_APP)
    app_cfg.update(data)
    try:
        cfg = load_config()
        headers = data["request_headers"] if "request_headers" in data else cfg.get("request_headers", [])
        validate_request_headers(headers, _instances_after({}, cfg, unsaved=_header_scope_ids(headers)))
    except RequestHeadersError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    app_cfg["request_headers"] = headers_for_app(headers, app_name)
    try:
        instance = build_app(app_name, app_cfg)
        message = instance.test_connection()
        return jsonify({"ok": True, "message": message})
    except Exception as exc:
        return jsonify({"ok": False, "message": humanize_error(exc)})


@bp.post("/api/test-notify")
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
