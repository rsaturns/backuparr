import io
import json
import sqlite3
import threading
import zipfile

import pytest
import requests

import discovery
from apps.prowlarr import ProwlarrApp


class Response:
    def __init__(self, data=None, content=b"", status=200, headers=None):
        self.data = data
        self.content = content
        self.status_code = status
        self.headers = headers or {}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("Remote error including sensitive response data")

    def iter_content(self, chunk_size):
        yield self.content


class FakeProwlarr(ProwlarrApp):
    url = "https://prowlarr.example/prowlarr"

    def __init__(self, providers, clients=None, archive=b""):
        super().__init__(self.url, "fixture-key")
        self.session.close()
        self.providers = providers
        self.clients = clients or []
        self.archive = archive
        self.session = self
        self.backup_lock = threading.Lock()
        self.created = False
        self.deleted = []
        self.status = 200
        self.clients_status = 200
        self.download_status = 200
        self.extra_backups = []
        self.fail_delete = False
        self.owns_backup = True

    def _api(self, path):
        return self.url + "/api/v1" + path

    def get(self, url, **kwargs):
        if url.endswith("/applications"):
            return Response(self.providers, status=self.status)
        if url.endswith("/downloadclient"):
            return Response(self.clients, status=self.clients_status)
        assert url == self.url + "/backup/manual/discovery.zip"
        return Response(content=self.archive, status=self.download_status)

    def list_backups(self):
        backups = [{"id": 1, "type": "manual", "path": "/backup/manual/existing.zip"}]
        if self.created:
            backups.append({"id": 2, "type": "manual", "path": "/backup/manual/discovery.zip"})
            backups.extend(self.extra_backups)
        return backups

    def trigger_backup(self):
        self.created = True

    def trigger_discovery_backup(self):
        self.trigger_backup()
        return self.owns_backup

    def delete_backup(self, backup_id):
        self.deleted.append(backup_id)
        if self.fail_delete:
            raise requests.ConnectionError("Unable to delete")


def provider(app="Radarr", provider_id=10, url="https://radarr.example/radarr", key="********", name="Movies"):
    return {"id": provider_id, "implementation": app, "name": name, "fields": [
        {"name": "baseUrl", "value": url}, {"name": "apiKey", "value": key},
        {"name": "prowlarrUrl", "value": "https://wrong.example"},
    ]}


def archive_with_settings(tmp_path, rows):
    db_path = tmp_path / "fixture.db"
    connection = sqlite3.connect(db_path)
    try:
        for table in ("Applications", "DownloadClients"):
            connection.execute(f'CREATE TABLE "{table}" (Id INTEGER, Implementation TEXT, Settings TEXT)')
        for table, provider_id, app, settings in rows:
            connection.execute(f'INSERT INTO "{table}" VALUES (?, ?, ?)', (provider_id, app, json.dumps(settings)))
        connection.commit()
    finally:
        connection.close()
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("prowlarr.db", db_path.read_bytes())
        archive.writestr("../../should-not-exist", b"do not extract")
    return content.getvalue()


def test_discovery_recovers_keys_and_deletes_only_its_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.tempfile, "tempdir", str(tmp_path))
    archive = archive_with_settings(tmp_path, [
        ("Applications", 10, "Radarr", {"baseUrl": "https://radarr.example/radarr", "apiKey": "real-radarr-key"}),
        ("Applications", 11, "Sonarr", {"baseUrl": "http://sonarr:8989/tv", "apiKey": "real-sonarr-key"}),
    ])
    instance = FakeProwlarr([provider(), provider("Sonarr", 11, "http://sonarr:8989/tv", name="TV")], archive=archive)
    steps = []
    result = discovery.discover_prowlarr(instance, steps.append)
    assert [(c["app"], c["url"], c["api_key"]) for c in result["candidates"]] == [
        ("radarr", "https://radarr.example/radarr", "real-radarr-key"),
        ("sonarr", "http://sonarr:8989/tv", "real-sonarr-key"),
    ]
    assert result["warnings"] == []
    assert instance.deleted == [2]
    assert not list(tmp_path.glob("backuparr-discovery*"))
    assert not (tmp_path.parent / "should-not-exist").exists()
    assert all("real-radarr-key" not in step for step in steps)


