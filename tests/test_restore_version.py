"""Restores only go onto the app version a backup came from (#108): the
check, the Restore tab's check endpoint, and the gate and checksum
verification in the restore itself."""
import hashlib
import logging

import pytest
import requests

import manifest
import restore_actions
import restore_version as rv

pytestmark = pytest.mark.version_check


class FakeApp:
    def __init__(self, version):
        self.version = version

    def app_version(self):
        if isinstance(self.version, Exception):
            raise self.version
        return self.version


@pytest.fixture
def installed(monkeypatch):
    """Sets what each app reports as its installed version."""
    versions = {}
    monkeypatch.setattr(rv, "build_app", lambda app, app_cfg: FakeApp(versions.get(app, requests.exceptions.ConnectionError("down"))))
    return versions


DOWNLOAD = b"backup"  # what the fake download below writes


def record(app, file, version, sha256=hashlib.sha256(DOWNLOAD).hexdigest()):
    manifest.record(app, file, ["local"], app_version=version, sha256=sha256, size=1, backuparr_version="2.5.0")


# ---------------------------------------------------------------- check ----

def test_matching_versions_pass(installed):
    installed["radarr"] = "6.4.4.10685"
    record("radarr", "radarr_1.zip", "6.4.4.10685")
    result = rv.check("radarr", "radarr_1.zip", {"url": "http://radarr:7878"})
    assert result == {"status": "match", "backup_version": "6.4.4.10685", "installed_version": "6.4.4.10685",
                      "message": "This backup is from Radarr 6.4.4.10685, the installed version.", "metadata": None}


def test_the_check_says_whether_a_media_servers_backup_holds_metadata(installed):
    installed["emby"] = "4.10.2.0"
    manifest.record("emby", "emby_1.zip", ["local"], app_version="4.10.2.0", sha256="a", size=1,
                    backuparr_version="3.0.0", metadata=True)
    manifest.record("emby", "emby_2.zip", ["local"], app_version="4.10.2.0", sha256="b", size=1,
                    backuparr_version="3.0.0", metadata=False)
    assert rv.check("emby", "emby_1.zip", {})["metadata"] is True
    assert rv.check("emby", "emby_2.zip", {})["metadata"] is False
    assert rv.check("emby", "emby_3.zip", {})["metadata"] is None  # not read yet
    manifest.record("emby", "emby_4.zip", ["local"], app_version="4.10.2.0", sha256="c", size=1,
                    backuparr_version="3.0.0-beta")  # recorded before the switch existed
    assert rv.check("emby", "emby_4.zip", {})["metadata"] is False


def test_versions_are_compared_normalised(installed):
    installed["tautulli"] = "v2.18.2"
    installed["jellyfin"] = "12.4.0"
    record("tautulli", "tautulli_1.zip", "2.18.2")
    record("jellyfin", "jellyfin_1.zip", "12.4.0")
    assert rv.check("tautulli", "tautulli_1.zip", {})["status"] == "match"
    assert rv.check("jellyfin", "jellyfin_1.zip", {})["status"] == "match"


@pytest.mark.parametrize("backup, now", [("6.4.4.10685", "6.5.0.10700"), ("6.5.0.10700", "6.4.4.10685")])
def test_any_other_version_is_refused_and_names_the_one_to_install(installed, backup, now):
    installed["radarr-4k"] = now
    record("radarr-4k", "radarr-4k_1.zip", backup)
    with pytest.raises(rv.RestoreVersionError) as raised:
        rv.check("radarr-4k", "radarr-4k_1.zip", {})
    assert str(raised.value) == (f"radarr-4k: this backup is from Radarr {backup} and Radarr {now} is installed. "
                                 f"Install Radarr {backup}, then restore.")


def test_lazylibrarian_names_the_image_tag_to_install(installed):
    installed["lazylibrarian"] = "943d297e"
    record("lazylibrarian", "lazylibrarian_1.tgz", "228abd29")
    with pytest.raises(rv.RestoreVersionError, match="Install the LazyLibrarian Docker image whose tag starts with 228abd29, then restore"):
        rv.check("lazylibrarian", "lazylibrarian_1.tgz", {})


