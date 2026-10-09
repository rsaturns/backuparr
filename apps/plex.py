"""Plex's native database export. This is not a full Plex data-directory backup."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET
import zipfile

import requests
from apps.plex_archive import library_databases


class PlexError(RuntimeError):
    pass


class PlexApp:
    def __init__(self, url, api_key, timeout=30):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None
                     and not parsed.query and not parsed.fragment and parsed.port != 0)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise PlexError("plex: enter an HTTP(S) server URL without credentials or query parameters")
        if not isinstance(api_key, str) or not api_key.strip():
            raise PlexError("plex: the server owner's Plex token is required")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Plex-Token": api_key})

    def _get(self, path, *, missing_ok=False, **kwargs):
        url = self.url + path
        origin = urlsplit(self.url)
        endpoint = urlsplit(path).path
        # Plex itself can redirect artwork. Follow only same-origin redirects,
        # explicitly: Requests forwards custom X-Plex-Token headers cross-host.
        for hop in range(6):
            try:
                response = self.session.get(url, allow_redirects=False, **kwargs)
            except requests.RequestException:
                raise PlexError(f"plex: could not reach {endpoint}; check server network access") from None
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("Location", "")
            response.close()
            try:
                target = urlsplit(urljoin(url, location))
                default_port = 443 if origin.scheme == "https" else 80
                safe = (bool(location) and "\\" not in location
                        and not any(ord(c) < 32 for c in location)
                        and target.username is None and target.fragment == ""
                        and (target.scheme, target.hostname, target.port or default_port)
                        == (origin.scheme, origin.hostname, origin.port or default_port))
            except ValueError:
                safe = False
            if not safe:
                if missing_ok and location:
                    return None  # Record unavailable artwork in the manifest.
                raise PlexError(f"plex: redirect refused at {endpoint}; destination must stay on the configured server and protocol")
            if hop == 5:
                raise PlexError(f"plex: too many redirects at {endpoint}; check the server URL and proxy")
            url = target.geturl()
            kwargs.pop("params", None)  # Location supplies the redirected query.
        if response.status_code != 200:
            status = response.status_code
            response.close()
            if status == 404 and missing_ok:
                return None
            if status in (401, 403):
                raise PlexError("plex: access denied; use the server owner's Plex token")
            if 300 <= status < 400:
                raise PlexError(f"plex: redirect refused at {endpoint}; check the server URL and proxy")
            raise PlexError(f"plex: HTTP {status} at {endpoint}")
        return response

    def test_connection(self):
        # /identity can succeed without a valid token; prefs requires owner access.
        with self._get("/:/prefs", timeout=self.timeout) as response:
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                raise PlexError("plex: expected Plex settings; check the server URL and proxy") from None
            if root.tag != "MediaContainer" or root.find("Setting") is None:
                raise PlexError("plex: the response is not Plex server settings")
        return "Plex owner access verified (databases, API settings and artwork)"

    def identity(self):
        root, _ = self._xml("/identity")
        version, identifier = root.get("version"), root.get("machineIdentifier")
        if not version or not identifier:
            raise PlexError("plex: server identity is missing its version or identifier")
        return {"version": version, "machine_identifier": identifier}

    def _download_databases(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        archive = Path(dest_dir) / "plex-databases.zip"
        try:
            with self._get("/diagnostics/databases", timeout=(self.timeout, 600), stream=True) as response:
                with archive.open("wb") as output:
                    os.chmod(archive, 0o600)
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        output.write(chunk)
            with zipfile.ZipFile(archive) as zf:
                # Accept Plex's diagnostic snapshot name, including its UUID,
                # as well as the on-disk name used by other backup variants.
                databases = library_databases(zf)
                if not databases:
                    raise PlexError("plex: export does not contain the Plex library database")
                if len(databases) > 1:
                    raise PlexError("plex: export contains multiple Plex library databases")
                with zf.open(databases[0]) as db:
                    if db.read(16) != b"SQLite format 3\0":
                        raise PlexError("plex: export contains an invalid library database")
                if zf.testzip() is not None:
                    raise PlexError("plex: database export is corrupt")
        except requests.RequestException:
            archive.unlink(missing_ok=True)
            raise PlexError("plex: database download interrupted; retry the backup") from None
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
            archive.unlink(missing_ok=True)
            if isinstance(exc, PlexError):
                raise
            raise PlexError("plex: expected a readable database ZIP; check the URL and proxy") from None
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        return str(archive)

    def _xml(self, path, **params):
        with self._get(path, timeout=self.timeout, params=params) as response:
            data = response.content
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise PlexError("plex: invalid XML response; check the server URL and proxy") from None
        if root.tag != "MediaContainer":
            raise PlexError("plex: expected a Plex MediaContainer response")
        return root, data

    def _metadata_pages(self, section, media_type):
        start, total, seen = 0, None, set()
        while True:
            params = {"X-Plex-Container-Start": start, "X-Plex-Container-Size": 500}
            if media_type is not None:
                params["type"] = media_type
            root, data = self._xml(f"/library/sections/{section}/all", **params)
            items = [item for item in root if item.tag in ("Directory", "Video", "Track", "Photo")]
            try:
                count = int(root.attrib["totalSize"])
                offset = int(root.get("offset", "0"))
            except (KeyError, ValueError):
                raise PlexError("plex: missing library pagination metadata") from None
            if count < 0 or offset != start or (total is not None and count != total):
                raise PlexError("plex: library changed during export; retry when scans are idle")
            total = count
            for item in items:
                key = item.get("ratingKey")
                if not key or key in seen:
                    raise PlexError("plex: duplicate or missing library item ID")
                seen.add(key)
            yield start, data, items
            start += len(items)
            if start == total:
                return
            if not items or start > total:
                raise PlexError("plex: incomplete library metadata export")

    def backup(self, dest_dir):
        identity = self.identity()
        database = Path(self._download_databases(dest_dir))
        archive = Path(dest_dir) / "plex-backup.zip"
        complete = False
        try:
            with archive.open("wb") as output:
                os.chmod(archive, 0o600)
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(database, "databases.zip")
                    _, prefs = self._xml("/:/prefs")
                    zf.writestr("server-preferences.xml", prefs)
                    sections, data = self._xml("/library/sections")
                    zf.writestr("library-sections.xml", data)
                    artwork, skipped_artwork = set(), set()
                    kinds = {"movie": (1, 18), "show": (2, 3, 4, 18),
                             "artist": (8, 9, 10, 18), "photo": (13, 14)}
                    for section in sections.findall("Directory"):
                        key = section.get("key", "")
                        if not re.fullmatch(r"[0-9]+", key):
                            raise PlexError("plex: invalid library section ID")
                        _, prefs = self._xml(f"/library/sections/{key}/prefs")
                        zf.writestr(f"library-preferences/{key}.xml", prefs)
                        for media_type in kinds.get(section.get("type"), (None,)):
                            for start, page, items in self._metadata_pages(key, media_type):
                                zf.writestr(f"metadata/{key}/{media_type or 'all'}-{start}.xml", page)
                                for item in items:
                                    for attribute in ("thumb", "art", "banner"):
                                        path = item.get(attribute)
                                        if not path:
                                            continue
                                        parsed = urlsplit(path)
                                        if (not parsed.scheme and not parsed.netloc
                                                and parsed.path.startswith("/library/metadata/")
                                                and ".." not in parsed.path.split("/")):
                                            artwork.add(path)
                                        else:
                                            skipped_artwork.add(path)
                    image_index = []
                    for number, path in enumerate(sorted(artwork)):
                        response = self._get(path, missing_ok=True, timeout=(self.timeout, 120), stream=True)
                        if response is None:
                            skipped_artwork.add(path)
                            continue
                        with response:
                            mime = response.headers.get("Content-Type", "")
                            if not mime.lower().startswith("image/"):
                                raise PlexError("plex: artwork endpoint did not return an image")
                            filename = f"artwork/{number}"
                            with zf.open(filename, "w", force_zip64=True) as image:
                                for chunk in response.iter_content(1024 * 1024):
                                    image.write(chunk)
                            image_index.append({"path": path, "file": filename, "content_type": mime})
                    zf.writestr("artwork/index.json", json.dumps(image_index, indent=2))
                    zf.writestr("manifest.json", json.dumps({
                        "format": "backuparr-plex-export", "format_version": 1,
                        "server": identity,
                        "database_archive": "databases.zip", "artwork_count": len(image_index),
                        "unavailable_artwork": sorted(skipped_artwork),
                        "limitations": [
                            "Preferences are an API reference export, not a native Preferences.xml file.",
                            "No media, subtitle files, codecs, caches, plugin binaries or opaque plugin data.",
                            "Artwork includes library thumbnails/backgrounds/banners exposed by the server; external image URLs are listed but not fetched with the Plex token.",
                            "Plex cloud-only account data is excluded. API settings/artwork reads are not atomic with the native database export.",
                            "The optional Plex restore agent restores the library database on the same server/version. API settings and artwork need separate manual recovery.",
                        ],
                    }, indent=2))
                    zf.writestr("RESTORE.txt", (
                        "Plex backup: databases, API settings, metadata and artwork\n\n"
                        "databases.zip is Plex's unchanged native database export. It includes\n"
                        "locally stored watched/unwatched state, progress and ratings for all users.\n"
                        "Use Backuparr's Restore tab with the optional Plex restore agent for\n"
                        "database recovery on the same server and Plex version. It preserves a\n"
                        "rollback copy, stops the container, restores the database and restarts it.\n"
                        "Settings and image files are not applied by the agent.\n\n"
                        "Alternatively, to restore manually:\n"
                        "Stop Plex before restoring. Extract databases.zip, then rename its\n"
                        "databaseBackup.db (possibly followed by a UUID) to\n"
                        "com.plexapp.plugins.library.db. If the archive\n"
                        "already uses com.plexapp.plugins.library.db, keep that name.\n"
                        "Keep a copy of the target databases before replacing them and remove\n"
                        "stale -wal/-shm companions as directed by Plex's official guide:\n"
                        "https://support.plex.tv/articles/202485658-restore-a-database-backed-up-via-scheduled-tasks/\n\n"
                        "server-preferences.xml and library-preferences/ are API response exports,\n"
                        "NOT native Preferences.xml files. Reapply reviewed settings in Settings,\n"
                        "or PUT /:/prefs and /library/sections/{id}/prefs using setting IDs/values.\n"
                        "Omit read-only/token/identity fields; match current library IDs.\n"
                        "The database preserves playlist/collection data; metadata XML and artwork\n"
                        "help recover images separately: use Edit > Poster/Background to upload the\n"
                        "exported files. Use artwork/index.json to match image files\n"
                        "to their old item URLs; IDs can change after rebuilding a library.\n"
                        "Check manifest.json for missing data. Protect these sensitive archives.\n"
                    ))
            complete = True
        except requests.RequestException:
            raise PlexError("plex: artwork download interrupted; retry the backup") from None
        finally:
            database.unlink(missing_ok=True)
            if not complete:
                archive.unlink(missing_ok=True)
        return str(archive)
