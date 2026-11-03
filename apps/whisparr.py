"""Whisparr backup/restore on the shared Servarr driver.

Whisparr v2 (a Sonarr fork) and v3 (a Radarr fork) both serve the same
system/backup, command and restore/upload endpoints as Radarr and Sonarr
under API v3 (Whisparr.Api.V3 BackupController on the v2-develop and eros
branches), so one driver covers both.

One v3 quirk, checked against the eros branch and Whisparr 3.6.5: v3 keeps
its database in whisparr3.db and puts that name in its backup zips, but its
BackupService.Restore only takes "nzbdrone.db" or "whisparr.db" out of an
uploaded zip. It still finds config.xml, so the upload answers 200 while the
database is silently left as it was - restoring a v3 backup from Whisparr's
own UI does the same. So a zip holding whisparr3.db is uploaded as a copy
with that member renamed to whisparr.db, which v3 accepts and stages as its
own whisparr3.restore. v2 zips hold whisparr2.db, which v2 accepts, and are
uploaded unchanged.
"""
import os
import shutil
import tempfile
import zipfile

from .servarr import ServarrApp

_V3_DB = "whisparr3.db"
_ACCEPTED_DB = "whisparr.db"


class WhisparrApp(ServarrApp):
    api_version = "v3"

    def __init__(self, url, api_key, **kwargs):
        super().__init__("whisparr", url, api_key, **kwargs)

    def restore_upload(self, file_path):
        with zipfile.ZipFile(file_path) as source:
            names = {info.filename.lower() for info in source.infolist()}
            if _V3_DB not in names or _ACCEPTED_DB in names:
                return super().restore_upload(file_path)
            # Same file name, so the upload looks exactly like the original.
            with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(file_path))) as tmp:
                staged = os.path.join(tmp, os.path.basename(file_path))
                with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as out:
                    for info in source.infolist():
                        name = _ACCEPTED_DB if info.filename.lower() == _V3_DB else info.filename
                        with source.open(info) as src, \
                                out.open(name, "w", force_zip64=info.file_size >= 0x7FFFFFFF) as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                return super().restore_upload(staged)
