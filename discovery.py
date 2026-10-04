"""Read configured services from Prowlarr, including masked API keys.

No configuration is saved here. Only a newly created discovery backup is
deleted; existing manual and scheduled backups are left alone.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from urllib.parse import urlsplit

import requests

MAX_BACKUP_BYTES = 512 * 1024 * 1024


class DiscoveryError(RuntimeError):
    pass


def valid_service_url(value):
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in ("http", "https") and parsed.hostname
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        return False


def _settings(fields):
    if isinstance(fields, dict):
        return {key.lower(): value for key, value in fields.items()}
    if not isinstance(fields, list):
        return {}
    return {
        field["name"].lower(): field.get("value")
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("name"), str)
    }


def _real_key(value):
    return value.strip() if isinstance(value, str) and value.strip().strip("*") else ""


def _service_url(app, settings):
    if app in ("radarr", "sonarr"):
        url = settings.get("baseurl", "")
    else:
        host = settings.get("host")
        if not isinstance(host, str) or not host.strip():
            return ""
        host = host.strip()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6
        try:
            parsed_host = urlsplit("//" + host)
            if (not parsed_host.hostname or parsed_host.port is not None
                    or parsed_host.username or parsed_host.password
                    or parsed_host.path or parsed_host.query or parsed_host.fragment):
                return ""
        except ValueError:
            return ""
        try:
            port = int(settings.get("port", 8080))
        except (TypeError, ValueError):
            return ""
        ssl = settings.get("usessl") in (True, 1, "true", "True")
        base = settings.get("urlbase") or ""
        if not isinstance(base, str):
            return ""
        url = f"{'https' if ssl else 'http'}://{host}:{port}"
        if base.strip("/"):
            url += "/" + base.strip("/")
    return url.rstrip("/") if valid_service_url(url) else ""


def _get_providers(instance, path):
    with instance.session.get(instance._api(path), timeout=15) as response:
        if response.status_code in (401, 403):
            raise DiscoveryError("Prowlarr rejected access. Check its API key and permissions.")
        if response.status_code != 200:
            raise DiscoveryError("Prowlarr could not return its configured services.")
        try:
            providers = response.json()
        except ValueError:
            raise DiscoveryError("Prowlarr returned an invalid service list.") from None
    if not isinstance(providers, list):
        raise DiscoveryError("Prowlarr returned an invalid service list.")
    return providers


def read_backup_settings(archive_path):
    """Read only supported provider settings from a private, read-only DB.

    Read the exact zip member instead of extractall (no archive path traversal).
    PostgreSQL deployments omit the database from Prowlarr's official backup.
    """
    with zipfile.ZipFile(archive_path) as archive:
        if "prowlarr.db" not in archive.namelist():
            return {}, ["The Prowlarr backup has no SQLite database (for example, when using PostgreSQL). Enter missing API keys manually."]
        info = archive.getinfo("prowlarr.db")
        if info.file_size > MAX_BACKUP_BYTES:
            raise DiscoveryError("The Prowlarr backup database is too large to read.")
        with tempfile.TemporaryDirectory(prefix="backuparr-discovery-db-") as tmp:
            db_path = os.path.join(tmp, "prowlarr.db")
            with archive.open(info) as source, open(db_path, "xb") as target:
                os.chmod(db_path, 0o600)
                shutil.copyfileobj(source, target)
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                result = {}
                for table, implementations in (
                    ("Applications", ("radarr", "sonarr")),
                    ("DownloadClients", ("sabnzbd",)),
                ):
                    placeholders = ",".join("?" for _ in implementations)
                    rows = connection.execute(
                        f'SELECT Id, Implementation, Settings FROM "{table}" '
                        f'WHERE lower(Implementation) IN ({placeholders})', implementations,
                    )
                    for provider_id, implementation, raw in rows:
                        try:
                            settings = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(settings, dict):
                            result[(implementation.lower(), provider_id)] = _settings(settings)
                return result, []
            finally:
                connection.close()


def _backup_settings(instance, progress, warnings):
    progress("Waiting for any Prowlarr backup to finish...")
    with instance.backup_lock:
        before = {backup["id"] for backup in instance.list_backups()}
        progress("Creating a temporary Prowlarr backup to read API keys...")
        try:
            instance.trigger_backup()
            created = [
                backup for backup in instance.list_backups()
                if backup.get("type") == "manual" and backup.get("id") not in before
            ]
        except (requests.RequestException, RuntimeError):
            raise DiscoveryError("Could not finish creating or identifying the temporary discovery backup. Check Prowlarr's System > Backup for a leftover backup; enter missing API keys manually.") from None
        if len(created) != 1 or created[0].get("id") is None:
            raise DiscoveryError("The temporary backup could not be identified safely. Existing backups were left untouched; check Prowlarr's backup list.")
        backup = created[0]
        try:
            progress("Reading API keys from the temporary Prowlarr backup...")
            with tempfile.TemporaryDirectory(prefix="backuparr-discovery-") as tmp:
                archive_path = os.path.join(tmp, "backup.zip")
                path = backup.get("path")
                if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                    raise DiscoveryError("Prowlarr returned an invalid backup path.")
                with instance.session.get(instance.url + path, timeout=30, stream=True) as response:
                    response.raise_for_status()
                    size = 0
                    with open(archive_path, "xb") as target:
                        os.chmod(archive_path, 0o600)
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            size += len(chunk)
                            if size > MAX_BACKUP_BYTES:
                                raise DiscoveryError("The Prowlarr backup is too large to download.")
                            target.write(chunk)
                settings, backup_warnings = read_backup_settings(archive_path)
                warnings.extend(backup_warnings)
                return settings
        finally:
            try:
                instance.delete_backup(backup["id"])
            except requests.RequestException:
                warnings.append("Could not remove the temporary discovery backup from Prowlarr. Remove it in Prowlarr's System > Backup.")


def discover_prowlarr(instance, progress=lambda message: None):
    warnings = []
    progress("Connecting to Prowlarr and finding configured services...")
    providers = _get_providers(instance, "/applications")
    try:
        providers += _get_providers(instance, "/downloadclient")
    except (DiscoveryError, requests.RequestException):
        warnings.append("Could not read Prowlarr's download clients. Radarr and Sonarr discovery can still proceed.")

    candidates = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        app = str(provider.get("implementation", "")).lower()
        if app not in ("radarr", "sonarr", "sabnzbd"):
            continue
        settings = _settings(provider.get("fields"))
        url = _service_url(app, settings)
        if not url:
            warnings.append(f"A configured {app} has an invalid or missing URL and was skipped.")
            continue
        candidates.append({
            "app": app,
            "name": str(provider.get("name") or app),
            "url": url,
            "api_key": _real_key(settings.get("apikey")),
            "provider_id": provider.get("id"),
        })

    if any(not candidate["api_key"] for candidate in candidates):
        try:
            saved = _backup_settings(instance, progress, warnings)
            for candidate in candidates:
                settings = saved.get((candidate["app"], candidate["provider_id"]), {})
                # A changed provider must not receive credentials from an old URL.
                if not candidate["api_key"] and _service_url(candidate["app"], settings) == candidate["url"]:
                    candidate["api_key"] = _real_key(settings.get("apikey"))
        except DiscoveryError as exc:
            warnings.append(str(exc))
        except (requests.RequestException, OSError, sqlite3.Error, zipfile.BadZipFile, RuntimeError):
            warnings.append("Could not read API keys from a temporary Prowlarr backup. Enter missing API keys manually.")

    unique = {}
    for candidate in candidates:
        candidate.pop("provider_id")
        unique.setdefault((candidate["app"], candidate["url"], candidate["api_key"]), candidate)
    candidates = list(unique.values())
    if any(not candidate["api_key"] for candidate in candidates):
        warnings.append("Some API keys are unavailable. Their URLs can be filled in; enter their keys manually.")
    return {"candidates": candidates, "warnings": warnings}
