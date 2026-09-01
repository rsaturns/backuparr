"""Tests for restore_actions.SAFE_FILENAME (the path-traversal allowlist
used across restore/history routes) and extract_zip()'s zip-slip
containment check."""
import os
import zipfile

import pytest

import restore_actions


# --- SAFE_FILENAME -----------------------------------------------------

REALISTIC_FILENAMES = [
    "radarr_20260901_120000.zip",       # backup.py's zip_name pattern: <app>_<timestamp>.zip
    "sonarr_20251231_235959.zip",
    "prowlarr_20260101_000000.zip",
    "bazarr_backup_v1234567890.zip",    # Bazarr's own restore-side naming convention
    "sabnzbd_config.json",
]


@pytest.mark.parametrize("filename", REALISTIC_FILENAMES)
def test_safe_filename_accepts_realistic_names(filename):
    assert restore_actions.SAFE_FILENAME.match(filename)


UNSAFE_FILENAMES = [
    "../etc/passwd",
    "../../foo.zip",
    "/etc/passwd",
    "foo/bar.zip",
    "",
]


@pytest.mark.parametrize("filename", UNSAFE_FILENAMES)
def test_safe_filename_rejects_path_traversal(filename):
    assert restore_actions.SAFE_FILENAME.match(filename) is None


def test_fetch_backup_rejects_unsafe_filename_before_touching_rclone():
    # No rclone remote is reachable in a test environment - if fetch_backup
    # tried to actually use it, this would hang/error for the wrong reason.
    # The ValueError must come from the SAFE_FILENAME check alone.
    with pytest.raises(ValueError, match="invalid backup filename"):
        restore_actions.fetch_backup("local:/backups", "radarr", filename="../../etc/passwd")


# --- extract_zip() zip-slip containment --------------------------------

def _write_zip(zip_path, members):
    """members: {arcname: content_bytes}"""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for arcname, content in members.items():
            zf.writestr(arcname, content)
    return str(zip_path)


def test_extract_zip_rejects_relative_traversal(tmp_path):
    zip_path = _write_zip(tmp_path / "evil.zip", {"../evil.txt": b"pwned"})
    dest_dir = tmp_path / "dest"

    with pytest.raises(ValueError, match="unsafe path"):
        restore_actions.extract_zip(zip_path, str(dest_dir))

    # Nothing should have escaped above dest_dir's parent.
    assert not (tmp_path / "evil.txt").exists()


def test_extract_zip_rejects_absolute_member_path(tmp_path):
    zip_path = _write_zip(tmp_path / "evil2.zip", {"/etc/evil.txt": b"pwned"})
    dest_dir = tmp_path / "dest"

    with pytest.raises(ValueError, match="unsafe path"):
        restore_actions.extract_zip(zip_path, str(dest_dir))

    assert not os.path.exists("/etc/evil.txt")


def test_extract_zip_extracts_well_formed_zip(tmp_path):
    zip_path = _write_zip(
        tmp_path / "good.zip",
        {"file1.txt": b"hello", "sub/file2.txt": b"world"},
    )
    dest_dir = tmp_path / "dest"

    result = restore_actions.extract_zip(zip_path, str(dest_dir))

    assert result == str(dest_dir)
    assert (dest_dir / "file1.txt").read_bytes() == b"hello"
    assert (dest_dir / "sub" / "file2.txt").read_bytes() == b"world"