def test_unmasked_keys_need_no_backup_and_duplicate_endpoints_are_merged():
    instance = FakeProwlarr([provider(key="real-key"), provider(provider_id=11, key="real-key")])
    result = discovery.discover_prowlarr(instance)
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["api_key"] == "real-key"
    assert not instance.created


def test_multiple_instances_are_returned_for_selection():
    result = discovery.discover_prowlarr(FakeProwlarr([
        provider(key="one"), provider(provider_id=11, url="http://radarr-4k:7878", key="two", name="4K"),
    ]))
    assert len(result["candidates"]) == 2
    assert result["candidates"][1]["name"] == "4K"


def test_multiple_sonarr_instances_keep_their_prowlarr_names_and_own_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.tempfile, "tempdir", str(tmp_path))
    archive = archive_with_settings(tmp_path, [
        ("Applications", 10, "Radarr", {"baseUrl": "http://radarr:7878", "apiKey": "radarr-key"}),
        ("Applications", 11, "Sonarr", {"baseUrl": "http://sonarr:8989", "apiKey": "tv-key"}),
        ("Applications", 12, "Sonarr", {"baseUrl": "http://sonarr-4k:8989", "apiKey": "uhd-key"}),
    ])
    instance = FakeProwlarr([
        provider(url="http://radarr:7878"),
        provider("Sonarr", 11, "http://sonarr:8989", name="TV"),
        provider("Sonarr", 12, "http://sonarr-4k:8989", name="Sonarr 4K"),
    ], archive=archive)
    result = discovery.discover_prowlarr(instance)
    sonarr = [c for c in result["candidates"] if c["app"] == "sonarr"]
    assert [(c["name"], c["url"], c["api_key"]) for c in sonarr] == [
        ("TV", "http://sonarr:8989", "tv-key"),
        ("Sonarr 4K", "http://sonarr-4k:8989", "uhd-key"),
    ]
    assert [c["api_key"] for c in result["candidates"] if c["app"] == "radarr"] == ["radarr-key"]
    assert result["warnings"] == []


def test_instance_label_is_the_prowlarr_name_or_the_app_type_when_blank():
    result = discovery.discover_prowlarr(FakeProwlarr([
        provider("Sonarr", 11, "http://sonarr:8989", key="one", name="Anime"),
        provider("Sonarr", 12, "http://sonarr-4k:8989", key="two", name=""),
    ]))
    assert [c["name"] for c in result["candidates"]] == ["Anime", "sonarr"]


def test_sabnzbd_ipv6_ssl_and_urlbase(tmp_path):
    client = {"id": 4, "name": "SAB", "implementation": "Sabnzbd", "fields": [
        {"name": "host", "value": "2001:db8::1"}, {"name": "port", "value": 9090},
        {"name": "useSsl", "value": True}, {"name": "urlBase", "value": "/sabnzbd/"},
        {"name": "apiKey", "value": "********"},
    ]}
    archive = archive_with_settings(tmp_path, [("DownloadClients", 4, "Sabnzbd", {
        "host": "2001:db8::1", "port": 9090, "useSsl": True, "urlBase": "/sabnzbd/", "apiKey": "sab-key",
    })])
    result = discovery.discover_prowlarr(FakeProwlarr([], [client], archive))
    assert result["candidates"][0]["url"] == "https://[2001:db8::1]:9090/sabnzbd"
    assert result["candidates"][0]["api_key"] == "sab-key"


