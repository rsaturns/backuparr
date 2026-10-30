"""Snapshots of a database in a folder whose name means something in a URI."""
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
        assert sqlite_snapshot.snapshot(str(src), str(dest)) == "ok"
        assert sqlite3.connect(dest).execute("SELECT v FROM t").fetchall() == [("marker",)]
    finally:
        db.close()
