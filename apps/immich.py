"""Backup/restore driver for Immich's database (#154).

The database is what holds users, albums, people and faces, shared links
and settings; the photos and videos themselves are files, out of scope.
Immich's admin API has made database dumps since v2.5.0 (still marked
alpha there: server/src/controllers/database-backup.controller.ts and
maintenance.controller.ts, checked against v3.4.0):

BACKUP: POST /api/jobs {"name": "backup-database"} queues the job that
pg_dumps into the backups folder as
immich-db-backup-<YYYYMMDDTHHMMSS>-v<immich>-pg<postgres>.sql.gz (written
as .tmp, listed once renamed). GET /api/queues/backupDatabase says when the
job is done, or failed; GET /api/admin/database-backups lists the dumps and
GET /api/admin/database-backups/<name> downloads one. Backuparr then
deletes the dump it made (DELETE /api/admin/database-backups), so it
doesn't push Immich's own nightly dumps out of its "keep last" count.

RESTORE: POST /api/admin/database-backups/upload (multipart "file"; Immich
keeps it as uploaded-<name>), then POST /api/admin/maintenance
{"action": "restore_database", "restoreBackupFilename": ...}. Immich
restarts into maintenance mode, dumps a restore point, replays the dump
with psql --single-transaction, runs its migrations and a health check -
rolling back to the restore point if those fail - then ends maintenance
mode and restarts. GET /api/admin/maintenance/status follows it; the
immich_maintenance_token cookie the restore call sets shows the real error
text, and can end maintenance mode. A failed psql step changed nothing (one
transaction), so Backuparr ends maintenance mode then; any later failure
leaves Immich in maintenance mode for its admin to look at.

A new install with no admin yet has no API key to use: there POST
/api/admin/database-backups/start-restore (public until an admin exists)
puts Immich into maintenance mode and sets the same cookie, which uploads
and restores. The restored database brings back its own users and API
keys, so the card's key works again afterwards.

The API key must be an admin's, with job.create, queue.read, maintenance,
backup.download, backup.upload and backup.delete; Test connection names any
that are missing (Immich answers "Missing required permission: <name>"
before it looks at the request, so each check changes nothing). The version
recorded with a backup comes from GET /api/server/version, which is public.
"""
import gzip
import logging
import os
import re
import time
import zlib

import requests

from apps.base import AppError, poll_until, save_stream
from apps.multipart import MultipartFile
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

DUMP_FILE = "immich-database.sql.gz"
PERMISSIONS = ("job.create", "queue.read", "maintenance", "backup.download", "backup.upload", "backup.delete")
MIN_VERSION = (2, 5, 0)
COOKIE = "immich_maintenance_token"
_KEY_WHERE = "Account Settings > API Keys in Immich"
_PROBE = "backuparr-permission-check"
_MISSING = re.compile(r"Missing required permission: ([\w.]+)")
# Immich's own name for a dump its job makes (isValidDatabaseRoutineBackupName).
_ROUTINE_DUMP = re.compile(r"immich-db-backup-\d{8}T\d{6}-v[\w.+-]*-pg[\w.+-]*\.sql\.gz")
_SAFE_NAME = re.compile(r"[\w.-]+")
_TIMESTAMP = re.compile(r"_(\d{8}_\d{6})\.")
_MIB = 1024 * 1024
_PICKUP_S = 60  # the queued job not started (or its dump not listed) after this: it didn't run
_BACKUP_TIMEOUT_S = 2 * 60 * 60
_MAINTENANCE_START_S = 120
_RESTORE_TIMEOUT_S = 3 * 60 * 60  # the maintenance cookie lasts 4 hours
_BACK_UP_S = 300
_UPLOAD_TIMEOUT_S = 600
_DROPPED_POLLS = 3  # "start" this many polls in a row (0.5 s apart): the restore was dropped
# The maintenance status's restore tasks, for the log.
_TASKS = {"ready": "starting", "backup": "making Immich's restore point", "restore": "replaying the backup",
          "migrations": "running Immich's migrations", "rollback": "rolling back to the restore point"}


class ImmichError(AppError):
    pass


