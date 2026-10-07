"""Operational failures (an unreachable app, a full disk, a wrong key, ...)
are configuration problems, not crashes: the log gets one readable line and
the UI a matching message. Real bugs keep their traceback."""
import errno
import logging
import socket
import zipfile

import pytest
import requests

import backup
import config_store
import destination_util
import rclone_util
from apps.servarr import ServarrError, UnsafeRedirectError

KEY = "SECRET-APP-KEY"
URL = f"http://sonarr-gone:8989/api/v3/command?apikey={KEY}"


def caught_from(call):
    with pytest.raises(Exception) as caught:
        call()
    return caught.value


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.exceptions.HTTPError(f"{status} Error for url: {URL}", response=response)


# --- classifying ------------------------------------------------------------

def test_a_failed_lookup_is_recognised_through_the_requests_wrapping(dns_down):
    assert backup.is_dns_failure(caught_from(lambda: requests.get(URL, timeout=2)))
    assert backup.is_dns_failure(socket.gaierror(-2, "Name does not resolve"))


def test_a_refused_connection_names_the_host_and_port():
    error = caught_from(lambda: requests.get("http://127.0.0.1:1/?apikey=" + KEY, timeout=2))
    message = backup.describe_failure(error, "http://127.0.0.1:1/?apikey=" + KEY)
    assert message.startswith("couldn't connect - connection refused by 127.0.0.1:1")
    assert KEY not in message


@pytest.mark.parametrize("error, expected", [
    (requests.exceptions.ReadTimeout(f"timed out: {URL}"), "couldn't connect - timed out waiting for sonarr-gone:8989"),
    (requests.exceptions.ConnectTimeout(f"timed out: {URL}"), "couldn't connect - timed out waiting for sonarr-gone:8989"),
    (requests.exceptions.SSLError(f"bad handshake: {URL}"), "TLS/SSL error talking to sonarr-gone:8989"),
    (requests.exceptions.ConnectionError(f"reset: {URL}"), "couldn't connect - check the URL and that it's reachable from this container"),
    (requests.exceptions.MissingSchema(f"No scheme: {URL}"), "that doesn't look like a valid URL"),
    (requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0), "sonarr-gone:8989 didn't answer with API data"),
    (requests.exceptions.ChunkedEncodingError(f"cut: {URL}"), "request to sonarr-gone:8989 failed (ChunkedEncodingError)"),
    (http_error(401), "sonarr-gone:8989 refused the API key (HTTP 401)"),
    (http_error(404), "sonarr-gone:8989 answered HTTP 404 - check the URL, including any base path"),
    (http_error(503), "sonarr-gone:8989 had an internal error (HTTP 503)"),
])
def test_request_failures_get_a_readable_line_without_the_url_or_key(error, expected):
    message = backup.describe_failure(error, URL)
    assert message.startswith(expected)
    assert KEY not in message and "apikey" not in message


@pytest.mark.parametrize("error, expected", [
    (OSError(errno.ENOSPC, "No space left on device", "/config/backuparr/backups/radarr/a.zip"), "no space left on the device (/config/backuparr/backups/radarr/a.zip)"),
    (PermissionError(errno.EACCES, "Permission denied", "/config/backuparr/config.json"), "permission denied - Backuparr runs as PUID:PGID"),
    (OSError(errno.EROFS, "Read-only file system", "/config/x"), "the filesystem is read-only (/config/x)"),
    (OSError(errno.EDQUOT, "Disk quota exceeded"), "disk quota exceeded"),
    (zipfile.BadZipFile("File is not a zip file"), "the backup file isn't a valid zip"),
    (FileNotFoundError("No backups found at /x/radarr/"), "No backups found at /x/radarr/"),
    (config_store.ConfigError("config.json isn't valid JSON"), "config.json isn't valid JSON"),
    (ServarrError("sonarr: unauthorized - check the API key"), "sonarr: unauthorized - check the API key"),
    (UnsafeRedirectError("refusing to follow a redirect"), "refusing to follow a redirect"),
    (destination_util.DestinationError("Dropbox is not connected"), "Dropbox is not connected"),
    (rclone_util.RcloneError("rclone copyto failed: boom"), "rclone copyto failed: boom"),
])
def test_other_expected_failures_are_described(error, expected):
    assert backup.describe_failure(error).startswith(expected)


