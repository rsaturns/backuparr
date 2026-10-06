import importlib

import pytest

import restore_actions as ra


@pytest.mark.parametrize("name", [
    "radarr_20261006_171108.zip",
    "profilarr_20261006_171108.tar.gz",
    "bazarr_backup_v1.2.3.zip",
    "odd..name.zip",
])
def test_real_backup_names_are_accepted(name):
    assert ra.SAFE_FILENAME.match(name)


@pytest.mark.parametrize("name", [
    ".", "..", ".hidden", "../x.zip", "a/b.zip", "a\\b.zip", "x.zip\n", "", " x.zip", "x;y.zip",
])
def test_directories_and_path_tricks_are_rejected(name):
    assert not ra.SAFE_FILENAME.match(name)


@pytest.mark.parametrize("name", [".", ".."])
def test_fetch_backup_refuses_directory_names_before_touching_the_destination(monkeypatch, name):
    def explode(*args, **kwargs):
        raise AssertionError("rclone must not be called")

    monkeypatch.setattr(ra.rclone_util, "copyto", explode)
    with pytest.raises(ValueError):
        ra.fetch_backup("/dest", "radarr", name)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUPARR_SECRET_KEY_PATH", str(tmp_path / "session.key"))
    monkeypatch.setenv("BACKUPARR_LOG_DIR", str(tmp_path / "logs"))
    module = importlib.import_module("webui.app")
    monkeypatch.setattr(module.auth_store, "has_credentials", lambda: True)
    calls = []
    monkeypatch.setattr(module.rclone_util, "copyto", lambda *a: calls.append(a))
    monkeypatch.setattr(module.rclone_util, "delete_file", lambda *a: calls.append(a))
    client = module.app.test_client()
    with client.session_transaction() as session:
        session["authed"] = True
    client.calls = calls
    return client


@pytest.mark.parametrize("name", ["%2e%2e", "%2e", "%2ehidden"])
def test_history_download_and_delete_reject_directory_names(client, name):
    base = f"/api/history/local/radarr/{name}"
    assert client.get(base + "/download").status_code == 400
    assert client.delete(base).status_code == 400
    assert client.calls == []
