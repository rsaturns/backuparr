"""A destination's backups: History (list, delete, download) and Restore."""
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile

from datetime import datetime, timezone

from flask import after_this_request, Blueprint, jsonify, request, send_file

import archive_crypto
import destination_util
import manifest
import rclone_util
import runlog
import restore_actions as ra
import restore_version as rv
from apps.http_headers import app_connection_config
from backup import humanize_error, log_failure
from config_store import DEST_NAMES, app_meta, instance_ids, instance_type, key_required, load_config, restore_supported
from webui import cookies
from webui.jobs import OwnedJobs
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


def _when(timestamp):
    """A listing's or a run's time, for sorting: rclone gives nanoseconds
    and a local offset, Backuparr's own times are UTC to the second."""
    text = re.sub(r"(\.\d{6})\d+", r"\1", (timestamp or "").replace("Z", "+00:00"))
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _add_runs(history, dest_id):
    """Links each backup to the run log that made it, and lists the runs
    that left no file on this destination - the backup failed, or its
    upload here did - with the reason. A run whose upload here worked but
    whose file is gone (this destination's retention, or a delete) isn't
    a failure, so it isn't listed."""
    for app_name, app_runs in runlog.all_runs().items():
        if not destination_util.is_backup_folder(app_name):  # an app, an instance or Backuparr's own
            continue
        entries = history.setdefault(app_name, [])
        listed = {e["name"]: e for e in entries}
        for run in app_runs:
            if run.get("file") in listed:
                listed[run["file"]]["run_id"] = run["id"]
                continue
            if dest_id not in run.get("destinations", {}):
                continue  # this destination wasn't on in that run
            result = run["destinations"][dest_id] or {}
            if result.get("ok"):
                continue
            entries.append({
                "name": run.get("file") or "", "size": None, "mod_time": run.get("finished") or run.get("started"),
                "app_version": run.get("app_version"), "recorded": True, "run_id": run["id"], "missing": True,
                "encrypted": archive_crypto.is_encrypted_name(run.get("file") or ""),
                "reason": result.get("error") or run.get("error") or "not uploaded",
            })


@bp.get("/api/history/<dest_id>")
def api_history(dest_id):
    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    # One recursive listing of Backuparr's folders instead of one rclone call
    # per app - much faster against remote destinations (Drive/OneDrive).
    # Keyed by instance id; a removed instance's backups are listed while any remain.
    # The app version comes from the manifest; "recorded" is False for a
    # backup the startup backfill hasn't reached yet.
    history = {name: [] for name in instance_ids(cfg)}
    try:
        entries = destination_util.list_backups(root)
    except rclone_util.RcloneError as exc:
        return jsonify({"error": humanize_error(exc)}), 502
    recorded = manifest.backups()
    for entry in entries:
        if entry.get("IsDir"):
            continue
        app_name, _, _ = entry["Path"].partition("/")
        if destination_util.is_backup_folder(app_name):
            details = recorded.get(manifest.key(app_name, entry["Name"]))
            history.setdefault(app_name, []).append({
                "name": entry["Name"], "size": entry["Size"], "mod_time": entry["ModTime"],
                "app_version": details.get("app_version") if details else None, "recorded": details is not None,
                "run_id": None, "encrypted": archive_crypto.is_encrypted_name(entry["Name"]),
            })
    _add_runs(history, dest_id)
    for entries in history.values():
        entries.sort(key=lambda e: _when(e["mod_time"]), reverse=True)
    return jsonify(history)


@bp.delete("/api/history/<dest_id>/<app_name>/<filename>")
def api_history_delete(dest_id, app_name, filename):
    if not destination_util.is_backup_folder(app_name):
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
    if manifest.get(app_name, filename) is None:
        runlog.forget_file(app_name, filename)  # gone from every destination
    return jsonify({"ok": True})