def check_dump(path):
    """Raises ImmichError unless path is a whole gzipped pg_dump: it must
    decompress (gzip checks its CRC and length at the end), start as a
    PostgreSQL dump and end with pg_dump's "dump complete" line."""
    head, tail = b"", b""
    try:
        with gzip.open(path, "rb") as f:
            while chunk := f.read(_MIB):
                if len(head) < 4096:
                    head += chunk[:4096 - len(head)]
                tail = (tail + chunk)[-4096:]
    except (OSError, EOFError, zlib.error):
        raise ImmichError("immich: the database backup is damaged - it doesn't decompress") from None
    if b"PostgreSQL database dump" not in head:
        raise ImmichError("immich: this isn't an Immich database backup (no PostgreSQL dump in it)")
    if b"PostgreSQL database dump complete" not in tail:
        raise ImmichError("immich: the database backup is incomplete - pg_dump didn't finish it")


def upload_name(path):
    """The name a backup is uploaded to Immich under. It must look like a
    dump to Immich (^[\\w.-]+\\.sql.gz) and hold no "-v<version>-": Immich
    takes a name with -v2.4 or older for a pg_dumpall cluster dump and
    restores it differently, and every dump Backuparr makes is a pg_dump."""
    match = _TIMESTAMP.search(os.path.basename(path))
    return f"backuparr-{match.group(1) if match else 'restore'}.sql.gz"


