"""History (list, download, delete) and the restore pipeline, with rclone and
the app drivers stubbed out - CI has no rclone binary."""
import logging
import os

import pytest

from conftest import PASSWORD
import requests

import restore_actions
from apps.servarr import ServarrError
from config_store import APP_NAMES


@pytest.fixture
def rclone(isolated_webui, monkeypatch):
    """Fake rclone_util calls; returns the lists they record into."""
    calls = {"lsjson": [], "delete": [], "copyto": [], "lsf": []}
    fake = isolated_webui.backups.rclone_util
    monkeypatch.setattr(fake, "lsjson", lambda root, recursive=False, include=(): calls["lsjson"].append((root, recursive)) or calls.get("entries", []))
    monkeypatch.setattr(fake, "delete_file", lambda path: calls["delete"].append(path))
    monkeypatch.setattr(fake, "copyto", lambda src, dst: calls["copyto"].append((src, dst)) or open(dst, "wb").write(b"backup bytes"))
    monkeypatch.setattr(fake, "lsf", lambda path: calls["lsf"].append(path) or list(calls.get("names", [])))
    return calls


def entry(path, size=10, mod="2026-01-01T00:00:00Z", is_dir=False):
    return {"Path": path, "Name": path.rsplit("/", 1)[-1], "Size": size, "ModTime": mod, "IsDir": is_dir}


# --- history list ----------------------------------------------------------

def test_history_groups_files_by_app_newest_first(authed_client, rclone):
    rclone["entries"] = [
        entry("radarr/radarr_1.zip", 5, "2026-01-01T00:00:00Z"),
        entry("radarr/radarr_3.zip", 7, "2026-01-03T00:00:00Z"),
        entry("sonarr/sonarr_2.zip", 9, "2026-01-02T00:00:00Z"),
        entry("radarr", is_dir=True),
        entry("stranger/file.zip"),
        entry("lonely.zip"),
    ]
    history = authed_client.get("/api/history/local").get_json()
    assert [f["name"] for f in history["radarr"]] == ["radarr_3.zip", "radarr_1.zip"]
    assert history["radarr"][0] == {"name": "radarr_3.zip", "size": 7, "mod_time": "2026-01-03T00:00:00Z",
                                    "app_version": None, "recorded": False, "run_id": None, "encrypted": False}
    assert [f["name"] for f in history["sonarr"]] == ["sonarr_2.zip"]
    assert "stranger" not in history and "lonely.zip" not in history
    assert set(history) == set(APP_NAMES)
    assert rclone["lsjson"][0][1] is True


def test_history_reports_a_destination_it_cant_list(authed_client, isolated_webui, monkeypatch):
    def expired(root, recursive=False, include=()):
        raise isolated_webui.backups.rclone_util.RcloneError("rclone lsjson failed", "invalid_grant (reconnect it)", 1)

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "lsjson", expired)
    response = authed_client.get("/api/history/local")
    assert response.status_code == 502 and response.get_json() == {"error": "invalid_grant (reconnect it)"}


def test_history_for_an_empty_destination_lists_every_app_with_nothing(authed_client, rclone):
    history = authed_client.get("/api/history/local").get_json()
    assert history and all(files == [] for files in history.values())


@pytest.mark.parametrize("path", [
    "/api/history/{dest}", "/api/history/{dest}/radarr/a.zip", "/api/restore/{dest}/radarr/backups",
])
def test_destination_problems_are_explained(authed_client, rclone, path):
    method = "delete" if path.count("/") == 5 and "history" in path else "get"
    assert getattr(authed_client, method)(path.format(dest="nope")).status_code == 404
    authed_client.post("/api/config", json={"destinations": {"local": {"enabled": False}}})
    disabled = getattr(authed_client, method)(path.format(dest="local"))
    assert disabled.status_code == 400 and "not enabled" in disabled.get_json()["error"]
    authed_client.post("/api/config", json={"destinations": {"gdrive": {"enabled": True, "client_id": "id"}}})
    unconnected = getattr(authed_client, method)(path.format(dest="gdrive"))
    assert unconnected.status_code == 400 and "not connected" in unconnected.get_json()["error"]
    assert rclone["lsjson"] == [] and rclone["delete"] == []


# --- delete / download -----------------------------------------------------

def test_delete_removes_exactly_that_file(authed_client, rclone, isolated_webui):
    response = authed_client.delete("/api/history/local/radarr/radarr_1.zip")
    assert response.get_json() == {"ok": True}
    assert rclone["delete"] == [os.path.join(isolated_webui.destinations.destination_util.DEFAULT_LOCAL_DIR, "radarr", "radarr_1.zip")]


@pytest.mark.parametrize("name", ["a b.zip", "a;b.zip", ".hidden", "a%0A.zip"])
def test_delete_and_download_refuse_unsafe_names(authed_client, rclone, name):
    assert authed_client.delete(f"/api/history/local/radarr/{name}").status_code == 400
    assert authed_client.get(f"/api/history/local/radarr/{name}/download").status_code == 400
    assert rclone["delete"] == [] and rclone["copyto"] == []