@bp.get("/api/history/<dest_id>/<app_name>/<filename>/download")
def api_history_download(dest_id, app_name, filename):
    if not destination_util.is_backup_folder(app_name):
        return jsonify({"error": "unknown app"}), 404
    if not ra.SAFE_FILENAME.match(filename):
        return jsonify({"error": "invalid filename"}), 400

    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    return send_backup(dest_id, root, app_name, filename)


def _decrypts_on_download(app_name, filename):
    """An app's encrypted backup is decrypted for the signed-in user who asks
    for it (#147). Backuparr's own stays encrypted: the setup screen restores
    it as it is."""
    return archive_crypto.is_encrypted_name(filename) and app_name != destination_util.SELF_BACKUP_FOLDER


class _NotThere(Exception):
    """A backup a download asked for that can't be had, and why, as
    (response body, status)."""


def _plain_copy(dest_id, root, app_name, filename, tmp_dir):
    """(path, name) of one backup ready to hand to the browser: the file
    where it is on Local storage, else a copy in tmp_dir; for an app's
    encrypted backup its decrypted copy in tmp_dir under the plain name.
    Raises _NotThere."""
    if dest_id == "local":
        # Never a link out of the folder.
        folder = os.path.realpath(os.path.join(root, app_name))
        path = os.path.realpath(os.path.join(folder, filename))
        if os.path.dirname(path) != folder or not os.path.isfile(path):
            raise _NotThere({"error": f"{filename} isn't on Local Storage any more"}, 404)
    else:
        path = os.path.join(tmp_dir, filename)
        try:
            rclone_util.copyto(f"{root}/{app_name}/{filename}", path)
        except rclone_util.RcloneError as exc:
            raise _NotThere({"error": humanize_error(exc)}, 500) from None
    if not _decrypts_on_download(app_name, filename):
        return path, filename
    plain = archive_crypto.plain_name(filename)
    plain_path = os.path.join(tmp_dir, f"plain-{app_name}-{plain}")
    try:
        archive_crypto.decrypt_file(path, plain_path)
    except archive_crypto.WrongKeyError:
        raise _NotThere({"error": f"{filename} was encrypted with a different encryption key. Decrypt it with that "
                                  "key using Backuparr's decrypt command."}, 409) from None
    except archive_crypto.ArchiveCryptoError as exc:
        raise _NotThere({"error": f"{filename}: {exc}"}, 409) from None
    finally:
        if dest_id != "local" and os.path.exists(path):
            os.remove(path)  # only the decrypted copy needs the space now
    return plain_path, plain


def send_backup(dest_id, root, app_name, filename):
    """The download response for one backup: the file itself, or for an
    app's encrypted backup its decrypted copy under the plain name. History
    and the Overview download through it, and so should anything else that
    hands a backup to the browser. app_name and filename must already be
    checked."""
    if dest_id == "local" and not _decrypts_on_download(app_name, filename):
        # Sent from where it is: a copy first would double a large backup's
        # disk use and hold up the download.
        try:
            path, name = _plain_copy(dest_id, root, app_name, filename, None)
        except _NotThere as exc:
            return jsonify(exc.args[0]), exc.args[1]
        return send_file(path, as_attachment=True, download_name=name)

    tmp_dir = tempfile.mkdtemp(prefix="backuparr-dl-")

    @after_this_request
    def _cleanup(response):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return response

    try:
        path, name = _plain_copy(dest_id, root, app_name, filename, tmp_dir)
    except _NotThere as exc:
        return jsonify(exc.args[0]), exc.args[1]
    return send_file(path, as_attachment=True, download_name=name)


# Several backups picked on History at once (#148).
MAX_BULK = 200
_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,120}")