class ImmichApp:
    def __init__(self, url, api_key, timeout=60):
        # The address people copy may end in /api, the API's own root.
        self.url = url.rstrip("/").removesuffix("/api")
        self.api_key = api_key
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update({"Accept": "application/json"})

    def _api(self, path):
        return f"{self.url}/api{path}"

    def _request(self, method, path, key=True, **kwargs):
        """A call to Immich's API, with the card's key unless key=False (a
        new install's maintenance-mode calls go by cookie alone)."""
        headers = kwargs.pop("headers", {})
        if key:
            headers["x-api-key"] = self.api_key
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, self._api(path), headers=headers, **kwargs)

    @staticmethod
    def _message(res):
        try:
            message = res.json().get("message")
        except (ValueError, AttributeError):
            return ""
        return message if isinstance(message, str) else ""

    def _refused(self, res):
        """The ImmichError for a 401/403, or None. A missing permission is
        named; a key of a user who isn't an admin gets a plain "Forbidden"."""
        if res.status_code == 401:
            return ImmichError(f"immich: the API key was refused - check it ({_KEY_WHERE})")
        if res.status_code != 403:
            return None
        missing = _MISSING.search(self._message(res))
        if missing:
            return ImmichError(f"immich: the API key is missing the {missing.group(1)} permission - "
                               f"give it {', '.join(PERMISSIONS)}")
        return ImmichError("immich: the API key must belong to an admin user")

    def _call(self, method, path, **kwargs):
        res = self._request(method, path, **kwargs)
        refused = self._refused(res)
        if refused:
            raise refused
        if res.status_code == 404 and path.startswith("/admin/database-backups"):
            raise ImmichError("immich: this Immich has no database backup API - update it to 2.5.0 or newer")
        res.raise_for_status()
        return res

    # ------------------------------------------------------------- server --
    def _server_config(self):
        """GET /api/server/config: public; says whether Immich has its admin
        yet and whether it's in maintenance mode."""
        try:
            res = self.session.get(self._api("/server/config"), timeout=self.timeout)
            config = res.json() if res.ok else None
        except ValueError:
            config = None
        if not isinstance(config, dict):
            raise ImmichError("immich: no Immich server at this URL - check it (e.g. http://immich:2283)")
        return config

    def _version(self):
        res = self.session.get(self._api("/server/version"), timeout=self.timeout)
        try:
            body = res.json() if res.ok else {}
            version = tuple(int(body[part]) for part in ("major", "minor", "patch"))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ImmichError("immich: no Immich server at this URL - check it (e.g. http://immich:2283)") from None
        return version

    def app_version(self):
        """Immich's version, e.g. "3.4.0" (public, so it needs no permission)."""
        return ".".join(str(part) for part in self._version())

    def _ready(self, for_restore=False):
        """Refuses an Immich that can't back up (or restore) right now.
        Returns its server config."""
        version = self._version()
        if version < MIN_VERSION:
            raise ImmichError(f"immich: Immich {'.'.join(map(str, version))} has no database backup API - "
                              "update it to 2.5.0 or newer")
        config = self._server_config()
        if config.get("maintenanceMode"):
            raise ImmichError("immich: Immich is in maintenance mode - end it in Immich first")
        if not config.get("isInitialized") and not for_restore:
            raise ImmichError("immich: Immich isn't set up yet - create its admin account, then an API key")
        return config

    def _dumps(self, key=True):
        body = self._call("GET", "/admin/database-backups", key=key).json()
        backups = body.get("backups") if isinstance(body, dict) else None
        if not isinstance(backups, list):
            raise ImmichError("immich: the database backup list didn't come back as expected")
        return [b.get("filename") for b in backups if isinstance(b, dict) and isinstance(b.get("filename"), str)]

    def _queue(self):
        body = self._call("GET", "/queues/backupDatabase").json()
        stats = body.get("statistics") if isinstance(body, dict) else None
        if not isinstance(stats, dict):
            raise ImmichError("immich: the backup job's queue didn't come back as expected")
        return bool(body.get("isPaused")), stats

    def _delete(self, name, key=True):
        """Deletes a file Backuparr made in Immich's backups folder; a
        failure is logged, as the backup or restore itself worked."""
        try:
            self._call("DELETE", "/admin/database-backups", key=key, json={"backups": [name]})
            logger.info("immich: deleted %s from Immich's backups folder", name)
            return True
        except (ImmichError, requests.RequestException) as exc:
            logger.warning("immich: couldn't delete %s from Immich's backups folder (%s) - delete it there by hand",
                           name, str(exc).removeprefix("immich: ") if isinstance(exc, ImmichError) else type(exc).__name__)
            return False

    # ---------------------------------------------------------- permissions --
    def _missing_permissions(self):
        """Each permission's check, in PERMISSIONS order. Every one is
        refused before Immich reads the request, so a key that has the
        permission gets a 4xx about the request itself and nothing
        changes."""
        probe_file = {"file": (f"{_PROBE}.txt", b"", "text/plain")}
        checks = [
            ("job.create", "POST", "/jobs", {"json": {"name": _PROBE}}),
            ("queue.read", "GET", "/queues/backupDatabase", {}),
            ("maintenance", "GET", "/admin/database-backups", {}),
            ("backup.download", "GET", f"/admin/database-backups/{_PROBE}.sql", {}),
            ("backup.upload", "POST", "/admin/database-backups/upload", {"files": probe_file}),
            ("backup.delete", "DELETE", "/admin/database-backups", {"json": {"backups": [f"{_PROBE}.txt"]}}),
        ]
        missing = []
        for permission, method, path, kwargs in checks:
            res = self._request(method, path, **kwargs)
            if res.status_code == 403 and _MISSING.search(self._message(res)):
                missing.append(permission)
                continue
            # Anything else got past the permission check, even a 500
            # (Immich 2.5.0 answers a missing download's name with one).
            refused = self._refused(res)
            if refused:
                raise refused
        return missing

    def test_connection(self):
        self._ready()
        missing = self._missing_permissions()
        if missing:
            raise ImmichError(f"immich: the API key is missing the {', '.join(missing)} "
                              f"permission{'' if len(missing) == 1 else 's'} - give it {', '.join(PERMISSIONS)}")
        count = len(self._dumps())
        return (f"immich {self.app_version()} reachable, the API key can back up and restore, "
                f"{count} database backup{'' if count == 1 else 's'} in Immich")

    # -------------------------------------------------------------- backup --
    def _wait_for_dump(self, before, failed_before):
        started = time.monotonic()

        def done():
            paused, stats = self._queue()
            if paused:
                raise ImmichError("immich: Immich's Backup Database job queue is paused - resume it under "
                                  "Administration > Job Queues")
            if any(int(stats.get(state) or 0) for state in ("active", "waiting", "delayed")):
                return None
            new = [name for name in self._dumps() if name not in before and _ROUTINE_DUMP.fullmatch(name)]
            if new:
                return max(new)  # names sort by their timestamp
            if int(stats.get("failed") or 0) > failed_before:
                raise ImmichError("immich: Immich's database backup job failed - its log says why")
            if time.monotonic() - started > _PICKUP_S:
                raise ImmichError("immich: Immich didn't make a database backup - its log may say why")
            return None

        name = poll_until(done, _BACKUP_TIMEOUT_S, 1,
                          lambda waited: logger.info("immich: waiting for Immich's database backup (%ds)...", waited))
        if name is None:
            raise ImmichError("immich: Immich's database backup didn't finish within 2 hours")
        return name

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        self._ready()
        before = set(self._dumps())
        failed_before = int(self._queue()[1].get("failed") or 0)
        logger.info("immich: starting Immich's database backup job...")
        self._call("POST", "/jobs", json={"name": "backup-database"})
        name = self._wait_for_dump(before, failed_before)
        if not _SAFE_NAME.fullmatch(name):
            raise ImmichError("immich: Immich named its database backup unexpectedly")
        path = os.path.join(dest_dir, DUMP_FILE)
        with self._call("GET", f"/admin/database-backups/{name}", stream=True) as res:
            save_stream(res, path)
        check_dump(path)
        logger.info("immich: downloaded %s (%d bytes)", name, os.path.getsize(path))
        self._delete(name)
        return path

    # ------------------------------------------------------------- restore --
    def _status(self):
        """Maintenance status, or None while Immich is restarting. With the
        maintenance cookie it holds the real error text."""
        try:
            res = self.session.get(self._api("/admin/maintenance/status"), timeout=10)
            status = res.json() if res.ok else None
        except (requests.RequestException, ValueError):
            return None
        return status if isinstance(status, dict) else None

    def _wait_until(self, check, timeout_s, what=None, interval=1):
        """poll_until; what, if given, names the wait in a log line every 10 s."""
        on_wait = (lambda waited: logger.info("immich: waiting for Immich to %s (%ds)...", what, waited)) if what else None
        return poll_until(check, timeout_s, interval, on_wait) is not None

    def _serving(self):
        """True once Immich answers normally (out of maintenance mode)."""
        try:
            return True if not self._server_config().get("maintenanceMode") else None
        except (ImmichError, requests.RequestException):
            return None

    def _start_fresh_restore(self):
        """A new install has no admin, so no key: Immich's own "restore from
        backup" start puts it into maintenance mode and sets the cookie."""
        res = self._request("POST", "/admin/database-backups/start-restore", key=False)
        if res.status_code >= 400 or COOKIE not in self.session.cookies:
            raise ImmichError(f"immich: Immich refused to start a restore on this new install "
                              f"({self._message(res) or f'HTTP {res.status_code}'})")

        def in_maintenance():
            try:
                return True if self._server_config().get("maintenanceMode") else None
            except (ImmichError, requests.RequestException):
                return None

        if not self._wait_until(in_maintenance, _MAINTENANCE_START_S, "go into maintenance mode"):
            raise ImmichError("immich: Immich didn't go into maintenance mode within 2 minutes - check it's running")

    def _upload(self, path, name, key):
        body = MultipartFile(path, "file", name, "application/gzip")
        try:
            self._call("POST", "/admin/database-backups/upload", key=key, data=body,
                       headers={"Content-Type": body.content_type}, timeout=_UPLOAD_TIMEOUT_S)
        finally:
            body.close()

    @staticmethod
    def _error_detail(status):
        text = str(status.get("error") or "")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        detail = next((line for line in lines if line.startswith("ERROR:")), lines[0] if lines else "")
        return detail.removeprefix("Error: ")[:200]

    def _wait_for_restore(self):
        """Follows the restore until Immich is out of maintenance mode, its
        restore reports an error, or the restore stops without one. Returns
        what it saw: the tasks and the last one's progress, and the "error"
        status or "interrupted"."""
        seen = {"restoring": False, "tasks": [], "progress": None, "error": None, "interrupted": False}
        last_log = [0.0]
        dropped = [0]

        def check():
            status = self._status()
            if status is None:
                return None
            # Immich sets its maintenance action back to "start" as a restore
            # begins, so if it restarts mid-restore (a container restart, or
            # another Immich's restart event on a shared Redis) it comes back
            # in maintenance mode, the restore dropped. Nothing else shows
            # "start" here: a restore is refused while Immich is in maintenance
            # mode, and a new install waits in "select_database_restore". A
            # few polls in a row, so a worker that's still starting isn't
            # taken for one.
            if status.get("active") and status.get("action") == "start":
                dropped[0] += 1
                if dropped[0] >= _DROPPED_POLLS:
                    seen["interrupted"] = True
                    return True
                return None
            dropped[0] = 0
            if status.get("active") and status.get("action") != "restore_database":
                return None  # a new install's select_database_restore, before it starts
            if status.get("active"):
                seen["restoring"] = True
                task = status.get("task")
                if task == "error":
                    seen["error"] = status
                    return True
                if task and task not in seen["tasks"]:
                    seen["tasks"].append(task)
                if isinstance(status.get("progress"), (int, float)):
                    seen["progress"] = status["progress"]
                if time.monotonic() - last_log[0] >= 10:
                    last_log[0] = time.monotonic()
                    progress = status.get("progress")
                    logger.info("immich: restoring - %s%s", _TASKS.get(task, task or "starting"),
                                f" ({round(progress * 100)}%)" if isinstance(progress, (int, float)) else "")
                return None
            return True if seen["restoring"] and not status.get("active") else None

        deadline = time.monotonic() + _MAINTENANCE_START_S

        def started_then_finished():
            if not seen["restoring"] and not seen["interrupted"] and time.monotonic() > deadline:
                raise ImmichError("immich: Immich didn't start the restore within 2 minutes - check it's running")
            return check()

        if not self._wait_until(started_then_finished, _RESTORE_TIMEOUT_S, interval=0.5):
            raise ImmichError("immich: Immich's restore didn't finish within 3 hours - it may still be running")
        return seen

    def _end_maintenance(self, uploaded):
        """Takes Immich out of maintenance mode with the restore's cookie and
        deletes the uploaded copy. True once Immich answers normally."""
        if COOKIE not in self.session.cookies:
            return False
        self._delete(uploaded, key=False)
        ended = self._request("POST", "/admin/maintenance", key=False, json={"action": "end"})
        return ended.ok and self._wait_until(self._serving, _BACK_UP_S, "end maintenance mode")

    def _failed(self, seen, uploaded):
        """The restore failed or was dropped, and Immich is still in
        maintenance mode. The dump replays in one transaction, so a failure
        in that step - or before it - left the database as it was: Immich is
        taken out of maintenance mode then. Later (migrations, Immich's
        checks, its rollback), it's left for its admin."""
        later = bool({"migrations", "rollback"} & set(seen["tasks"]))
        if seen["error"]:
            detail = self._error_detail(seen["error"])
            unchanged = "psql" in str(seen["error"].get("error") or "") and not later
            what = f"Immich couldn't restore the backup ({detail or 'see its log'})"
        else:
            # Stopped without an error: unchanged only if the restore was seen
            # no further than the replay, short of its commit.
            replayed = seen["tasks"][-1:] == ["restore"] and (seen["progress"] or 0) >= 0.99
            unchanged = bool(seen["tasks"]) and not later and not replayed
            what = "Immich restarted in the middle of the restore"
        if unchanged and self._end_maintenance(uploaded):
            raise ImmichError(f"immich: {what}. Its database wasn't changed, and Immich is running again")
        raise ImmichError(f"immich: {what} and is still in maintenance mode. Its log explains it and has the link "
                          "to its maintenance page")

    def restore_from_file(self, path):
        """Restores a database backup. Returns {"fresh": restored onto a new
        install, "key_ok": the card's API key works on the restored
        database, "cleaned": the uploaded copy was deleted}."""
        check_dump(path)
        config = self._ready(for_restore=True)
        fresh = not config.get("isInitialized")
        name = upload_name(path)
        uploaded = f"uploaded-{name}"
        if fresh:
            logger.info("immich: Immich has no admin yet - restoring through its new-install restore")
            self._start_fresh_restore()
        logger.info("immich: uploading the backup to Immich...")
        self._upload(path, name, key=not fresh)
        res = self._call("POST", "/admin/maintenance", key=not fresh,
                         json={"action": "restore_database", "restoreBackupFilename": uploaded})
        if COOKIE not in self.session.cookies:
            logger.debug("immich: no maintenance cookie came back (HTTP %s)", res.status_code)
        seen = self._wait_for_restore()
        if seen["error"] or seen["interrupted"]:
            self._failed(seen, uploaded)
        if not self._wait_until(self._serving, _BACK_UP_S, "restart"):
            raise ImmichError("immich: the restore finished, but Immich didn't come back within 5 minutes - check it's running")
        self.session.cookies.clear()
        try:
            key_ok = self._request("GET", "/admin/database-backups").status_code != 401
        except requests.RequestException:
            key_ok = False
        cleaned = key_ok and self._delete(uploaded)
        return {"fresh": fresh, "key_ok": key_ok, "cleaned": cleaned}
