"""Authenticated API for one fixed container and one fixed database directory."""
import hmac
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from plex_restore_agent.docker import AgentError, Docker
from plex_restore_agent.restore import RestoreManager, sync_directory


def create_app(manager, token):
    if len(token) < 32 or any(c.isspace() for c in token):
        raise AgentError("Agent token must contain at least 32 non-whitespace characters")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = manager.max_bytes

    @app.before_request
    def authenticate():
        supplied = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(supplied, ("Bearer " + token).encode()):
            return jsonify(error="Agent authentication failed"), 401

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(AgentError)
    def agent_error(error):
        return jsonify(error=str(error)), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.name), error.code

    @app.get("/v1/status")
    def status():
        return jsonify(manager.status())

    @app.get("/v1/health")
    def health():
        blocked = manager.recovery_blocked()
        return jsonify(ok=not blocked, recovery_required=blocked), 503 if blocked else 200

    @app.route("/v1/restores/<job_id>", methods=["GET", "PUT"])
    def restore(job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            return jsonify(error="Invalid restore ID"), 400
        existing = manager.read_job(job_id)
        if request.method == "GET":
            if existing is None:
                return jsonify(error="Restore not found"), 404
            return jsonify(existing)
        # Idempotency also covers the case where the upload response was lost.
        if existing is not None:
            return jsonify(existing), 200
        if request.mimetype != "application/zip":
            return jsonify(error="Expected application/zip"), 415
        if request.content_length is None:
            return jsonify(error="Content-Length is required"), 411
        if not 0 < request.content_length <= manager.max_bytes:
            return jsonify(error="Archive exceeds the configured upload limit"), 413
        plex_token = request.headers.get("X-Plex-Token", "")
        identity = request.headers.get("X-Plex-Machine-Identifier", "")
        if not plex_token or not identity or len(identity) > 200:
            return jsonify(error="Plex token and expected server identity are required"), 400
        if not manager.lock.acquire(blocking=False):
            return jsonify(error="Another restore is in progress"), 409
        handed_off = False
        job = None
        try:
            # Another request may have committed this ID before acquiring lock.
            existing = manager.read_job(job_id)
            if existing is not None:
                return jsonify(existing), 200
            if manager.recovery_blocked():
                return jsonify(error="An earlier restore needs recovery; inspect its status and agent state volume"), 409
            directory = manager.state_dir / job_id
            directory.mkdir(mode=0o700)
            sync_directory(manager.state_dir)
            job = {"id": job_id, "phase": "uploading"}
            manager.save(job)
            with (directory / "upload.zip").open("xb") as output:
                os.chmod(output.name, 0o600)
                received = 0
                while chunk := request.stream.read(1024 * 1024):
                    received += len(chunk)
                    if received > manager.max_bytes:
                        raise AgentError("Archive exceeds the configured upload limit")
                    output.write(chunk)
                if received != request.content_length:
                    raise AgentError("Archive upload was interrupted")
                output.flush()
                os.fsync(output.fileno())
            manager.save(job, "queued")
            # Snapshot response before worker mutates the same job object.
            response = jsonify(dict(job))
            thread = threading.Thread(target=manager.execute, args=(job, plex_token, identity), daemon=True)
            thread.start()
            handed_off = True
            return response, 202
        except Exception:
            if job:
                manager.save(job, "failed", error="Archive upload could not be completed; Plex was not changed")
                manager.cleanup(job)
            raise
        finally:
            if not handed_off:
                manager.lock.release()

    return app


def from_environment():
    os.umask(0o077)
    token_file = os.environ.get("AGENT_TOKEN_FILE", "/run/secrets/plex_restore_token")
    token = Path(token_file).read_text().strip()
    plex_url = os.environ["PLEX_URL"].rstrip("/")
    parsed = urlsplit(plex_url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.query or parsed.fragment or parsed.port == 0):
        raise AgentError("PLEX_URL must be a direct HTTP(S) server URL without credentials or a query")
    maximum = int(os.environ.get("MAX_ARCHIVE_BYTES", str(20 * 1024**3)))
    timeout = int(os.environ.get("PLEX_HEALTH_TIMEOUT", "180"))
    if maximum < 1024 * 1024 or not 10 <= timeout <= 900:
        raise AgentError("Invalid archive size limit or Plex health timeout")
    manager = RestoreManager(
        Docker(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")),
        container=os.environ["PLEX_CONTAINER"],
        agent_container=os.environ.get("AGENT_CONTAINER") or os.environ["HOSTNAME"],
        database_dir=os.environ.get("PLEX_DATABASE_DIR", "/plex-data/Plug-in Support/Databases"),
        container_database_dir=os.environ["PLEX_CONTAINER_DATABASE_DIR"],
        state_dir=os.environ.get("AGENT_STATE_DIR", "/state"),
        plex_url=plex_url, max_bytes=maximum, health_timeout=timeout,
    )
    # Waitress spools large HTTP bodies to disk. Use persistent disk space,
    # not a small container /tmp tmpfs or the Plex data directory.
    spool = manager.state_dir / "http-spool"
    spool.mkdir(mode=0o700, exist_ok=True)
    if spool.is_symlink():
        raise AgentError("HTTP spool directory must not be a symlink")
    import tempfile
    tempfile.tempdir = str(spool)
    # Refuse a mismatched/missing mount before recovering any journalled job.
    manager.validate_target()
    app = create_app(manager, token)
    manager.recover()
    return app
