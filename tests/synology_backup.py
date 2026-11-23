"""A DSM configuration backup (.dss), made the way DSM 6.2 and 7.2 made it
(#168): an xz-compressed tar holding ConfigBkp/config_info (key="value"
lines) and ConfigBkp/_Syno_ConfBkp.db (SQLite), plus a few more files on
DSM 7."""
import io
import os
import sqlite3
import tarfile
import tempfile


def settings_db():
    """A small SQLite database shaped like DSM's _Syno_ConfBkp.db."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE confbkp_config_tb (key TEXT, value TEXT)")
        db.execute("CREATE TABLE confbkp_user_tb (name TEXT, uid INTEGER)")
        db.execute("INSERT INTO confbkp_user_tb VALUES ('labadmin', 1026)")
        db.commit()
        db.close()
        with open(path, "rb") as f:
            return f.read()


def config_info(major="7", minor="2", build="72806", os_name="DSM"):
    return (f'os_name="{os_name}"\nconf_version="5"\ndsm_majorversion="{major}"\n'
            f'dsm_minorversion="{minor}"\ndsm_buildnumber="{build}"\nunique="synology_kvmx64_virtualdsm"\n').encode()


def make_dss(info=None, database=None, members=None, drop=()):
    """A .dss's bytes. `members` adds or replaces files (None removes one);
    `drop` leaves out the named standard files."""
    files = {"ConfigBkp/_Syno_ConfBkp.db": settings_db() if database is None else database,
             "ConfigBkp/config_info": config_info() if info is None else info,
             "ConfigBkp/tls_profile/datastore.json": b"{}"}
    files.update(members or {})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        dirs = tarfile.TarInfo("ConfigBkp")
        dirs.type = tarfile.DIRTYPE
        tar.addfile(dirs)
        for name, data in files.items():
            if data is None or name in drop:
                continue
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
    return buf.getvalue()
