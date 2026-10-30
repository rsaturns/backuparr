"""destination_util.sync runs before every listing, download and backup, so
an unchanged setup costs one `rclone config dump` and nothing else."""
import json

import pytest

import destination_util
import rclone_util
from config_store import DEFAULTS


@pytest.fixture
def rclone(monkeypatch):
    state = {"remotes": {}, "calls": []}

    def run(args, redact=()):
        state["calls"].append(args[:2])
        if args[:2] == ["config", "dump"]:
            return json.dumps(state["remotes"])
        if args[:2] in (["config", "create"], ["config", "update"]):
            name, rest = args[2], args[3:]
            if args[1] == "create":
                remote, rest = {"type": rest[0]}, rest[1:]
            else:
                remote = state["remotes"].setdefault(name, {})
            pairs = rest[:rest.index("config_refresh_token")]
            remote.update(dict(zip(pairs[::2], pairs[1::2])))
            state["remotes"][name] = remote
        if args[:2] == ["config", "delete"]:
            state["remotes"].pop(args[2], None)
        return ""
    monkeypatch.setattr(rclone_util, "_run", run)
    return state


def config(**dests):
    cfg = json.loads(json.dumps(DEFAULTS))
    for name, values in dests.items():
        cfg["destinations"][name].update(values)
    return cfg


def test_nothing_connected_costs_one_dump(rclone):
    destination_util.sync(config())
    assert rclone["calls"] == [["config", "dump"]]


def test_an_unchanged_setup_costs_one_dump(rclone):
    cfg = config(gdrive={"client_id": "c", "client_secret": "s", "refresh_token": "rt"},
                 onedrive={"token": '{"refresh_token":"o"}', "drive_id": "d", "drive_type": "personal", "item_id": "i"},
                 dropbox={"token": '{"refresh_token":"x"}'})
    destination_util.sync(cfg)
    assert len([c for c in rclone["calls"] if c != ["config", "dump"]]) == 3
    rclone["calls"].clear()
    destination_util.sync(cfg)
    assert rclone["calls"] == [["config", "dump"]]
    # A changed OneDrive folder is written, without resending the token.
    cfg["destinations"]["onedrive"]["item_id"] = "other"
    rclone["calls"].clear()
    destination_util.sync(cfg)
    assert ["config", "update"] in rclone["calls"] and rclone["remotes"]["backuparr-onedrive"]["root_folder_id"] == "other"
