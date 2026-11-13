import os
"""Tests for rclone_util._run()'s secret redaction and config_set()'s use
of SENSITIVE_FIELDS to drive it."""
from unittest.mock import MagicMock, patch

import pytest

import rclone_util


def test_run_redacts_secret_from_argv_and_stderr_in_error_message():
    secret = "s3kr1t-token-value"
    fake_result = MagicMock()
    fake_result.returncode = 1
    # The secret shows up in both the failing command's own args (joined
    # into the message) and in the simulated stderr.
    fake_result.stderr = f"failed to authenticate: token={secret} was rejected"

    with patch("rclone_util.subprocess.run", return_value=fake_result):
        with pytest.raises(rclone_util.RcloneError) as exc_info:
            rclone_util._run(["config", "update", "gdrive", "token", secret], redact=[secret])

    message = str(exc_info.value)
    assert secret not in message
    assert "***" in message


def test_run_without_redact_leaves_message_unredacted():
    # Sanity check that the previous test is actually exercising redaction,
    # not something else stripping the secret.
    secret = "s3kr1t-token-value"
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = f"failed: {secret}"

    with patch("rclone_util.subprocess.run", return_value=fake_result):
        with pytest.raises(rclone_util.RcloneError) as exc_info:
            rclone_util._run(["config", "update", "gdrive", "token", secret])

    assert secret in str(exc_info.value)


def test_run_collapses_repeated_retry_lines_to_the_last_one():
    # Real shape of rclone's stderr on a failed copyto: one timestamped
    # ERROR line per retry attempt, then a final NOTICE summary line -
    # all three attempts repeat the same underlying cause.
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = (
        '2026/09/14 03:02:00 ERROR : Attempt 1/3 failed with 1 errors and: couldn\'t fetch token: invalid_grant\n'
        '2026/09/14 03:02:00 ERROR : Attempt 2/3 failed with 1 errors and: couldn\'t fetch token: invalid_grant\n'
        '2026/09/14 03:02:00 ERROR : Attempt 3/3 failed with 1 errors and: couldn\'t fetch token: invalid_grant\n'
        '2026/09/14 03:02:00 NOTICE: Failed to copyto: couldn\'t fetch token: invalid_grant: maybe token expired?'
    )

    with patch("rclone_util.subprocess.run", return_value=fake_result):
        with pytest.raises(rclone_util.RcloneError) as exc_info:
            rclone_util._run(["copyto", "a.zip", "gdrive:a.zip"])

    message = str(exc_info.value)
    assert message.count("Attempt") == 0
    assert message.count("invalid_grant") == 1
    assert "Failed to copyto: couldn't fetch token: invalid_grant: maybe token expired?" in message


def test_run_collapses_embedded_request_url():
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = (
        '2026/09/14 03:02:00 NOTICE: Failed to copyto: couldn\'t list directory: '
        'Get "https://www.googleapis.com/drive/v3/files?alt=json&fields=id%2Cname&pageSize=1000": '
        "couldn't fetch token: invalid_grant"
    )

    with patch("rclone_util.subprocess.run", return_value=fake_result):
        with pytest.raises(rclone_util.RcloneError) as exc_info:
            rclone_util._run(["copyto", "a.zip", "gdrive:a.zip"])

    message = str(exc_info.value)
    assert "googleapis.com" not in message
    assert '"<url>"' in message


def test_config_set_redacts_sensitive_fields_via_run(monkeypatch):
    secret = "super-secret-client-secret-value"
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = f"create failed: client_secret {secret} was invalid"

    # Isolate from a real rclone.conf / real "config dump" subprocess call.
    monkeypatch.setattr(rclone_util, "config_dump", lambda: {})

    with patch("rclone_util.subprocess.run", return_value=fake_result):
        with pytest.raises(rclone_util.RcloneError) as exc_info:
            rclone_util.config_set("gdrive", "drive", {"client_secret": secret})

    assert secret not in str(exc_info.value)


@pytest.mark.parametrize("root, expected", [
    ("backuparr-gdrive:", "backuparr-gdrive:"),
    ("backuparr-gdrive:/", "backuparr-gdrive:"),
    ("backuparr-dropbox:Backuparr", "backuparr-dropbox:"),
    ("backuparr-dropbox:Backuparr/radarr", "backuparr-dropbox:"),
    ("backuparr-onedrive", "backuparr-onedrive:"),
])
def test_check_remote_lists_only_the_remote_itself(monkeypatch, root, expected):
    calls = []
    monkeypatch.setattr(rclone_util, "_run", lambda args, redact=(): calls.append(args))
    rclone_util.check_remote(root)
    assert calls == [["lsd", "--max-depth", "1", expected]]


