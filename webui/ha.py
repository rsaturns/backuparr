"""Read-only backup status for Home Assistant (#170).

GET /api/ha/status (everything) and GET /api/ha/status/<app_id> (one app)
answer to "Authorization: Bearer <token>" alone - never to Backuparr's
login, so Home Assistant never holds the admin session that opens every
restore and app key. The token only opens these two routes: auth.py lets
them past the session check and they check the token themselves, so they
stay closed with BACKUPARR_DISABLE_AUTH too. While the integration is off
they answer 404, as if they didn't exist.

The token is made on the Backuparr card (POST /api/ha/token, behind the
login) and shown once. config.json keeps only its SHA-256 (it's 256 random
bits, so a slow hash adds nothing); New token replaces it, and turning the
integration off (POST /api/ha/disable) removes it. A wrong token counts
towards the login lockout.

Each app's "ok" is its latest run, as the Overview's bar (#167) judges it:
true, false, or null before its first backup. The top-level "ok" is true
only when every enabled app, and Backuparr's own backup while it's on,
succeeded. "error" is the run's error as the Overview shows it, which never
holds a URL or key."""
import hashlib
import hmac
import secrets

from flask import Blueprint, jsonify, request

import runlog
from config_store import app_label, editing_config, enabled_apps, load_config
from webui import auth
from webui.runs import RUN_STATE
from webui.version import VERSION

bp = Blueprint("ha", __name__)

STATUS_PATH = "/api/ha/status"
SELF_ID = "backuparr"


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------- the card's switch ----
@bp.post("/api/ha/token")
def api_ha_token():
    """Turns the integration on with a new token, shown this once; any
    earlier token stops working."""
    token = secrets.token_urlsafe(32)
    with editing_config() as cfg:
        cfg["ha_enabled"] = True
        cfg["ha_token_hash"] = _hash(token)
    return jsonify({"token": token})


@bp.post("/api/ha/disable")
def api_ha_disable():
    with editing_config() as cfg:
        cfg["ha_enabled"] = False
        cfg["ha_token_hash"] = ""
    return jsonify({"ok": True})


# ------------------------------------------------------ bearer routes ----
def _refused(cfg):
    """None when the request carries the token, else the response."""
    if not cfg.get("ha_enabled") or not cfg.get("ha_token_hash"):
        return jsonify({"error": "not found"}), 404
    ip = request.remote_addr or "unknown"
    wait = auth._login_lockout_remaining(ip)
    if wait:
        return jsonify({"error": f"Too many failed attempts - try again in {int(wait) + 1}s"}), 429
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return jsonify({"error": "send the token as Authorization: Bearer <token>"}), 401, {"WWW-Authenticate": "Bearer"}
    if not hmac.compare_digest(_hash(token), cfg["ha_token_hash"]):
        auth._login_record_failure(ip)
        return jsonify({"error": "that token isn't valid - make a new one in Backuparr's Settings (Server > Backuparr)"}), 401, {"WWW-Authenticate": "Bearer"}
    return None


def _name(cfg, app_id):
    return app_label(app_id, cfg["apps"].get(app_id))


def _when(run):
    return run.get("finished") or run.get("started")


def _apps(cfg):
    ids = ([SELF_ID] if cfg.get("self_backup", True) is not False else []) + enabled_apps(cfg)
    runs = runlog.all_runs()
    apps = {}
    for app_id in ids:
        history = runs.get(app_id, [])
        last = history[-1] if history else None
        success = next((run for run in reversed(history) if run.get("ok")), None)
        ok = None if last is None else bool(last.get("ok"))
        apps[app_id] = {
            "name": _name(cfg, app_id),
            "ok": ok,
            "last_run": _when(last) if last else None,
            "last_success": _when(success) if success else None,
            "error": (last.get("error") or "unknown error") if ok is False else None,
        }
    return apps


@bp.get(STATUS_PATH)
def api_ha_status():
    cfg = load_config()
    refused = _refused(cfg)
    if refused:
        return refused
    apps = _apps(cfg)
    succeeded = sum(1 for a in apps.values() if a["ok"] is True)
    failed = sum(1 for a in apps.values() if a["ok"] is False)
    return jsonify({"ok": bool(apps) and succeeded == len(apps), "total": len(apps), "succeeded": succeeded,
                    "failed": failed, "running": bool(RUN_STATE["running"]), "version": VERSION, "apps": apps})


@bp.get(f"{STATUS_PATH}/<app_id>")
def api_ha_app_status(app_id):
    cfg = load_config()
    refused = _refused(cfg)
    if refused:
        return refused
    apps = _apps(cfg)
    if app_id not in apps:
        return jsonify({"error": "no enabled app with that id"}), 404
    return jsonify(apps[app_id])
