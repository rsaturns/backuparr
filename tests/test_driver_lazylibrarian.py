import io
import json
import logging
import os
import sqlite3
import tarfile
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

import backup
import restore_actions
from apps.lazylibrarian import LazyLibrarianApp, LazyLibrarianError
from config_store import app_meta, restore_supported

KEY = "0123456789abcdef0123456789abcdef"
ARCHIVE = "api_Tue_Nov__3_08_28_49_2026.tgz"
HELP_FULL = "<html><table><tr><td>getVersion</td></tr><tr><td>backup</td><td>Backup database and config files</td></tr></table>"
HELP_READ_ONLY = "<html><table><tr><td>getVersion</td></tr></table>"


def make_db(path, rows=("book",), wal=False):
    db = sqlite3.connect(path)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS books (name TEXT)")
    db.execute("DELETE FROM books")
    db.executemany("INSERT INTO books VALUES (?)", [(r,) for r in rows])
    db.commit()
    return db


def rows(path):
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT name FROM books ORDER BY name")]
    finally:
        db.close()


def config_ini(key=KEY, port=5299):
    return f"[General]\nebook_dir = /books\n\n[API]\napi_enabled = True\napi_key = {key}\n\n[WebServer]\nhttp_port = {port}\n"


def make_tgz(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            if isinstance(data, tarfile.TarInfo):
                tar.addfile(data)
                continue
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return str(path)


@pytest.fixture
def folder(tmp_path):
    """A stopped LazyLibrarian's /config folder."""
    root = tmp_path / "lazylibrarian"
    (root / "cache").mkdir(parents=True)
    (root / "config.ini").write_text(config_ini())
    (root / "dicts.json").write_text('{"&": "and"}')
    (root / "scheduled_Mon_Nov__2_03_00_00_2026.tgz").write_bytes(b"its own scheduled backup")
    make_db(root / "lazylibrarian.db").close()
    return root


@pytest.fixture
def server(monkeypatch, folder):
    """Answers /api by cmd; `backup` writes an archive into the folder like
    LazyLibrarian's dbbackup and answers with its path in LazyLibrarian's container."""
    state = type("Server", (), {})()
    state.sent = []

    def write_backup():
        snapshot = folder / "snapshot.db"
        source = sqlite3.connect(folder / "lazylibrarian.db")
        target = sqlite3.connect(snapshot)
        source.backup(target)
        source.close()
        target.close()
        make_tgz(folder / ARCHIVE, {"lazylibrarian.db": snapshot.read_bytes(),
                                    "config.ini": (folder / "config.ini").read_bytes(),
                                    "dicts.json": (folder / "dicts.json").read_bytes()})
        snapshot.unlink()
        return {"Success": True, "Data": f"/config/{ARCHIVE}", "Error": {"Code": 200, "Message": ""}}

    state.routes = {
        "getVersion": lambda: {"Success": True, "current_version": "fc698c6c", "install_type": "source DOCKER"},
        "help": lambda: HELP_FULL,
        "backup": write_backup,
    }

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        parts = urlsplit(request.url)
        query = parse_qs(parts.query)
        res = requests.Response()
        res.request, res.url = request, request.url
        handler = state.routes.get(query.get("cmd", [""])[0]) if parts.path == "/api" else None
        if isinstance(handler, Exception):
            raise handler
        if handler is None:
            res.status_code, body = 404, "not found"
        elif query.get("apikey", [""])[0] != KEY:
            res.status_code, body = 200, {"Success": False, "Data": "", "Error": {"Code": 401, "Message": "Incorrect API key"}}
        else:
            res.status_code, body = 200, handler()
        res._content = (body if isinstance(body, str) else json.dumps(body)).encode()
        res.raw = io.BytesIO(res._content)
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


@pytest.fixture
def app(folder):
    return LazyLibrarianApp("http://lazylibrarian:5299/", KEY, data_dir=str(folder))


def make_backup(app, tmp_path):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return app.backup(str(work))


def stop(server):
    server.routes["getVersion"] = requests.ConnectionError("refused")


# --- connection --------------------------------------------------------------

def test_the_key_goes_in_the_query_string_like_lazylibrarians_api_expects(app, server):
    assert app.test_connection() == "lazylibrarian fc698c6c reachable, data folder OK"
    assert [parse_qs(urlsplit(r.url).query) for r in server.sent] == [
        {"apikey": [KEY], "cmd": ["getVersion"]}, {"apikey": [KEY], "cmd": ["help"]}]
    assert server.sent[0].url.startswith("http://lazylibrarian:5299/api?")


def test_a_wrong_key_is_reported_though_lazylibrarian_answers_200(folder, server):
    with pytest.raises(LazyLibrarianError, match="unauthorized - check the API key"):
        LazyLibrarianApp("http://lazylibrarian:5299", "f" * 32, data_dir=str(folder)).test_connection()


def test_the_read_only_key_is_refused_since_it_cant_run_a_backup(app, server):
    server.routes["help"] = lambda: HELP_READ_ONLY
    with pytest.raises(LazyLibrarianError, match="read-only API key - use the full-access API key"):
        app.test_connection()


def test_something_else_at_the_url_is_reported(app, server):
    server.routes["getVersion"] = lambda: "<html>a login page</html>"
    with pytest.raises(LazyLibrarianError, match="didn't return LazyLibrarian's API answer"):
        app.test_connection()


def test_connection_errors_never_carry_the_key(app, server):
    server.routes["getVersion"] = requests.ConnectionError(f"http://lazylibrarian:5299/api?apikey={KEY}&cmd=getVersion")
    with pytest.raises(requests.ConnectionError) as caught:
        app.test_connection()
    assert KEY not in str(caught.value) and caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_data_folder_must_be_set_exist_and_be_lazylibrarians(server, tmp_path, folder):
    for data_dir, message in ((None, "set the data folder"), (str(tmp_path / "nope"), "doesn't exist in this container"),
                              (str(tmp_path), "doesn't look like LazyLibrarian's data folder")):
        with pytest.raises(LazyLibrarianError, match=message):
            LazyLibrarianApp("http://lazylibrarian:5299", KEY, data_dir=data_dir).test_connection()


def test_another_lazylibrarians_folder_is_the_wrong_mount(app, server, folder):
    (folder / "config.ini").write_text(config_ini(key="9" * 32))
    with pytest.raises(LazyLibrarianError, match="holds another LazyLibrarian's data"):
        app.test_connection()


def test_a_read_only_folder_passes_with_a_note(app, server, monkeypatch):
    monkeypatch.setattr(os, "access", lambda path, mode: mode != os.W_OK)
    assert app.test_connection() == ("lazylibrarian fc698c6c reachable; data folder is read-only, so each "
                                     "backup's file stays in it and restores won't work")


def test_it_is_a_restorable_app_with_a_data_folder():
    meta = app_meta("lazylibrarian")
    assert meta["status"] == "available" and restore_supported("lazylibrarian")
    assert [f["name"] for f in meta["extra_fields"]] == ["data_dir"]


# --- backup ------------------------------------------------------------------

def test_backup_collects_lazylibrarians_own_archive_and_tidies_it_away(app, server, folder, tmp_path):
    path = make_backup(app, tmp_path)
    assert os.path.basename(path) == ARCHIVE and backup.archive_suffix(path) == ".tgz"
    with tarfile.open(path) as tar:
        assert sorted(tar.getnames()) == ["config.ini", "dicts.json", "lazylibrarian.db"]
    assert not (folder / ARCHIVE).exists(), "the API's archive must not pile up in LazyLibrarian's folder"
    assert (folder / "scheduled_Mon_Nov__2_03_00_00_2026.tgz").exists(), "LazyLibrarian's own backups are left alone"


def test_a_read_only_folder_keeps_the_archive_and_says_so(app, server, folder, tmp_path, monkeypatch, caplog):
    def refuse(path):
        raise PermissionError(13, "Read-only file system")
    monkeypatch.setattr(os, "remove", refuse)
    with caplog.at_level(logging.WARNING):
        path = make_backup(app, tmp_path)
    assert os.path.exists(path) and (folder / ARCHIVE).exists()
    assert "mount it read-write so backups don't pile up there" in caplog.text


def test_an_archive_lazylibrarian_wrote_elsewhere_means_the_wrong_mount(app, server, folder, tmp_path):
    server.routes["backup"] = lambda: {"Success": True, "Data": f"/config/{ARCHIVE}"}
    with pytest.raises(LazyLibrarianError, match="isn't in .* mount the folder LazyLibrarian has at /config"):
        make_backup(app, tmp_path)


@pytest.mark.parametrize("data", ["/config/../../etc/passwd", "/config/notes.txt", "", None])
def test_only_an_api_archive_name_is_ever_read_or_removed(app, server, folder, tmp_path, data):
    server.routes["backup"] = lambda: {"Success": True, "Data": data}
    with pytest.raises(LazyLibrarianError, match="unexpected backup file name"):
        make_backup(app, tmp_path)


def test_a_failed_backup_reports_lazylibrarians_message(app, server, tmp_path):
    server.routes["backup"] = lambda: {"Success": False, "Data": "", "Error": {"Code": 200, "Message": "disk full"}}
    with pytest.raises(LazyLibrarianError, match="lazylibrarian: disk full"):
        make_backup(app, tmp_path)


def test_backup_refuses_another_lazylibrarians_folder(app, server, folder, tmp_path):
    (folder / "config.ini").write_text(config_ini(key="9" * 32))
    with pytest.raises(LazyLibrarianError, match="holds another LazyLibrarian's data"):
        make_backup(app, tmp_path)
    assert not (folder / ARCHIVE).exists()


def test_backup_is_built_through_the_app_registry(folder):
    app = backup.build_app("lazylibrarian", {"url": "http://lazylibrarian:5299", "api_key": KEY, "data_dir": str(folder)})
    assert isinstance(app, LazyLibrarianApp) and app.data_dir == str(folder)


# --- restore -----------------------------------------------------------------

def test_restore_brings_back_the_database_and_settings(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "lazylibrarian.db", rows=("added later",)).close()
    (folder / "config.ini").write_text(config_ini(port=9999))
    (folder / "dicts.json").write_text("{}")
    stop(server)
    app.restore_from_file(archive)
    assert rows(folder / "lazylibrarian.db") == ["book"]
    assert "http_port = 5299" in (folder / "config.ini").read_text()
    assert (folder / "dicts.json").read_text() == '{"&": "and"}'


def test_a_stopped_lazylibrarians_leftover_wal_is_never_replayed_over_the_restore(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    # A change only in the -wal, as a killed LazyLibrarian leaves it.
    db = make_db(folder / "lazylibrarian.db", wal=True)
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("INSERT INTO books VALUES ('only in the wal')")
    db.commit()
    os.link(folder / "lazylibrarian.db-wal", tmp_path / "kept-wal")
    db.close()
    os.link(tmp_path / "kept-wal", folder / "lazylibrarian.db-wal")
    stop(server)
    app.restore_from_file(archive)
    assert rows(folder / "lazylibrarian.db") == ["book"]
    assert not (folder / "lazylibrarian.db-wal").exists() and not (folder / "lazylibrarian.db-shm").exists()


def test_restore_keeps_each_files_mode(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    os.chmod(folder / "config.ini", 0o600)
    stop(server)
    app.restore_from_file(archive)
    assert os.stat(folder / "config.ini").st_mode & 0o777 == 0o600


def test_restore_is_refused_while_lazylibrarian_answers(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "lazylibrarian.db", rows=("current",)).close()
    with pytest.raises(LazyLibrarianError, match="it's still answering at http://lazylibrarian:5299"):
        app.restore_from_file(archive)
    assert rows(folder / "lazylibrarian.db") == ["current"]


def test_restore_is_refused_while_something_has_the_database_open(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    running = make_db(folder / "lazylibrarian.db", rows=("current",), wal=True)
    stop(server)
    try:
        with pytest.raises(LazyLibrarianError, match="its database is in use"):
            app.restore_from_file(archive)
    finally:
        running.close()
    assert rows(folder / "lazylibrarian.db") == ["current"]


def test_restore_needs_writable_files(app, server, folder, tmp_path, monkeypatch):
    archive = make_backup(app, tmp_path)
    stop(server)
    monkeypatch.setattr(os, "access", lambda path, mode: not str(path).endswith("config.ini"))
    with pytest.raises(LazyLibrarianError, match="aren't writable - mount the data folder read-write"):
        app.restore_from_file(archive)


def test_restore_refuses_a_file_that_isnt_a_lazylibrarian_backup(app, server, tmp_path):
    (tmp_path / "x.tgz").write_bytes(b"PK\x03\x04 not a tarball")
    with pytest.raises(LazyLibrarianError, match="isn't a LazyLibrarian backup"):
        app.restore_from_file(str(tmp_path / "x.tgz"))
    no_db = make_tgz(tmp_path / "y.tgz", {"config.ini": b"[API]\n"})
    with pytest.raises(LazyLibrarianError, match="no database"):
        app.restore_from_file(no_db)


def _link(name, target):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, target
    return info


@pytest.mark.parametrize("member", [("../escape.txt", b"x"), ("cache/x.jpg", b"x"), ("notes.txt", b"x"),
                                    ("dicts.json", _link("dicts.json", "/etc/passwd"))])
def test_restore_refuses_anything_but_what_a_backup_holds(app, server, folder, tmp_path, member):
    name, data = member
    archive = make_tgz(tmp_path / "bad.tgz", {"lazylibrarian.db": (folder / "lazylibrarian.db").read_bytes(), name: data})
    stop(server)
    with pytest.raises(LazyLibrarianError, match="unexpected contents in the backup"):
        app.restore_from_file(archive)
    assert not (tmp_path / "escape.txt").exists()


def test_a_backup_with_a_broken_database_is_refused_before_any_write(app, server, folder, tmp_path):
    archive = make_tgz(tmp_path / "broken.tgz", {"lazylibrarian.db": b"SQLite format 3\x00" + b"\x00" * 200,
                                                 "config.ini": b"[API]\napi_key = other\n"})
    before = (folder / "config.ini").read_text()
    stop(server)
    with pytest.raises(LazyLibrarianError, match="the backup's database"):
        app.restore_from_file(archive)
    assert (folder / "config.ini").read_text() == before and rows(folder / "lazylibrarian.db") == ["book"]


def test_the_restore_action_uses_the_data_folder(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "lazylibrarian.db", rows=("current",)).close()
    stop(server)
    cfg = {"url": "http://lazylibrarian:5299", "api_key": KEY, "data_dir": str(folder)}
    assert restore_actions.restore_app("lazylibrarian", cfg, str(tmp_path), archive) == {"kind": "lazylibrarian"}
    assert rows(folder / "lazylibrarian.db") == ["book"]
