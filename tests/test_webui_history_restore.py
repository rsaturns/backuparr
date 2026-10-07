"""History (list, download, delete) and the restore pipeline, with rclone and
the app drivers stubbed out - CI has no rclone binary."""
import logging
import os

import pytest
import requests

import restore_actions
from apps.servarr import ServarrError
from config_store import APP_NAMES


@pytest.fixture
def rclone(isolated_webui, monkeypatch):
    """Fake rclone_util calls; returns the lists they record into."""
    calls = {"lsjson": [], "delete": [], "copyto": [], "lsf": []}
    fake = isolated_webui.rclone_util
    monkeypatch.setattr(fake, "lsjson", lambda root, recursive=False: calls["lsjson"].append((root, recursive)) or calls.get("entries", []))
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
    assert history["radarr"][0] == {"name": "radarr_3.zip", "size": 7, "mod_time": "2026-01-03T00:00:00Z"}
    assert [f["name"] for f in history["sonarr"]] == ["sonarr_2.zip"]
    assert "stranger" not in history and "lonely.zip" not in history
    assert "seerr" not in history and set(history) == {a for a in APP_NAMES if a != "seerr"}
    assert rclone["lsjson"][0][1] is True


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
    assert rclone["delete"] == [os.path.join(isolated_webui.destination_util.DEFAULT_LOCAL_DIR, "radarr", "radarr_1.zip")]


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
        raise isolated_webui.rclone_util.RcloneError("permission denied")

    monkeypatch.setattr(isolated_webui.rclone_util, "delete_file", fail)
    response = authed_client.delete("/api/history/local/radarr/a.zip")
    assert response.status_code == 500 and "permission denied" in response.get_json()["error"]


def test_download_streams_the_file_and_removes_its_temporary_copy(authed_client, rclone):
    response = authed_client.get("/api/history/local/radarr/radarr_1.zip/download")
    assert response.status_code == 200 and response.data == b"backup bytes"
    assert "attachment" in response.headers["Content-Disposition"] and "radarr_1.zip" in response.headers["Content-Disposition"]
    _src, temp_path = rclone["copyto"][0]
    assert not os.path.exists(os.path.dirname(temp_path))


def test_a_failed_download_cleans_up_and_explains(authed_client, isolated_webui, monkeypatch):
    seen = []

    def fail(src, dst):
        seen.append(dst)
        raise isolated_webui.rclone_util.RcloneError("not found")

    monkeypatch.setattr(isolated_webui.rclone_util, "copyto", fail)
    response = authed_client.get("/api/history/local/radarr/a.zip/download")
    assert response.status_code == 500 and "not found" in response.get_json()["error"]
    assert not os.path.exists(os.path.dirname(seen[0]))


# --- restore: listing and preview -------------------------------------------

def test_restore_listing_is_newest_first(authed_client, rclone):
    rclone["names"] = ["radarr_1.zip", "radarr_2.zip"]
    assert authed_client.get("/api/restore/local/radarr/backups").get_json() == {"files": ["radarr_2.zip", "radarr_1.zip"]}


def test_restore_listing_reports_an_rclone_failure(authed_client, isolated_webui, monkeypatch):
    def fail(path):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(isolated_webui.rclone_util, "lsf", fail)
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
                ("radarr", "sonarr", "prowlarr", "bazarr", "tdarr", "tautulli", "sabnzbd", "profilarr")}
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

    monkeypatch.setattr(restore_actions, "fetch_backup", fetch)
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
    inline.RESTORE_RUN_STATE["running"] = True
    response = start(authed_client)
    assert response.status_code == 409 and restore["calls"] == []


# --- restore: running ------------------------------------------------------

@pytest.mark.parametrize("app, message", [
    ("radarr", "radarr restore uploaded, app is restarting"),
    ("sonarr", "sonarr restore uploaded, app is restarting"),
    ("prowlarr", "prowlarr restore uploaded, app is restarting"),
    ("bazarr", "bazarr restore triggered, app is restarting"),
    ("tdarr", "tdarr restore complete"),
])
def test_each_app_restores_and_reports_its_own_message(restore, authed_client, app, message):
    assert start(authed_client, app).get_json() == {"started": True}
    state = result(authed_client)
    assert state["ok"] is True and state["error"] is None and state["message"] == message
    assert state["app"] == app and state["file"] == f"{app}_latest.zip" and state["running"] is False
    assert state["started_at"] and state["finished_at"]
    assert not restore["tmp"].exists(), "the downloaded backup must be removed afterwards"


def test_a_named_file_is_restored_instead_of_the_newest(restore, authed_client):
    start(authed_client, "radarr", file="radarr_20260101_000000.zip")
    assert restore["calls"][0][3] == "radarr_20260101_000000.zip" and result(authed_client)["file"] == "radarr_20260101_000000.zip"


def test_tautulli_and_sabnzbd_restores_return_their_summary(restore, authed_client):
    restore["results"] = {"tautulli": {"summary": {"config": "started"}}, "sabnzbd": {"summary": {"servers_restored": ["news"]}}}
    start(authed_client, "tautulli")
    assert result(authed_client)["summary"] == {"config": "started"} and result(authed_client)["message"] == "tautulli restore uploaded"
    start(authed_client, "sabnzbd", passwords={"news": "pw"})
    assert result(authed_client)["summary"] == {"servers_restored": ["news"]} and result(authed_client)["message"] == "sabnzbd restore complete"


def test_a_staged_tautulli_config_is_reported_as_needing_a_step(restore, authed_client):
    staged = "Tautulli has a login set up, so Backuparr can't start the import. The config is staged. To apply it, log in to Tautulli if asked, then open http://tautulli:1/restart_import_config"
    restore["results"] = {"tautulli": {"summary": {"config": "staged", "config_staged": staged}}}
    start(authed_client, "tautulli")
    state = result(authed_client)
    assert state["ok"] is True and state["summary"]["config_staged"] == staged
    assert state["message"] == "tautulli config staged, finish it in Tautulli"


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
    assert result(authed_client)["error"] == "radarr: unauthorized - check the API key"
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


def test_override_may_set_an_apps_own_extra_fields(restore, authed_client):
    start(authed_client, "bazarr", override={"username": "u", "password": "p", "enabled": False})
    _, _, app_cfg, _ = restore["calls"][-1]
    assert (app_cfg["username"], app_cfg["password"], app_cfg["enabled"]) == ("u", "p", True)


def test_override_can_supply_a_target_for_an_unconfigured_app(restore, authed_client):
    authed_client.post("/api/config", json={"apps": {"radarr": {"enabled": False, "url": "", "api_key": ""}}})
    assert start(authed_client, "radarr").status_code == 400
    assert start(authed_client, "radarr", override={"url": "http://new:7878", "api_key": "k"}).status_code == 200
