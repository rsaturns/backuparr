"""Read the services configured in Prowlarr, recovering their masked API keys.

Nothing is saved here, and only a backup created by discovery is ever deleted.
"""
import json
import os
import sqlite3
import tempfile
from urllib.parse import urlsplit
import zipfile

import requests

from apps.servarr import ServarrError, UnsafeRedirectError

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


def _sabnzbd_url(settings):
    host = settings.get("host")
    if not isinstance(host, str) or not host.strip():
        return ""
    host = host.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6
    try:
        parsed = urlsplit("//" + host)
        if (not parsed.hostname or parsed.port is not None or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            return ""
        port = int(settings.get("port", 8080))
    except (TypeError, ValueError):
        return ""
    base = settings.get("urlbase") or ""
    if not isinstance(base, str):
        return ""
    scheme = "https" if settings.get("usessl") in (True, 1, "true", "True") else "http"
    return f"{scheme}://{host}:{port}" + (f"/{base.strip('/')}" if base.strip("/") else "")


def _service_url(app, settings):
    url = settings.get("baseurl", "") if app in ("radarr", "sonarr") else _sabnzbd_url(settings)
    return url.rstrip("/") if valid_service_url(url) else ""


def _get_providers(instance, path):
    try:
        response = instance.session.get(instance._api(path), timeout=15)
    except UnsafeRedirectError as exc:
        raise DiscoveryError(str(exc)) from None
    with response:
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
    """Read the supported providers' settings from the backup's SQLite database.

    Returns ({(implementation, id): settings}, warnings). Prowlarr on PostgreSQL
    has no database in its backup.
    """
    with zipfile.ZipFile(archive_path) as archive:
        if "prowlarr.db" not in archive.namelist():
            return {}, ["The Prowlarr backup has no SQLite database (for example, when using PostgreSQL). Enter missing API keys manually."]
        with tempfile.TemporaryDirectory(prefix="backuparr-discovery-db-") as tmp:
            db_path = os.path.join(tmp, "prowlarr.db")
            with archive.open("prowlarr.db") as source, open(db_path, "xb") as target:
                os.chmod(db_path, 0o600)
                written = 0
                while chunk := source.read(1024 * 1024):  # the zip header's size isn't trusted
                    written += len(chunk)
                    if written > MAX_BACKUP_BYTES:
                        raise DiscoveryError("The Prowlarr backup database is too large to read.")
                    target.write(chunk)
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                result = {}
                for table in ("Applications", "DownloadClients"):
                    rows = connection.execute(
                        f'SELECT Id, Implementation, Settings FROM "{table}" '
                        "WHERE lower(Implementation) IN ('radarr', 'sonarr', 'sabnzbd')")
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
            owned = instance.trigger_discovery_backup()
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
                try:
                    instance.download_backup(backup.get("path"), archive_path, max_bytes=MAX_BACKUP_BYTES)
                except ServarrError as exc:
                    raise DiscoveryError(str(exc)) from None
                try:
                    settings, backup_warnings = read_backup_settings(archive_path)
                except zipfile.BadZipFile:
                    raise DiscoveryError("Prowlarr's backup download did not return a valid ZIP archive. Check authentication and reverse proxy access to its /backup/ route.") from None
                except sqlite3.Error:
                    raise DiscoveryError("Could not read service settings from Prowlarr's backup database. Its SQLite database may be corrupt or use an unsupported schema.") from None
                warnings.extend(backup_warnings)
                return settings, backup_warnings
        finally:
            if owned:
                try:
                    instance.delete_backup(backup["id"])
                except requests.RequestException:
                    warnings.append("Could not remove the temporary discovery backup from Prowlarr. Remove it in Prowlarr's System > Backup.")
            else:
                warnings.append("Prowlarr reused an existing backup command. Its backup was read but left in Prowlarr's System > Backup.")


def _fill_missing_keys(instance, candidates, progress, warnings):
    """Fill candidates' masked API keys from a temporary Prowlarr backup.

    Candidates that stay without a key get an ``api_key_error`` saying why.
    """
    key_error = ""
    try:
        saved, backup_warnings = _backup_settings(instance, progress, warnings)
        for candidate in candidates:
            if candidate["api_key"]:
                continue
            settings = saved.get((candidate["app"], candidate["provider_id"]), {})
            # A changed provider must not receive credentials from an old URL.
            matches_url = _service_url(candidate["app"], settings) == candidate["url"]
            if matches_url:
                candidate["api_key"] = _real_key(settings.get("apikey"))
            if not candidate["api_key"]:
                if not settings:
                    reason = backup_warnings[0] if backup_warnings else "The Prowlarr backup has no matching settings for this service. Run discovery again after checking the application in Prowlarr."
                elif not matches_url:
                    reason = "The service URL in the backup differs from its current Prowlarr URL. Run discovery again after checking the application in Prowlarr."
                else:
                    reason = "The Prowlarr backup has no readable API key for this service. Check the application's API key in Prowlarr."
                candidate["api_key_error"] = reason
    except DiscoveryError as exc:
        key_error = str(exc)
    except (OSError, RuntimeError):
        key_error = "Could not read API keys from a temporary Prowlarr backup. Enter missing API keys manually."
    if key_error:
        warnings.append(key_error)
        for candidate in candidates:
            if not candidate["api_key"]:
                candidate["api_key_error"] = key_error


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
        _fill_missing_keys(instance, candidates, progress, warnings)

    unique = {}
    for candidate in candidates:
        candidate.pop("provider_id")
        unique.setdefault((candidate["app"], candidate["url"], candidate["api_key"]), candidate)
    candidates = list(unique.values())
    if any(not candidate["api_key"] for candidate in candidates):
        warnings.append("Some API keys are unavailable. Their URLs can be filled in; enter their keys manually.")
    return {"candidates": candidates, "warnings": warnings}