def _bulk_backups(items):
    """[(app, file)] from a list of {"app", "file"} (or "app/file" strings),
    or None if any isn't one of Backuparr's backups."""
    if not isinstance(items, list) or len(items) > MAX_BULK:
        return None
    out = []
    for item in items:
        if isinstance(item, str):
            app_name, _, filename = item.partition("/")
        elif isinstance(item, dict):
            app_name, filename = item.get("app"), item.get("file")
        else:
            return None
        if not (isinstance(app_name, str) and isinstance(filename, str) and destination_util.is_backup_folder(app_name)
                and ra.SAFE_FILENAME.match(filename)):
            return None
        out.append((app_name, filename))
    return list(dict.fromkeys(out))


@bp.post("/api/history/<dest_id>/delete")
def api_history_bulk_delete(dest_id):
    """Deletes the backups and failed runs picked on History: {"backups":
    [{"app", "file"}], "runs": [{"app", "run_id"}]}. Returns what was
    deleted and what couldn't be, with why."""
    data = request.get_json(force=True, silent=True) or {}
    backups = _bulk_backups(data.get("backups", []))
    runs = data.get("runs", [])
    if backups is None or not isinstance(runs, list) or len(runs) > MAX_BULK or not all(
            isinstance(r, dict) and isinstance(r.get("app"), str) and destination_util.is_backup_folder(r["app"])
            and isinstance(r.get("run_id"), str) and _RUN_ID.fullmatch(r["run_id"]) for r in runs):
        return jsonify({"error": "pick backups and runs from the list"}), 400
    if not backups and not runs:
        return jsonify({"error": "nothing picked"}), 400
    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    deleted, failed = [], []
    for app_name, filename in backups:
        try:
            rclone_util.delete_file(f"{root}/{app_name}/{filename}")
        except rclone_util.RcloneError as exc:
            # Already gone (retention, or another browser) is what was wanted.
            if "object not found" not in (exc.detail or ""):
                failed.append({"app": app_name, "file": filename, "error": humanize_error(exc)})
                continue
        manifest.remove(app_name, filename, dest_id)
        if manifest.get(app_name, filename) is None:
            runlog.forget_file(app_name, filename)  # gone from every destination
        deleted.append({"app": app_name, "file": filename})
    for run in runs:
        if runlog.forget_run(run["app"], run["run_id"], dest_id):
            deleted.append({"app": run["app"], "run_id": run["run_id"]})
        else:
            failed.append({"app": run["app"], "run_id": run["run_id"], "error": "that run's log is already gone"})
    log.info("history: deleted %d item(s) on %s%s", len(deleted), dest_id,
             f", {len(failed)} couldn't be" if failed else "")
    return jsonify({"deleted": deleted, "failed": failed})


# Several backups as one zip (#164). The zip is built in the background, as
# a job the page polls for "Preparing 3 of 5...", then fetched once; one
# nobody fetches is removed after DOWNLOAD_TTL.
DOWNLOAD_TTL = 600
MAX_DOWNLOADS_PREPARING = 2


def _forget_download(job):
    shutil.rmtree(job["tmp_dir"], ignore_errors=True)


DOWNLOAD_JOBS = OwnedJobs(
    "backuparr_download", lambda job: job.get("finished", float("inf")) < time.monotonic() - DOWNLOAD_TTL,
    forget=_forget_download)


def _expire_downloads():
    with DOWNLOAD_JOBS.lock:
        DOWNLOAD_JOBS.purge_locked()


def _build_download(job_id, dest_id, root, backups):
    with DOWNLOAD_JOBS.lock:
        job = DOWNLOAD_JOBS.current_locked(job_id)
        tmp_dir = job["tmp_dir"]

    def update(**fields):
        with DOWNLOAD_JOBS.lock:
            current = DOWNLOAD_JOBS.current_locked(job_id)
            if current is not None:
                current.update(fields)

    outcome = {}
    try:
        bundle = os.path.join(tmp_dir, "bundle.zip")
        # Stored, not deflated: the archives are compressed already.
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for done, (app_name, filename) in enumerate(backups, 1):
                path, name = _plain_copy(dest_id, root, app_name, filename, tmp_dir)
                z.write(path, f"{app_name}/{name}")
                if path.startswith(tmp_dir):
                    os.remove(path)
                update(done=done)
        outcome = {"state": "ready", "path": bundle}
    except _NotThere as exc:
        outcome = {"state": "failed", "message": exc.args[0]["error"]}
    except Exception as exc:  # a full disk, mostly
        log_failure(log, "history: preparing a download", exc)
        outcome = {"state": "failed", "message": humanize_error(exc)}
    finally:
        update(**outcome, finished=time.monotonic())
        timer = threading.Timer(DOWNLOAD_TTL + 1, _expire_downloads)
        timer.daemon = True
        timer.start()


