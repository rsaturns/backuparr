"""Consistent copies of a SQLite database another program keeps writing to,
for the drivers that read an app's mounted data folder (Emby, Plex, Seerr,
LazyLibrarian, Vaultwarden)."""
import logging
import os
import re
import sqlite3
from urllib.parse import quote

# Under "backuparr", like every driver, so its lines reach backup.log and the run log.
logger = logging.getLogger(f"backuparr.{__name__}")

# For an ordinary (non-STRICT) table, quick_check also reports a number kept
# in a TEXT column ("NUMERIC value in t.c") or number-like text in a numeric
# one ("TEXT value in t.c"). That's how the app stored the value: it reads
# back fine and a restore puts back the same, so it isn't damage. Long-running
# Sonarr databases have them in SceneMappings.
_TYPE_NOTE = re.compile(r"(NUMERIC|TEXT) value in .+")
# quick_check stops after this many rows; type notes mustn't use up the
# rows before any real damage is reported.
_MAX_ROWS = 1_000_000


def open_ro(src):
    """A running app in WAL mode keeps a -wal file next to its database,
    and a read-only connection reads it consistently through the shared
    -shm index. Without a -wal the file can't change under us, so it's
    opened immutable - which a read-only mount needs, since SQLite can't
    create the -shm file there. The path is quoted: in a URI, "?" and "#"
    end it and "%" starts an escape."""
    uri = f"file:{quote(str(src))}?mode=ro"
    if not os.path.exists(f"{src}-wal"):
        uri += "&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=30)


def quick_check(db):
    """SQLite's quick_check on an open connection: the problems it finds,
    an empty list when the database is sound. Type notes aren't problems."""
    rows = [row[0] for row in db.execute(f"PRAGMA quick_check({_MAX_ROWS})")]
    notes = [row for row in rows if _TYPE_NOTE.fullmatch(row)]
    if notes:
        logger.debug("quick_check type notes, not damage: %s", "; ".join(notes[:3]))
    problems = [row for row in rows if row != "ok" and not _TYPE_NOTE.fullmatch(row)]
    if not problems and len(rows) >= _MAX_ROWS:
        problems = [f"over {_MAX_ROWS} type notes, so the rest wasn't checked"]
    return problems


def snapshot(src, dest):
    """Copies src to dest with SQLite's online backup API, which takes a
    consistent snapshot while the app keeps writing. Returns the copy's
    quick_check problems: an empty list when it's sound."""
    source = open_ro(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
            return quick_check(target)
        finally:
            target.close()
    finally:
        source.close()