def test_unknown_apps_are_refused(authed_client, rclone):
    assert authed_client.delete("/api/history/local/nope/a.zip").status_code == 404
    assert authed_client.get("/api/history/local/nope/a.zip/download").status_code == 404


def test_delete_reports_an_rclone_failure(authed_client, isolated_webui, monkeypatch):
    def fail(path):
        raise isolated_webui.backups.rclone_util.RcloneError("permission denied")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "delete_file", fail)
    response = authed_client.delete("/api/history/local/radarr/a.zip")
    assert response.status_code == 500 and "permission denied" in response.get_json()["error"]


@pytest.fixture
def dropbox(isolated_webui, monkeypatch):
    """Dropbox connected and enabled, with rclone.conf left alone."""
    cfg = isolated_webui.backups.load_config()
    cfg["destinations"]["dropbox"].update(enabled=True, token="{}")
    isolated_webui.settings.save_config(cfg)
    monkeypatch.setattr(isolated_webui.backups.destination_util, "sync", lambda cfg: None)


def local_backup(isolated_webui, name, data=b"backup bytes"):
    folder = os.path.join(isolated_webui.backups.destination_util.DEFAULT_LOCAL_DIR, "radarr")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "wb") as f:
        f.write(data)
    return folder


def test_a_local_backup_is_sent_from_its_folder_without_a_copy(authed_client, rclone, isolated_webui):
    local_backup(isolated_webui, "radarr_1.zip")
    response = authed_client.get("/api/history/local/radarr/radarr_1.zip/download")
    assert response.status_code == 200 and response.data == b"backup bytes"
    assert "attachment" in response.headers["Content-Disposition"] and "radarr_1.zip" in response.headers["Content-Disposition"]
    assert rclone["copyto"] == []


def test_a_local_download_never_follows_a_link_out_of_the_folder(authed_client, rclone, isolated_webui, tmp_path):
    folder = local_backup(isolated_webui, "radarr_1.zip")
    (tmp_path / "secret.txt").write_text("not a backup")
    os.symlink(tmp_path / "secret.txt", os.path.join(folder, "radarr_2.zip"))
    for name in ("radarr_2.zip", "radarr_3.zip"):  # a link out, and a file that's gone
        response = authed_client.get(f"/api/history/local/radarr/{name}/download")
        assert response.status_code == 404 and "isn't on Local Storage" in response.get_json()["error"]


def test_download_streams_the_file_and_removes_its_temporary_copy(authed_client, rclone, dropbox):
    response = authed_client.get("/api/history/dropbox/radarr/radarr_1.zip/download")
    assert response.status_code == 200 and response.data == b"backup bytes"
    assert "attachment" in response.headers["Content-Disposition"] and "radarr_1.zip" in response.headers["Content-Disposition"]
    _src, temp_path = rclone["copyto"][0]
    assert not os.path.exists(os.path.dirname(temp_path))


def test_a_failed_download_cleans_up_and_explains(authed_client, isolated_webui, monkeypatch, dropbox):
    seen = []

    def fail(src, dst):
        seen.append(dst)
        raise isolated_webui.backups.rclone_util.RcloneError("not found")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "copyto", fail)
    response = authed_client.get("/api/history/dropbox/radarr/a.zip/download")
    assert response.status_code == 500 and "not found" in response.get_json()["error"]
    assert not os.path.exists(os.path.dirname(seen[0]))


# --- restore: listing and preview -------------------------------------------

def test_restore_listing_is_newest_first(authed_client, rclone):
    rclone["names"] = ["radarr_20261101_030000.zip", "radarr_20261102_030000.zip", "notes.txt"]
    assert authed_client.get("/api/restore/local/radarr/backups").get_json() == {
        "files": ["radarr_20261102_030000.zip", "radarr_20261101_030000.zip"]}


def test_restore_listing_reports_an_rclone_failure(authed_client, isolated_webui, monkeypatch):
    def fail(path):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "lsf", fail)
    response = authed_client.get("/api/restore/local/radarr/backups")
    assert response.status_code == 500 and "couldn't connect" in response.get_json()["error"]


def sab_config(*servers):
    return {"config": {"servers": list(servers)}}


def test_sabnzbd_preview_lists_the_servers_that_need_a_password(authed_client, isolated_webui, monkeypatch, tmp_path):
    work = tmp_path / "preview"
    work.mkdir()
    monkeypatch.setattr(restore_actions, "fetch_backup", lambda root, app, filename=None: (str(work), str(work / "x.zip"), "sabnzbd_1.zip"))
    monkeypatch.setattr(restore_actions, "load_sabnzbd_config", lambda tmp, zip_: sab_config(
        {"name": "news", "host": "news.example", "password": "*" * 10}, {"name": "free", "host": "free.example", "password": ""}))
    response = authed_client.post("/api/restore/local/sabnzbd/preview", json={"file": "sabnzbd_1.zip"})
    assert response.get_json() == {"file": "sabnzbd_1.zip", "servers": [
        {"name": "news", "host": "news.example", "needs_password": True},
        {"name": "free", "host": "free.example", "needs_password": False}]}
    assert not work.exists()


