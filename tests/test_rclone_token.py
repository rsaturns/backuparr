import base64
import json
import time

import pytest

import onedrive_oauth
import rclone_token

TOKEN = {"access_token": "acc", "token_type": "bearer", "refresh_token": "ref", "expiry": "2026-10-06T12:00:00Z"}
B64 = base64.b64encode(json.dumps(TOKEN).encode()).decode()


def test_parses_the_full_terminal_block():
    block = f"Paste the following into your remote machine --->\n{B64}\n<---End paste"
    token_json, access = rclone_token.parse_token_blob(block, "dropbox")
    assert access == "acc" and json.loads(token_json) == TOKEN


def test_whitespace_around_the_pasted_token_is_ignored():
    block = f"Paste the following into your remote machine   --->  \n {B64} \n  <---   End paste"
    assert rclone_token.parse_token_blob(block, "dropbox")[1] == "acc"


def test_a_long_whitespace_run_after_the_marker_fails_fast():
    # Overlapping \s* around the token made this cubic: about 8s at this size.
    started = time.perf_counter()
    with pytest.raises(rclone_token.TokenError):
        rclone_token.parse_token_blob("Paste the following into your remote machine --->" + " " * 3_000 + "x", "dropbox")
    assert time.perf_counter() - started < 1


def test_a_paste_repeating_the_marker_fails_fast():
    # A search() retrying at every copy of the marker took seconds at this size.
    started = time.perf_counter()
    with pytest.raises(rclone_token.TokenError):
        rclone_token.parse_token_blob("Paste the following into your remote machine --->" * 2_000 + " " * 100_000, "dropbox")
    assert time.perf_counter() - started < 1


def test_text_before_the_terminal_block_is_ignored():
    block = f"$ rclone authorize dropbox\nPaste the following into your remote machine --->\n{B64}\n<---End paste\n"
    assert rclone_token.parse_token_blob(block, "dropbox")[1] == "acc"


def test_parses_the_bare_base64_line_and_raw_json():
    assert rclone_token.parse_token_blob(B64, "dropbox")[1] == "acc"
    assert rclone_token.parse_token_blob(json.dumps(TOKEN, indent=2), "dropbox")[1] == "acc"


def test_the_stored_token_is_a_single_line():
    token_json, _ = rclone_token.parse_token_blob(json.dumps(TOKEN, indent=2), "dropbox")
    assert "\n" not in token_json


@pytest.mark.parametrize("pasted", [None, "", "   "])
def test_empty_paste_names_the_backend_command(pasted):
    with pytest.raises(rclone_token.TokenError, match="rclone authorize dropbox"):
        rclone_token.parse_token_blob(pasted, "dropbox")


@pytest.mark.parametrize("pasted", [
    "not a token",
    base64.b64encode(b"not json").decode(),
    json.dumps([1, 2]),
    json.dumps({"access_token": "acc"}),  # no refresh token: Backuparr could not keep it working
    json.dumps({"refresh_token": "ref"}),
])
def test_rejects_anything_that_is_not_a_usable_token(pasted):
    with pytest.raises(rclone_token.TokenError, match="rclone authorize onedrive"):
        rclone_token.parse_token_blob(pasted, "onedrive")


def test_onedrive_keeps_raising_its_own_error():
    assert onedrive_oauth.parse_token_blob(B64)[1] == "acc"
    with pytest.raises(onedrive_oauth.OneDriveOAuthError, match="rclone authorize onedrive"):
        onedrive_oauth.parse_token_blob("nope")