@bp.post("/api/history/<dest_id>/download")
def api_history_bulk_download(dest_id):
    """Starts preparing several backups as one zip ({"backups": [{"app",
    "file"}]}), each under <app>/ as a single download would give it: an
    app's encrypted backup decrypted, Backuparr's own as it is."""
    data = request.get_json(force=True, silent=True) or {}
    backups = _bulk_backups(data.get("backups", []))
    if not backups:
        return jsonify({"error": "pick the backups to download"}), 400
    cfg = load_config()
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return error
    owner, new_owner = DOWNLOAD_JOBS.owner()
    with DOWNLOAD_JOBS.lock:
        DOWNLOAD_JOBS.purge_locked()
        if sum(job["state"] == "preparing" for _, job in DOWNLOAD_JOBS.jobs.values()) >= MAX_DOWNLOADS_PREPARING:
            return jsonify({"error": "other downloads are still being prepared - try again when they're done"}), 429
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_id = DOWNLOAD_JOBS.add(owner, {"state": "preparing", "done": 0, "total": len(backups), "dest": dest_id,
                                       "tmp_dir": tempfile.mkdtemp(prefix="backuparr-dl-"),
                                       "name": f"backuparr-{dest_id}-{stamp}.zip"})
    threading.Thread(target=_build_download, args=(job_id, dest_id, root, backups), daemon=True).start()
    response = jsonify({"job_id": job_id, "total": len(backups)})
    DOWNLOAD_JOBS.set_cookie(response, new_owner, cookies.secure())
    return response, 202


def _my_download(dest_id, job_id):
    job = DOWNLOAD_JOBS.mine_locked(job_id)
    return job if job is not None and job["dest"] == dest_id else None


@bp.get("/api/history/<dest_id>/download/<job_id>")
def api_history_download_status(dest_id, job_id):
    with DOWNLOAD_JOBS.lock:
        job = _my_download(dest_id, job_id)
        if job is None:
            return jsonify({"error": "that download expired - pick the backups again"}), 404
        return jsonify({key: job[key] for key in ("state", "done", "total", "message") if key in job})


@bp.get("/api/history/<dest_id>/download/<job_id>/file")
def api_history_download_file(dest_id, job_id):
    """The prepared zip, once: it's removed from the server as it's sent."""
    with DOWNLOAD_JOBS.lock:
        job = _my_download(dest_id, job_id)
        if job is None:
            return jsonify({"error": "that download expired - pick the backups again"}), 404
        if job["state"] != "ready":
            return jsonify({"error": "the download isn't ready"}), 409
        del DOWNLOAD_JOBS.jobs[job_id]  # its files go after the response below
    response = send_file(job["path"], as_attachment=True, download_name=job["name"])
    shutil.rmtree(job["tmp_dir"], ignore_errors=True)  # open already, so it still sends
    return response


# ------------------------------------------------------------- restore ----
# Shares _start_tracked_run()/_run_tracked() with the backup run state above:
# a lock guarding one-at-a-time, a background thread, and live log output the
# UI polls for instead of blocking on one long request.


# Version checks passed on the Restore tab, for the apps restored while
# stopped: {(app, file, url): (time.monotonic(), result)}.
_VERSION_CHECKS = {}
_VERSION_CHECKS_LOCK = threading.Lock()
VERSION_CHECK_TTL = 6 * 3600