def test_lidarr_whisparr_and_lazylibrarian_are_found_like_radarr(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.tempfile, "tempdir", str(tmp_path))
    archive = archive_with_settings(tmp_path, [
        ("Applications", 20, "Lidarr", {"baseUrl": "http://lidarr:8686", "apiKey": "lidarr-key"}),
        ("Applications", 21, "Lidarr", {"baseUrl": "http://lidarr-flac:8686", "apiKey": "flac-key"}),
        ("Applications", 22, "Whisparr", {"baseUrl": "http://whisparr:6969", "apiKey": "whisparr-key"}),
        ("Applications", 23, "LazyLibrarian", {"baseUrl": "http://lazylibrarian:5299", "apiKey": "ll-key"}),
    ])
    instance = FakeProwlarr([
        provider("Lidarr", 20, "http://lidarr:8686", name="Lidarr"),
        provider("Lidarr", 21, "http://lidarr-flac:8686", name="Lidarr FLAC"),
        provider("Whisparr", 22, "http://whisparr:6969", name="Whisparr"),
        provider("LazyLibrarian", 23, "http://lazylibrarian:5299", name="LazyLibrarian"),
    ], archive=archive)
    result = discovery.discover_prowlarr(instance)
    assert [(c["app"], c["name"], c["url"], c["api_key"]) for c in result["candidates"]] == [
        ("lidarr", "Lidarr", "http://lidarr:8686", "lidarr-key"),
        ("lidarr", "Lidarr FLAC", "http://lidarr-flac:8686", "flac-key"),
        ("whisparr", "Whisparr", "http://whisparr:6969", "whisparr-key"),
        ("lazylibrarian", "LazyLibrarian", "http://lazylibrarian:5299", "ll-key"),
    ]
    assert result["warnings"] == [] and instance.deleted == [2]


def nzbget_client(host="nzbget", port=6789, password="********", username="nzbget", **extra):
    fields = {"host": host, "port": port, "useSsl": False, "urlBase": "", "username": username, "password": password, **extra}
    return {"id": 5, "name": "NZBGet", "implementation": "Nzbget",
            "fields": [{"name": name, "value": value} for name, value in fields.items() if value is not None]}


def test_nzbget_gets_its_username_and_its_password_from_the_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.tempfile, "tempdir", str(tmp_path))
    archive = archive_with_settings(tmp_path, [("DownloadClients", 5, "Nzbget", {
        "host": "nzbget", "port": 6789, "useSsl": False, "urlBase": "", "username": "nzbget", "password": "tegbzn6789",
    })])
    result = discovery.discover_prowlarr(FakeProwlarr([], [nzbget_client()], archive))
    assert result["candidates"] == [{"app": "nzbget", "name": "NZBGet", "url": "http://nzbget:6789",
                                     "password": "tegbzn6789", "username": "nzbget"}]
    assert result["warnings"] == []


def test_nzbget_url_uses_its_own_default_port_ssl_and_url_base():
    result = discovery.discover_prowlarr(FakeProwlarr([], [nzbget_client(port=None, password="real", useSsl=True, urlBase="/nzbget/")]))
    assert result["candidates"][0]["url"] == "https://nzbget:6789/nzbget"


def test_nzbget_whose_url_changed_gets_no_old_password(tmp_path):
    archive = archive_with_settings(tmp_path, [("DownloadClients", 5, "Nzbget", {
        "host": "old-host", "port": 6789, "username": "nzbget", "password": "old-password"})])
    result = discovery.discover_prowlarr(FakeProwlarr([], [nzbget_client()], archive))
    candidate = result["candidates"][0]
    assert candidate["password"] == "" and "differs from its current Prowlarr URL" in candidate["password_error"]
    assert "api_key" not in candidate and "api_key_error" not in candidate
    assert "Some API keys or passwords are unavailable" in result["warnings"][-1]


def test_postgres_backup_preserves_urls_and_reports_missing_keys():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("config.xml", "<Config />")
    instance = FakeProwlarr([provider()], archive=content.getvalue())
    result = discovery.discover_prowlarr(instance)
    assert result["candidates"][0]["api_key"] == ""
    assert result["candidates"][0]["url"] == "https://radarr.example/radarr"
    assert any("PostgreSQL" in message for message in result["warnings"])
    assert "PostgreSQL" in result["candidates"][0]["api_key_error"]
    assert instance.deleted == [2]


