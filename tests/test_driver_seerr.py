import io
import json
import os
import sqlite3
import zipfile

import pytest
import requests

import backup
import restore_actions
from apps import seerr
from apps.seerr import MANIFEST, SeerrApp, SeerrError
from config_store import restore_supported

KEY = "seerr-secret-key"
CLIENT_ID = "d6d9ae99-b67f-4729-b41d-cfa5b287eaaf"
MIGRATION = 1789257612345


def make_db(path, rows=("request",), migration=MIGRATION, wal=False):
    db = sqlite3.connect(path)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS migrations (id INTEGER PRIMARY KEY, timestamp BIGINT, name TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS media_request (name TEXT)")
    db.execute("DELETE FROM migrations")
    db.execute("DELETE FROM media_request")
    db.execute("INSERT INTO migrations (timestamp, name) VALUES (?, 'Latest')", (migration,))
    db.executemany("INSERT INTO media_request VALUES (?)", [(r,) for r in rows])
    db.commit()
    return db


def rows(path):
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT name FROM media_request ORDER BY name")]
    finally:
        db.close()


def settings(client_id=CLIENT_ID, title="Seerr"):
    return json.dumps({"clientId": client_id, "main": {"apiKey": KEY, "applicationTitle": title}})


@pytest.fixture
def folder(tmp_path):
    """A stopped Seerr's config folder."""
    root = tmp_path / "seerr"
    for sub in ("db", "logs", "cache/images"):
        (root / sub).mkdir(parents=True)
    (root / "settings.json").write_text(settings())
    (root / "settings.old.json").write_text(settings())
    (root / "logs/seerr-2026-10-29.log").write_text("log")
    (root / "cache/images/x.jpg").write_bytes(b"jpg")
    make_db(root / "db/db.sqlite3").close()
    return root


@pytest.fixture
def server(monkeypatch):
    """Answers each "METHOD /path" from `routes` (status, body); records requests."""
    state = type("Server", (), {})()
    state.sent = []
    state.routes = {
        "GET /api/v1/settings/about": (200, {"version": "3.5.1", "totalRequests": 1}),
        "GET /api/v1/settings/public": (200, {"plexClientIdentifier": CLIENT_ID, "initialized": True}),
    }

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        status, body = state.routes.get(f"{request.method} {request.path_url.split('?')[0]}", (404, {}))
        if isinstance(body, Exception):
            raise body
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = json.dumps(body).encode()
        res.raw = io.BytesIO(res._content)
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


@pytest.fixture
def app(folder):
    return SeerrApp("http://seerr:5055/", KEY, data_dir=str(folder))


def make_backup(app, tmp_path):
    archive = str(tmp_path / "seerr_20261029_030000.zip")
    backup.zip_dir(app.backup(str(tmp_path / "work")), archive)
    return archive


def stop_seerr(server):
    server.routes["GET /api/v1/status"] = (502, {})  # A proxy in front of a stopped Seerr.


# --- connection --------------------------------------------------------------

def test_the_key_goes_in_its_own_header_never_the_url(app, server):
    assert app.test_connection() == "seerr 3.5.1 reachable, data folder OK"
    request = server.sent[0]
    assert request.url == "http://seerr:5055/api/v1/settings/about"
    assert request.headers["X-Api-Key"] == KEY and KEY not in request.url and "Authorization" not in request.headers


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_says_where_the_key_lives(app, server, status):
    server.routes["GET /api/v1/settings/about"] = (status, {})
    with pytest.raises(SeerrError, match=r"Settings > General.*finish Seerr's setup"):
        app.test_connection()


def test_the_data_folder_must_be_set_exist_and_be_seerr_s(server, tmp_path, folder):
    with pytest.raises(SeerrError, match="set the data folder"):
        SeerrApp("http://seerr:5055", KEY).test_connection()
    with pytest.raises(SeerrError, match="doesn't exist in this container"):
        SeerrApp("http://seerr:5055", KEY, data_dir=str(tmp_path / "missing")).test_connection()
    with pytest.raises(SeerrError, match="doesn't look like Seerr's config folder"):
        SeerrApp("http://seerr:5055", KEY, data_dir=str(folder / "db")).test_connection()


def test_a_postgres_seerr_is_explained(app, server, folder):
    os.remove(folder / "db/db.sqlite3")
    with pytest.raises(SeerrError, match="no db/db.sqlite3 .* PostgreSQL isn't supported"):
        app.test_connection()


def test_another_servers_folder_is_the_wrong_mount(app, server, folder):
    (folder / "settings.json").write_text(settings(client_id="someotherseerr"))
    with pytest.raises(SeerrError, match="holds another Seerr server's data .*someotherseerr"):
        app.test_connection()


def test_a_broken_settings_file_is_reported(app, server, folder):
    (folder / "settings.json").write_text("{not json")
    with pytest.raises(SeerrError, match="can't read settings.json"):
        app.test_connection()


def test_a_read_only_folder_still_passes_with_a_note(app, server, monkeypatch):
    monkeypatch.setattr(seerr.os, "access", lambda path, mode: mode != os.W_OK)
    assert app.test_connection() == "seerr 3.5.1 reachable; data folder is read-only, so backups work but restores won't"


def test_an_unreadable_database_is_reported(app, server, folder):
    (folder / "db/db.sqlite3").write_bytes(b"not a database, just some bytes" * 200)
    with pytest.raises(SeerrError, match="can't read db/db.sqlite3"):
        app.test_connection()


def test_connection_failures_are_left_for_the_shared_explanation(app, server):
    server.routes["GET /api/v1/settings/about"] = (0, requests.ConnectionError("refused"))
    with pytest.raises(requests.ConnectionError):
        app.test_connection()


# --- backup ------------------------------------------------------------------

def test_backup_takes_the_database_and_settings_but_not_cache_or_logs(app, server, tmp_path):
    with zipfile.ZipFile(make_backup(app, tmp_path)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read(MANIFEST))
        assert json.loads(zf.read("settings.json"))["clientId"] == CLIENT_ID
    assert names == {MANIFEST, "db/db.sqlite3", "settings.json"}
    assert manifest == {"seerr_version": "3.5.1", "client_id": CLIENT_ID, "migration": MIGRATION}


def test_a_live_database_is_snapshotted_with_its_unflushed_changes(app, server, folder, tmp_path):
    writer = make_db(folder / "db/db.sqlite3", rows=("before",), wal=True)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO media_request VALUES ('in the wal')")
    writer.commit()  # Seerr keeps the connection open, so the change stays in db.sqlite3-wal.
    assert (folder / "db/db.sqlite3-wal").stat().st_size > 0
    archive = make_backup(app, tmp_path)
    writer.close()
    with zipfile.ZipFile(archive) as zf:
        zf.extract("db/db.sqlite3", tmp_path / "out")
    assert rows(tmp_path / "out/db/db.sqlite3") == ["before", "in the wal"]


def test_backup_refuses_another_servers_folder(app, server, tmp_path):
    server.routes["GET /api/v1/settings/public"] = (200, {"plexClientIdentifier": "different"})
    with pytest.raises(SeerrError, match="holds another Seerr server's data"):
        app.backup(str(tmp_path / "work"))


def test_backup_is_built_through_the_app_registry(folder):
    app = backup.build_app("seerr", {"url": "http://seerr:5055", "api_key": KEY, "data_dir": str(folder)})
    assert isinstance(app, SeerrApp) and app.data_dir == str(folder)
    assert restore_supported("seerr")


# --- restore -----------------------------------------------------------------

def test_restore_brings_back_the_database_and_settings(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db/db.sqlite3", rows=("after the backup",)).close()
    (folder / "settings.json").write_text(settings(title="Changed"))
    (folder / "db/db.sqlite3-shm").write_bytes(b"stale")
    stop_seerr(server)

    app.restore_from_file(archive)

    assert rows(folder / "db/db.sqlite3") == ["request"]
    assert json.loads((folder / "settings.json").read_text())["main"]["applicationTitle"] == "Seerr"
    assert not (folder / "db/db.sqlite3-shm").exists() and not (folder / "db/db.sqlite3-wal").exists()
    # Not part of a backup, so left alone.
    assert (folder / "logs/seerr-2026-10-29.log").exists() and (folder / "settings.old.json").exists()


def test_a_stopped_seerrs_leftover_wal_is_never_replayed_over_the_restore(app, server, folder, tmp_path):
    # Seerr doesn't close its database on shutdown, so its -wal stays behind.
    archive = make_backup(app, tmp_path)
    writer = make_db(folder / "db/db.sqlite3", rows=("before",), wal=True)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO media_request VALUES ('left in the wal')")
    writer.commit()
    stale_wal = (folder / "db/db.sqlite3-wal").read_bytes()
    writer.close()
    (folder / "db/db.sqlite3-wal").write_bytes(stale_wal)  # As a killed Seerr leaves it.
    stop_seerr(server)

    app.restore_from_file(archive)

    assert not (folder / "db/db.sqlite3-wal").exists()
    assert rows(folder / "db/db.sqlite3") == ["request"]


def test_restore_keeps_each_files_owner_and_mode(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    os.chmod(folder / "settings.json", 0o640)
    os.chmod(folder / "db/db.sqlite3", 0o600)
    stop_seerr(server)
    app.restore_from_file(archive)
    assert (folder / "settings.json").stat().st_mode & 0o777 == 0o640
    assert (folder / "db/db.sqlite3").stat().st_mode & 0o777 == 0o600


def test_restore_is_refused_while_seerr_answers(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    server.routes["GET /api/v1/status"] = (200, {"version": "3.5.1"})
    with pytest.raises(SeerrError, match="it's still answering at http://seerr:5055"):
        app.restore_from_file(archive)
    assert all(r.method == "GET" for r in server.sent)


def test_restore_is_refused_while_something_has_the_database_open(app, server, folder, tmp_path):
    # Seerr running where Backuparr can't reach it: its open database still gives it away.
    archive = make_backup(app, tmp_path)
    running = make_db(folder / "db/db.sqlite3", rows=("current",), wal=True)
    stop_seerr(server)
    try:
        with pytest.raises(SeerrError, match="its database is in use"):
            app.restore_from_file(archive)
    finally:
        running.close()
    assert rows(folder / "db/db.sqlite3") == ["current"]


def test_an_unreachable_seerr_counts_as_stopped(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    server.routes["GET /api/v1/status"] = (0, requests.ConnectionError("refused"))
    make_db(folder / "db/db.sqlite3", rows=("current",)).close()
    app.restore_from_file(archive)
    assert rows(folder / "db/db.sqlite3") == ["request"]


def test_restore_needs_writable_files(app, server, folder, tmp_path, monkeypatch):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db/db.sqlite3", rows=("current",)).close()
    stop_seerr(server)
    locked = str(folder / "settings.json")
    monkeypatch.setattr(seerr.os, "access", lambda path, mode: not (path == locked and mode == os.W_OK))
    with pytest.raises(SeerrError, match="aren't writable - mount the data folder read-write"):
        app.restore_from_file(archive)
    assert rows(folder / "db/db.sqlite3") == ["current"]


def test_a_backup_from_a_newer_seerr_is_refused(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db/db.sqlite3", rows=("current",), migration=MIGRATION - 1).close()
    stop_seerr(server)
    with pytest.raises(SeerrError, match="newer Seerr than the one installed - update Seerr first"):
        app.restore_from_file(archive)
    assert rows(folder / "db/db.sqlite3") == ["current"]


def test_a_backup_from_an_older_seerr_is_restored_for_seerr_to_migrate(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db/db.sqlite3", rows=("current",), migration=MIGRATION + 1).close()
    stop_seerr(server)
    app.restore_from_file(archive)
    assert rows(folder / "db/db.sqlite3") == ["request"]


def test_restore_onto_a_fresh_install_brings_back_the_original_identity(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    (folder / "settings.json").write_text(settings(client_id="fresh-install"))
    stop_seerr(server)
    app.restore_from_file(archive)
    assert json.loads((folder / "settings.json").read_text())["clientId"] == CLIENT_ID


def test_restore_refuses_a_file_that_isnt_a_seerr_backup(app, server, tmp_path):
    other = tmp_path / "seerr_x.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("db/db.sqlite3", "x")
    with pytest.raises(SeerrError, match="isn't a Seerr backup made by Backuparr"):
        app.restore_from_file(str(other))
    assert server.sent == []


@pytest.mark.parametrize("extra", ["../escape.txt", "db/db.sqlite3-wal", "logs/x.log", "/etc/passwd", None])
def test_restore_refuses_anything_but_what_a_backup_holds(app, server, folder, tmp_path, extra):
    archive = tmp_path / "seerr_bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(MANIFEST, json.dumps({"seerr_version": "3.5.1"}))
        zf.writestr("db/db.sqlite3", (folder / "db/db.sqlite3").read_bytes())
        if extra:
            zf.writestr(extra, "x")
            zf.writestr("settings.json", settings())
    stop_seerr(server)
    with pytest.raises(SeerrError, match="unexpected contents in the backup"):
        app.restore_from_file(str(archive))
    assert not (tmp_path / "escape.txt").exists()


def test_a_backup_with_a_broken_database_is_refused_before_any_write(app, server, folder, tmp_path):
    archive = tmp_path / "seerr_bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(MANIFEST, json.dumps({"seerr_version": "3.5.1"}))
        zf.writestr("db/db.sqlite3", b"not a database, just some bytes" * 200)
        zf.writestr("settings.json", settings(title="From the bad backup"))
    stop_seerr(server)
    with pytest.raises(SeerrError, match="the backup's database can't be read"):
        app.restore_from_file(str(archive))
    assert json.loads((folder / "settings.json").read_text())["main"]["applicationTitle"] == "Seerr"


def test_the_restore_action_uses_the_data_folder(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db/db.sqlite3", rows=("current",)).close()
    stop_seerr(server)
    cfg = {"url": "http://seerr:5055", "api_key": KEY, "data_dir": str(folder)}
    assert restore_actions.restore_app("seerr", cfg, str(tmp_path), archive) == {"kind": "seerr"}
    assert rows(folder / "db/db.sqlite3") == ["request"]
