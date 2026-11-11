import io
import json
import os
import sqlite3
import zipfile

import pytest
import requests

import backup
import restore_actions
from apps import vaultwarden
from apps.vaultwarden import MANIFEST, VaultwardenApp, VaultwardenError
from config_store import always_encrypted, restore_supported

MIGRATION = "20261007120000"  # Vaultwarden 1.37.5's newest migration (lab, 2026-11-11)
RSA_KEY = "-----BEGIN PRIVATE KEY-----\nlab\n-----END PRIVATE KEY-----\n"


def make_db(path, users=("alice@lab.local",), migration=MIGRATION, wal=False):
    db = sqlite3.connect(path)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS __diesel_schema_migrations (version VARCHAR(50) PRIMARY KEY, run_on TIMESTAMP)")
    db.execute("CREATE TABLE IF NOT EXISTS users (email TEXT)")
    db.execute("DELETE FROM __diesel_schema_migrations")
    db.execute("DELETE FROM users")
    db.executemany("INSERT INTO __diesel_schema_migrations (version) VALUES (?)", [("20180114171611",), (migration,)])
    db.executemany("INSERT INTO users VALUES (?)", [(u,) for u in users])
    db.commit()
    return db


def users(path):
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT email FROM users ORDER BY email")]
    finally:
        db.close()


@pytest.fixture
def folder(tmp_path):
    """A stopped Vaultwarden's /data, laid out as 1.37.5 writes it."""
    root = tmp_path / "vaultwarden"
    for sub in ("attachments/cipher-1", "sends/send-1", "icon_cache", "tmp"):
        (root / sub).mkdir(parents=True)
    make_db(root / "db.sqlite3").close()
    (root / "rsa_key.pem").write_text(RSA_KEY)
    (root / "config.json").write_text(json.dumps({"admin_token": "secret", "signups_allowed": False}))
    (root / "attachments/cipher-1/file-1").write_bytes(b"attachment")
    (root / "sends/send-1/file-2").write_bytes(b"send file")
    (root / "icon_cache/example.com.png").write_bytes(b"png")
    (root / "tmp/upload").write_bytes(b"tmp")
    return root


@pytest.fixture
def server(monkeypatch):
    """Answers each "METHOD /path" from `routes` (status, body); records requests."""
    state = type("Server", (), {})()
    state.sent = []
    state.routes = {"GET /api/version": (200, "1.37.5"), "GET /alive": (200, "2026-11-11T05:24:48Z")}

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
    return VaultwardenApp("http://vaultwarden/", data_dir=str(folder))


def make_backup(app, tmp_path):
    archive = str(tmp_path / "vaultwarden_20261111_030000.zip")
    backup.zip_dir(app.backup(str(tmp_path / "work")), archive)
    return archive


def stop(server):
    server.routes["GET /alive"] = (502, {})  # A proxy in front of a stopped Vaultwarden.


# --- connection --------------------------------------------------------------

def test_it_has_no_key_and_is_always_encrypted(folder):
    app = backup.build_app("vaultwarden", {"url": "http://vaultwarden", "api_key": "", "data_dir": str(folder)})
    assert isinstance(app, VaultwardenApp) and app.data_dir == str(folder)
    assert always_encrypted("vaultwarden") and restore_supported("vaultwarden")


def test_connection_reads_the_version_without_a_login(app, server):
    assert app.test_connection() == "vaultwarden 1.37.5 reachable, data folder OK"
    assert server.sent[0].url == "http://vaultwarden/api/version"
    assert "Authorization" not in server.sent[0].headers


@pytest.mark.parametrize("route", [(404, {}), (200, {"status": "ok"})])
def test_something_else_at_the_url_is_reported(app, server, route):
    server.routes["GET /api/version"] = route
    with pytest.raises(VaultwardenError, match="doesn't look like Vaultwarden"):
        app.test_connection()


def test_the_data_folder_must_be_set_and_exist(server, tmp_path):
    with pytest.raises(VaultwardenError, match="set the data folder"):
        VaultwardenApp("http://vaultwarden").test_connection()
    with pytest.raises(VaultwardenError, match="doesn't exist in this container"):
        VaultwardenApp("http://vaultwarden", data_dir=str(tmp_path / "missing")).test_connection()


