"""Consistent copies of a SQLite database another program keeps writing to,
for the drivers that read an app's mounted data folder (Emby, Seerr)."""
import os
import sqlite3


def open_ro(src):
    """A running app in WAL mode keeps a -wal file next to its database,
    and a read-only connection reads it consistently through the shared
    -shm index. Without a -wal the file can't change under us, so it's
    opened immutable - which a read-only mount needs, since SQLite can't
    create the -shm file there."""
    uri = f"file:{src}?mode=ro"
    if not os.path.exists(f"{src}-wal"):
        uri += "&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=30)


def snapshot(src, dest):
    """Copies src to dest with SQLite's online backup API, which takes a
    consistent snapshot while the app keeps writing. Returns the copy's
    quick_check result: "ok" when it's sound."""
    source = open_ro(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
            return target.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
