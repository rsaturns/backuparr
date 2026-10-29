import io
import json
import os
import sqlite3
import zipfile

import pytest
import requests

import backup
from apps import emby
from apps.emby import MANIFEST, EmbyApp, EmbyError

KEY = "emby-secret-key"
SERVER_ID = "beea808781814898b62764997c69f124"


def make_db(path, rows=("watched",), wal=False):
    db = sqlite3.connect(path)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS UserDatas (name TEXT)")
    db.execute("DELETE FROM UserDatas")
    db.executemany("INSERT INTO UserDatas VALUES (?)", [(r,) for r in rows])
    db.commit()
    return db


def rows(path):
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT name FROM UserDatas ORDER BY name")]
    finally:
        db.close()


@pytest.fixture
def folder(tmp_path):
    """A stopped Emby's data folder: databases, settings, plugins, lists."""
    root = tmp_path / "emby"
    for sub in ("config/users/u1", "data/playlists/Mine [playlist]", "plugins/configurations",
                "metadata/collections/Set", "metadata/library/aa", "cache/images", "logs"):
        (root / sub).mkdir(parents=True)
    (root / "config/system.xml").write_text("<ServerConfiguration><ServerName>Lab</ServerName></ServerConfiguration>")
    (root / "config/users/u1/policy.xml").write_text("<policy/>")
    (root / "data/device.txt").write_text("﻿" + SERVER_ID, encoding="utf-8")
    (root / "data/lastversion.txt").write_text("4.10.2.0")
    (root / "data/wan.dat").write_text("203.0.113.1")
    (root / "data/playlists/Mine [playlist]/Mine.m3u").write_text("/media/a.mkv")
    (root / "plugins/MovieDb.dll").write_bytes(b"dll")
    (root / "plugins/configurations/MovieDb.xml").write_text("<cfg/>")
    (root / "metadata/collections/Set/collection.nfo").write_text("<nfo/>")
    (root / "metadata/library/aa/poster.jpg").write_bytes(b"jpg")
    (root / "cache/images/x.jpg").write_bytes(b"jpg")
    (root / "logs/embyserver.txt").write_text("log")
    for name in ("library", "users", "authentication"):
        make_db(root / "data" / f"{name}.db").close()
    return root


@pytest.fixture
def server(monkeypatch):
    """Answers each "METHOD /path" from `routes` (status, body); records requests."""
    state = type("Server", (), {})()
    state.sent = []
    state.routes = {"GET /emby/System/Info": (200, {"Version": "4.10.2.0", "Id": SERVER_ID})}

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
    return EmbyApp("http://emby:8096/emby/", KEY, data_dir=str(folder))


def make_backup(app, tmp_path):
    archive = str(tmp_path / "emby_20261028_030000.zip")
    backup.zip_dir(app.backup(str(tmp_path / "work")), archive)
    return archive


# --- connection --------------------------------------------------------------

def test_the_key_goes_in_its_own_header_never_the_url(app, server):
    assert app.test_connection() == "emby 4.10.2.0 reachable, data folder OK"
    request = server.sent[0]
    assert request.url == "http://emby:8096/emby/System/Info"
    assert request.headers["X-Emby-Token"] == KEY and KEY not in request.url and "Authorization" not in request.headers


def test_a_rejected_key_says_where_keys_live(app, server):
    server.routes["GET /emby/System/Info"] = (401, {})
    with pytest.raises(EmbyError, match="Settings > Advanced > API Keys"):
        app.test_connection()


def test_the_data_folder_must_be_set_exist_and_be_emby_s(server, tmp_path, folder):
    with pytest.raises(EmbyError, match="set the data folder"):
        EmbyApp("http://emby:8096", KEY).test_connection()
    with pytest.raises(EmbyError, match="doesn't exist in this container"):
        EmbyApp("http://emby:8096", KEY, data_dir=str(tmp_path / "missing")).test_connection()
    with pytest.raises(EmbyError, match="doesn't look like Emby's data folder"):
        EmbyApp("http://emby:8096", KEY, data_dir=str(folder / "data")).test_connection()


def test_another_servers_folder_is_the_wrong_mount(app, server, folder):
    (folder / "data/device.txt").write_text("someotherserver")
    with pytest.raises(EmbyError, match="holds another Emby server's data .*someotherserver"):
        app.test_connection()


