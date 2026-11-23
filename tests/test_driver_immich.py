"""Immich (#154): the database driver against a fake Immich API that keeps
its backups folder and maintenance mode the way Immich v3.4.0 does."""
import gzip
import http.client
import io
import json
import re

import pytest
import requests

import backup
import restore_actions
from apps import base, immich
from apps.immich import ImmichApp, ImmichError, upload_name
from config_store import always_encrypted, app_meta, restore_supported

KEY = "immich-api-key"
DUMP = gzip.compress(b"--\n-- PostgreSQL database dump\n--\n\nCREATE TABLE t ();\n\n--\n-- PostgreSQL database dump complete\n--\n")
NIGHTLY = "immich-db-backup-20261110T020000-v3.4.0-pg14.19.sql.gz"
NEW = "immich-db-backup-20261111T172224-v3.4.0-pg14.19.sql.gz"
ALL = list(immich.PERMISSIONS)


class _Raw(io.BytesIO):
    """A response body that also carries headers, as requests reads cookies from them."""

    def __init__(self, content, cookie=None):
        super().__init__(content)
        msg = http.client.HTTPMessage()
        if cookie:
            msg["Set-Cookie"] = cookie
        self._original_response = type("Original", (), {"msg": msg})()


class FakeImmich:
    def __init__(self):
        self.sent = []
        self.version = {"major": 3, "minor": 4, "patch": 0, "prerelease": None}
        self.initialized = True
        self.maintenance = False
        self.permissions = set(ALL)
        self.admin = True
        self.files = {NIGHTLY}
        self.paused = False
        self.failed = 0
        self.job_makes = NEW  # None: the job fails; "": it never runs
        self.queued = 0
        self.restore_statuses = []  # statuses the maintenance status returns in turn
        self.after_restore_key_ok = True
        self.uploads = {}
        self.key_valid = True
        self.missing_download = 404  # Immich 2.5.0 answers 500

    # Each handler returns (status, body[, set-cookie]).
    def handle(self, request):
        method, path = request.method, request.path_url.split("?")[0]
        assert path.startswith("/api/")
        path = path[len("/api"):]
        public = {"/server/version", "/server/config", "/admin/maintenance/status"}
        if path == "/server/version":
            return 200, self.version
        if path == "/server/config":
            return 200, {"isInitialized": self.initialized, "maintenanceMode": self.maintenance}
        if path == "/admin/maintenance/status":
            if self.restore_statuses:
                status = self.restore_statuses.pop(0)
                if status is None:
                    raise requests.ConnectionError("restarting")
                if not status.get("active"):
                    self.maintenance = False
                    self.key_valid = self.after_restore_key_ok
                return 200, status
            return 200, {"active": self.maintenance, "action": "end"}
        if path == "/admin/database-backups/start-restore":
            if self.initialized:
                return 403, {"message": "Forbidden"}
            self.maintenance = True
            return 201, b"", "immich_maintenance_token=jwt-token; Path=/; HttpOnly"
        if path not in public:
            cookie = request.headers.get("Cookie", "")
            if self.maintenance:
                if "immich_maintenance_token=jwt-token" not in cookie:
                    return 401, {"message": "Missing JWT Token"}
            else:
                denied = self._authenticate(request, path, method)
                if denied:
                    return denied
        return self._route(method, path, request)

    def _authenticate(self, request, path, method):
        if request.headers.get("x-api-key") != KEY or not self.key_valid:
            return 401, {"message": "Invalid API key"}
        if not self.admin:
            return 403, {"message": "Forbidden"}
        needed = {("POST", "/jobs"): "job.create", ("GET", "/queues/backupDatabase"): "queue.read",
                  ("GET", "/admin/database-backups"): "maintenance", ("POST", "/admin/maintenance"): "maintenance",
                  ("POST", "/admin/database-backups/upload"): "backup.upload",
                  ("DELETE", "/admin/database-backups"): "backup.delete"}.get((method, path))
        if needed is None and method == "GET" and path.startswith("/admin/database-backups/"):
            needed = "backup.download"
        if needed and needed not in self.permissions:
            return 403, {"message": f"Missing required permission: {needed}"}
        return None

    def _route(self, method, path, request):
        if (method, path) == ("POST", "/jobs"):
            if json.loads(request.body).get("name") != "backup-database":
                return 400, {"message": "Validation failed"}
            self.queued += 1
            if self.job_makes:
                self.files.add(self.job_makes)
            elif self.job_makes is None:
                self.failed += 1
            return 204, b""
        if (method, path) == ("GET", "/queues/backupDatabase"):
            return 200, {"name": "backupDatabase", "isPaused": self.paused,
                         "statistics": {"active": 0, "completed": 0, "failed": self.failed, "delayed": 0,
                                        "waiting": 0, "paused": 0}}
        if (method, path) == ("GET", "/admin/database-backups"):
            return 200, {"backups": [{"filename": f, "filesize": 1, "timezone": "UTC"} for f in sorted(self.files)]}
        if method == "GET" and path.startswith("/admin/database-backups/"):
            name = path.rsplit("/", 1)[1]
            return (200, DUMP) if name in self.files else (self.missing_download, {"message": "Not Found"})
        if (method, path) == ("DELETE", "/admin/database-backups"):
            names = json.loads(request.body)["backups"]
            if not all(re.fullmatch(r"[\w.-]+\.sql(\.gz)?", n) for n in names):
                return 400, {"message": "Invalid backup name!"}
            self.files -= set(names)
            return 200, b""
        if (method, path) == ("POST", "/admin/database-backups/upload"):
            body = request.body if isinstance(request.body, bytes) else request.body.read()
            name = re.search(rb'filename="([^"]+)"', body).group(1).decode()
            if not re.fullmatch(r"[\w.-]+\.sql(\.gz)?", name):
                return 400, {"message": "Invalid backup name!"}
            self.uploads[name] = (body, request.headers)
            self.files.add(f"uploaded-{name}")
            return 201, b""
        if (method, path) == ("POST", "/admin/maintenance"):
            action = json.loads(request.body)
            if action["action"] == "end":
                self.maintenance = False
                return 201, b""
            assert action == {"action": "restore_database", "restoreBackupFilename": action["restoreBackupFilename"]}
            self.restoring = action["restoreBackupFilename"]
            self.maintenance = True
            return 201, b"", "immich_maintenance_token=jwt-token; Path=/; HttpOnly"
        return 404, {"message": "Not Found"}


