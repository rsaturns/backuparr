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
