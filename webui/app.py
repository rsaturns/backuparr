"""Web UI for Backuparr: configure apps/API keys, trigger backups, browse
history, and restore - all from the browser instead of editing
docker-compose env vars by hand. The routes live in the blueprint modules
next to this one; this module builds the app around them.
"""
import logging
import os
from datetime import timedelta

from flask import Flask, jsonify, render_template, request
from flask.sessions import SecureCookieSessionInterface

import backup
import manifest_backfill
from backup import describe_failure
from config_store import APP_CATEGORIES, APP_META, ConfigError, DESTINATION_META, DISCOVERABLE_APPS, SELF_APP
from webui import auth, backups, cookies, destinations, ha, https, runs, services, settings
from webui.version import VERSION

app = Flask(__name__)
log = logging.getLogger("backuparr.webui")
for _blueprint in (auth.bp, settings.bp, services.bp, destinations.bp, runs.bp, backups.bp, ha.bp, https.bp):
    app.register_blueprint(_blueprint)

# The session cookie is Secure over HTTPS, and named per scheme so signing
# in over HTTP after HTTPS still works (webui/cookies.py, #177). Behind your
# own TLS reverse proxy (BACKUPARR_FORCE_HTTPS) it's always Secure; the
# proxy's X-Forwarded-* headers are applied by webui/forwarded.py, which
# the server (webui/serve.py) puts in front of this app.
class _SessionInterface(SecureCookieSessionInterface):
    def get_cookie_name(self, app):
        return cookies.name(super().get_cookie_name(app))

    def get_cookie_secure(self, app):
        return cookies.secure()


app.session_interface = _SessionInterface()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=cookies.BEHIND_HTTPS_PROXY,
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
    # Settings lists the apps by category (#148). Within one, Prowlarr and the
    # apps it discovers lead, side by side, until an app is enabled and saved;
    # app.js then puts the enabled ones first.
    lead = ("prowlarr", *DISCOVERABLE_APPS)
    apps = sorted(APP_META, key=lambda m: lead.index(m["id"]) if m["id"] in lead else len(lead))
    categories = [{**c, "apps": [m for m in apps if m["category"] == c["id"]]} for c in APP_CATEGORIES]
    # Backuparr's own card (#165) heads its category, which shows even with
    # no other app in it.
    shown = [c for c in categories if c["apps"] or c["id"] == SELF_APP["category"]]
    return render_template("index.html", app_categories=shown, self_app=SELF_APP,
                           destination_meta=DESTINATION_META, version=VERSION,
                           auth_disabled=auth._AUTH_DISABLED, username=auth.auth_store.username() or "",
                           ui_meta={"apps": APP_META, "categories": APP_CATEGORIES, "destinations": DESTINATION_META,
                                    "self_app": SELF_APP})


# ------------------------------------------------------------- startup ----
def create_app():
    """The app, ready to serve: logging set up, the session key loaded, the
    scheduler running and existing backups being added to the manifest
    (paused while a backup or restore runs). Importing this module does none of that; the
    server (webui/serve.py) calls this, once."""
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