def test_a_read_only_folder_still_passes_with_a_note(app, server, monkeypatch):
    monkeypatch.setattr(emby.os, "access", lambda path, mode: mode != os.W_OK)
    assert app.test_connection() == "emby 4.10.2.0 reachable; data folder is read-only, so backups work but restores won't"


def test_an_unreadable_database_is_reported(app, server, folder):
    (folder / "data/library.db").write_bytes(b"not a database, just some bytes" * 200)
    with pytest.raises(EmbyError, match="can't read data/library.db"):
        app.test_connection()


# --- backup ------------------------------------------------------------------

def test_backup_takes_settings_databases_and_lists_but_not_images_cache_or_logs(app, server, tmp_path):
    with zipfile.ZipFile(make_backup(app, tmp_path)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read(MANIFEST))
    assert names == {
        MANIFEST, "config/system.xml", "config/users/u1/policy.xml",
        "data/device.txt", "data/lastversion.txt", "data/library.db", "data/users.db", "data/authentication.db",
        "data/playlists/Mine [playlist]/Mine.m3u", "plugins/MovieDb.dll", "plugins/configurations/MovieDb.xml",
        "metadata/collections/Set/collection.nfo",
    }
    assert manifest == {"emby_version": "4.10.2.0", "server_id": SERVER_ID,
                        "databases": ["authentication.db", "library.db", "users.db"]}


def test_a_live_database_is_snapshotted_with_its_unflushed_changes(app, server, folder, tmp_path):
    writer = make_db(folder / "data/library.db", rows=("before",), wal=True)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO UserDatas VALUES ('in the wal')")
    writer.commit()  # Emby keeps the connection open, so the change stays in library.db-wal.
    assert (folder / "data/library.db-wal").stat().st_size > 0
    archive = make_backup(app, tmp_path)
    writer.close()
    with zipfile.ZipFile(archive) as zf:
        zf.extract("data/library.db", tmp_path / "out")
    assert rows(tmp_path / "out/data/library.db") == ["before", "in the wal"]


def test_a_stopped_emby_on_a_read_only_mount_can_still_be_read(folder, tmp_path, monkeypatch):
    # No -wal file: opened immutable, since SQLite can't create its -shm there.
    opened, real = [], sqlite3.connect
    monkeypatch.setattr(emby.sqlite3, "connect", lambda target, **kw: opened.append(target) or real(target, **kw))
    emby.snapshot_db(str(folder / "data/library.db"), str(tmp_path / "copy.db"))
    assert opened[0].endswith("?mode=ro&immutable=1") and rows(tmp_path / "copy.db") == ["watched"]


def test_backup_refuses_another_servers_folder(app, server, folder, tmp_path):
    server.routes["GET /emby/System/Info"] = (200, {"Version": "4.10.2.0", "Id": "different"})
    with pytest.raises(EmbyError, match="holds another Emby server's data"):
        app.backup(str(tmp_path / "work"))


# --- restore -----------------------------------------------------------------

def stop_emby(server):
    server.routes["GET /emby/System/Info/Public"] = (502, {})  # A proxy in front of a stopped Emby.


def test_restore_brings_back_the_backups_files_and_drops_what_came_after(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "data/library.db", rows=("after the backup",)).close()
    (folder / "config/system.xml").write_text("<changed/>")
    (folder / "data/playlists/Later [playlist]").mkdir()
    (folder / "data/playlists/Later [playlist]/Later.m3u").write_text("/media/b.mkv")
    (folder / "metadata/collections/Set/collection.nfo").unlink()
    (folder / "data/library.db-shm").write_bytes(b"stale")
    stop_emby(server)

    app.restore_from_file(archive)

    assert rows(folder / "data/library.db") == ["watched"]
    assert (folder / "config/system.xml").read_text().startswith("<ServerConfiguration>")
    assert sorted(os.listdir(folder / "data/playlists")) == ["Mine [playlist]"]
    assert (folder / "metadata/collections/Set/collection.nfo").exists()
    assert not (folder / "data/library.db-shm").exists()
    # Not part of a backup, so left alone.
    assert (folder / "metadata/library/aa/poster.jpg").exists() and (folder / "data/wan.dat").exists()