def test_sabnzbd_preview_failure_is_explained_and_cleaned_up(authed_client, monkeypatch, tmp_path):
    work = tmp_path / "preview"
    work.mkdir()
    monkeypatch.setattr(restore_actions, "fetch_backup", lambda root, app, filename=None: (str(work), str(work / "x.zip"), "x.zip"))

    def broken(tmp, zip_):
        raise FileNotFoundError("no sabnzbd_config.json")

    monkeypatch.setattr(restore_actions, "load_sabnzbd_config", broken)
    response = authed_client.post("/api/restore/local/sabnzbd/preview", json={})
    assert response.status_code == 500 and "sabnzbd_config.json" in response.get_json()["error"]
    assert not work.exists()


# --- restore: guards --------------------------------------------------------

def configure(client, **apps):
    body = {"bazarr_backup_dir": "/mnt/bazarr", "apps": {}}
    defaults = {name: {"enabled": True, "url": f"http://{name}:1", "api_key": f"{name}-key"} for name in
                ("radarr", "sonarr", "lidarr", "whisparr", "prowlarr", "bazarr", "tdarr", "tautulli", "sabnzbd", "profilarr",
                 "pihole")}
    defaults["jellyfin"] = {"enabled": True, "url": "http://jellyfin:1", "api_key": "jellyfin-key", "backup_dir": "/mnt/jellyfin"}
    defaults["emby"] = {"enabled": True, "url": "http://emby:1", "api_key": "emby-key", "data_dir": "/mnt/emby"}
    defaults["plex"] = {"enabled": True, "url": "http://plex:1", "api_key": "plex-key", "data_dir": "/mnt/plex"}
    defaults["nzbget"] = {"enabled": True, "url": "http://nzbget:1", "username": "nzbget", "password": "nzbget-pass"}
    body["apps"].update({name: {**defaults[name], **extra} for name, extra in {**{n: {} for n in defaults}, **apps}.items()})
    assert client.post("/api/config", json=body).status_code == 200


@pytest.fixture
def restore(inline, authed_client, monkeypatch, tmp_path):
    """Stub fetch/restore; returns a recorder with the calls and the temp dir used."""
    recorder = {"calls": [], "tmp": tmp_path / "work", "results": {}, "fail": None, "fetch_fail": None}

    def fetch(root, app, filename=None):
        if recorder["fetch_fail"]:
            raise recorder["fetch_fail"]
        recorder["tmp"].mkdir(exist_ok=True)
        recorder["calls"].append(("fetch", root, app, filename))
        return str(recorder["tmp"]), str(recorder["tmp"] / "backup.zip"), filename or f"{app}_latest.zip"

    def restore_app(app_name, app_cfg, tmp_dir, local_zip, **kwargs):
        recorder["calls"].append(("restore", app_name, dict(app_cfg), kwargs))
        if recorder["fail"]:
            raise recorder["fail"]
        if kwargs.get("sabnzbd_password_prompt"):
            recorder["prompts"] = {n: kwargs["sabnzbd_password_prompt"](n, {}) for n in ("news", "free", "unlisted")}
        return recorder["results"].get(app_name, {})

    def list_backups(root, app):
        if recorder["fetch_fail"]:
            raise recorder["fetch_fail"]
        return [f"{app}_latest.zip"]

    monkeypatch.setattr(restore_actions, "fetch_backup", fetch)
    monkeypatch.setattr(restore_actions, "list_backups", list_backups)
    monkeypatch.setattr(restore_actions, "restore_app", restore_app)
    configure(authed_client)
    return recorder


def start(client, app="radarr", **body):
    return client.post(f"/api/restore/local/{app}", json={"confirm": True, **body})


def result(client):
    return client.get("/api/restore/status").get_json()


def test_restore_status_starts_idle(authed_client):
    state = result(authed_client)
    assert state["running"] is False and state["ok"] is False and state["error"] is None and state["app"] is None


@pytest.mark.parametrize("app, code, fragment", [
    ("nope", 404, "unknown app"),
    ("profilarr", 400, "does not support automated restore"),
])
def test_restore_refuses_unknown_and_unsupported_apps(restore, authed_client, app, code, fragment):
    response = start(authed_client, app)
    assert response.status_code == code and fragment in response.get_json()["error"]


def test_restore_needs_explicit_confirmation(restore, authed_client):
    for body in ({}, {"confirm": False}, {"confirm": ""}):
        assert authed_client.post("/api/restore/local/radarr", json=body).status_code == 400
    assert restore["calls"] == []


