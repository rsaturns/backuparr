import zipfile

import pytest
import requests

import backup
import destination_util
import dropbox_oauth


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code, self.text = status_code, text


def test_remote_root_needs_a_token_and_targets_the_backuparr_folder():
    with pytest.raises(dropbox_oauth.DropboxOAuthError, match="rclone authorize dropbox"):
        dropbox_oauth.remote_root({"token": ""})
    assert dropbox_oauth.remote_root({"token": "{}"}) == "backuparr-dropbox:Backuparr"


def test_destination_util_resolves_dropbox():
    assert destination_util.remote_root("dropbox", {"token": "{}"}) == "backuparr-dropbox:Backuparr"
    with pytest.raises(destination_util.DestinationError, match="not connected"):
        destination_util.remote_root("dropbox", {"token": ""})


def test_verify_sends_the_bearer_token_to_dropbox(monkeypatch):
    calls = []

    def post(url, headers, timeout):
        calls.append((url, headers["Authorization"]))
        return FakeResponse(200)

    monkeypatch.setattr(requests, "post", post)
    dropbox_oauth.verify_access_token("tok")
    assert calls == [("https://api.dropboxapi.com/2/users/get_current_account", "Bearer tok")]


def test_verify_reports_a_rejected_token_without_echoing_it(monkeypatch):
    class JsonResponse(FakeResponse):
        def json(self):
            return {"error": {".tag": "invalid_access_token"}, "error_summary": "invalid_access_token/"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: JsonResponse(401, "raw body"))
    with pytest.raises(dropbox_oauth.DropboxOAuthError) as exc:
        dropbox_oauth.verify_access_token("secret-access-token")
    assert "(invalid_access_token)" in str(exc.value) and "secret-access-token" not in str(exc.value)


def test_verify_falls_back_to_the_raw_body_when_it_is_not_json(monkeypatch):
    class NotJson(FakeResponse):
        def json(self):
            raise ValueError("no json")

    monkeypatch.setattr(requests, "post", lambda *a, **k: NotJson(503, "upstream unavailable"))
    with pytest.raises(dropbox_oauth.DropboxOAuthError, match="upstream unavailable"):
        dropbox_oauth.verify_access_token("tok")


@pytest.fixture
def rclone(monkeypatch):
    state = {"remotes": {}, "sets": [], "deleted": []}
    monkeypatch.setattr(dropbox_oauth.rclone_util, "config_dump", lambda: dict(state["remotes"]))
    monkeypatch.setattr(dropbox_oauth.rclone_util, "config_set", lambda name, kind, fields, force=False: state["sets"].append((name, kind, dict(fields), force)))
    monkeypatch.setattr(dropbox_oauth.rclone_util, "config_delete", state["deleted"].append)
    return state


def test_sync_removes_the_remote_when_disconnected(rclone):
    dropbox_oauth.sync_rclone_remote({"token": ""})
    assert rclone["deleted"] == ["backuparr-dropbox"] and rclone["sets"] == []


def test_sync_creates_a_missing_remote_with_the_token(rclone):
    dropbox_oauth.sync_rclone_remote({"token": "T"})
    assert rclone["sets"] == [("backuparr-dropbox", "dropbox", {"token": "T"}, False)]


def test_sync_keeps_the_token_rclone_has_already_refreshed(rclone):
    rclone["remotes"]["backuparr-dropbox"] = {"token": "refreshed"}
    dropbox_oauth.sync_rclone_remote({"token": "stale"})
    assert rclone["sets"] == [("backuparr-dropbox", "dropbox", {}, False)]


def test_sync_force_replaces_the_token(rclone):
    rclone["remotes"]["backuparr-dropbox"] = {"token": "old"}
    dropbox_oauth.sync_rclone_remote({"token": "new"}, force=True)
    assert rclone["sets"] == [("backuparr-dropbox", "dropbox", {"token": "new"}, True)]


def test_backups_upload_under_the_backuparr_folder(tmp_path, monkeypatch):
    class FakeApp:
        def backup(self, work_dir):
            path = tmp_path / "radarr_backup.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("a.txt", "x")
            return str(path)

    uploaded, retention = [], []
    cfg = {"retention_days": 7, "apps": {"radarr": {}}, "destinations": {"dropbox": {"enabled": True, "token": "{}"}}}
    monkeypatch.setattr(backup, "enabled_apps", lambda c: ["radarr"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda c: ["dropbox"])
    monkeypatch.setattr(backup.destination_util, "sync", lambda c: None)
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: FakeApp())
    monkeypatch.setattr(backup.rclone_util, "copyto", lambda src, dst: uploaded.append(dst))
    monkeypatch.setattr(backup.rclone_util, "delete_older_than", lambda root, age: retention.append(root))
    ok, failed = backup.run_backup(cfg)
    assert (ok, failed) == (["radarr"], [])
    assert len(uploaded) == 1 and uploaded[0].startswith("backuparr-dropbox:Backuparr/radarr/radarr_")
    assert retention == ["backuparr-dropbox:Backuparr"]
