"""HTTPS on the Backuparr card (#177): the certificate in use, and the
HTTPS only switch, which closes plain HTTP straight away (webui/serve.py).

The switch can only be turned on from a page that came in on the HTTPS
port itself: that proves the port is published, so turning it on can't
lock anyone out. BACKUPARR_HTTPS_ONLY, when set, wins and locks the switch;
setting it to false reopens HTTP for anyone who did get locked out."""
from flask import Blueprint, jsonify, request

import tls
from config_store import load_config, save_config
from webui import serve

bp = Blueprint("https", __name__)


def _came_in_on_https():
    return request.environ.get(serve.LISTENER_KEY) == "https"


@bp.get("/api/https")
def api_https():
    cert = tls.reload()  # renews when due, and the server picks it up
    return jsonify({
        "port": serve.https_port(),
        "http_port": serve.http_port(),
        "own_certificate": cert.own,
        "certificate_error": cert.error,
        "fingerprint": cert.fingerprint,
        "expires": cert.expires.isoformat(timespec="seconds"),
        "https_only": serve.https_only(),
        "https_only_locked": serve.https_only_env() is not None,
        "on_https": _came_in_on_https(),
    })


@bp.post("/api/https/only")
def api_https_only():
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be true or false"}), 400
    if serve.https_only_env() is not None:
        return jsonify({"error": "BACKUPARR_HTTPS_ONLY sets this - change it there"}), 409
    if enabled and not _came_in_on_https():
        return jsonify({"error": f"open Backuparr over HTTPS on port {serve.https_port()} first, "
                                 "so this can't lock you out"}), 409
    cfg = load_config()
    cfg["https_only"] = enabled
    save_config(cfg)
    serve.set_http_open(not enabled)
    return jsonify({"ok": True, "https_only": enabled})