def test_restore_needs_an_enabled_destination_and_a_configured_app(restore, authed_client):
    assert start(authed_client, "radarr").status_code == 200
    assert authed_client.post("/api/restore/nope/radarr", json={"confirm": True}).status_code == 404
    authed_client.post("/api/config", json={"apps": {"radarr": {"url": ""}}})
    unconfigured = start(authed_client, "radarr")
    assert unconfigured.status_code == 400 and "not configured" in unconfigured.get_json()["error"]
    authed_client.post("/api/config", json={"apps": {"sonarr": {"api_key": ""}}})
    assert start(authed_client, "sonarr").status_code == 400


def test_bazarr_restore_needs_its_backup_folder(restore, authed_client):
    authed_client.post("/api/config", json={"bazarr_backup_dir": ""})
    response = start(authed_client, "bazarr")
    assert response.status_code == 400 and "bazarr_backup_dir" in response.get_json()["error"]
    assert start(authed_client, "bazarr", bazarr_backup_dir="/other/folder").status_code == 200
    assert restore["calls"][-1][3]["bazarr_backup_dir"] == "/other/folder"


def test_only_one_restore_runs_at_a_time(restore, authed_client, inline):
    inline.runs.RESTORE_RUN_STATE["running"] = True
    response = start(authed_client)
    assert response.status_code == 409 and restore["calls"] == []


# --- restore: running ------------------------------------------------------

@pytest.mark.parametrize("app, message", [
    ("radarr", "Radarr restore uploaded, app is restarting"),
    ("sonarr", "Sonarr restore uploaded, app is restarting"),
    ("lidarr", "Lidarr restore uploaded, app is restarting"),
    ("whisparr", "Whisparr restore uploaded, app is restarting"),
    ("prowlarr", "Prowlarr restore uploaded, app is restarting"),
    ("bazarr", "Bazarr restore triggered, app is restarting"),
    ("tdarr", "Tdarr restore complete"),
])
def test_each_app_restores_and_reports_its_own_message(restore, authed_client, app, message):
    assert start(authed_client, app).get_json() == {"started": True}
    state = result(authed_client)
    assert state["ok"] is True and state["error"] is None and state["message"] == message
    assert state["app"] == app and state["file"] == f"{app}_latest.zip" and state["running"] is False
    assert state["started_at"] and state["finished_at"]
    assert not restore["tmp"].exists(), "the downloaded backup must be removed afterwards"


@pytest.mark.parametrize("outcome, warning, fragment", [
    ({"back": True, "password_ok": True, "sudo_ok": True, "attention": False}, False, "Pi-hole restarted to apply it"),
    ({"back": True, "password_ok": False, "sudo_ok": None, "attention": True}, True, "the app password in Settings no longer works"),
])
def test_a_restore_that_needs_attention_finishes_as_a_warning(restore, authed_client, outcome, warning, fragment):
    restore["results"]["pihole"] = outcome
    start(authed_client, "pihole")
    state = result(authed_client)
    assert state["ok"] is True and state["warning"] is warning and fragment in state["message"]
    start(authed_client, "radarr")
    assert result(authed_client)["warning"] is False  # reset for the next run


def test_a_named_file_is_restored_instead_of_the_newest(restore, authed_client):
    start(authed_client, "radarr", file="radarr_20260101_000000.zip")
    assert restore["calls"][0][3] == "radarr_20260101_000000.zip" and result(authed_client)["file"] == "radarr_20260101_000000.zip"


def test_jellyfin_restore_uses_its_backups_folder_and_says_it_restarts(restore, authed_client):
    start(authed_client, "jellyfin")
    assert restore["calls"][-1][2]["backup_dir"] == "/mnt/jellyfin"
    assert result(authed_client)["message"] == "Jellyfin restore started, Jellyfin is restarting to apply it"


def test_jellyfin_restore_needs_its_backups_folder(restore, authed_client):
    authed_client.post("/api/config", json={"apps": {"jellyfin": {"enabled": False, "backup_dir": ""}}})
    refused = start(authed_client, "jellyfin")
    assert refused.status_code == 400 and refused.get_json()["error"] == "jellyfin: Backups folder is not configured"
    assert start(authed_client, "jellyfin", override={"backup_dir": "/mnt/other"}).status_code == 200
    assert restore["calls"][-1][2]["backup_dir"] == "/mnt/other"


def test_nzbget_restore_uses_its_credentials_and_says_it_reloads(restore, authed_client):
    restore["results"]["nzbget"] = {"kind": "nzbget", "reloaded": True}
    start(authed_client, "nzbget")
    assert restore["calls"][-1][2]["username"] == "nzbget" and restore["calls"][-1][2]["password"] == "nzbget-pass"
    assert result(authed_client)["message"] == "NZBGet restore complete, NZBGet is reloading to apply it"


def test_nzbget_restore_says_to_restart_when_the_reload_isnt_confirmed(restore, authed_client):
    restore["results"]["nzbget"] = {"kind": "nzbget", "reloaded": False}
    start(authed_client, "nzbget")
    outcome = result(authed_client)
    assert outcome["ok"] is True and "restart NZBGet to apply them" in outcome["message"]