def test_restore_keeps_each_files_owner_and_mode_and_makes_new_ones_readable(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    os.chmod(folder / "config/system.xml", 0o640)
    (folder / "config/users/u1/policy.xml").unlink()
    os.rmdir(folder / "config/users/u1")
    stop_emby(server)
    old_umask = os.umask(0o077)  # Backuparr's private umask.
    try:
        app.restore_from_file(archive)
    finally:
        os.umask(old_umask)
    assert (folder / "config/system.xml").stat().st_mode & 0o777 == 0o640
    assert (folder / "config/users/u1").stat().st_mode & 0o777 == 0o755
    assert (folder / "config/users/u1/policy.xml").stat().st_mode & 0o777 == 0o644


def test_restore_is_refused_while_emby_has_its_databases_open(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    (folder / "data/library.db-wal").write_bytes(b"")
    stop_emby(server)
    with pytest.raises(EmbyError, match=r"stop Emby before restoring - its databases are in use \(found data/library.db-wal\)"):
        app.restore_from_file(archive)
    assert all(r.method == "GET" for r in server.sent)


def test_restore_is_refused_while_emby_answers(app, server, tmp_path):
    archive = make_backup(app, tmp_path)
    server.routes["GET /emby/System/Info/Public"] = (200, {"Version": "4.10.2.0"})
    with pytest.raises(EmbyError, match="it's still answering at http://emby:8096"):
        app.restore_from_file(archive)


def test_an_unreachable_emby_counts_as_stopped(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    server.routes["GET /emby/System/Info/Public"] = (0, requests.ConnectionError("refused"))
    app.restore_from_file(archive)
    assert rows(folder / "data/library.db") == ["watched"]


def test_restore_needs_a_writable_folder(app, server, tmp_path, monkeypatch):
    archive = make_backup(app, tmp_path)
    stop_emby(server)
    monkeypatch.setattr(emby.os, "access", lambda path, mode: mode != os.W_OK)
    with pytest.raises(EmbyError, match="isn't writable - mount it read-write"):
        app.restore_from_file(archive)


def test_a_file_backuparr_cant_replace_stops_the_restore_before_any_write(app, server, folder, tmp_path, monkeypatch):
    archive = make_backup(app, tmp_path)
    make_db(folder / "data/library.db", rows=("current",)).close()
    stop_emby(server)
    locked = str(folder / "data" / "users.db")
    monkeypatch.setattr(emby.os, "access", lambda path, mode: not (path == locked and mode == os.W_OK))
    with pytest.raises(EmbyError, match="can't write data/users.db .* Emby's PUID:PGID"):
        app.restore_from_file(archive)
    assert rows(folder / "data/library.db") == ["current"]


def test_a_backup_from_a_newer_emby_is_refused(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    (folder / "data/lastversion.txt").write_text("4.9.1.0")
    stop_emby(server)
    with pytest.raises(EmbyError, match="comes from Emby 4.10.2.0, newer than the installed 4.9.1.0 - update Emby first"):
        app.restore_from_file(archive)


def test_a_pre_4_8_backup_is_refused_on_4_8_or_newer(app, server, folder, tmp_path):
    server.routes["GET /emby/System/Info"] = (200, {"Version": "4.7.14.0", "Id": SERVER_ID})
    archive = make_backup(app, tmp_path)
    stop_emby(server)
    with pytest.raises(EmbyError, match="before Emby 4.8 can't be restored onto Emby 4.8 or newer"):
        app.restore_from_file(archive)


def test_restore_refuses_a_file_that_isnt_an_emby_backup(app, server, folder, tmp_path):
    other = tmp_path / "emby_x.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("data/library.db", "x")
    with pytest.raises(EmbyError, match="isn't an Emby backup made by Backuparr"):
        app.restore_from_file(str(other))
    assert server.sent == []


@pytest.mark.parametrize("member", ["../escape.txt", "data/../../escape.txt", "cache/x.jpg", "data/library.db-wal",
                                    "logs/embyserver.txt", "/etc/passwd", "config//x"])
def test_restore_refuses_paths_outside_what_a_backup_holds(app, server, folder, tmp_path, member):
    archive = tmp_path / "emby_bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(MANIFEST, json.dumps({"emby_version": "4.10.2.0"}))
        zf.writestr(member, "x")
    stop_emby(server)
    with pytest.raises(EmbyError, match="unexpected path in the backup"):
        app.restore_from_file(str(archive))
    assert not (tmp_path / "escape.txt").exists()
