"""The pre-upload check (#107): an archive is only uploaded once it opens,
reads back intact, its SQLite databases pass a quick check and the files
the app's restore needs are there."""
import io
import json
import os
import sqlite3
import tarfile
import zipfile

import pytest

import archive_check
import backup
import destination_util
import manifest
import rclone_util

pytestmark = pytest.mark.archive_check


def sqlite_bytes(tmp_path, setup=None, name="db"):
    path = tmp_path / f"{name}.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.executemany("INSERT INTO t (v) VALUES (?)", [(f"row {i}",) for i in range(500)])
    if setup:
        setup(db)
    db.commit()
    db.close()
    data = path.read_bytes()
    path.unlink()
    return data


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return str(path)


def make_tar(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return str(path)


def sound_archives(tmp_path):
    """One minimal sound backup per app type, laid out as the real ones are."""
    db = sqlite_bytes(tmp_path)
    j = lambda value: json.dumps(value).encode()  # noqa: E731
    return {
        "radarr": make_zip(tmp_path / "radarr.zip", {"config.xml": "<Config/>", "radarr.db": db, "INFO": "v6.4\n"}),
        "sonarr-4k": make_zip(tmp_path / "sonarr.zip", {"config.xml": "<Config/>", "sonarr.db": db, "INFO": "v4.0\n"}),
        "lidarr": make_zip(tmp_path / "lidarr.zip", {"config.xml": "<Config/>", "lidarr.db": db, "INFO": "v3.1\n"}),
        "whisparr-v3": make_zip(tmp_path / "w3.zip", {"config.xml": "<Config/>", "whisparr3.db": db,
                                                       "whisparr3.db-journal": b"\0" * 512, "INFO": "v3.6\n"}),
        "prowlarr": make_zip(tmp_path / "prowlarr.zip", {"config.xml": "<Config/>"}),  # PostgreSQL: no database
        "profilarr": make_tar(tmp_path / "p.tar.gz", {"data/profilarr.db": db, "data/INFO.json": j({"appVersion": "2.3.1"})}),
        "bazarr": make_zip(tmp_path / "bazarr.zip", {"bazarr.db": db, "config.yaml": "general: {}"}),
        "tdarr": make_zip(tmp_path / "tdarr.zip", {"LibrarySettingsJSONDB.json": j([{"_id": "a"}]), "FlowsJSONDB.json": j([])}),
        "sabnzbd": make_zip(tmp_path / "sab.zip", {"sabnzbd_config.json": j({"config": {}}), "_READ_ME_FIRST.txt": "x"}),
        "nzbget": make_zip(tmp_path / "nzbget.zip", {"nzbget.conf": "MainDir=/data", "backuparr-nzbget.json": j({"nzbget_version": "26.3"})}),
        "tautulli": make_zip(tmp_path / "tautulli.zip", {"tautulli.db": db, "config.ini": "[General]"}),
        "jellyfin": make_zip(tmp_path / "jellyfin.zip", {"manifest.json": j({"ServerVersion": "12.4.0.0"}), "Database/Users.json": j([])}),
        "emby": make_zip(tmp_path / "emby.zip", {"backuparr-emby.json": j({"emby_version": "4.10.2.0"}), "data/library.db": db,
                                                  "data/users.db": db, "config/system.xml": "<x/>"}),
        "plex": make_zip(tmp_path / "plex.zip", {"backuparr-plex.json": j({"plex_version": "1.43"}),
                                                  "com.plexapp.plugins.library.db": db, "Preferences.xml": "<Preferences/>"}),
        "seerr": make_zip(tmp_path / "seerr.zip", {"backuparr-seerr.json": j({"seerr_version": "3.5.1"}), "db/db.sqlite3": db,
                                                    "settings.json": j({"main": {}})}),
        "lazylibrarian": make_tar(tmp_path / "ll.tgz", {"lazylibrarian.db": db, "config.ini": b"[General]\n"}),
    }


def test_a_sound_backup_of_every_app_passes(tmp_path):
    archives = sound_archives(tmp_path)
    assert set(map(archive_check.instance_type, archives)) == set(backup.DRIVERS)
    for app, path in archives.items():
        archive_check.check(app, path)


@pytest.mark.parametrize("app, members, missing", [
    ("radarr", {"radarr.db": b""}, "config.xml is missing"),
    ("bazarr", {"bazarr.db": b""}, "config.yaml or config.ini is missing"),
    ("tdarr", {"notes.txt": b""}, "LibrarySettingsJSONDB.json or SettingsGlobalJSONDB.json ... is missing"),
    ("plex", {"backuparr-plex.json": b"{}", "Preferences.xml": b""}, "com.plexapp.plugins.library.db is missing"),
    ("seerr", {"backuparr-seerr.json": b"{}", "settings.json": b"{}"}, "db/db.sqlite3 is missing"),
    ("nzbget", {"nzbget.conf": b""}, "backuparr-nzbget.json is missing"),
])
def test_a_backup_missing_what_its_restore_needs_fails(tmp_path, app, members, missing):
    with pytest.raises(archive_check.ArchiveCheckError) as raised:
        archive_check.check(app, make_zip(tmp_path / "b.zip", members))
    assert str(raised.value) == f"{app}: the backup failed its check, so it wasn't uploaded - {missing} from it"


def test_older_bazarr_backups_with_config_ini_pass(tmp_path):
    archive_check.check("bazarr", make_zip(tmp_path / "b.zip", {"bazarr.db": sqlite_bytes(tmp_path), "config.ini": "[general]"}))


def test_a_needed_json_file_that_does_not_parse_fails(tmp_path):
    with pytest.raises(archive_check.ArchiveCheckError, match="sabnzbd_config.json in it isn't valid JSON"):
        archive_check.check("sabnzbd", make_zip(tmp_path / "s.zip", {"sabnzbd_config.json": '{"config": '}))


def test_a_file_whose_contents_are_damaged_fails(tmp_path):
    path = make_zip(tmp_path / "r.zip", {"config.xml": "<Config/>" * 200})
    data = bytearray(open(path, "rb").read())
    offset = data.index(b"<Config/>")  # stored uncompressed by writestr's default
    data[offset] ^= 0xFF
    open(path, "wb").write(data)
    with pytest.raises(archive_check.ArchiveCheckError, match="config.xml in it is damaged"):
        archive_check.check("radarr", path)


def test_a_cut_short_tar_fails(tmp_path):
    path = make_tar(tmp_path / "ll.tgz", {"lazylibrarian.db": os.urandom(200_000), "config.ini": b"[General]\n"})
    data = open(path, "rb").read()
    open(path, "wb").write(data[: len(data) // 2])
    with pytest.raises(archive_check.ArchiveCheckError, match="it's damaged"):
        archive_check.check("lazylibrarian", path)


def test_something_that_is_not_an_archive_fails(tmp_path):
    path = tmp_path / "radarr.zip"
    path.write_bytes(b"<html>502 Bad Gateway</html>")
    with pytest.raises(archive_check.ArchiveCheckError, match="isn't a zip or tar archive"):
        archive_check.check("radarr", str(path))


def test_a_damaged_database_fails_the_quick_check(tmp_path):
    db = bytearray(sqlite_bytes(tmp_path))
    for offset in range(4096, 4096 + 800):  # wreck the table's first page
        db[offset] = 0xAB
    path = make_zip(tmp_path / "t.zip", {"tautulli.db": bytes(db), "config.ini": "[General]"})
    with pytest.raises(archive_check.ArchiveCheckError, match="tautulli.db in it failed SQLite's quick check"):
        archive_check.check("tautulli", path)


def test_a_database_with_type_notes_passes(tmp_path):
    """A long-running Sonarr keeps whole numbers in SceneMappings' TEXT
    columns; quick_check notes them, but the database is sound."""
    def numbers_in_text_columns(db):
        db.execute("CREATE TABLE SceneMappings (Id INTEGER PRIMARY KEY, SearchTerm, ParseTerm)")
        db.execute("INSERT INTO SceneMappings (SearchTerm, ParseTerm) VALUES (1883, 1883)")
        db.commit()
        db.execute("PRAGMA writable_schema=ON")
        db.execute("UPDATE sqlite_master SET sql = 'CREATE TABLE SceneMappings (Id INTEGER PRIMARY KEY, "
                   "SearchTerm TEXT, ParseTerm TEXT)' WHERE name = 'SceneMappings'")
    db = sqlite_bytes(tmp_path, numbers_in_text_columns)
    archive_check.check("sonarr", make_zip(tmp_path / "s.zip", {"config.xml": "<Config/>", "sonarr.db": db}))


def test_a_file_that_is_not_a_database_fails(tmp_path):
    path = make_zip(tmp_path / "t.zip", {"tautulli.db": b"not a database" * 100, "config.ini": "[General]"})
    with pytest.raises(archive_check.ArchiveCheckError, match="tautulli.db in it failed SQLite's quick check"):
        archive_check.check("tautulli", path)


def plex_like(db):
    """Plex's library database: an index on a collation Plex registers
    itself, and virtual tables on modules stock SQLite doesn't have."""
    db.create_collation("icu_root", lambda a, b: (a > b) - (a < b))
    db.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, tag TEXT COLLATE icu_root)")
    db.execute("CREATE INDEX tags_tag ON tags (tag)")
    db.executemany("INSERT INTO tags (tag) VALUES (?)", [(f"tag {i}",) for i in range(200)])
    db.execute("PRAGMA writable_schema=ON")
    db.execute("INSERT INTO sqlite_master VALUES ('table', 'fts4_tag_titles_icu', 'fts4_tag_titles_icu', 0, "
               "'CREATE VIRTUAL TABLE fts4_tag_titles_icu USING fts4(tag, tokenize=collating)')")
    db.execute("INSERT INTO sqlite_master VALUES ('table', 'spellfix_tag_titles', 'spellfix_tag_titles', 0, "
               "'CREATE VIRTUAL TABLE spellfix_tag_titles USING spellfix1')")


def test_a_plex_database_with_its_own_collation_and_virtual_tables_passes(tmp_path):
    db = sqlite_bytes(tmp_path, plex_like)
    path = make_zip(tmp_path / "plex.zip", {"backuparr-plex.json": "{}", "com.plexapp.plugins.library.db": db,
                                            "Preferences.xml": "<Preferences/>"})
    archive_check.check("plex", path)
    with zipfile.ZipFile(path) as z:  # the check worked on its own copy
        assert z.read("com.plexapp.plugins.library.db") == db


def test_an_instance_is_checked_as_its_app_type(tmp_path):
    with pytest.raises(archive_check.ArchiveCheckError, match="^radarr-4k: .*config.xml is missing"):
        archive_check.check("radarr-4k", make_zip(tmp_path / "r.zip", {"radarr.db": b""}))


# ---------------------------------------------------------- backup run ----

class BrokenRadarr:
    def backup(self, dest_dir):
        path = os.path.join(dest_dir, "radarr_backup.zip")
        make_zip(path, {"radarr.db": b"not a database"})
        return path

    def app_version(self):
        return "6.4"


def test_a_backup_that_fails_the_check_is_not_uploaded_or_recorded(monkeypatch, caplog):
    uploads = []
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["radarr"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local"])
    monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: "dest:")
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: BrokenRadarr())
    monkeypatch.setattr(backup, "apply_retention", lambda *args, **kwargs: None)
    monkeypatch.setattr(rclone_util, "copyto", lambda src, dst: uploads.append(dst))
    ok, failed = backup.run_backup({"apps": {"radarr": {"url": "http://radarr:7878"}}, "destinations": {"local": {}}})
    assert ok == [] and uploads == [] and manifest.backups() == {}
    assert failed == ["radarr: the backup failed its check, so it wasn't uploaded - config.xml is missing from it"]
    assert "Traceback" not in caplog.text  # one clean line, not a stack
