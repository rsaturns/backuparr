"""Backuparr's record of the backups it made: manifest.json beside
config.json. For each archive it keeps the destinations holding it, the
version of the app that made it, Backuparr's version, its size, its
SHA-256 (of the file as stored, so of the encrypted file when it's
encrypted) and whether it's encrypted (#147).

The destinations stay the truth for which backups exist: History and
retention list them, and read only those details from here. An entry for
a file that's no longer on a destination is dropped the next time
retention lists that destination."""
import hashlib
import json
import logging
import os
import re
import threading

import atomic_file
import config_store

log = logging.getLogger("backuparr.manifest")

SCHEMA = 1
_lock = threading.RLock()
# Bumped by clear(): the startup backfill stops when it changes under it.
_generation = 0


def path():
    return os.path.join(os.path.dirname(config_store.CONFIG_PATH), "manifest.json")


def key(app, file):
    return f"{app}/{file}"


def normalise_version(app_type, version):
    """The form versions are stored and compared in: no "v" prefix
    (Tautulli's v2.18.2, the *arrs' INFO file), Jellyfin's 12.4.0.0 in an
    archive the same as 12.4.0 from its API, LazyLibrarian's commit in
    lower case. None when there's no version."""
    if version is None:
        return None
    text = str(version).strip()
    if not text or text == "?":
        return None
    if re.match(r"[vV]\d", text):
        text = text[1:]
    if app_type == "jellyfin":
        parts = text.split(".")
        if len(parts) == 4 and parts[3] == "0":
            text = ".".join(parts[:3])
    elif app_type == "lazylibrarian":
        text = text.lower()
    return text


def file_sha256(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read():
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # Keep the unreadable file for a look, and start again: the backfill
        # rebuilds what's on the destinations.
        bad = path() + ".bad"
        try:
            os.replace(path(), bad)
        except OSError:
            pass
        log.warning("manifest: %s couldn't be read (%s), moved to %s and started again",
                    path(), getattr(exc, "strerror", None) or type(exc).__name__, bad)
        return {}
    backups = data.get("backups") if isinstance(data, dict) else None
    return backups if isinstance(backups, dict) else {}


def _write(backups):
    atomic_file.write_private(path(), json.dumps({"schema": SCHEMA, "backups": backups}, indent=1, sort_keys=True))


def backups():
    """Every entry, keyed "<app>/<file>"."""
    with _lock:
        return _read()


def get(app, file):
    with _lock:
        return _read().get(key(app, file))


def generation():
    return _generation


def record(app, file, destinations, *, app_version, sha256, size, backuparr_version, encrypted=False):
    """A backup this run made, on the destinations it reached."""
    with _lock:
        entries = _read()
        old = entries.get(key(app, file)) or {}
        entries[key(app, file)] = {
            "app": app,
            "type": config_store.instance_type(app),
            "file": file,
            "destinations": sorted(set(old.get("destinations", [])) | set(destinations)),
            "app_version": app_version,
            "backuparr_version": backuparr_version,
            "sha256": sha256,
            "size": size,
            "encrypted": bool(encrypted),
        }
        _write(entries)


def add_found(app, file, dest_id, *, app_version, sha256, size, expected_generation, encrypted=False):
    """A backup the backfill found on dest_id. One already recorded only
    gains the destination. Returns False, recording nothing, once clear()
    has run since the backfill started."""
    with _lock:
        if _generation != expected_generation:
            return False
        entries = _read()
        entry = entries.get(key(app, file))
        if entry is None:
            entry = entries[key(app, file)] = {
                "app": app,
                "type": config_store.instance_type(app),
                "file": file,
                "destinations": [],
                "app_version": app_version,
                "backuparr_version": None,  # Not known for a backup made before the manifest.
                "sha256": sha256,
                "size": size,
                "encrypted": bool(encrypted),
            }
        entry["destinations"] = sorted(set(entry.get("destinations", [])) | {dest_id})
        _write(entries)
        return True


def remove(app, file, dest_id):
    """The file was deleted from dest_id; the entry goes once no destination
    holds it."""
    with _lock:
        entries = _read()
        entry = entries.get(key(app, file))
        if entry is None or dest_id not in entry.get("destinations", []):
            return
        entry["destinations"] = [d for d in entry["destinations"] if d != dest_id]
        if not entry["destinations"]:
            del entries[key(app, file)]
        _write(entries)


def reconcile(dest_id, present):
    """present: every "<app>/<file>" key a full listing of dest_id found.
    Entries claiming dest_id for any other file lose it."""
    with _lock:
        entries = _read()
        changed = False
        for entry_key in list(entries):
            entry = entries[entry_key]
            if dest_id in entry.get("destinations", []) and entry_key not in present:
                entry["destinations"] = [d for d in entry["destinations"] if d != dest_id]
                if not entry["destinations"]:
                    del entries[entry_key]
                changed = True
        if changed:
            _write(entries)


def clear():
    """Reset: the manifest goes with the rest of Backuparr's config."""
    global _generation
    with _lock:
        _generation += 1
        try:
            os.remove(path())
        except FileNotFoundError:
            pass
