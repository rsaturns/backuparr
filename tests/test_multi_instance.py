"""More than one Radarr/Sonarr (#46): extra instances are "<app>-<suffix>"
ids beside the bare first one, each with its own config, remote folder,
archives, history and restore."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

import backup
import config_store
import restore_actions
import secrets_crypto
from apps.radarr import RadarrApp
from apps.sonarr import SonarrApp


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUPARR_SECRETS_KEY", raising=False)
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "secrets.key"))
    monkeypatch.setattr(secrets_crypto, "_fernet", None)
    yield
    monkeypatch.setattr(secrets_crypto, "_fernet", None)


def write_config(data):
    with open(config_store.CONFIG_PATH, "w") as f:
        json.dump(data, f)


# --- ids -------------------------------------------------------------------

@pytest.mark.parametrize("instance_id, kind", [
    ("radarr", "radarr"), ("plex", "plex"), ("radarr-4k", "radarr"), ("sonarr-anime", "sonarr"),
    ("radarr-4k-hdr", "radarr"),
    ("prowlarr-2", None), ("plex-2", None), ("radarr-", None), ("radarr--4k", None), ("radarr-4K", None),
    ("radarr_4k", None), ("radarr-4k/..", None), ("radarr-" + "a" * 40, None), ("", None), (None, None),
])
def test_instance_type(instance_id, kind):
    assert config_store.instance_type(instance_id) == kind


def test_an_instance_shares_its_apps_metadata():
    assert config_store.app_meta("radarr-4k")["id"] == "radarr"
    assert config_store.key_required("sonarr-anime") is True
    assert config_store.restore_supported("radarr-4k") is True
    assert config_store.app_meta("nope") is None


# --- config ----------------------------------------------------------------

def test_extra_instances_load_save_and_keep_their_secrets_encrypted(isolated_config):
    cfg = config_store.load_config()
    cfg["apps"]["radarr-4k"] = {**config_store.DEFAULT_INSTANCE, "name": "Radarr 4K", "enabled": True,
                                "url": "http://radarr4k:7878", "api_key": "radarr-4k-key"}
    config_store.save_config(cfg)

    with open(config_store.CONFIG_PATH) as f:
        assert "radarr-4k-key" not in f.read()
    reloaded = config_store.load_config()
    assert reloaded["apps"]["radarr-4k"]["api_key"] == "radarr-4k-key"
    assert reloaded["apps"]["radarr-4k"]["name"] == "Radarr 4K"


def test_a_config_from_before_instances_loads_unchanged(isolated_config):
    write_config({"apps": {"radarr": {"enabled": True, "url": "http://radarr:7878"}}})
    cfg = config_store.load_config()
    assert config_store.instance_ids(cfg) == config_store.APP_NAMES
    assert config_store.enabled_apps(cfg) == ["radarr"]


def test_malformed_or_unsupported_instance_ids_are_dropped(isolated_config):
    write_config({"apps": {"radarr-4k": {"url": "http://a"}, "plex-2": {"url": "http://b"},
                           "radarr_x": {}, "../etc": {}}})
    cfg = config_store.load_config()
    assert [i for i in cfg["apps"] if i not in config_store.APP_NAMES] == ["radarr-4k"]


def test_instances_are_listed_and_backed_up_right_after_their_app(isolated_config):
    write_config({"apps": {
        "sonarr-anime": {"enabled": True, "url": "http://s2", "api_key": "k"},
        "radarr-uhd": {"enabled": True, "url": "http://r3", "api_key": "k"},
        "radarr-4k": {"enabled": False, "url": "http://r2", "api_key": "k"},
        "radarr": {"enabled": True, "url": "http://r1", "api_key": "k"},
        "prowlarr": {"enabled": True, "url": "http://p", "api_key": "k"},
    }})
    cfg = config_store.load_config()
    assert config_store.instance_ids(cfg)[:5] == ["radarr", "radarr-4k", "radarr-uhd", "sonarr", "sonarr-anime"]
    assert config_store.enabled_apps(cfg) == ["radarr", "radarr-uhd", "sonarr-anime", "prowlarr"]


def test_removing_an_instance_never_hands_its_unreadable_secret_to_another(isolated_config, monkeypatch):
    cfg = config_store.load_config()
    for instance_id in ("radarr-a", "radarr-b"):
        cfg["apps"][instance_id] = {**config_store.DEFAULT_INSTANCE, "name": instance_id, "api_key": f"{instance_id}-key"}
    config_store.save_config(cfg)

    # Under a wrong key both load blank; radarr-a is removed and the rest saved.
    monkeypatch.setattr(secrets_crypto, "_fernet", None)
    monkeypatch.setenv("BACKUPARR_SECRETS_KEY", "A" * 43 + "=")
    cfg = config_store.load_config()
    del cfg["apps"]["radarr-a"]
    config_store.save_config(cfg)

    monkeypatch.setattr(secrets_crypto, "_fernet", None)
    monkeypatch.delenv("BACKUPARR_SECRETS_KEY")
    cfg = config_store.load_config()
    assert "radarr-a" not in cfg["apps"]
    assert cfg["apps"]["radarr-b"]["api_key"] == "radarr-b-key"


def test_a_header_may_be_scoped_to_an_instance(isolated_config):
    cfg = config_store.load_config()
    cfg["apps"]["radarr-4k"] = {**config_store.DEFAULT_INSTANCE, "name": "4K"}
    cfg["request_headers"] = [{"name": "X-Token", "value": "t", "apps": ["radarr-4k"]}]
    config_store.save_config(cfg)
    cfg = config_store.load_config()
    assert backup.app_connection_config(cfg, "radarr-4k")["request_headers"] == [{"name": "X-Token", "value": "t"}]
    assert backup.app_connection_config(cfg, "radarr")["request_headers"] == []


# --- backup ----------------------------------------------------------------

def test_build_app_builds_an_instance_as_its_app():
    assert isinstance(backup.build_app("radarr-4k", {"url": "http://r", "api_key": "k"}), RadarrApp)
    assert isinstance(backup.build_app("sonarr-anime", {"url": "http://s", "api_key": "k"}), SonarrApp)


def test_each_instance_gets_its_own_folder_and_archive_names(monkeypatch):
    built, uploaded = [], []

    class Fake:
        def backup(self, work_dir):
            path = Path(work_dir) / "files"
            path.mkdir()
            (path / "a.json").write_text("{}")
            return str(path)

    apps = ["radarr", "radarr-4k"]
    cfg = {"apps": {"radarr": {"url": "http://r1"}, "radarr-4k": {"url": "http://r2"}}, "destinations": {"local": {}}}
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: apps)
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local"])
    monkeypatch.setattr(backup.destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(backup.destination_util, "remote_root", lambda dest_id, cfg: "/dest")
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: built.append((name, app_cfg["url"])) or Fake())
    monkeypatch.setattr(backup.rclone_util, "copyto", lambda src, dst: uploaded.append(dst))
    monkeypatch.setattr(backup, "apply_retention", lambda *args: None)

    ok, failed = backup.run_backup(cfg)

    assert not failed and ok == apps
    assert built == [("radarr", "http://r1"), ("radarr-4k", "http://r2")]
    assert re.fullmatch(r"/dest/radarr/radarr_\d{8}_\d{6}\.zip", uploaded[0])
    assert re.fullmatch(r"/dest/radarr-4k/radarr-4k_\d{8}_\d{6}\.zip", uploaded[1])


def test_retention_covers_instances_and_keeps_a_removed_ones_newest(monkeypatch):
    files = ["radarr-4k/radarr-4k_20260101_030000.zip", "radarr-4k/radarr-4k_20260102_030000.zip",
             "radarr/radarr-4k_20260101_030000.zip", "radarr-4k/radarr_20260101_030000.zip",
             "plex-2/plex-2_20260101_030000.zip", "plex-2/plex-2_20260102_030000.zip"]
    deleted = []
    monkeypatch.setattr(backup.rclone_util, "lsjson", lambda root, recursive=False: [
        {"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False} for p in files])
    monkeypatch.setattr(backup.rclone_util, "delete_file", deleted.append)
    backup.apply_retention("dest:", 7, now=datetime(2026, 10, 29))
    # Another app's names in a folder, and folders that aren't instances, are left alone.
    assert deleted == ["dest:/radarr-4k/radarr-4k_20260101_030000.zip"]


# --- web UI: settings ----------------------------------------------------------

def save(client, **body):
    return client.post("/api/config", json=body)


INSTANCE = {"name": "Radarr 4K", "enabled": True, "url": "http://radarr4k:7878", "api_key": "k4"}


def test_an_instance_is_added_renamed_and_removed_through_settings(authed_client):
    assert save(authed_client, apps={"radarr-4k": INSTANCE}).status_code == 200
    cfg = authed_client.get("/api/config").get_json()
    assert cfg["apps"]["radarr-4k"] == {**config_store.DEFAULT_INSTANCE, **INSTANCE}

    assert save(authed_client, apps={"radarr-4k": {"name": "UHD", "bogus": "x"}}).status_code == 200
    cfg = authed_client.get("/api/config").get_json()
    assert cfg["apps"]["radarr-4k"]["name"] == "UHD" and "bogus" not in cfg["apps"]["radarr-4k"]

    assert save(authed_client, apps={"radarr-4k": None}).status_code == 200
    assert "radarr-4k" not in authed_client.get("/api/config").get_json()["apps"]


@pytest.mark.parametrize("apps, fragment", [
    ({"prowlarr-2": {"name": "x"}}, "unknown app"),
    ({"radarr_4k": {"name": "x"}}, "unknown app"),
    ({"radarr-4k": {"url": "http://x"}}, "a name of 1-64 characters"),
    ({"radarr-4k": {"name": "  "}}, "a name of 1-64 characters"),
    ({"radarr-4k": {"name": "x" * 65}}, "a name of 1-64 characters"),
    ({"radarr-4k": None}, "unknown instance"),
    ({"radarr": None}, "can't be removed"),
    ({"radarr-4k": {"name": "4K", "enabled": True}}, "a URL is required"),
])
def test_bad_instance_changes_are_refused(authed_client, apps, fragment):
    response = save(authed_client, apps=apps)
    assert response.status_code == 400 and fragment in response.get_json()["error"]


def test_the_number_of_extra_instances_is_capped(authed_client):
    apps = {f"sonarr-{n}": {"name": str(n)} for n in range(config_store.MAX_EXTRA_INSTANCES)}
    assert save(authed_client, apps=apps).status_code == 200
    response = save(authed_client, apps={"sonarr-one-more": {"name": "x"}})
    assert response.status_code == 400 and "at most" in response.get_json()["error"]
    # Removing one and adding another in the same save is fine.
    assert save(authed_client, apps={"sonarr-0": None, "sonarr-one-more": {"name": "x"}}).status_code == 200


def test_headers_can_be_scoped_to_an_instance_added_in_the_same_save(authed_client):
    header = {"name": "X-Token", "value": "t", "apps": ["radarr-4k"]}
    assert save(authed_client, apps={"radarr-4k": INSTANCE}, request_headers=[header]).status_code == 200


def test_an_instance_a_header_is_scoped_to_cant_be_removed_from_under_it(authed_client):
    header = {"name": "X-Token", "value": "t", "apps": ["radarr-4k"]}
    save(authed_client, apps={"radarr-4k": INSTANCE}, request_headers=[header])
    response = save(authed_client, apps={"radarr-4k": None})
    assert response.status_code == 400 and "known app" in response.get_json()["error"]
    assert save(authed_client, apps={"radarr-4k": None}, request_headers=[]).status_code == 200


def test_an_unsaved_instance_can_be_tested(authed_client, isolated_webui, monkeypatch):
    seen = []

    class Fake:
        def test_connection(self):
            return "ok"

    monkeypatch.setattr(isolated_webui, "build_app", lambda name, app_cfg: seen.append((name, app_cfg)) or Fake())
    header = {"name": "X-Token", "value": "t", "apps": ["radarr-4k"]}
    response = authed_client.post("/api/test/radarr-4k", json={"url": "http://r", "api_key": "k", "request_headers": [header]})
    assert response.get_json() == {"ok": True, "message": "ok"}
    assert seen[0][0] == "radarr-4k" and seen[0][1]["request_headers"] == [{"name": "X-Token", "value": "t"}]
    assert authed_client.post("/api/test/plex-2", json={"url": "http://r"}).status_code == 404


# --- web UI: history and restore -------------------------------------------------

def test_history_lists_instances_including_removed_ones_with_backups_left(authed_client, isolated_webui, monkeypatch):
    save(authed_client, apps={"sonarr-anime": {"name": "Anime"}})
    entries = [{"Path": p, "Name": p.rsplit("/", 1)[-1], "Size": 1, "ModTime": "2026-01-01T00:00:00Z", "IsDir": False}
               for p in ("radarr-old/radarr-old_20260101_030000.zip", "plex-2/plex-2_20260101_030000.zip")]
    monkeypatch.setattr(isolated_webui.rclone_util, "lsjson", lambda root, recursive=False: entries)
    history = authed_client.get("/api/history/local").get_json()
    assert history["sonarr-anime"] == []
    assert [f["name"] for f in history["radarr-old"]] == ["radarr-old_20260101_030000.zip"]
    assert "plex-2" not in history


def test_an_instance_restores_from_its_own_folder_with_its_own_connection(inline, authed_client, monkeypatch, tmp_path):
    calls, work = [], tmp_path / "work"  # removed after the restore
    work.mkdir()
    monkeypatch.setattr(restore_actions, "fetch_backup",
                        lambda root, app, filename=None: calls.append(("fetch", app)) or (str(work), str(work / "b.zip"), f"{app}_1.zip"))
    monkeypatch.setattr(restore_actions, "restore_app",
                        lambda app, app_cfg, tmp_dir, local_zip, **kw: calls.append(("restore", app, app_cfg["url"])) or {})
    save(authed_client, apps={"radarr-4k": INSTANCE})

    assert authed_client.post("/api/restore/local/radarr-4k", json={"confirm": True}).get_json() == {"started": True}
    state = authed_client.get("/api/restore/status").get_json()
    assert state["ok"] is True and state["message"] == "radarr-4k restore uploaded, app is restarting"
    assert calls == [("fetch", "radarr-4k"), ("restore", "radarr-4k", "http://radarr4k:7878")]

    missing = authed_client.post("/api/restore/local/radarr-gone", json={"confirm": True})
    assert missing.status_code == 400 and "not configured" in missing.get_json()["error"]


def test_restore_app_uploads_to_an_instance(monkeypatch):
    uploaded = []
    monkeypatch.setattr(restore_actions, "restore_upload_app", lambda name, app_cfg, local_zip: uploaded.append(name))
    assert restore_actions.restore_app("sonarr-anime", {}, "/tmp", "/tmp/b.zip") == {"kind": "upload"}
    assert uploaded == ["sonarr-anime"]