@pytest.fixture
def server(monkeypatch):
    fake = FakeImmich()

    def send(_adapter, request, **kwargs):
        fake.sent.append(request)
        answer = fake.handle(request)
        status, body = answer[0], answer[1]
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        res.raw = _Raw(content, answer[2] if len(answer) > 2 else None)
        res.headers["Content-Type"] = "application/json"
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    return fake


@pytest.fixture
def app():
    return ImmichApp("http://immich:2283/api/", KEY)


def paths(server):
    return [f"{r.method} {r.path_url}" for r in server.sent]


def test_the_card_is_an_optional_encrypted_multi_instance_media_server():
    built = backup.build_app("immich-family", {"url": "http://immich:2283", "api_key": KEY})
    assert isinstance(built, ImmichApp) and built.url == "http://immich:2283"
    meta = app_meta("immich")
    assert meta["category"] == "media-servers" and meta["multi_instance"] and not meta["extra_fields"]
    assert not always_encrypted("immich") and restore_supported("immich")


def test_connection_checks_every_permission_without_changing_anything(app, server):
    assert app.test_connection() == "immich 3.4.0 reachable, the API key can back up and restore, 1 database backup in Immich"
    assert server.queued == 0 and server.files == {NIGHTLY} and not server.uploads
    keyed = [r for r in server.sent if r.headers.get("x-api-key")]
    assert keyed and all(KEY not in r.url for r in server.sent)
    assert not any(r.headers.get("x-api-key") for r in server.sent if r.path_url == "/api/server/version")