def test_lazylibrarian_commits_of_different_lengths_match(installed):
    installed["lazylibrarian"] = "228abd29"
    record("lazylibrarian", "lazylibrarian_1.tgz", "228abd293b7e")
    assert rv.check("lazylibrarian", "lazylibrarian_1.tgz", {})["status"] == "match"


def test_a_backup_without_a_version_is_allowed_with_a_warning(installed):
    installed["sabnzbd"] = "5.1.3"
    record("sabnzbd", "sabnzbd_1.zip", None)
    result = rv.check("sabnzbd", "sabnzbd_1.zip", {})
    assert result["status"] == "unknown"
    assert result["message"] == "This backup doesn't record SABnzbd's version, so it can't be checked against the installed SABnzbd 5.1.3."


def test_a_backup_not_read_yet_is_allowed_with_a_warning(installed):
    installed["tdarr"] = "2.94.03"
    result = rv.check("tdarr", "tdarr_1.zip", {})
    assert result["status"] == "unknown" and result["message"].startswith("This backup's version hasn't been read yet")


def test_an_app_that_cannot_be_asked_stops_the_check(installed):
    record("plex", "plex_1.zip", "1.43")
    with pytest.raises(rv.RestoreVersionError, match="couldn't ask Plex for its version.*Plex must be running for the check"):
        rv.check("plex", "plex_1.zip", {"url": "http://plex:32400"})


def test_the_checksum_is_verified(tmp_path):
    archive = tmp_path / "radarr_1.zip"
    archive.write_bytes(b"backup")
    assert rv.verify_checksum("radarr", "radarr_1.zip", str(archive)) is False  # nothing recorded
    record("radarr", "radarr_1.zip", "6.4", sha256=manifest.file_sha256(archive))
    assert rv.verify_checksum("radarr", "radarr_1.zip", str(archive)) is True
    archive.write_bytes(b"backup, changed")
    with pytest.raises(rv.RestoreVersionError, match="doesn't match the checksum"):
        rv.verify_checksum("radarr", "radarr_1.zip", str(archive))


# --------------------------------------------------------------- web UI ----

@pytest.fixture
def restores(inline, authed_client, monkeypatch, tmp_path, installed, caplog):
    """Fake downloads and restores, with the real version gate."""
    caplog.set_level(logging.INFO, logger="backuparr")
    calls = []
    work = tmp_path / "work"

    def fetch(root, app, filename=None):
        work.mkdir(exist_ok=True)
        path = work / filename
        path.write_bytes(DOWNLOAD)
        calls.append(("fetch", app, filename))
        return str(work), str(path), filename

    monkeypatch.setattr(restore_actions, "fetch_backup", fetch)
    monkeypatch.setattr(restore_actions, "list_backups", lambda root, app: [f"{app}_2.zip"])
    monkeypatch.setattr(restore_actions, "restore_app", lambda app, *a, **kw: calls.append(("restore", app)) or {})
    apps = {name: {"enabled": True, "url": f"http://{name}:1", "api_key": f"{name}-key"} for name in ("radarr", "sabnzbd")}
    apps["plex"] = {"enabled": True, "url": "http://plex:1", "api_key": "plex-key", "data_dir": "/mnt/plex"}
    assert authed_client.post("/api/config", json={"apps": apps}).status_code == 200
    return calls


def restore_result(client, app, **body):
    response = client.post(f"/api/restore/local/{app}", json={"confirm": True, **body})
    assert response.status_code == 200
    return client.get("/api/restore/status").get_json()


def test_the_restore_tab_check_reports_the_result(authed_client, restores, installed):
    installed["radarr"] = "6.4"
    record("radarr", "radarr_1.zip", "6.4")
    response = authed_client.post("/api/restore/local/radarr/check", json={"file": "radarr_1.zip"})
    assert response.status_code == 200 and response.get_json()["status"] == "match"
    installed["radarr"] = "6.5"
    response = authed_client.post("/api/restore/local/radarr/check", json={"file": "radarr_1.zip"})
    assert response.status_code == 200
    assert response.get_json() == {"status": "refused", "message": "this backup is from Radarr 6.4 and Radarr 6.5 is installed. Install Radarr 6.4, then restore."}


