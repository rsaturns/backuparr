"""apps.mounted: helpers shared by the drivers that work on a mounted folder."""
import io
import os
import sqlite3
import stat
import zipfile

import pytest

from apps import mounted


class Boom(RuntimeError):
    pass


def test_require_dir_says_what_to_set_and_where_to_mount(tmp_path):
    with pytest.raises(Boom, match="^app: set the data folder - App's /config folder, mounted into this container$"):
        mounted.require_dir("", Boom, "app", "data folder", "App's /config folder", "mount it")
    with pytest.raises(Boom, match="doesn't exist in this container - mount it$"):
        mounted.require_dir(str(tmp_path / "nope"), Boom, "app", "data folder", "App's /config folder", "mount it")
    mounted.require_dir(str(tmp_path), Boom, "app", "data folder", "App's /config folder", "mount it")


def test_is_backup_zip_needs_every_name_and_a_sound_zip(tmp_path):
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("db", "x")
    assert mounted.is_backup_zip(path, {"manifest.json", "db"})
    assert not mounted.is_backup_zip(path, {"manifest.json", "settings"})
    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    assert not mounted.is_backup_zip(tmp_path / "bad.zip", {"manifest.json"})


def test_write_in_place_keeps_an_existing_files_mode_and_makes_a_new_one_readable(tmp_path):
    old_umask = os.umask(0o077)
    try:
        existing = tmp_path / "existing"
        existing.write_text("old")
        existing.chmod(0o600)
        mounted.write_in_place(io.BytesIO(b"new"), existing)
        mounted.write_in_place(io.BytesIO(b"fresh"), tmp_path / "created")
    finally:
        os.umask(old_umask)
    assert existing.read_text() == "new" and stat.S_IMODE(existing.stat().st_mode) == 0o600
    assert (tmp_path / "created").read_text() == "fresh"
    assert stat.S_IMODE((tmp_path / "created").stat().st_mode) == 0o644


def test_claim_database_is_refused_while_another_connection_writes(tmp_path):
    path = str(tmp_path / "app.db")
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (x)")
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Boom, match="^app: stop App before restoring - its database is in use"):
            mounted.claim_database(path, Boom, "app", "App")
    finally:
        db.execute("ROLLBACK")
        db.close()
    mounted.claim_database(path, Boom, "app", "App")
    assert not os.path.exists(path + "-wal")


def test_remove_journal_files(tmp_path):
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"app.db{suffix}").write_text("")
    mounted.remove_journal_files(str(tmp_path / "app.db"))
    mounted.remove_journal_files(str(tmp_path / "app.db"))  # nothing left to remove
    assert list(tmp_path.iterdir()) == []
