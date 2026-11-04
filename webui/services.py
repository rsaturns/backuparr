"""Plex sign-in and Prowlarr discovery: jobs a browser starts and polls."""
import logging
import threading
import time

from flask import Blueprint, current_app, jsonify, request

import discovery
from apps.http_headers import configure_app_headers, headers_for_app, RequestHeadersError, validate_request_headers
from apps.prowlarr import ProwlarrApp
from config_store import load_config
from webui.jobs import OwnedJobs
from webui.plex_auth import PlexAuthError, PlexLogin
from webui.settings import _header_scope_ids, _instances_after
from webui.version import VERSION


bp = Blueprint("services", __name__)
log = logging.getLogger("backuparr.webui")


# ------------------------------------------------------- Plex sign-in ----
PLEX_LOGINS = OwnedJobs("backuparr_plex_login", lambda login: time.monotonic() >= login.expires_at)
PLEX_START_LOCK = threading.Lock()
MAX_PLEX_LOGINS = 20


@bp.post("/api/plex/auth")
def api_plex_auth_start():
    # JSON-only, with no CORS allowance: a cross-origin form cannot start login.
    if not isinstance(request.get_json(silent=True), dict):
        return jsonify({"error": "A JSON request is required."}), 400
    if not PLEX_START_LOCK.acquire(blocking=False):
        return jsonify({"error": "Plex sign-in is starting. Try again in a moment."}), 429
    try:
        owner, new_owner = PLEX_LOGINS.owner()
        PLEX_LOGINS.discard_owner(owner)
        with PLEX_LOGINS.lock:
            PLEX_LOGINS.purge_locked()
            if len(PLEX_LOGINS.jobs) >= MAX_PLEX_LOGINS:
                return jsonify({"error": "Too many pending Plex sign-ins. Try again later."}), 429
        login = PlexLogin(VERSION)
        job_id = PLEX_LOGINS.add(owner, login)
        response = jsonify({"job_id": job_id, "auth_url": login.auth_url, "expires_in": login.expires_in})
        PLEX_LOGINS.set_cookie(response, new_owner, current_app.config["SESSION_COOKIE_SECURE"])
        return response, 201
    except PlexAuthError as exc:
        return jsonify({"error": str(exc)}), exc.status
    finally:
        PLEX_START_LOCK.release()


@bp.route("/api/plex/auth/<job_id>", methods=["GET", "DELETE"])
def api_plex_auth_status(job_id):
    with PLEX_LOGINS.lock:
        login = PLEX_LOGINS.mine_locked(job_id)
        if login is None:
            return jsonify({"error": "Plex sign-in expired or was cancelled. Click Get Plex token to try again."}), 404
        if request.method == "DELETE":
            del PLEX_LOGINS.jobs[job_id]
            return jsonify({"ok": True})
    try:
        result = login.poll()
        with PLEX_LOGINS.lock:
            if PLEX_LOGINS.current_locked(job_id) is not login:
                return jsonify({"error": "Plex sign-in was cancelled."}), 404
        return jsonify(result)
    except PlexAuthError as exc:
        return jsonify({"error": str(exc)}), exc.status


# Discovery results contain API keys: they live in memory for a few minutes,
# visible only to the browser that started the job, and are purged on the next
# discovery request.
DISCOVERY_RUN_LOCK = threading.Lock()
DISCOVERY_RESULT_TTL = 600
DISCOVERY_JOBS = OwnedJobs(
    "backuparr_discovery", lambda job: job.get("finished", float("inf")) < time.monotonic() - DISCOVERY_RESULT_TTL)


def _discovery_work(job_id, url, api_key, request_headers):
    def update(**fields):
        with DISCOVERY_JOBS.lock:
            job = DISCOVERY_JOBS.current_locked(job_id)
            if job is not None:
                job.update(fields)

    try:
        instance = ProwlarrApp(url, api_key, strict_redirects=True)
        configure_app_headers(instance, request_headers)
        result = discovery.discover_prowlarr(instance, lambda message: update(message=message))
        outcome = {"state": "completed", "message": "Discovery complete.", "result": result}
    except discovery.DiscoveryError as exc:
        outcome = {"state": "failed", "message": str(exc)}
    except Exception:
        # Remote error text can carry URLs or credentials, so it is never surfaced.
        outcome = {"state": "failed", "message": "Could not discover services. Check Prowlarr's URL, API key and that it is reachable from Backuparr."}
    finally:
        update(**outcome, finished=time.monotonic())
        DISCOVERY_RUN_LOCK.release()


@bp.post("/api/discovery/prowlarr")
def api_discovery_prowlarr():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not discovery.valid_service_url(data.get("url")):
        return jsonify({"error": "A valid Prowlarr URL starting with http:// or https:// is required."}), 400
    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        return jsonify({"error": "Prowlarr's API key is required."}), 400
    try:
        cfg = load_config()
        headers = data["request_headers"] if "request_headers" in data else cfg.get("request_headers", [])
        validate_request_headers(headers, _instances_after({}, cfg, unsaved=_header_scope_ids(headers)))
    except RequestHeadersError as exc:
        return jsonify({"error": str(exc)}), 400
    headers = headers_for_app(headers, "prowlarr")
    if not DISCOVERY_RUN_LOCK.acquire(blocking=False):
        return jsonify({"error": "Discovery is already running. Try again when it finishes."}), 409
    owner, new_owner = DISCOVERY_JOBS.owner()
    job_id = DISCOVERY_JOBS.add(owner, {"state": "running", "message": "Connecting to Prowlarr..."})
    try:
        threading.Thread(target=_discovery_work, args=(job_id, data["url"].strip(), api_key.strip(), headers), daemon=True).start()
    except RuntimeError:
        DISCOVERY_JOBS.discard(job_id)
        DISCOVERY_RUN_LOCK.release()
        return jsonify({"error": "Could not start discovery. Try again."}), 503
    response = jsonify({"job_id": job_id})
    DISCOVERY_JOBS.set_cookie(response, new_owner, current_app.config["SESSION_COOKIE_SECURE"])
    return response, 202


@bp.route("/api/discovery/prowlarr/<job_id>", methods=["GET", "DELETE"])
def api_discovery_status(job_id):
    with DISCOVERY_JOBS.lock:
        job = DISCOVERY_JOBS.mine_locked(job_id)
        if job is None:
            return jsonify({"error": "Discovery results expired. Run discovery again."}), 404
        if request.method == "DELETE":
            if job["state"] == "running":
                return jsonify({"error": "Discovery is still running."}), 409
            del DISCOVERY_JOBS.jobs[job_id]
            return jsonify({"ok": True})
        return jsonify({key: value for key, value in job.items() if key != "finished"})
