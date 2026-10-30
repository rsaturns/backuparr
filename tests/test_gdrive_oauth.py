"""Google Drive's rclone remote is only rewritten when its settings change,
since rclone takes the client secret and token on its command line."""
import json

import pytest

import gdrive_oauth

CFG = {"client_id": "cid", "client_secret": "csecret", "refresh_token": "rt", "folder_id": "folder"}


@pytest.fixture
def rclone(monkeypatch):
    state = {"remotes": {}, "sets": [], "deleted": []}
    monkeypatch.setattr(gdrive_oauth.rclone_util, "config_dump", lambda: dict(state["remotes"]))

    def config_set(name, kind, fields, force=False):
        state["sets"].append((name, kind, dict(fields), force))
        state["remotes"][name] = {"type": kind, **fields, "config_refresh_token": "false"}
    monkeypatch.setattr(gdrive_oauth.rclone_util, "config_set", config_set)
    monkeypatch.setattr(gdrive_oauth.rclone_util, "config_delete", state["deleted"].append)
    return state


def test_a_missing_remote_is_created(rclone):
    gdrive_oauth.sync_rclone_remote(CFG)
    [(name, kind, fields, force)] = rclone["sets"]
    assert (name, kind, force) == ("backuparr-gdrive", "drive", True)
    assert json.loads(fields["token"])["refresh_token"] == "rt" and fields["root_folder_id"] == "folder"


def test_an_unchanged_remote_is_left_alone_even_after_rclone_refreshes_it(rclone):
    gdrive_oauth.sync_rclone_remote(CFG)
    remote = rclone["remotes"]["backuparr-gdrive"]
    remote["token"] = json.dumps({"access_token": "fresh", "refresh_token": "rt", "expiry": "2030-01-01T00:00:00Z"})
    gdrive_oauth.sync_rclone_remote(CFG)
    assert len(rclone["sets"]) == 1


@pytest.mark.parametrize("change", [
    {"client_secret": "rotated"}, {"client_id": "other"}, {"refresh_token": "reconnected"},
    {"folder_id": "elsewhere"}, {"folder_id": ""},
])
def test_a_changed_setting_rewrites_the_remote(rclone, change):
    gdrive_oauth.sync_rclone_remote(CFG)
    gdrive_oauth.sync_rclone_remote({**CFG, **change})
    assert len(rclone["sets"]) == 2
    assert ("root_folder_id" in rclone["sets"][1][2]) == bool({**CFG, **change}["folder_id"])


def test_a_remote_with_an_unreadable_token_is_rewritten(rclone):
    rclone["remotes"]["backuparr-gdrive"] = {"type": "drive", "client_id": "cid", "client_secret": "csecret",
                                             "scope": "drive.file", "root_folder_id": "folder", "token": "garbage"}
    gdrive_oauth.sync_rclone_remote(CFG)
    assert len(rclone["sets"]) == 1


def test_disconnecting_removes_the_remote(rclone):
    gdrive_oauth.sync_rclone_remote({**CFG, "refresh_token": ""})
    assert rclone["deleted"] == ["backuparr-gdrive"] and rclone["sets"] == []
