"""Names used by Plex's unchanged diagnostic export (no Plex dependencies)."""
import re

LIBRARY_DATABASE_NAME = re.compile(
    r"(?:com\.plexapp\.plugins\.library\.db|databaseBackup\.db"
    r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})?)", re.IGNORECASE,
)


def library_databases(archive):
    return [info for info in archive.infolist()
            if not info.is_dir() and LIBRARY_DATABASE_NAME.fullmatch(
                info.filename.replace("\\", "/").split("/")[-1])]