def test_a_500_from_immich_2_5_0_still_means_the_permission_is_there(app, server):
    server.missing_download = 500
    assert app.test_connection().startswith("immich 3.4.0 reachable")


def test_connection_names_every_missing_permission(app, server):
    server.permissions = {"maintenance", "backup.download"}
    with pytest.raises(ImmichError) as error:
        app.test_connection()
    assert str(error.value) == ("immich: the API key is missing the job.create, queue.read, backup.upload, backup.delete "
                                "permissions - give it " + ", ".join(ALL))


@pytest.mark.parametrize("change, fragment", [
    (lambda s: setattr(s, "admin", False), "must belong to an admin user"),
    (lambda s: setattr(s, "maintenance", True), "in maintenance mode - end it in Immich first"),
    (lambda s: setattr(s, "initialized", False), "isn't set up yet"),
    (lambda s: setattr(s, "version", {"major": 2, "minor": 4, "patch": 1}), "Immich 2.4.1 has no database backup API"),
])
def test_connection_says_what_to_fix(app, server, change, fragment):
    change(server)
    with pytest.raises(ImmichError, match=re.escape(fragment)):
        app.test_connection()


def test_a_wrong_key_is_refused(server):
    with pytest.raises(ImmichError, match="the API key was refused"):
        ImmichApp("http://immich:2283", "wrong").test_connection()


def test_backup_runs_the_job_downloads_the_new_dump_and_deletes_it(app, server, tmp_path):
    path = app.backup(str(tmp_path))
    assert open(path, "rb").read() == DUMP and backup.archive_suffix(path) == ".sql.gz"
    assert server.queued == 1 and server.files == {NIGHTLY}  # Immich's own dump is left alone
    assert f"GET /api/admin/database-backups/{NEW}" in paths(server)
    assert app.app_version() == "3.4.0"


def test_a_backup_that_cant_delete_its_dump_still_succeeds(app, server, tmp_path, caplog):
    server.permissions.discard("backup.delete")
    assert open(app.backup(str(tmp_path)), "rb").read() == DUMP
    assert NEW in server.files and f"couldn't delete {NEW}" in caplog.text


@pytest.mark.parametrize("change, fragment", [
    (lambda s: setattr(s, "paused", True), "job queue is paused"),
    (lambda s: setattr(s, "job_makes", None), "database backup job failed - its log says why"),
    (lambda s: setattr(s, "maintenance", True), "in maintenance mode"),
])
def test_backup_failures_say_why(app, server, tmp_path, change, fragment):
    change(server)
    with pytest.raises(ImmichError, match=fragment):
        app.backup(str(tmp_path))


def test_a_job_that_never_makes_a_dump_times_out(app, server, tmp_path, monkeypatch):
    monkeypatch.setattr(immich, "_PICKUP_S", -1)
    server.job_makes = ""
    with pytest.raises(ImmichError, match="didn't make a database backup"):
        app.backup(str(tmp_path))


@pytest.mark.parametrize("archive, expected", [
    ("immich_20261111_120000.sql.gz", "backuparr-20261111_120000.sql.gz"),
    ("immich-v2_20261111_120000.sql.gz", "backuparr-20261111_120000.sql.gz"),
    ("odd.sql.gz", "backuparr-restore.sql.gz"),
])
def test_uploads_never_look_like_an_old_cluster_dump(archive, expected):
    name = upload_name(f"/tmp/x/{archive}")
    assert name == expected and "-v" not in name


RESTORE_RUN = [None, {"active": True, "action": "restore_database", "task": "backup", "progress": 0.05},
               {"active": True, "action": "restore_database", "task": "restore", "progress": 0.5},
               {"active": True, "action": "restore_database", "task": "migrations", "progress": 0.9},
               None, {"active": False, "action": "end"}]