@pytest.mark.parametrize("stderr, hint", [
    ("Failed to copyto: mkdir /config/backuparr/backups/radarr: permission denied", "the folder isn't writable by Backuparr's user (PUID:PGID)"),
    ("Failed to copyto: write /backups/a.zip: no space left on device", "the destination is out of space"),
    ("Failed to copyto: open /backups/a.zip: read-only file system", "the destination is read-only"),
    ("Failed to copyto: googleapi: Error 403: storageQuotaExceeded", "the destination is out of space"),
    # Dropbox, as a full lab account answered.
    ("Failed to copyto: upload failed: batch upload failed: upload failed: path/insufficient_space", "the destination is out of space"),
    # OneDrive, as a full lab account answered.
    ("Failed to copyto: quotaLimitReached: Quota limit reached", "the destination is out of space"),
    ("Failed to copyto: couldn't fetch token: invalid_grant: maybe token expired?", "reconnect it in Settings"),
    ("Failed to lsd: Get \"https://api.dropboxapi.com\": dial tcp: lookup api.dropboxapi.com on 127.0.0.11:53: no such host", "check this container's network and DNS"),
])
def test_run_adds_a_plain_hint_for_common_destination_failures(stderr, hint):
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = f"2026/10/07 03:00:00 NOTICE: {stderr}"
    with patch("rclone_util.subprocess.run", return_value=fake_result):
        with pytest.raises(rclone_util.RcloneError) as exc_info:
            rclone_util._run(["copyto", "a.zip", "local:a.zip"])
    assert str(exc_info.value).endswith(f"({hint})") or hint in str(exc_info.value)
    assert str(exc_info.value).count(hint) == 1


def test_run_adds_no_hint_to_an_unrecognised_failure():
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = "2026/10/07 03:00:00 NOTICE: Failed to copyto: something unusual"
    with patch("rclone_util.subprocess.run", return_value=fake_result):
        with pytest.raises(rclone_util.RcloneError) as exc_info:
            rclone_util._run(["copyto", "a.zip", "local:a.zip"])
    assert str(exc_info.value).endswith("Failed to copyto: something unusual")


def test_error_detail_is_the_cause_without_the_command_and_is_redacted():
    secret = "s3cr3t-token-value"
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = f"2026/10/07 03:00:00 NOTICE: Failed to copyto: no space left on device {secret}"
    with patch("rclone_util.subprocess.run", return_value=fake_result):
        with pytest.raises(rclone_util.RcloneError) as exc_info:
            rclone_util._run(["copyto", "a.zip", f"x:{secret}"], redact=[secret])
    assert exc_info.value.detail == "Failed to copyto: no space left on device *** (the destination is out of space)"
    assert "rclone copyto" in str(exc_info.value) and secret not in str(exc_info.value)


def _rclone_exits(code, stdout="", stderr="ERROR : boom"):
    result = MagicMock(returncode=code, stdout=stdout, stderr=stderr)
    return patch("rclone_util.subprocess.run", return_value=result)


def test_a_missing_folder_lists_as_empty():
    with _rclone_exits(3, stderr="ERROR : error listing: directory not found"):
        assert rclone_util.lsf("remote:radarr/") == [] and rclone_util.lsjson("remote:", recursive=True) == []


@pytest.mark.parametrize("listing", [lambda: rclone_util.lsf("remote:radarr/"), lambda: rclone_util.lsjson("remote:")])
def test_any_other_listing_failure_raises_instead_of_looking_empty(listing):
    with _rclone_exits(1, stderr="CRITICAL: couldn't fetch token: invalid_grant"):
        with pytest.raises(rclone_util.RcloneError, match="expired or been revoked") as exc:
            listing()
    assert exc.value.returncode == 1


def test_the_bandwidth_limit_applies_to_remotes_but_not_local_storage(monkeypatch):
    monkeypatch.setenv("RCLONE_BWLIMIT", "1M")
    assert rclone_util._env(["copyto", "/tmp/a.zip", "gdrive:radarr/a.zip"]) is None  # inherits the limit
    local = rclone_util._env(["copyto", "/tmp/a.zip", "/config/backuparr/backups/radarr/a.zip"])
    assert "RCLONE_BWLIMIT" not in local and local.get("PATH") == os.environ.get("PATH")
    monkeypatch.delenv("RCLONE_BWLIMIT")
    assert rclone_util._env(["copyto", "/tmp/a", "/tmp/b"]) is None


def test_a_listing_passes_its_include_patterns(monkeypatch):
    calls = []
    monkeypatch.setattr(rclone_util, "_run", lambda args, redact=(): calls.append(args) or "[]")
    rclone_util.lsjson("remote:", recursive=True, include=["/{a,b-*}/**"])
    rclone_util.lsjson("remote:")
    assert calls == [["lsjson", "--recursive", "--include", "/{a,b-*}/**", "remote:"], ["lsjson", "remote:"]]
