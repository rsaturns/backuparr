"""Destinations: their test, Google Drive's OAuth flow, and connecting or
disconnecting OneDrive and Dropbox."""
import hmac
import logging
import os
import secrets
import threading
import time
import urllib.parse

from flask import Blueprint, jsonify, redirect, request, session

import destination_util
import dropbox_oauth
import gdrive_oauth
import onedrive_oauth
import rclone_token
import rclone_util
from backup import humanize_error, log_failure
from config_store import DEFAULT_DEST, DEST_EDITABLE_FIELDS, DEST_NAMES, editing_config, load_config


bp = Blueprint("destinations", __name__)
log = logging.getLogger("backuparr.webui")


# CSRF state for the OAuth callback: state -> time.monotonic() it was
# issued, so each is used once and expires. Requests run on several threads
# (webui/wsgi_asgi.py), so it's only touched under its lock. The browser
# that started the sign-in keeps it in its session too (its last few, for a
# second click on Connect): the callback must come back to that browser.
_OAUTH_SESSION_KEY = "gdrive_oauth_states"
_OAUTH_SESSION_KEEP = 3
_OAUTH_STATE = {}
_OAUTH_STATE_LOCK = threading.Lock()
_OAUTH_STATE_TTL = 600

# --------------------------------------------------------- destinations ----
def _not_connected_message(dest_id, request_data):
    """Test only checks the saved connection, so a token that's pasted but not
    yet connected (the UI sends token_pasted) gets a pointer to Connect."""
    if dest_id == "gdrive":
        return "Not connected yet - click Connect Google Drive first"
    if request_data.get("token_pasted"):
        label = {"onedrive": "OneDrive", "dropbox": "Dropbox"}[dest_id]
        return f"The pasted token isn't used until you click Connect {label} - connect first, then test"
    return f"Not connected yet - paste a token from `rclone authorize {dest_id}` first"


@bp.post("/api/test-destination/<dest_id>")
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

        remote = destination_util.REMOTES[dest_id]
        if not dest_cfg.get(remote.TOKEN_FIELD):
            return jsonify({"ok": False, "message": _not_connected_message(dest_id, data)})
        cfg["destinations"][dest_id] = dest_cfg
        destination_util.sync(cfg)
        rclone_util.check_remote(destination_util.remote_root(dest_id, dest_cfg))
        return jsonify({"ok": True, "message": f"connected, backing up to {remote.backup_target(dest_cfg)}"})
    except (rclone_util.RcloneError, destination_util.DestinationError, OSError) as exc:
        return jsonify({"ok": False, "message": humanize_error(exc)})


# ------------------------------------------------------- gdrive oauth ----
def _gdrive_redirect_uri():
    return request.host_url.rstrip("/") + "/api/destinations/gdrive/oauth/callback"


def _redirect_with_error(message):
    return redirect("/?gdrive_error=" + urllib.parse.quote(str(message)))


def _oauth_state_new():
    now = time.monotonic()
    state = secrets.token_urlsafe(24)
    with _OAUTH_STATE_LOCK:
        for s, ts in list(_OAUTH_STATE.items()):
            if now - ts > _OAUTH_STATE_TTL:
                del _OAUTH_STATE[s]
        _OAUTH_STATE[state] = now
    mine = session.get(_OAUTH_SESSION_KEY)
    session[_OAUTH_SESSION_KEY] = [*(mine if isinstance(mine, list) else [])][-(_OAUTH_SESSION_KEEP - 1):] + [state]
    return state


def _oauth_state_consume(state):
    mine = session.get(_OAUTH_SESSION_KEY)
    mine = [s for s in mine if isinstance(s, str)] if isinstance(mine, list) else []
    started_here = any(hmac.compare_digest(s.encode("utf-8"), state.encode("utf-8")) for s in mine)
    if started_here:
        session[_OAUTH_SESSION_KEY] = [s for s in mine if s != state]
    with _OAUTH_STATE_LOCK:
        ts = _OAUTH_STATE.pop(state, None)
    return started_here and ts is not None and (time.monotonic() - ts) <= _OAUTH_STATE_TTL