def test_changed_provider_does_not_receive_key_for_old_url(tmp_path):
    archive = archive_with_settings(tmp_path, [("Applications", 10, "Radarr", {
        "baseUrl": "https://old.example", "apiKey": "old-key",
    })])
    result = discovery.discover_prowlarr(FakeProwlarr([provider()], archive=archive))
    assert result["candidates"][0]["api_key"] == ""
    assert "URL in the backup differs" in result["candidates"][0]["api_key_error"]


@pytest.mark.parametrize("failure", ["download", "invalid-zip", "delete"])
def test_backup_failures_clean_up_and_keep_partial_results(tmp_path, failure):
    archive = archive_with_settings(tmp_path, [("Applications", 10, "Radarr", {
        "baseUrl": "https://radarr.example/radarr", "apiKey": "key",
    })])
    instance = FakeProwlarr([provider()], archive=archive)
    if failure == "download":
        instance.download_status = 500
    elif failure == "invalid-zip":
        instance.archive = b"not a zip"
    else:
        instance.fail_delete = True
    result = discovery.discover_prowlarr(instance)
    assert result["candidates"][0]["url"] == "https://radarr.example/radarr"
    assert instance.deleted == [2]
    assert result["warnings"]
    assert "sensitive response data" not in str(result)


def test_concurrent_external_backup_does_not_delete_an_ambiguous_backup():
    instance = FakeProwlarr([provider()])
    instance.extra_backups = [{"id": 3, "type": "manual", "path": "/backup/manual/other.zip"}]
    result = discovery.discover_prowlarr(instance)
    assert not instance.deleted
    assert any("identified safely" in message for message in result["warnings"])


def test_joined_external_backup_recovers_keys_without_deleting_it(tmp_path):
    archive = archive_with_settings(tmp_path, [("Applications", 10, "Radarr", {
        "baseUrl": "https://radarr.example/radarr", "apiKey": "key",
    })])
    instance = FakeProwlarr([provider()], archive=archive)
    instance.owns_backup = False
    result = discovery.discover_prowlarr(instance)
    assert result["candidates"][0]["api_key"] == "key"
    assert not instance.deleted
    assert any("reused an existing backup command" in warning for warning in result["warnings"])


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
def test_backup_download_failure_reports_stage_without_secrets(status):
    instance = FakeProwlarr([provider()])
    instance.download_status = status
    result = discovery.discover_prowlarr(instance)
    assert result["candidates"][0]["api_key"] == ""
    assert any(f"HTTP {status}" in warning and "/backup/" in warning for warning in result["warnings"])
    assert f"HTTP {status}" in result["candidates"][0]["api_key_error"]
    assert "sensitive response data" not in str(result)
    assert instance.deleted == [2]


def test_html_login_page_instead_of_backup_has_actionable_warning():
    instance = FakeProwlarr([provider()], archive=b"<html>secret login page</html>")
    result = discovery.discover_prowlarr(instance)
    assert any("valid ZIP" in warning and "reverse proxy" in warning for warning in result["warnings"])
    assert "secret login page" not in str(result)


@pytest.mark.parametrize("rows, reason", [
    ([], "no matching settings"),
    ([("Applications", 10, "Radarr", {"baseUrl": "https://radarr.example/radarr", "apiKey": ""})], "no readable API key"),
])
def test_missing_key_reason_is_attached_to_affected_service(tmp_path, rows, reason):
    archive = archive_with_settings(tmp_path, rows)
    result = discovery.discover_prowlarr(FakeProwlarr([
        provider(), provider("Sonarr", 11, "http://sonarr:8989", key="existing-sonarr-key"),
    ], archive=archive))
    radarr, sonarr = result["candidates"]
    assert reason in radarr["api_key_error"]
    assert radarr["api_key"] == ""
    assert sonarr["api_key"] == "existing-sonarr-key"
    assert "api_key_error" not in sonarr