def _remember_check(app_name, file, app_cfg, result):
    with _VERSION_CHECKS_LOCK:
        now = time.monotonic()
        for key in [k for k, (at, _r) in _VERSION_CHECKS.items() if now - at > VERSION_CHECK_TTL]:
            del _VERSION_CHECKS[key]
        _VERSION_CHECKS[(app_name, file, app_cfg.get("url"))] = (now, result)


def _remembered_check(app_name, file, app_cfg):
    with _VERSION_CHECKS_LOCK:
        found = _VERSION_CHECKS.get((app_name, file, app_cfg.get("url")))
    if found and time.monotonic() - found[0] <= VERSION_CHECK_TTL:
        return found[1]
    return None


def _version_gate(app_name, file, app_cfg):
    """The restore's first step: the backup must come from the installed
    version. An app restored while stopped can't be asked, so it needs the
    check the Restore tab ran while it was still running."""
    log.info("restore: checking %s's version...", app_name)
    try:
        result = rv.check(app_name, file, app_cfg)
    except rv.RestoreVersionError:
        if instance_type(app_name) not in rv.STOPPED_FOR_RESTORE:
            raise
        try:
            rv.installed_version(app_name, app_cfg)
        except rv.RestoreVersionError:
            # Stopped, as it should be: use the check made before it was.
            result = _remembered_check(app_name, file, app_cfg)
            if result is None:
                label = rv.app_label(app_name)
                raise rv.RestoreVersionError(
                    f"{app_name}: {label}'s version hasn't been checked against this backup. Start {label}, pick the "
                    f"backup on the Restore tab so its version is checked, then stop {label} and restore.") from None
        else:
            raise  # It answered, so the mismatch is real.
    if result["status"] == "unknown":
        log.warning("restore: %s", result["message"])
    else:
        log.info("restore: %s", result["message"])
    return result


def _restore_work(app_name, root, app_cfg, data, bazarr_backup_dir):
    tmp_dir = None
    try:
        filename = data.get("file") or ra.latest_backup(root, app_name)
        RESTORE_RUN_STATE["file"] = filename
        _version_gate(app_name, filename, app_cfg)
        log.info("restore: fetching %s backup...", app_name)
        tmp_dir, local_zip, filename = ra.fetch_backup(root, app_name, filename)
        if rv.verify_checksum(app_name, filename, local_zip):
            log.info("restore: the download matches the backup's checksum")
        else:
            log.warning("restore: no checksum is recorded for this backup, so the download couldn't be verified")
        if archive_crypto.is_encrypted(local_zip):
            log.info("restore: decrypting the backup...")
        local_zip = ra.decrypted(tmp_dir, local_zip, _key(data))

        passwords = data.get("passwords", {})

        def password_prompt(name, _server):
            return passwords.get(name) or None

        log.info("restore: %s...", ra.restore_step(app_name))
        result = ra.restore_app(app_name, app_cfg, tmp_dir, local_zip, bazarr_backup_dir=bazarr_backup_dir,
                                sabnzbd_password_prompt=password_prompt)
        RESTORE_RUN_STATE["summary"] = result.get("summary")
        RESTORE_RUN_STATE["message"] = ra.restore_message(app_name, result)
        RESTORE_RUN_STATE["warning"] = bool(result.get("attention"))
        if RESTORE_RUN_STATE["warning"]:
            log.warning("restore: %s", RESTORE_RUN_STATE["message"])
        RESTORE_RUN_STATE["ok"] = True
    except Exception as exc:
        log_failure(log, f"restore failed for {app_name}", exc, app_cfg.get("url"), app=app_name)
        RESTORE_RUN_STATE["error"] = humanize_error(exc, app_cfg.get("url"))
        # The Restore tab then asks for the key the backup was made with.
        RESTORE_RUN_STATE["key_needed"] = isinstance(exc, archive_crypto.WrongKeyError)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _key(data):
    """The key a restore request gives for a backup made with another key
    (#147), or None for this install's. Never logged."""
    key = data.get("key")
    return key.strip() if isinstance(key, str) and key.strip() else None


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
            "warning": False,
            "error": None,
            "key_needed": False,
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
        config = ra.load_sabnzbd_config(tmp_dir, ra.decrypted(tmp_dir, local_zip, _key(data)))
        servers = ra.sabnzbd_server_summary(config)
        return jsonify({"file": filename, "servers": servers})
    except archive_crypto.WrongKeyError as exc:
        return jsonify({"error": humanize_error(exc), "key_needed": True}), 409
    except Exception as exc:
        return jsonify({"error": humanize_error(exc)}), 500
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@bp.post("/api/restore/<dest_id>/<app_name>")
def api_restore(dest_id, app_name):
    if not instance_type(app_name):
        return jsonify({"error": "unknown app"}), 404

    data = request.get_json(force=True, silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "confirm must be true"}), 400

    cfg, root, app_cfg, error = _restore_target(dest_id, app_name, data)
    if error:
        return error

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