@pytest.mark.parametrize("error", [KeyError("x"), ValueError("bug"), TypeError("bug"), OSError(errno.EIO, "I/O error"), RuntimeError("?")])
def test_anything_else_is_a_bug_and_keeps_its_traceback(error):
    assert backup.describe_failure(error) is None
    assert backup.humanize_error(error) == str(error)


def test_the_message_names_the_host_but_never_the_url():
    message = backup.dns_failure_message(URL)
    assert "couldn't resolve hostname 'sonarr-gone'" in message
    assert KEY not in message and "8989" not in message
    assert "the hostname in the URL" in backup.dns_failure_message("")


def test_log_failure_writes_one_line_or_the_full_stack(caplog):
    logger = logging.getLogger("backuparr.test")
    with caplog.at_level(logging.INFO, logger="backuparr.test"):
        backup.log_failure(logger, "radarr: backup failed", OSError(errno.ENOSPC, "No space left on device"))
        try:
            {}["missing"]
        except KeyError as exc:
            backup.log_failure(logger, "radarr: backup failed", exc)
    clean, bug = caplog.records
    assert clean.exc_info is None and clean.getMessage() == "radarr: backup failed - no space left on the device"
    assert bug.exc_info and bug.getMessage() == "radarr: backup failed"


# --- a whole run ------------------------------------------------------------

@pytest.fixture
def one_app_run(tmp_path, monkeypatch):
    def run(app_url, key=KEY, app_name="sonarr"):
        monkeypatch.setattr(backup, "enabled_apps", lambda cfg: [app_name])
        monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local"])
        monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
        monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: str(tmp_path))
        monkeypatch.setattr(backup.rclone_util, "delete_older_than", lambda *args: None)
        cfg = {"retention_days": 7, "apps": {app_name: {"url": app_url, "api_key": key}}, "destinations": {"local": {}}}
        return backup.run_backup(cfg)

    return run


def failure_records(caplog, needle="backup failed"):
    return [r for r in caplog.records if needle in r.getMessage()]


def test_a_backup_that_cannot_resolve_the_host_logs_one_clean_line(one_app_run, dns_down, caplog):
    with caplog.at_level(logging.INFO):
        ok, failed = one_app_run("http://sonarr-gone:8989")

    assert not ok
    assert failed == ["sonarr: couldn't resolve hostname 'sonarr-gone' - check the URL and that this container can look it up (same Docker network, or working DNS)"]
    (record,) = failure_records(caplog)
    assert record.exc_info is None
    assert record.getMessage().startswith("sonarr: backup failed - couldn't resolve hostname 'sonarr-gone'")
    assert "Traceback" not in caplog.text and KEY not in caplog.text


def test_a_refused_connection_during_a_backup_is_one_clean_line(one_app_run, caplog):
    with caplog.at_level(logging.INFO):
        ok, failed = one_app_run("http://127.0.0.1:1")
    assert failed == ["sonarr: couldn't connect - connection refused by 127.0.0.1:1; is the app running, and is the port right?"]
    (record,) = failure_records(caplog)
    assert record.exc_info is None and "Traceback" not in caplog.text


def test_a_full_disk_while_packaging_a_backup_is_one_clean_line(one_app_run, monkeypatch, caplog):
    def disk_full(self, work_dir):
        raise OSError(errno.ENOSPC, "No space left on device", work_dir + "/sonarr.db")

    monkeypatch.setattr(backup.SonarrApp, "backup", disk_full)
    with caplog.at_level(logging.INFO):
        ok, failed = one_app_run("http://sonarr:8989")
    assert not ok and len(failed) == 1 and failed[0].startswith("sonarr: no space left on the device (")
    (record,) = failure_records(caplog)
    assert record.exc_info is None and "Traceback" not in caplog.text