def test_emby_restore_uses_its_data_folder_and_says_to_start_emby(restore, authed_client):
    start(authed_client, "emby")
    assert restore["calls"][-1][2]["data_dir"] == "/mnt/emby"
    assert result(authed_client)["message"] == "Emby restore complete, start Emby again to load it"


def test_plex_restore_uses_its_data_folder_and_says_to_start_plex(restore, authed_client):
    start(authed_client, "plex")
    assert restore["calls"][-1][2]["data_dir"] == "/mnt/plex"
    assert result(authed_client)["message"] == "Plex restore complete, start Plex again to load it"


def test_plex_restore_needs_its_data_folder(restore, authed_client):
    authed_client.post("/api/config", json={"apps": {"plex": {"enabled": False, "data_dir": ""}}})
    refused = start(authed_client, "plex")
    assert refused.status_code == 400 and refused.get_json()["error"] == "plex: Data folder is not configured"
    assert start(authed_client, "plex", override={"data_dir": "/mnt/other"}).status_code == 200
    assert restore["calls"][-1][2]["data_dir"] == "/mnt/other"


def test_emby_restore_needs_its_data_folder(restore, authed_client):
    authed_client.post("/api/config", json={"apps": {"emby": {"enabled": False, "data_dir": ""}}})
    refused = start(authed_client, "emby")
    assert refused.status_code == 400 and refused.get_json()["error"] == "emby: Data folder is not configured"
    assert start(authed_client, "emby", override={"data_dir": "/mnt/other"}).status_code == 200
    assert restore["calls"][-1][2]["data_dir"] == "/mnt/other"


def test_tautulli_and_sabnzbd_restores_return_their_summary(restore, authed_client):
    restore["results"] = {"tautulli": {"summary": {"config": "started"}}, "sabnzbd": {"summary": {"servers_restored": ["news"]}}}
    start(authed_client, "tautulli")
    assert result(authed_client)["summary"] == {"config": "started"} and result(authed_client)["message"] == "Tautulli restore uploaded"
    start(authed_client, "sabnzbd", passwords={"news": "pw"})
    assert result(authed_client)["summary"] == {"servers_restored": ["news"]} and result(authed_client)["message"] == "SABnzbd restore complete"


def test_a_staged_tautulli_config_is_reported_as_needing_a_step(restore, authed_client):
    staged = "Tautulli has a login set up, so Backuparr can't start the import. The config is staged. To apply it, log in to Tautulli if asked, then open http://tautulli:1/restart_import_config"
    restore["results"] = {"tautulli": {"summary": {"config": "staged", "config_staged": staged}}}
    start(authed_client, "tautulli")
    state = result(authed_client)
    assert state["ok"] is True and state["summary"]["config_staged"] == staged
    assert state["message"] == "Tautulli config staged, finish it in Tautulli"


def test_sabnzbd_passwords_come_from_the_request_and_blank_means_skip(restore, authed_client):
    start(authed_client, "sabnzbd", passwords={"news": "secret", "free": ""})
    assert restore["prompts"] == {"news": "secret", "free": None, "unlisted": None}
    start(authed_client, "sabnzbd")
    assert restore["prompts"] == {"news": None, "free": None, "unlisted": None}


def test_a_failed_restore_is_reported_and_cleaned_up(restore, authed_client):
    restore["fail"] = requests.exceptions.ConnectionError("http://radarr:1/api?apikey=secret refused")
    assert start(authed_client).status_code == 200
    state = result(authed_client)
    assert state["ok"] is False and "couldn't connect" in state["error"] and "secret" not in state["error"]
    assert state["running"] is False and state["finished_at"] and not restore["tmp"].exists()