def test_interrupted_backup_keeps_urls_and_reports_possible_leftover(monkeypatch):
    instance = FakeProwlarr([provider()])

    def interrupted():
        instance.created = True
        raise requests.ConnectionError("Remote failure with secret details")

    monkeypatch.setattr(instance, "trigger_backup", interrupted)
    result = discovery.discover_prowlarr(instance)
    assert result["candidates"][0]["url"] == "https://radarr.example/radarr"
    assert any("leftover backup" in message for message in result["warnings"])
    assert "secret details" not in str(result)
    assert not instance.backup_lock.locked()


def test_empty_or_unsupported_providers_do_not_create_a_backup():
    instance = FakeProwlarr([provider("Readarr"), provider("Mylar"), provider(url="file:///etc/passwd")])
    result = discovery.discover_prowlarr(instance)
    assert result["candidates"] == []
    assert not instance.created


def test_failed_download_client_list_does_not_block_app_discovery():
    instance = FakeProwlarr([provider(key="real-key")])
    instance.clients_status = 500
    result = discovery.discover_prowlarr(instance)
    assert result["candidates"][0]["api_key"] == "real-key"
    assert any("download clients" in message for message in result["warnings"])


def test_authentication_failure_is_actionable_and_secret_free():
    instance = FakeProwlarr([])
    instance.status = 401
    with pytest.raises(discovery.DiscoveryError, match="API key and permissions"):
        discovery.discover_prowlarr(instance)


@pytest.mark.parametrize("value", ["file:///etc/passwd", "http://host:99999", "http://user:secret@host", "http://host?apikey=secret", "http://", None])
def test_reject_invalid_service_urls(value):
    assert not discovery.valid_service_url(value)


def test_archive_database_size_is_limited(tmp_path, monkeypatch):
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w") as content:
        content.writestr("prowlarr.db", b"x" * 20)
    monkeypatch.setattr(discovery, "MAX_BACKUP_BYTES", 10)
    with pytest.raises(discovery.DiscoveryError, match="too large"):
        discovery.read_backup_settings(archive)


@pytest.mark.parametrize("host", ["host/path", "host?token=secret", "http://host", "user:password@host", "host:8080"])
def test_sabnzbd_requires_a_host_separate_from_port_and_path(host):
    instance = FakeProwlarr([], [{"id": 1, "implementation": "Sabnzbd", "fields": [
        {"name": "host", "value": host}, {"name": "apiKey", "value": "key"},
    ]}])
    assert discovery.discover_prowlarr(instance)["candidates"] == []


def test_prowlarr_normal_backups_hold_the_discovery_lock(monkeypatch):
    from apps.prowlarr import ProwlarrApp
    from apps.servarr import ServarrApp

    lock = threading.Lock()
    monkeypatch.setattr(ProwlarrApp, "backup_lock", lock)

    def backup(instance, destination):
        assert lock.locked()
        assert destination == "destination"
        return "backup.zip"

    monkeypatch.setattr(ServarrApp, "backup", backup)
    assert ProwlarrApp("http://prowlarr:9696", "key").backup("destination") == "backup.zip"
    assert not lock.locked()


@pytest.mark.parametrize("joined", [False, True])
def test_discovery_backup_ownership_uses_original_command_user_agent(monkeypatch, joined):
    from apps.prowlarr import ProwlarrApp

    instance = ProwlarrApp("http://prowlarr:9696", "key")
    def post(url, json, timeout, headers):
        assert json == {"name": "Backup"}
        marker = "external-browser" if joined else headers["User-Agent"]
        instance.command = {"id": 7, "status": "completed", "body": {"clientUserAgent": marker}}
        return Response({"id": 7})

    monkeypatch.setattr(instance.session, "post", post)
    monkeypatch.setattr(instance.session, "get", lambda *args, **kwargs: Response(instance.command))
    assert instance.trigger_discovery_backup() is (not joined)