def test_a_folder_without_sqlite_is_explained(app, server, folder):
    os.remove(folder / "db.sqlite3")
    with pytest.raises(VaultwardenError, match="no db.sqlite3 - mount the folder Vaultwarden has at /data.*PostgreSQL or MySQL"):
        app.test_connection()


def test_an_unreadable_database_is_reported(app, server, folder):
    (folder / "db.sqlite3").write_bytes(b"not a database, just some bytes" * 200)
    with pytest.raises(VaultwardenError, match="can't read db.sqlite3"):
        app.test_connection()


def test_files_backuparr_cant_write_pass_with_a_note(app, server, folder, monkeypatch):
    # Vaultwarden runs as root by default, so its files are root's.
    real = os.access
    monkeypatch.setattr(vaultwarden.os, "access", lambda path, mode: real(path, mode) and not (
        mode == os.W_OK and str(path).endswith("db.sqlite3")))
    assert app.test_connection() == ("vaultwarden 1.37.5 reachable; Backuparr can't write its files, so backups work "
                                     "but restores won't (run Vaultwarden as Backuparr's user to restore here)")


def test_connection_failures_are_left_for_the_shared_explanation(app, server):
    server.routes["GET /api/version"] = (0, requests.ConnectionError("refused"))
    with pytest.raises(requests.ConnectionError):
        app.test_connection()


# --- backup ------------------------------------------------------------------

def test_backup_takes_what_vaultwardens_guide_lists_and_nothing_else(app, server, tmp_path):
    with zipfile.ZipFile(make_backup(app, tmp_path)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read(MANIFEST))
        assert zf.read("rsa_key.pem").decode() == RSA_KEY
        assert zf.read("attachments/cipher-1/file-1") == b"attachment"
    assert names == {MANIFEST, "db.sqlite3", "rsa_key.pem", "config.json", "attachments/cipher-1/file-1",
                     "sends/send-1/file-2"}
    assert manifest == {"vaultwarden_version": "1.37.5", "migration": MIGRATION}


def test_older_rsa_key_files_and_a_missing_config_are_handled(app, server, folder, tmp_path):
    (folder / "rsa_key.der").write_bytes(b"der")
    (folder / "rsa_key.pub.der").write_bytes(b"pub")
    os.remove(folder / "config.json")
    with zipfile.ZipFile(make_backup(app, tmp_path)) as zf:
        names = set(zf.namelist())
    assert {"rsa_key.pem", "rsa_key.der", "rsa_key.pub.der"} <= names and "config.json" not in names


def test_links_and_odd_names_are_left_out(app, server, folder, tmp_path, caplog):
    (tmp_path / "outside").write_text("not Vaultwarden's")
    os.symlink(tmp_path / "outside", folder / "attachments/cipher-1/link")
    os.symlink(tmp_path, folder / "sends/linked-dir")
    (folder / "attachments/cipher-1/.hidden").write_text("x")
    with zipfile.ZipFile(make_backup(app, tmp_path)) as zf:
        names = set(zf.namelist())
    assert not {n for n in names if "link" in n or ".hidden" in n}
    assert "left out attachments/cipher-1/.hidden" in caplog.text


def test_a_live_database_is_snapshotted_with_its_unflushed_changes(app, server, folder, tmp_path):
    writer = make_db(folder / "db.sqlite3", users=("before",), wal=True)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO users VALUES ('in the wal')")
    writer.commit()  # Vaultwarden keeps its connections open, so the change stays in db.sqlite3-wal.
    assert (folder / "db.sqlite3-wal").stat().st_size > 0
    archive = make_backup(app, tmp_path)
    writer.close()
    with zipfile.ZipFile(archive) as zf:
        zf.extract("db.sqlite3", tmp_path / "out")
    assert users(tmp_path / "out/db.sqlite3") == ["before", "in the wal"]