@bp.get("/api/destinations/gdrive/oauth/start")
def api_gdrive_oauth_start():
    cfg = load_config()
    gdrive_cfg = cfg["destinations"]["gdrive"]
    if not gdrive_cfg.get("client_id") or not gdrive_cfg.get("client_secret"):
        return _redirect_with_error("Save a Client ID and Client Secret first")
    state = _oauth_state_new()
    url = gdrive_oauth.build_auth_url(gdrive_cfg["client_id"], _gdrive_redirect_uri(), state)
    return redirect(url)


@bp.get("/api/destinations/gdrive/oauth/callback")
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

    with editing_config() as cfg:  # the exchange took a while: change the config as it is now
        cfg["destinations"]["gdrive"]["refresh_token"] = tokens["refresh_token"]
        cfg["destinations"]["gdrive"]["enabled"] = True
    destination_util.sync(cfg)
    return redirect("/?gdrive=connected")


@bp.post("/api/destinations/gdrive/access-token")
def api_gdrive_access_token():
    cfg = load_config()
    try:
        token = gdrive_oauth.get_access_token(cfg["destinations"]["gdrive"])
        return jsonify({"access_token": token})
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 400


@bp.post("/api/destinations/gdrive/folder")
def api_gdrive_folder():
    data = request.get_json(force=True, silent=True) or {}
    folder_id = data.get("folder_id", "")
    folder_name = data.get("folder_name", "")
    if not folder_id:
        return jsonify({"error": "folder_id is required"}), 400

    if not load_config()["destinations"]["gdrive"].get("refresh_token"):
        return jsonify({"error": "Google Drive is not connected"}), 400
    with editing_config() as cfg:
        gdrive_cfg = cfg["destinations"]["gdrive"]
        gdrive_cfg["folder_id"] = folder_id
        gdrive_cfg["folder_name"] = folder_name
    destination_util.sync(cfg)
    return jsonify({"ok": True})


@bp.post("/api/destinations/<dest_id>/disconnect")
def api_destination_disconnect(dest_id):
    """Forgets a connected destination's account; what the user entered
    themselves (Google Drive's OAuth client) stays."""
    if dest_id not in destination_util.REMOTES:
        return jsonify({"error": "unknown destination"}), 404
    kept = DEST_EDITABLE_FIELDS[dest_id] - {"enabled"}
    with editing_config() as cfg:
        cfg["destinations"][dest_id].update(
            {key: value for key, value in DEFAULT_DEST[dest_id].items() if key not in kept}, enabled=False)
    destination_util.sync(cfg)
    return jsonify({"ok": True})


# --------------------------------------------------------- onedrive ----
@bp.post("/api/destinations/onedrive/connect")
def api_onedrive_connect():
    """Validates the pasted `rclone authorize onedrive` token and resolves
    the app folder's drive_id/drive_type via one Graph API call."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        token_json, access_token = rclone_token.parse_token_blob(data.get("token_blob", ""), "onedrive")
        approot = onedrive_oauth.approot_metadata(access_token)
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 400

    with editing_config() as cfg:
        onedrive_cfg = cfg["destinations"]["onedrive"]
        onedrive_cfg["token"] = token_json
        onedrive_cfg["drive_id"] = approot["parentReference"]["driveId"]
        onedrive_cfg["drive_type"] = approot["parentReference"]["driveType"]
        onedrive_cfg["item_id"] = approot["id"]
        onedrive_cfg["enabled"] = True
    # force=True: fresh token should win over whatever's already stored.
    onedrive_oauth.sync_rclone_remote(onedrive_cfg, force=True)
    return jsonify({"ok": True})


# ---------------------------------------------------------- dropbox ----
@bp.post("/api/destinations/dropbox/connect")
def api_dropbox_connect():
    """Validates the pasted `rclone authorize dropbox` token against
    Dropbox before saving it."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        token_json, access_token = rclone_token.parse_token_blob(data.get("token_blob", ""), "dropbox")
        dropbox_oauth.verify_access_token(access_token)
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 400

    with editing_config() as cfg:
        dropbox_cfg = cfg["destinations"]["dropbox"]
        dropbox_cfg["token"] = token_json
        dropbox_cfg["enabled"] = True
    # force=True: fresh token should win over whatever's already stored.
    dropbox_oauth.sync_rclone_remote(dropbox_cfg, force=True)
    return jsonify({"ok": True})