def write_backup(tmp_path):
    path = tmp_path / "immich_20261111_120000.sql.gz"
    path.write_bytes(DUMP)
    return str(path)


def test_restore_uploads_streams_restores_and_cleans_up(app, server, tmp_path):
    server.restore_statuses = list(RESTORE_RUN)
    assert app.restore_from_file(write_backup(tmp_path)) == {"fresh": False, "key_ok": True, "cleaned": True}
    body, headers = server.uploads["backuparr-20261111_120000.sql.gz"]
    assert DUMP in body and int(headers["Content-Length"]) == len(body)
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert server.restoring == "uploaded-backuparr-20261111_120000.sql.gz"
    assert server.files == {NIGHTLY}  # the uploaded copy is gone


def test_restore_reports_a_card_key_the_restored_database_doesnt_have(app, server, tmp_path):
    server.restore_statuses = list(RESTORE_RUN)
    server.after_restore_key_ok = False
    assert app.restore_from_file(write_backup(tmp_path)) == {"fresh": False, "key_ok": False, "cleaned": False}


def test_restore_onto_a_new_install_goes_without_the_key(app, server, tmp_path):
    server.initialized = False
    server.restore_statuses = [{"active": True, "action": "restore_database", "task": "restore"},
                               None, {"active": False, "action": "end"}]
    assert app.restore_from_file(write_backup(tmp_path))["fresh"] is True
    start = next(i for i, r in enumerate(server.sent) if r.path_url.endswith("/start-restore"))
    maintenance = server.sent[start:start + 4]
    assert not any(r.headers.get("x-api-key") for r in maintenance)
    upload = next(r for r in server.sent if r.path_url.endswith("/upload"))
    assert "x-api-key" not in upload.headers and "immich_maintenance_token=jwt-token" in upload.headers["Cookie"]


def test_a_failed_sql_step_ends_maintenance_mode(app, server, tmp_path):
    server.restore_statuses = [{"active": True, "action": "restore_database", "task": "backup"},
                               {"active": True, "action": "restore_database", "task": "error",
                                "error": "Error: /usr/lib/postgresql/14/bin/psql non-zero exit code (3)\n"
                                         "NOTICE:  drop cascades to 131 other objects\nERROR:  syntax error at or near \";\""}]
    with pytest.raises(ImmichError) as error:
        app.restore_from_file(write_backup(tmp_path))
    assert str(error.value) == ("immich: Immich couldn't restore the backup (ERROR:  syntax error at or near \";\"). "
                                "Its database wasn't changed, and Immich is running again")
    assert not server.maintenance and "uploaded-backuparr-20261111_120000.sql.gz" not in server.files
    end = [r for r in server.sent if r.path_url == "/api/admin/maintenance" and b'"end"' in (r.body or b"")]
    assert end and "x-api-key" not in end[0].headers


def test_a_failure_after_the_sql_step_leaves_maintenance_mode_on(app, server, tmp_path):
    server.restore_statuses = [{"active": True, "action": "restore_database", "task": "restore"},
                               {"active": True, "action": "restore_database", "task": "rollback"},
                               {"active": True, "action": "restore_database", "task": "error",
                                "error": "Error: Server health check failed, no admin exists."}]
    with pytest.raises(ImmichError, match="still in maintenance mode. Its log explains it"):
        app.restore_from_file(write_backup(tmp_path))
    assert server.maintenance
    assert not any(b'"end"' in (r.body or b"") for r in server.sent if isinstance(r.body, bytes))


def test_a_restore_that_never_starts_is_reported(app, server, tmp_path, monkeypatch):
    monkeypatch.setattr(immich, "_MAINTENANCE_START_S", -1)
    server.restore_statuses = [{"active": False, "action": "end"}] * 5
    with pytest.raises(ImmichError, match="didn't start the restore"):
        app.restore_from_file(write_backup(tmp_path))