def test_a_restore_that_cannot_resolve_the_host_logs_one_clean_line(restore, authed_client, dns_down, caplog):
    with pytest.raises(requests.exceptions.ConnectionError) as caught:
        requests.get("http://radarr:1/api/v3/system/status?apikey=secret")
    restore["fail"] = caught.value
    with caplog.at_level(logging.INFO):
        assert start(authed_client).status_code == 200
    state = result(authed_client)
    assert state["ok"] is False and state["error"].startswith("couldn't resolve hostname 'radarr'")
    failures = [r for r in caplog.records if "restore failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].exc_info is None
    assert "couldn't resolve hostname 'radarr'" in failures[0].getMessage()
    assert "secret" not in caplog.text and "Traceback" not in caplog.text


def test_a_restore_rejected_by_the_app_does_not_repeat_the_app_name_in_the_log(restore, authed_client, caplog):
    restore["fail"] = ServarrError("radarr: unauthorized - check the API key")
    with caplog.at_level(logging.INFO):
        assert start(authed_client).status_code == 200
    assert result(authed_client)["error"] == "Radarr: unauthorized - check the API key"
    (line,) = [r for r in caplog.records if "restore failed" in r.getMessage()]
    assert line.getMessage() == "restore failed for radarr - unauthorized - check the API key"


def test_a_missing_backup_is_reported(restore, authed_client):
    restore["fetch_fail"] = FileNotFoundError("No backups found at /x/radarr/")
    start(authed_client)
    state = result(authed_client)
    assert state["ok"] is False and "No backups found" in state["error"]


def test_the_next_restore_forgets_the_previous_result(restore, authed_client):
    restore["fail"] = RuntimeError("first failed")
    start(authed_client, "radarr")
    restore["fail"] = None
    start(authed_client, "sonarr")
    state = result(authed_client)
    assert state["ok"] is True and state["error"] is None and state["app"] == "sonarr"


# --- restore: one-off target override --------------------------------------

def test_override_redirects_one_restore_without_touching_settings(restore, authed_client, isolated_webui):
    before = authed_client.get("/api/config").get_json()
    start(authed_client, "radarr", override={"url": "http://throwaway:7878", "api_key": "temp-key", "enabled": False, "username": "x"})
    _, _, app_cfg, _ = restore["calls"][-1]
    assert app_cfg["url"] == "http://throwaway:7878" and app_cfg["api_key"] == "temp-key"
    assert app_cfg["enabled"] is True and app_cfg["username"] == ""  # only url/api_key (plus an app's own extras) may be overridden
    assert authed_client.get("/api/config").get_json() == before


def test_an_override_url_never_gets_the_saved_web_login(restore, authed_client):
    authed_client.post("/api/config", json={"apps": {"radarr": {"enabled": True, "url": "http://radarr:7878", "api_key": "k",
                                                                  "username": "admin", "password": "web-pass"}}})
    start(authed_client, "radarr", override={"url": "http://other:7878", "username": "x", "password": "y"})
    _, _, app_cfg, _ = restore["calls"][-1]
    assert (app_cfg["url"], app_cfg["username"], app_cfg["password"]) == ("http://other:7878", "", "")
    start(authed_client, "radarr")
    _, _, app_cfg, _ = restore["calls"][-1]
    assert (app_cfg["username"], app_cfg["password"]) == ("admin", "web-pass")


def test_override_may_set_an_apps_own_extra_fields(restore, authed_client):
    start(authed_client, "bazarr", override={"username": "u", "password": "p", "enabled": False})
    _, _, app_cfg, _ = restore["calls"][-1]
    assert (app_cfg["username"], app_cfg["password"], app_cfg["enabled"]) == ("u", "p", True)


def test_override_can_supply_a_target_for_an_unconfigured_app(restore, authed_client):
    authed_client.post("/api/config", json={"apps": {"radarr": {"enabled": False, "url": "", "api_key": ""}}})
    assert start(authed_client, "radarr").status_code == 400
    assert start(authed_client, "radarr", override={"url": "http://new:7878", "api_key": "k"}).status_code == 200


def test_a_restore_waits_for_a_running_backup(restore, authed_client, isolated_webui, monkeypatch):
    monkeypatch.setitem(isolated_webui.runs.RUN_STATE, "running", True)
    response = start(authed_client)
    assert response.status_code == 409 and "a backup is running" in response.get_json()["error"]
    assert not [c for c in restore["calls"] if c[0] == "restore"]


# --- several backups at once (#148) -------------------------------------------

def test_bulk_delete_removes_the_picked_backups_and_failed_runs(authed_client, rclone, isolated_webui):
    runlog = isolated_webui.backups.runlog
    runlog.save({"id": "radarr_20260101_030000", "app": "radarr", "file": None, "ok": False, "error": "refused",
                 "destinations": {"local": None, "dropbox": None}, "lines": []})
    response = authed_client.post("/api/history/local/delete", json={
        "backups": [{"app": "radarr", "file": "radarr_1.zip"}, {"app": "sonarr", "file": "sonarr_2.zip.enc"}],
        "runs": [{"app": "radarr", "run_id": "radarr_20260101_030000"}]})
    body = response.get_json()
    assert response.status_code == 200 and body["failed"] == []
    assert len(body["deleted"]) == 3
    assert [p.rsplit("/", 2)[-2:] for p in rclone["delete"]] == [["radarr", "radarr_1.zip"], ["sonarr", "sonarr_2.zip.enc"]]
    # The failed run is gone from this destination's History, still listed on the other one.
    assert runlog.get("radarr", "radarr_20260101_030000")["destinations"] == {"dropbox": None}


def test_bulk_delete_reports_what_it_couldnt_delete(authed_client, isolated_webui, monkeypatch):
    def refuse(path):
        raise isolated_webui.backups.rclone_util.RcloneError("delete failed", "permission denied")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "delete_file", refuse)
    body = authed_client.post("/api/history/local/delete", json={
        "backups": [{"app": "radarr", "file": "radarr_1.zip"}], "runs": [{"app": "radarr", "run_id": "radarr_9"}]}).get_json()
    assert body["deleted"] == []
    assert [f.get("file") or f.get("run_id") for f in body["failed"]] == ["radarr_1.zip", "radarr_9"]
    assert "permission denied" in body["failed"][0]["error"]