def test_a_backup_without_an_rsa_key_says_what_that_means(app, server, folder, tmp_path, caplog):
    os.remove(folder / "rsa_key.pem")
    make_backup(app, tmp_path)
    assert "no rsa_key.pem" in caplog.text and "signs everyone out" in caplog.text


# --- restore -----------------------------------------------------------------

def test_restore_brings_back_the_database_and_files(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db.sqlite3", users=("added later",)).close()
    (folder / "rsa_key.pem").write_text("a new key")
    (folder / "config.json").write_text("{}")
    os.remove(folder / "attachments/cipher-1/file-1")
    (folder / "attachments/cipher-2").mkdir()
    (folder / "attachments/cipher-2/newer").write_bytes(b"added since")
    (folder / "db.sqlite3-shm").write_bytes(b"stale")
    stop(server)

    app.restore_from_file(archive)

    assert users(folder / "db.sqlite3") == ["alice@lab.local"]
    assert (folder / "rsa_key.pem").read_text() == RSA_KEY
    assert json.loads((folder / "config.json").read_text())["admin_token"] == "secret"
    assert (folder / "attachments/cipher-1/file-1").read_bytes() == b"attachment"
    assert not (folder / "db.sqlite3-shm").exists() and not (folder / "db.sqlite3-wal").exists()
    # Not in the backup, so left alone.
    assert (folder / "attachments/cipher-2/newer").exists() and (folder / "icon_cache/example.com.png").exists()


def test_restore_onto_a_fresh_install_makes_its_folders_readable(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    for path in ("attachments/cipher-1/file-1", "sends/send-1/file-2", "config.json"):
        os.remove(folder / path)
    for path in ("attachments/cipher-1", "attachments", "sends/send-1", "sends"):
        os.rmdir(folder / path)
    os.umask(0o077)  # Backuparr's own, private umask
    try:
        stop(server)
        app.restore_from_file(archive)
    finally:
        os.umask(0o022)
    assert (folder / "attachments/cipher-1").stat().st_mode & 0o777 == 0o755
    assert (folder / "attachments/cipher-1/file-1").stat().st_mode & 0o777 == 0o644
    assert (folder / "config.json").stat().st_mode & 0o777 == 0o644


def test_restore_keeps_each_files_owner_and_mode(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    os.chmod(folder / "rsa_key.pem", 0o600)
    os.chmod(folder / "db.sqlite3", 0o640)
    stop(server)
    app.restore_from_file(archive)
    assert (folder / "rsa_key.pem").stat().st_mode & 0o777 == 0o600
    assert (folder / "db.sqlite3").stat().st_mode & 0o777 == 0o640


def test_a_stopped_vaultwardens_leftover_wal_is_never_replayed_over_the_restore(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    writer = make_db(folder / "db.sqlite3", users=("before",), wal=True)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO users VALUES ('left in the wal')")
    writer.commit()
    stale_wal = (folder / "db.sqlite3-wal").read_bytes()
    writer.close()
    (folder / "db.sqlite3-wal").write_bytes(stale_wal)  # As a stopped Vaultwarden leaves it.
    stop(server)
    app.restore_from_file(archive)
    assert not (folder / "db.sqlite3-wal").exists()
    assert users(folder / "db.sqlite3") == ["alice@lab.local"]


def test_restore_is_refused_while_vaultwarden_answers(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    with pytest.raises(VaultwardenError, match="it's still answering at http://vaultwarden"):
        app.restore_from_file(archive)
    assert all(r.method == "GET" for r in server.sent)


def test_restore_is_refused_while_something_has_the_database_open(app, server, folder, tmp_path):
    # Vaultwarden running where Backuparr can't reach it: its open database gives it away.
    archive = make_backup(app, tmp_path)
    running = make_db(folder / "db.sqlite3", users=("current",), wal=True)
    stop(server)
    try:
        with pytest.raises(VaultwardenError, match="its database is in use"):
            app.restore_from_file(archive)
    finally:
        running.close()
    assert users(folder / "db.sqlite3") == ["current"]


def test_an_unreachable_vaultwarden_counts_as_stopped(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    server.routes["GET /alive"] = (0, requests.ConnectionError("refused"))
    make_db(folder / "db.sqlite3", users=("current",)).close()
    app.restore_from_file(archive)
    assert users(folder / "db.sqlite3") == ["alice@lab.local"]


@pytest.mark.parametrize("locked", ["db.sqlite3", "attachments/cipher-1", "rsa_key.pem"])
def test_nothing_is_written_unless_everything_can_be(app, server, folder, tmp_path, monkeypatch, locked):
    archive = make_backup(app, tmp_path)
    os.remove(folder / "attachments/cipher-1/file-1")  # so the folder must take a new file
    make_db(folder / "db.sqlite3", users=("current",)).close()
    stop(server)
    blocked = str(folder / locked)
    real = os.access
    monkeypatch.setattr(vaultwarden.os, "access", lambda path, mode: real(path, mode) and not (
        mode == os.W_OK and str(path) == blocked))
    with pytest.raises(VaultwardenError, match=f"can't write {blocked} - mount the data folder read-write.*runs as root"):
        app.restore_from_file(archive)
    assert users(folder / "db.sqlite3") == ["current"]
    assert not (folder / "attachments/cipher-1/file-1").exists()


def test_a_backup_from_a_newer_vaultwarden_is_refused(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db.sqlite3", users=("current",), migration="20260902120000").close()
    stop(server)
    with pytest.raises(VaultwardenError, match="newer Vaultwarden than the one installed - update Vaultwarden first"):
        app.restore_from_file(archive)
    assert users(folder / "db.sqlite3") == ["current"]


def test_a_backup_from_an_older_vaultwarden_is_restored_for_it_to_migrate(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db.sqlite3", users=("current",), migration="20270101120000").close()
    stop(server)
    app.restore_from_file(archive)
    assert users(folder / "db.sqlite3") == ["alice@lab.local"]


def test_restore_refuses_a_file_that_isnt_a_vaultwarden_backup(app, server, tmp_path):
    other = tmp_path / "vaultwarden_x.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("db.sqlite3", "x")
    with pytest.raises(VaultwardenError, match="isn't a Vaultwarden backup made by Backuparr"):
        app.restore_from_file(str(other))
    assert server.sent == []


@pytest.mark.parametrize("extra", ["../escape.txt", "attachments/../escape.txt", "attachments/a/../../escape.txt",
                                   "db.sqlite3-wal", "icon_cache/x.png", "/etc/passwd", "attachments", "rsa_key.pem.bak",
                                   "sends/.hidden"])
def test_restore_refuses_anything_but_what_a_backup_holds(app, server, folder, tmp_path, extra):
    archive = tmp_path / "vaultwarden_bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(MANIFEST, json.dumps({"vaultwarden_version": "1.37.5"}))
        zf.writestr("db.sqlite3", (folder / "db.sqlite3").read_bytes())
        zf.writestr(extra, "x")
    stop(server)
    with pytest.raises(VaultwardenError, match="unexpected contents in the backup"):
        app.restore_from_file(str(archive))
    assert not (tmp_path / "escape.txt").exists()


def test_a_backup_with_a_broken_database_is_refused_before_any_write(app, server, folder, tmp_path):
    archive = tmp_path / "vaultwarden_bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(MANIFEST, json.dumps({"vaultwarden_version": "1.37.5"}))
        zf.writestr("db.sqlite3", b"not a database, just some bytes" * 200)
        zf.writestr("rsa_key.pem", "from the bad backup")
    stop(server)
    with pytest.raises(VaultwardenError, match="the backup's database can't be read"):
        app.restore_from_file(str(archive))
    assert (folder / "rsa_key.pem").read_text() == RSA_KEY


def test_the_restore_action_uses_the_data_folder(app, server, folder, tmp_path):
    archive = make_backup(app, tmp_path)
    make_db(folder / "db.sqlite3", users=("current",)).close()
    stop(server)
    cfg = {"url": "http://vaultwarden", "data_dir": str(folder)}
    assert restore_actions.restore_app("vaultwarden", cfg, str(tmp_path), archive) == {"kind": "vaultwarden"}
    assert users(folder / "db.sqlite3") == ["alice@lab.local"]