def test_a_damaged_backup_is_never_uploaded(app, server, tmp_path):
    path = tmp_path / "immich_20261111_120000.sql.gz"
    path.write_bytes(DUMP[:-8])
    with pytest.raises(ImmichError, match="damaged"):
        app.restore_from_file(str(path))
    assert not server.sent


def test_restore_message_says_when_the_cards_key_stopped_working(monkeypatch, tmp_path):
    monkeypatch.setattr(ImmichApp, "restore_from_file", lambda self, path: {"fresh": False, "key_ok": False, "cleaned": False})
    result = restore_actions.restore_app("immich", {"url": "http://immich:2283", "api_key": KEY}, str(tmp_path), "x.sql.gz")
    assert result["attention"] and "the API key in Settings no longer works" in restore_actions.restore_message("immich", result)
    ok = {"kind": "immich", "fresh": False, "key_ok": True, "cleaned": True, "attention": False}
    assert restore_actions.restore_message("immich", ok) == "immich restore complete, Immich restarted with the restored database"


def test_a_restore_dropped_by_a_restart_mid_replay_ends_maintenance_mode(app, server, tmp_path):
    """Immich resets its maintenance action to "start" as a restore begins:
    restarted mid-replay, it stays in maintenance mode with the restore gone,
    and the transaction never committed."""
    server.restore_statuses = [{"active": True, "action": "restore_database", "task": "restore", "progress": 0.7},
                               None, *[{"active": True, "action": "start"}] * 3]
    with pytest.raises(ImmichError) as error:
        app.restore_from_file(write_backup(tmp_path))
    assert str(error.value) == ("immich: Immich restarted in the middle of the restore. Its database wasn't changed, "
                                "and Immich is running again")
    assert not server.maintenance and "uploaded-backuparr-20261111_120000.sql.gz" not in server.files


@pytest.mark.parametrize("statuses", [
    [{"active": True, "action": "restore_database", "task": "migrations", "progress": 0.9}],
    [{"active": True, "action": "restore_database", "task": "restore", "progress": 1}],
])
def test_a_restore_dropped_after_the_replay_leaves_maintenance_mode_on(app, server, tmp_path, statuses):
    server.restore_statuses = [*statuses, None, *[{"active": True, "action": "start"}] * 3]
    with pytest.raises(ImmichError, match="restarted in the middle of the restore and is still in maintenance mode"):
        app.restore_from_file(write_backup(tmp_path))
    assert server.maintenance


def test_a_new_install_waiting_to_pick_a_backup_isnt_an_interrupted_restore(app, server, tmp_path):
    server.initialized = False
    server.restore_statuses = [{"active": True, "action": "select_database_restore"},
                               {"active": True, "action": "restore_database", "task": "restore"},
                               None, {"active": False, "action": "end"}]
    assert app.restore_from_file(write_backup(tmp_path))["fresh"] is True


def test_a_restore_dropped_before_backuparr_saw_it_run_stays_in_maintenance_mode(app, server, tmp_path):
    """Seen only as "start": the stage it reached is unknown, so Immich is
    left for its admin rather than taken out of maintenance mode."""
    server.restore_statuses = [None, *[{"active": True, "action": "start"}] * 3]
    with pytest.raises(ImmichError, match="restarted in the middle of the restore and is still in maintenance mode"):
        app.restore_from_file(write_backup(tmp_path))
    assert server.maintenance


def test_a_moment_of_start_while_immich_boots_isnt_a_dropped_restore(app, server, tmp_path):
    server.restore_statuses = [{"active": True, "action": "start"}, {"active": True, "action": "start"},
                               {"active": True, "action": "restore_database", "task": "restore"},
                               None, {"active": False, "action": "end"}]
    assert app.restore_from_file(write_backup(tmp_path))["key_ok"] is True
