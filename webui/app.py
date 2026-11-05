"""Web UI for Backuparr: configure apps/API keys, trigger backups, browse
history, and restore - all from the browser instead of editing
docker-compose env vars by hand. The routes live in the blueprint modules
next to this one; this module builds the app around them.
"""
import logging
import os
from datetime import timedelta

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

import backup
import manifest_backfill
from backup import describe_failure
from config_store import APP_META, ConfigError, DESTINATION_META, DISCOVERABLE_APPS
from webui import auth, backups, destinations, runs, services, settings
from webui.version import VERSION

app = Flask(__name__)
log = logging.getLogger("backuparr.webui")
for _blueprint in (auth.bp, settings.bp, services.bp, destinations.bp, runs.bp, backups.bp):
    app.register_blueprint(_blueprint)

# Off by default - the documented deployment is plain HTTP on a trusted
# LAN, where a Secure-only cookie would just break login. Set this if
# Backuparr sits behind your own TLS-terminating reverse proxy - it also
# makes the app trust that proxy's X-Forwarded-Proto/-For, so redirect
# URIs built for OAuth (Google Drive) come out https:// instead of the
# http:// this container actually listens on, and login-lockout tracking
# sees the real client IP instead of the proxy's.
_BEHIND_HTTPS_PROXY = os.environ.get("BACKUPARR_FORCE_HTTPS", "").lower() in ("1", "true", "yes")
if _BEHIND_HTTPS_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_for=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_BEHIND_HTTPS_PROXY,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    # Nothing sent here is large; the biggest is Backuparr's own backup,
    # uploaded as base64 to restore it (#111).
    MAX_CONTENT_LENGTH=64 * 1024 * 1024,
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
    # Once an app is enabled and saved, app.js groups the cards by Enabled.
    lead = ("prowlarr", *DISCOVERABLE_APPS)
    apps = sorted(APP_META, key=lambda m: lead.index(m["id"]) if m["id"] in lead else len(lead))
    return render_template("index.html", app_meta=apps, destination_meta=DESTINATION_META, version=VERSION,
                           auth_disabled=auth._AUTH_DISABLED, ui_meta={"apps": APP_META, "destinations": DESTINATION_META})


# ------------------------------------------------------------- startup ----
def create_app():
    """The app, ready to serve: logging set up, the session key loaded, the
    scheduler running and existing backups being added to the manifest
    (paused while a backup or restore runs). Importing this module does none of that; waitress
    calls this (entrypoint.sh), once."""
    backup.configure_logging()
    if auth._AUTH_DISABLED:
        log.warning("BACKUPARR_DISABLE_AUTH is set: local authentication is DISABLED. "
                    "Anyone who can reach this port has full access - make sure an "
                    "authenticating reverse proxy protects it and direct access is blocked.")
    app.secret_key = auth._load_or_create_secret_key()
    runs.start_scheduler()
    manifest_backfill.start(should_pause=runs.busy)
    return app


if __name__ == "__main__":
    create_app().run(
        host=os.environ.get("WEBUI_HOST") or "0.0.0.0",
        port=int(os.environ.get("WEBUI_PORT", 8990)),
        debug=False,
    )