@bp.post("/api/restore/<dest_id>/<app_name>/check")
def api_restore_check(dest_id, app_name):
    """The version check, run on the Restore tab when a backup is picked -
    for Emby, Plex, Seerr, LazyLibrarian and Vaultwarden while they're still
    running."""
    if not instance_type(app_name):
        return jsonify({"error": "unknown app"}), 404
    data = request.get_json(force=True, silent=True) or {}
    file = data.get("file")
    if not isinstance(file, str) or not ra.SAFE_FILENAME.match(file):
        return jsonify({"error": "invalid filename"}), 400
    _cfg, _root, app_cfg, error = _restore_target(dest_id, app_name, data)
    if error:
        return error
    # A refusal is the check's answer, not a failed request.
    try:
        result = rv.check(app_name, file, app_cfg)
    except rv.RestoreVersionError as exc:
        return jsonify({"status": "refused", "message": humanize_error(exc, app=app_name)})
    _remember_check(app_name, file, app_cfg, result)
    return jsonify(result)


def _restore_target(dest_id, app_name, data):
    """(cfg, destination root, app connection, None) for a restore or its
    check, with the request's one-off override applied, or
    (None, None, None, error response)."""
    if not restore_supported(app_name):
        return None, None, None, (jsonify({"error": f"{app_name} does not support automated restore - see its page in the docs"}), 400)
    cfg = load_config()
    if app_name not in cfg["apps"]:
        return None, None, None, (jsonify({"error": f"{app_name} is not configured"}), 400)
    root, error = _destination_root_or_error(cfg, dest_id)
    if error:
        return None, None, None, error
    app_cfg = app_connection_config(cfg, app_name)

    # One-off target override for this restore only - never written back to
    # config.json. Lets you point a restore at a throwaway/test instance
    # without touching the app's configured connection in Settings.
    override = data.get("override")
    if override and not isinstance(override, dict):
        return None, None, None, (jsonify({"error": "override must be an object"}), 400)
    if override:
        allowed = _restore_override_fields(app_name)
        app_cfg = dict(app_cfg)
        if override.get("url"):
            # The saved web login belongs to the configured app: never take
            # it to another one (a restore doesn't need it anyway).
            for field in (app_meta(app_name) or {}).get("extra_fields", []):
                if field.get("login_only"):
                    app_cfg[field["name"]] = ""
        for field in allowed:
            value = override.get(field)
            if value:
                app_cfg[field] = value

    if not app_cfg.get("url") or (key_required(app_name) and not app_cfg.get("api_key")):
        return None, None, None, (jsonify({"error": f"{app_name} is not configured"}), 400)
    missing = _missing_required_field(app_name, app_cfg)
    if missing:
        return None, None, None, (jsonify({"error": f"{app_name}: {missing} is not configured"}), 400)
    return cfg, root, app_cfg, None
