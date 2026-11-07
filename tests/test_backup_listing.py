"""Retention and History list only Backuparr's own folders at a
destination's root, so a folder Backuparr can't read there (ext4's
lost+found at the root of a disk) doesn't fail the listing."""
import fnmatch
import re

import backup
import destination_util
import rclone_util


def _folders_matched(pattern, names):
    """The names rclone's "/{a,b-*}/**" pattern lets a listing enter."""
    alternatives = re.fullmatch(r"/\{(.*)\}/\*\*", pattern).group(1).split(",")
    return [n for n in names if any(fnmatch.fnmatchcase(n, alt) for alt in alternatives)]


def test_the_pattern_covers_every_backup_folder_and_nothing_else():
    names = ["radarr", "radarr-4k", "sonarr-anime", "lidarr-flac", "whisparr-v3", "plex", "lazylibrarian",
             "backuparr", "lost+found", "photos", "System Volume Information", "$RECYCLE.BIN", "@eaDir"]
    matched = _folders_matched(destination_util.backup_folder_pattern(), names)
    assert matched == ["radarr", "radarr-4k", "sonarr-anime", "lidarr-flac", "whisparr-v3", "plex", "lazylibrarian",
                       "backuparr"]
    assert all(destination_util.is_backup_folder(name) for name in matched)


def test_every_app_is_in_the_pattern():
    from config_store import APP_NAMES
    assert _folders_matched(destination_util.backup_folder_pattern(), APP_NAMES) == APP_NAMES


def test_retention_and_history_list_with_the_pattern(monkeypatch, authed_client, isolated_webui):
    calls = []
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): calls.append((root, recursive, include)) or [])
    backup.apply_retention("dest:", 7)
    assert authed_client.get("/api/history/local").status_code == 200
    pattern = [destination_util.backup_folder_pattern()]
    assert calls[0] == ("dest:", True, pattern)
    assert calls[1][1:] == (True, pattern)