def test_bulk_delete_counts_a_backup_already_gone_as_deleted(authed_client, rclone, monkeypatch, isolated_webui):
    def gone(path):
        raise isolated_webui.backups.rclone_util.RcloneError(
            "rclone deletefile ...", "Failed to deletefile: radarr_1.zip is a directory or doesn't exist: object not found")
    monkeypatch.setattr(isolated_webui.backups.rclone_util, "delete_file", gone)
    body = authed_client.post("/api/history/local/delete", json={"backups": [{"app": "radarr", "file": "radarr_1.zip"}]}).get_json()
    assert body == {"deleted": [{"app": "radarr", "file": "radarr_1.zip"}], "failed": []}


@pytest.mark.parametrize("body", [
    {"backups": [{"app": "radarr", "file": "../x.zip"}]},
    {"backups": [{"app": "stranger", "file": "a.zip"}]},
    {"backups": "radarr/a.zip"},
    {"runs": [{"app": "radarr", "run_id": "a/b"}]},
    {},
])
def test_bulk_delete_refuses_anything_that_isnt_a_listed_backup(authed_client, rclone, body):
    assert authed_client.post("/api/history/local/delete", json=body).status_code == 400
    assert rclone["delete"] == []


def bulk_download(client, dest, *items):
    """Starts a bulk download job and waits until it's ready or failed:
    (status, job url)."""
    import time
    backups = [dict(zip(("app", "file"), item.split("/"))) for item in items]
    response = client.post(f"/api/history/{dest}/download", json={"backups": backups})
    assert response.status_code == 202, response.get_json()
    job = f"/api/history/{dest}/download/{response.get_json()['job_id']}"
    for _ in range(200):
        status = client.get(job).get_json()
        if status["state"] != "preparing":
            return status, job
        time.sleep(0.02)
    raise AssertionError("the download never finished")


def test_a_bulk_download_is_one_zip_of_the_backups_as_single_downloads_give_them(authed_client, isolated_webui):
    import io
    import zipfile as zf
    import archive_crypto
    local_backup(isolated_webui, "radarr_1.zip", b"PK radarr")
    local_backup(isolated_webui, "radarr_2.zip.enc", archive_crypto.encrypt_bytes(b"PK secret"))
    own = os.path.join(isolated_webui.backups.destination_util.DEFAULT_LOCAL_DIR, "backuparr")
    os.makedirs(own, exist_ok=True)
    sealed = archive_crypto.encrypt_bytes(b"PK self")
    with open(os.path.join(own, "backuparr_1.enc"), "wb") as f:
        f.write(sealed)
    status, job = bulk_download(authed_client, "local", "radarr/radarr_1.zip", "radarr/radarr_2.zip.enc",
                                "backuparr/backuparr_1.enc")
    assert status == {"state": "ready", "done": 3, "total": 3}
    response = authed_client.get(f"{job}/file")
    assert response.status_code == 200 and "backuparr-local-" in response.headers["Content-Disposition"]
    with zf.ZipFile(io.BytesIO(response.data)) as z:
        assert sorted(z.namelist()) == ["backuparr/backuparr_1.enc", "radarr/radarr_1.zip", "radarr/radarr_2.zip"]
        assert z.read("radarr/radarr_2.zip") == b"PK secret" and z.read("backuparr/backuparr_1.enc") == sealed
    response.close()
    # Fetched once: then it's gone from the server.
    assert authed_client.get(f"{job}/file").status_code == 404
    assert authed_client.get(job).status_code == 404


def test_a_bulk_download_names_a_backup_that_isnt_there(authed_client, isolated_webui):
    local_backup(isolated_webui, "radarr_1.zip")
    status, job = bulk_download(authed_client, "local", "radarr/radarr_1.zip", "radarr/radarr_9.zip")
    assert status["state"] == "failed" and "radarr_9.zip" in status["message"]
    assert authed_client.get(f"{job}/file").status_code == 409


def test_a_bulk_download_from_the_cloud_leaves_no_copies(authed_client, rclone, dropbox):
    status, job = bulk_download(authed_client, "dropbox", "radarr/radarr_1.zip", "sonarr/sonarr_1.zip")
    assert status["state"] == "ready"
    tmp_dir = os.path.dirname(rclone["copyto"][0][1])
    assert os.path.isdir(tmp_dir)
    response = authed_client.get(f"{job}/file")
    assert response.status_code == 200
    response.close()
    assert not os.path.exists(tmp_dir)


def test_a_bulk_download_needs_backups_picked(authed_client, rclone):
    assert authed_client.post("/api/history/local/download", json={}).status_code == 400
    assert authed_client.post("/api/history/local/download", json={"backups": [{"app": "stranger", "file": "a.zip"}]}).status_code == 400


def test_a_bulk_download_is_only_for_the_browser_that_started_it(authed_client, isolated_webui):
    local_backup(isolated_webui, "radarr_1.zip")
    _, job = bulk_download(authed_client, "local", "radarr/radarr_1.zip")
    other = isolated_webui.app.test_client()
    assert other.post("/api/login", json={"username": "admin", "password": PASSWORD}).status_code == 200
    assert other.get(job).status_code == 404 and other.get(f"{job}/file").status_code == 404
    # Nor under another destination's address.
    assert authed_client.get(job.replace("/local/", "/dropbox/")).status_code == 404


