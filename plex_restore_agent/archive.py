"""Bounded streaming extraction into fixed filenames, never ZIP member paths."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import zipfile

from apps.plex_archive import library_databases
from plex_restore_agent.docker import AgentError


def copy_member(archive, info, target, limit):
    if info.file_size > limit or info.flag_bits & 1:
        raise AgentError("Backup member is too large or encrypted")
    if shutil.disk_usage(target.parent).free < info.file_size + 64 * 1024 * 1024:
        raise AgentError("Not enough free space to stage the Plex backup")
    with archive.open(info) as source, target.open("xb") as out:
        os.chmod(target, 0o600)
        total = 0
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise AgentError("Expanded backup exceeds the configured size limit")
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())


def stage_database(upload, directory, limit):
    """Require the version/identity manifest added with automated restore."""
    directory = Path(directory)
    native = directory / "databases.zip"
    database = directory / "incoming.db"
    try:
        with zipfile.ZipFile(upload) as archive:
            names = archive.namelist()
            if names.count("manifest.json") != 1 or names.count("databases.zip") != 1:
                raise AgentError("Expected a Backuparr Plex export with one manifest and database archive")
            info = archive.getinfo("manifest.json")
            if info.file_size > 1024 * 1024:
                raise AgentError("Backup manifest is too large")
            manifest = json.loads(archive.read(info))
            if (not isinstance(manifest, dict) or manifest.get("format") != "backuparr-plex-export"
                    or manifest.get("format_version") != 1
                    or manifest.get("database_archive") != "databases.zip"):
                raise AgentError("Unsupported Plex backup format")
            identity = manifest.get("server")
            if (not isinstance(identity, dict) or any(
                    not isinstance(identity.get(field), str) or not identity[field]
                    or len(identity[field]) > 200 for field in ("version", "machine_identifier"))):
                raise AgentError("Backup has no Plex version/server identity; create a new backup or restore the older export manually")
            copy_member(archive, archive.getinfo("databases.zip"), native, limit)
        with zipfile.ZipFile(native) as archive:
            candidates = library_databases(archive)
            if len(candidates) != 1:
                raise AgentError("Backup must contain exactly one Plex library database")
            copy_member(archive, candidates[0], database, limit)
        with database.open("rb") as source:
            if source.read(16) != b"SQLite format 3\0":
                raise AgentError("Invalid SQLite database header")
        # Stock SQLite cannot run integrity_check on Plex's custom 'collating'
        # tokenizer. Validate the schema/readability without pretending otherwise.
        with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
            for table in ("metadata_items", "metadata_item_settings", "library_sections"):
                db.execute(f"SELECT * FROM {table} LIMIT 1").fetchall()
        return {key: identity[key] for key in ("version", "machine_identifier")}
    except AgentError:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, ValueError,
            sqlite3.Error, EOFError):
        raise AgentError("Invalid or corrupt Plex database archive") from None
    finally:
        native.unlink(missing_ok=True)
