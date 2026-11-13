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



# --- write_archive (#196) -----------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 300
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 300
WEBP = b"RIFF\0\0\0\0WEBP" + b"\0" * 300


def app_folder(root):
    (root / "metadata/library/aa").mkdir(parents=True)
    (root / "metadata/library/aa/poster.jpg").write_bytes(JPEG)
    (root / "metadata/library/aa/logo.png").write_bytes(PNG)
    (root / "metadata/library/aa/backdrop").write_bytes(WEBP)  # Plex's images have no extension.
    (root / "metadata/library/aa/item.nfo").write_text("<nfo>" + "x" * 300 + "</nfo>")
    (root / "config").mkdir()
    (root / "config/system.xml").write_text("<cfg/>")
    return root


def test_write_archive_takes_staged_files_then_the_apps_files_read_where_they_are(tmp_path):
    root = app_folder(tmp_path / "app")
    (tmp_path / "stage/data").mkdir(parents=True)
    (tmp_path / "stage/data/library.db").write_bytes(b"snapshot")
    (tmp_path / "stage/backuparr-app.json").write_text("{}")
    added = mounted.write_archive(str(tmp_path / "out.zip"), str(tmp_path / "stage"),
                                  [(str(root / "metadata"), "metadata"), (str(root / "config/system.xml"), "config/system.xml"),
                                   (str(root / "missing"), "missing")], str(root), Boom, "app", "App")
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        assert zf.testzip() is None
        assert zf.namelist() == ["backuparr-app.json", "data/library.db", "metadata/library/aa/backdrop",
                                 "metadata/library/aa/item.nfo", "metadata/library/aa/logo.png",
                                 "metadata/library/aa/poster.jpg", "config/system.xml"]
        assert zf.read("metadata/library/aa/poster.jpg") == JPEG
        kinds = {i.filename: i.compress_type for i in zf.infolist()}
    assert added == 5
    # Images are compressed already: stored as they are; anything else is deflated.
    assert {n: kinds[n] for n in kinds if n.startswith("metadata/")} == {
        "metadata/library/aa/backdrop": zipfile.ZIP_STORED, "metadata/library/aa/logo.png": zipfile.ZIP_STORED,
        "metadata/library/aa/poster.jpg": zipfile.ZIP_STORED, "metadata/library/aa/item.nfo": zipfile.ZIP_DEFLATED}


def test_write_archive_follows_symlinks_only_inside_the_mounted_folder(tmp_path):
    root = app_folder(tmp_path / "app")
    (tmp_path / "secret").write_text("Backuparr's own file")
    links = root / "metadata/links"
    links.mkdir()
    (links / "inside").symlink_to(root / "metadata/library/aa/poster.jpg")
    (links / "outside").symlink_to(tmp_path / "secret")
    (links / "dangling").symlink_to(root / "gone")
    (root / "metadata/out-dir").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "stage").mkdir()
    mounted.write_archive(str(tmp_path / "out.zip"), str(tmp_path / "stage"), [(str(root / "metadata"), "metadata")],
                          str(root), Boom, "app", "App")
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        names = zf.namelist()
        assert zf.read("metadata/links/inside") == JPEG
    assert "metadata/links/outside" not in names and "metadata/links/dangling" not in names
    assert not any("secret" in n or n.startswith("metadata/out-dir") for n in names)


def test_write_archive_skips_a_file_the_app_removed_meanwhile(tmp_path, monkeypatch):
    root = app_folder(tmp_path / "app")
    (tmp_path / "stage").mkdir()
    real = zipfile.ZipFile.write

    def write(self, filename, *args, **kwargs):
        if filename.endswith("logo.png"):
            raise FileNotFoundError(2, "No such file or directory", filename)
        return real(self, filename, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", write)
    added = mounted.write_archive(str(tmp_path / "out.zip"), str(tmp_path / "stage"), [(str(root / "metadata"), "metadata")],
                                  str(root), Boom, "app", "App")
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        assert "metadata/library/aa/logo.png" not in zf.namelist()
    assert added == 3


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_write_archive_stops_at_a_file_or_folder_it_cant_read(tmp_path):
    root = app_folder(tmp_path / "app")
    (tmp_path / "stage").mkdir()
    poster = root / "metadata/library/aa/poster.jpg"
    poster.chmod(0)
    try:
        with pytest.raises(Boom, match=r"^app: can't read metadata/library/aa/poster.jpg in .* run Backuparr with App's PUID:PGID"):
            mounted.write_archive(str(tmp_path / "out.zip"), str(tmp_path / "stage"), [(str(root / "metadata"), "metadata")],
                                  str(root), Boom, "app", "App")
    finally:
        poster.chmod(0o644)
    folder = root / "metadata/library"
    folder.chmod(0)
    try:
        with pytest.raises(Boom, match=r"^app: can't read metadata/library in "):
            mounted.write_archive(str(tmp_path / "out.zip"), str(tmp_path / "stage"), [(str(root / "metadata"), "metadata")],
                                  str(root), Boom, "app", "App")
    finally:
        folder.chmod(0o755)


def test_write_files_makes_folders_readable_and_check_writable_refuses_first(tmp_path, monkeypatch):
    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("metadata/library/aa/poster.jpg", JPEG)
    root = tmp_path / "app"
    root.mkdir()
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        old_umask = os.umask(0o077)
        try:
            mounted.check_writable(members, str(root), Boom, "app", "App")
            mounted.write_files(zf, members, str(root))
        finally:
            os.umask(old_umask)
        assert (root / "metadata/library/aa/poster.jpg").read_bytes() == JPEG
        assert stat.S_IMODE((root / "metadata/library").stat().st_mode) == 0o755
        monkeypatch.setattr(mounted.os, "access", lambda path, mode: False)
        with pytest.raises(Boom, match=r"^app: can't write metadata/library/aa/poster.jpg in .* give it write access to App's files$"):
            mounted.check_writable(members, str(root), Boom, "app", "App")