def test_an_unfetched_bulk_download_is_removed(authed_client, isolated_webui, monkeypatch):
    local_backup(isolated_webui, "radarr_1.zip")
    status, job = bulk_download(authed_client, "local", "radarr/radarr_1.zip")
    jobs = isolated_webui.backups.DOWNLOAD_JOBS
    (_, prepared), = jobs.jobs.values()
    assert os.path.isdir(prepared["tmp_dir"])
    prepared["finished"] -= isolated_webui.backups.DOWNLOAD_TTL + 1
    isolated_webui.backups._expire_downloads()
    assert not os.path.exists(prepared["tmp_dir"]) and not jobs.jobs
    assert authed_client.get(job).status_code == 404


def test_only_a_few_bulk_downloads_are_prepared_at_once(authed_client, isolated_webui):
    jobs = isolated_webui.backups.DOWNLOAD_JOBS
    for _ in range(isolated_webui.backups.MAX_DOWNLOADS_PREPARING):
        jobs.add("someone", {"state": "preparing", "tmp_dir": "/nonexistent", "dest": "local"})
    response = authed_client.post("/api/history/local/download", json={"backups": [{"app": "radarr", "file": "radarr_1.zip"}]})
    assert response.status_code == 429
    jobs.jobs.clear()


def test_a_bulk_download_counts_up_as_it_prepares(authed_client, isolated_webui, monkeypatch):
    import threading
    import time
    local_backup(isolated_webui, "radarr_1.zip")
    local_backup(isolated_webui, "radarr_2.zip")
    gate = threading.Event()
    real = isolated_webui.backups._plain_copy

    def held(dest_id, root, app_name, filename, tmp_dir):
        if filename == "radarr_2.zip":
            gate.wait(5)
        return real(dest_id, root, app_name, filename, tmp_dir)
    monkeypatch.setattr(isolated_webui.backups, "_plain_copy", held)
    response = authed_client.post("/api/history/local/download", json={"backups": [
        {"app": "radarr", "file": "radarr_1.zip"}, {"app": "radarr", "file": "radarr_2.zip"}]})
    job = f"/api/history/local/download/{response.get_json()['job_id']}"
    for _ in range(200):
        status = authed_client.get(job).get_json()
        if status["done"] == 1:
            break
        time.sleep(0.01)
    assert status == {"state": "preparing", "done": 1, "total": 2}
    gate.set()
    for _ in range(200):
        if authed_client.get(job).get_json()["state"] == "ready":
            break
        time.sleep(0.01)
    assert authed_client.get(job).get_json() == {"state": "ready", "done": 2, "total": 2}


# --- apps restored by hand (#151) ---------------------------------------------

def test_every_manual_restore_has_steps_and_a_docs_page():
    from config_store import APP_META, restore_supported
    from restore_actions import RESTORES
    manual = [m for m in APP_META if m.get("manual_restore")]
    assert [m["id"] for m in manual] == ["profilarr", "opnsense", "pfsense", "unifi", "bitwarden", "unraid"]
    for meta in manual:
        info = meta["manual_restore"]
        assert info["docs"].startswith("https://rsaturns.github.io/backuparr/apps/")
        assert info["steps"] and all(group["title"] and group["steps"] for group in info["steps"])
        assert all(isinstance(step, str) and step.endswith((".", '."')) for group in info["steps"] for step in group["steps"])
        # Backuparr itself never restores it.
        assert not restore_supported(meta["id"]) and meta["id"] not in RESTORES


def test_a_manual_restore_apps_backup_downloads_decrypted(authed_client, rclone, isolated_webui):
    import archive_crypto
    folder = os.path.join(isolated_webui.backups.destination_util.DEFAULT_LOCAL_DIR, "profilarr")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "profilarr_20261111_030000.tar.gz.enc"), "wb") as f:
        f.write(archive_crypto.encrypt_bytes(b"profilarr tar"))
    response = authed_client.get("/api/history/local/profilarr/profilarr_20261111_030000.tar.gz.enc/download")
    assert response.status_code == 200 and response.data == b"profilarr tar"
    assert "profilarr_20261111_030000.tar.gz" in response.headers["Content-Disposition"]
    assert ".enc" not in response.headers["Content-Disposition"]


def test_the_restore_tab_has_the_manual_panel_and_the_steps_reach_the_page(authed_client):
    from test_webui_settings import ui_meta
    page = authed_client.get("/").get_data(as_text=True)
    assert 'id="r-manual"' in page and 'id="r-auto"' in page
    profilarr = next(m for m in ui_meta(authed_client)["apps"] if m["id"] == "profilarr")
    assert profilarr["manual_restore"]["steps"][0]["title"] == "Restore in Profilarr"
