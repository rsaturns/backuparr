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
