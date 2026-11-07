"""Snapshots of a database in a folder whose name means something in a URI,
and the quick check that every SQLite database Backuparr copies goes through."""
import sqlite3

import pytest

from apps import sqlite_snapshot


@pytest.mark.parametrize("folder", ["media#2", "50%off", "what?", "plain", "spaces in it"])
@pytest.mark.parametrize("wal", [False, True])
def test_a_snapshot_reads_the_right_file_whatever_its_path(tmp_path, folder, wal):
    data = tmp_path / folder
    data.mkdir()
    src = data / "library.db"
    db = sqlite3.connect(src)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (v TEXT)")
    db.execute("INSERT INTO t VALUES ('marker')")
    db.commit()
    try:
        dest = tmp_path / "copy.db"
        assert sqlite_snapshot.snapshot(str(src), str(dest)) == []
        assert sqlite3.connect(dest).execute("SELECT v FROM t").fetchall() == [("marker",)]
    finally:
        db.close()


def retype(path, table, sql):
    """Gives table a new definition without touching its rows, the way a
    long-running app's database can end up holding values its column types
    wouldn't store today. Returns a new connection, which reads the new
    definition."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA writable_schema=ON")
    db.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?", (sql, table))
    db.commit()
    db.close()
    return sqlite3.connect(path)


def with_type_notes(path, rows=3):
    """A Sonarr-like SceneMappings table with whole numbers in its TEXT
    columns, and number-like text in an INTEGER one."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE SceneMappings (Id INTEGER PRIMARY KEY, SearchTerm, ParseTerm, TvdbId)")
    db.executemany("INSERT INTO SceneMappings (SearchTerm, ParseTerm, TvdbId) VALUES (?, ?, ?)",
                   [(1883 + i, 1883 + i, "42") for i in range(rows)])
    db.commit()
    db.close()
    return retype(path, "SceneMappings", "CREATE TABLE SceneMappings (Id INTEGER PRIMARY KEY, SearchTerm TEXT, "
                                         "ParseTerm TEXT, TvdbId INTEGER)")


def test_type_notes_are_not_problems(tmp_path):
    db = with_type_notes(tmp_path / "sonarr.db")
    try:
        rows = [row[0] for row in db.execute("PRAGMA quick_check")]
        assert "NUMERIC value in SceneMappings.SearchTerm" in rows
        assert "TEXT value in SceneMappings.TvdbId" in rows
        assert sqlite_snapshot.quick_check(db) == []
    finally:
        db.close()


def test_type_notes_are_logged_where_backuparrs_logs_go(tmp_path, caplog):
    # A run's log and backup.log hang off the "backuparr" logger.
    caplog.set_level("DEBUG", logger="backuparr")
    db = with_type_notes(tmp_path / "sonarr.db")
    try:
        sqlite_snapshot.quick_check(db)
    finally:
        db.close()
    assert any(r.name.startswith("backuparr.") and "type notes" in r.getMessage() for r in caplog.records)


def test_a_snapshot_of_a_database_with_type_notes_is_sound(tmp_path):
    with_type_notes(tmp_path / "sonarr.db").close()
    assert sqlite_snapshot.snapshot(str(tmp_path / "sonarr.db"), str(tmp_path / "copy.db")) == []


def test_real_problems_are_still_reported_beside_type_notes(tmp_path):
    db = with_type_notes(tmp_path / "sonarr.db")
    try:
        db.execute("CREATE TABLE t (v TEXT)")
        db.execute("INSERT INTO t VALUES (NULL)")
        db.commit()
        db.close()
        db = retype(tmp_path / "sonarr.db", "t", "CREATE TABLE t (v TEXT NOT NULL)")
        assert sqlite_snapshot.quick_check(db) == ["NULL value in t.v"]
    finally:
        db.close()


def test_type_notes_cannot_use_up_the_rows_quick_check_returns(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_snapshot, "_MAX_ROWS", 5)
    db = with_type_notes(tmp_path / "sonarr.db", rows=10)
    try:
        assert sqlite_snapshot.quick_check(db) == ["over 5 type notes, so the rest wasn't checked"]
    finally:
        db.close()
