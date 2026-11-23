"""Builds Home Assistant backups for the tests: a plain tar holding
backup.json and a tar per part, as Home Assistant writes them (#155)."""
import io
import json
import tarfile


def _tar_bytes(members, gzip=True):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gzip else "w") as tar:
        for name, data in members.items():
            data = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_ha_backup(path, version="2026.11.1", database=b"", meta=None, folders=(), addons=(), extra=None,
                   settings=True):
    """A Home Assistant backup at path. database: the history database's
    bytes (left out when None); folders and addons get a tar each."""
    data = {"data/configuration.yaml": "default_config:\n", "data/.storage/core.config": "{}"}
    if database is not None:
        data["data/home-assistant_v2.db"] = database
    backup_json = {
        "slug": "abc12345", "name": "Backuparr 1a2b3c4d", "date": "2026-11-11T21:00:00+00:00", "type": "partial",
        "compressed": True, "protected": False, "folders": list(folders),
        "addons": [{"slug": slug, "name": slug.title(), "version": "1.0", "size": 1} for slug in addons],
        "homeassistant": {"version": version, "exclude_database": database is None},
        **(meta or {}),
    }
    members = {"./backup.json": json.dumps(backup_json).encode()}
    if settings:
        members["./homeassistant.tar.gz"] = _tar_bytes(data)
    for folder in folders:
        members[f"./{folder.replace('/', '_')}.tar.gz"] = _tar_bytes({"x": "y"})
    for slug in addons:
        members[f"./{slug}.tar.gz"] = _tar_bytes({"addon.json": "{}"})
    members.update(extra or {})
    with tarfile.open(path, "w") as tar:
        for name, blob in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
    return str(path)