def test_the_check_refuses_a_bad_filename(authed_client, restores):
    response = authed_client.post("/api/restore/local/radarr/check", json={"file": "../config.json"})
    assert response.status_code == 400


def test_a_mismatched_restore_fails_before_anything_is_downloaded(authed_client, restores, installed):
    installed["radarr"] = "6.5"
    record("radarr", "radarr_1.zip", "6.4")
    state = restore_result(authed_client, "radarr", file="radarr_1.zip")
    assert state["ok"] is False and "Install Radarr 6.4, then restore" in state["error"]
    assert restores == []


def test_a_matching_restore_verifies_the_download_and_runs(authed_client, restores, installed):
    installed["radarr"] = "6.4"
    record("radarr", "radarr_1.zip", "6.4")
    state = restore_result(authed_client, "radarr", file="radarr_1.zip")
    assert state["ok"] is True and restores == [("fetch", "radarr", "radarr_1.zip"), ("restore", "radarr")]
    log = "\n".join(state["log"])
    assert "This backup is from Radarr 6.4, the installed version." in log
    assert "the download matches the backup's checksum" in log


def test_a_download_that_does_not_match_its_checksum_is_not_restored(authed_client, restores, installed):
    installed["radarr"] = "6.4"
    record("radarr", "radarr_1.zip", "6.4", sha256="0" * 64)
    state = restore_result(authed_client, "radarr", file="radarr_1.zip")
    assert state["ok"] is False and "doesn't match the checksum" in state["error"]
    assert restores == [("fetch", "radarr", "radarr_1.zip")]


def test_without_a_file_the_newest_backup_is_checked_and_restored(authed_client, restores, installed):
    installed["sabnzbd"] = "5.1.3"
    state = restore_result(authed_client, "sabnzbd")
    assert state["ok"] is True and state["file"] == "sabnzbd_2.zip"
    assert any("hasn't been read yet" in line for line in state["log"])


def test_a_stopped_app_restores_after_the_tab_checked_it_while_running(authed_client, restores, installed):
    record("plex", "plex_1.zip", "1.43")
    installed["plex"] = "1.43"
    assert authed_client.post("/api/restore/local/plex/check", json={"file": "plex_1.zip"}).status_code == 200
    del installed["plex"]  # stopped for the restore
    state = restore_result(authed_client, "plex", file="plex_1.zip")
    assert state["ok"] is True and ("restore", "plex") in restores


def test_a_stopped_app_that_was_never_checked_is_refused(authed_client, restores, installed):
    record("plex", "plex_1.zip", "1.43")
    state = restore_result(authed_client, "plex", file="plex_1.zip")
    assert state["ok"] is False
    assert "Start Plex, pick the backup on the Restore tab so its version is checked, then stop Plex and restore" in state["error"]
    assert restores == []


def test_a_refused_check_does_not_let_a_stopped_app_restore(authed_client, restores, installed):
    record("plex", "plex_1.zip", "1.43")
    installed["plex"] = "1.44"
    assert authed_client.post("/api/restore/local/plex/check", json={"file": "plex_1.zip"}).get_json()["status"] == "refused"
    del installed["plex"]
    assert restore_result(authed_client, "plex", file="plex_1.zip")["ok"] is False
    assert restores == []


def test_a_check_for_another_backup_does_not_count(authed_client, restores, installed):
    record("plex", "plex_1.zip", "1.43")
    record("plex", "plex_2.zip", "1.43")
    installed["plex"] = "1.43"
    authed_client.post("/api/restore/local/plex/check", json={"file": "plex_2.zip"})
    del installed["plex"]
    assert restore_result(authed_client, "plex", file="plex_1.zip")["ok"] is False


def test_a_stopped_app_still_running_with_another_version_is_refused(authed_client, restores, installed):
    record("plex", "plex_1.zip", "1.43")
    installed["plex"] = "1.43"
    authed_client.post("/api/restore/local/plex/check", json={"file": "plex_1.zip"})
    installed["plex"] = "1.44"  # upgraded since the check, and still answering
    state = restore_result(authed_client, "plex", file="plex_1.zip")
    assert state["ok"] is False and "Install Plex 1.43, then restore" in state["error"]
