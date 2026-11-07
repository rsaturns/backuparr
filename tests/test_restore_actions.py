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


# --- the restore table -------------------------------------------------

def test_every_app_with_automated_restore_has_a_restore_and_no_other_does():
    from config_store import APP_META, restore_supported
    assert set(restore_actions.RESTORES) == {m["id"] for m in APP_META if restore_supported(m["id"])}


def test_restore_warnings_have_a_lead_and_text():
    from config_store import APP_META
    warnings = [m["restore_warning"] for m in APP_META if "restore_warning" in m]
    assert warnings and all(w["lead"] and w["text"] and set(w) <= {"lead", "text", "danger"} for w in warnings)


@pytest.mark.parametrize("app, result, message", [
    ("radarr-4k", {"kind": "upload"}, "radarr-4k restore uploaded, app is restarting"),
    ("emby", {"kind": "emby"}, "emby restore complete, start Emby again to load it"),
    ("lazylibrarian", {}, "lazylibrarian restore complete, start LazyLibrarian again to load it"),
    ("tautulli", {"summary": {"config_staged": "log in"}}, "tautulli config staged, finish it in Tautulli"),
    ("tautulli", {"summary": {"config": "ok"}}, "tautulli restore uploaded"),
    ("nzbget", {"reloaded": False}, "nzbget settings restored, but NZBGet didn't confirm the reload - restart NZBGet to apply them"),
])
def test_restore_message(app, result, message):
    assert restore_actions.restore_message(app, result) == message


def test_restore_step_names_the_instance():
    assert restore_actions.restore_step("sonarr-anime") == "uploading backup to sonarr-anime, it will restart"
    assert restore_actions.restore_step("plex") == "writing the backup into plex's data folder"


def test_an_app_without_automated_restore_is_refused():
    with pytest.raises(ValueError, match="no restore action"):
        restore_actions.restore_app("profilarr", {"url": "http://p", "api_key": "k"}, "/tmp", "/tmp/b.tar.gz")


# --- listing an app's backups ------------------------------------------

def test_only_backuparrs_archives_are_listed_and_picked_as_latest(monkeypatch):
    # rclone lsf lists folders with a trailing "/", and anything else kept there.
    monkeypatch.setattr(restore_actions.rclone_util, "lsf", lambda remote_dir: [
        "radarr_20261101_030000.zip", "radarr_20261107_030000.zip", "zz-notes.txt", "radarr_old/",
        "sonarr_20261109_030000.zip",
    ])
    assert restore_actions.list_backups("dest:", "radarr") == ["radarr_20261101_030000.zip", "radarr_20261107_030000.zip"]
    assert restore_actions.latest_backup("dest:", "radarr") == "radarr_20261107_030000.zip"


def test_a_folder_with_no_archives_has_no_latest(monkeypatch):
    monkeypatch.setattr(restore_actions.rclone_util, "lsf", lambda remote_dir: ["notes.txt"])
    with pytest.raises(FileNotFoundError, match="no backups of radarr on this destination"):
        restore_actions.latest_backup("dest:", "radarr")