def test_a_work_folder_that_cannot_be_created_fails_that_app_only(one_app_run, monkeypatch, caplog):
    def read_only(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", path)

    monkeypatch.setattr(backup.os, "makedirs", read_only)
    with caplog.at_level(logging.INFO):
        ok, failed = one_app_run("http://sonarr:8989")
    assert not ok and failed[0].startswith("sonarr: permission denied - Backuparr runs as PUID:PGID")
    assert failure_records(caplog)[0].exc_info is None


def test_a_rejected_api_key_is_one_clean_line(one_app_run, monkeypatch, caplog):
    def unauthorized(self, work_dir):
        raise ServarrError("sonarr: unauthorized - check the API key")

    monkeypatch.setattr(backup.SonarrApp, "backup", unauthorized)
    with caplog.at_level(logging.INFO):
        ok, failed = one_app_run("http://sonarr:8989")
    assert failed == ["sonarr: unauthorized - check the API key"]
    (record,) = failure_records(caplog)
    assert record.exc_info is None and record.getMessage() == "sonarr: backup failed - unauthorized - check the API key"


def test_an_unexpected_error_still_logs_its_traceback(one_app_run, monkeypatch, caplog):
    def bug(self, work_dir):
        raise KeyError("surprise")

    monkeypatch.setattr(backup.SonarrApp, "backup", bug)
    with caplog.at_level(logging.INFO):
        ok, failed = one_app_run("http://sonarr:8989")
    assert not ok and failed == ["sonarr: 'surprise'"]
    (record,) = failure_records(caplog)
    assert record.exc_info and record.exc_info[0] is KeyError


def test_an_upload_failure_shows_the_cause_not_the_whole_rclone_command(one_app_run, monkeypatch, caplog):
    def refuse(local_path, remote_path):
        raise rclone_util.RcloneError(
            f"rclone copyto {local_path} {remote_path} failed: Failed to copyto: mkdir /ro/sonarr: read-only file system (the destination is read-only)",
            "Failed to copyto: mkdir /ro/sonarr: read-only file system (the destination is read-only)",
        )

    monkeypatch.setattr(backup.SonarrApp, "backup", lambda self, work_dir: _write_archive(work_dir))
    monkeypatch.setattr(backup.rclone_util, "copyto", refuse)
    with caplog.at_level(logging.INFO):
        ok, failed = one_app_run("http://sonarr:8989")
    assert failed == ["sonarr: failed on local: Failed to copyto: mkdir /ro/sonarr: read-only file system (the destination is read-only)"]
    (line,) = [r for r in caplog.records if "upload to local failed" in r.getMessage()]
    assert "rclone copyto" not in line.getMessage() and line.exc_info is None


def _write_archive(work_dir):
    import pathlib

    folder = pathlib.Path(work_dir) / "backup"
    folder.mkdir()
    (folder / "sonarr.db").write_text("x")
    return str(folder)


def test_rclone_errors_describe_themselves_by_their_cause():
    error = rclone_util.RcloneError("rclone copyto a b failed: boom (hint)", "boom (hint)")
    assert backup.describe_failure(error) == "boom (hint)"
    assert str(error).startswith("rclone copyto a b failed")
    assert rclone_util.RcloneError("just this").detail == "just this"


def test_the_apps_own_prefix_is_not_repeated_after_its_label():
    error = ServarrError("sonarr: unauthorized - check the API key")
    assert backup.humanize_error(error) == "sonarr: unauthorized - check the API key"
    assert backup.humanize_error(error, app="sonarr") == "unauthorized - check the API key"
    assert backup.humanize_error(error, app="radarr") == "sonarr: unauthorized - check the API key"
    assert backup.humanize_error(KeyError("k"), app="sonarr") == "'k'"
