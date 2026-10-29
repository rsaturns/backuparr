import io
import json
import os
import zipfile

import pytest
import requests

from apps import jellyfin
from apps.jellyfin import BACKUP_OPTIONS, JellyfinApp, JellyfinError

KEY = "jellyfin-secret-key"
SERVER_DIR = "/config/data/backups"


def archive(path, manifest=True):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Database/Users.json", "[]")
        if manifest:
            zf.writestr("manifest.json", json.dumps({"ServerVersion": "12.4.0.0"}))
    return str(path)


def entry(name):
    return {"Path": f"{SERVER_DIR}/{name}", "ServerVersion": "12.4.0.0"}


@pytest.fixture
def server(monkeypatch):
    """Answers each "METHOD /path" from `routes` (status, body); records requests."""
    state = type("Server", (), {})()
    state.sent, state.routes = [], {"GET /System/Info": (200, {"Version": "12.4.0"}), "GET /Backup": (200, [])}

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        path = request.path_url.split("?")[0]
        status, body = state.routes.get(f"{request.method} {path}", (404, {}))
        if callable(body):
            body = body(request)
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = json.dumps(body).encode()
        res.raw = io.BytesIO(res._content)
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "jellyfin-backups"
    path.mkdir()
    return path


@pytest.fixture
def app(folder):
    return JellyfinApp("http://jellyfin:8096/", KEY, backup_dir=str(folder))


@pytest.fixture
def fast_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(jellyfin.time, "time", lambda: now[0])
    monkeypatch.setattr(jellyfin.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))


# --- connection --------------------------------------------------------------

def test_the_key_goes_in_the_authorization_header_never_the_url(app, server):
    assert app.test_connection() == "jellyfin 12.4.0 reachable, backups folder OK"
    assert [r.url for r in server.sent] == ["http://jellyfin:8096/System/Info", "http://jellyfin:8096/Backup"]
    assert all(r.headers["Authorization"] == f'MediaBrowser Token="{KEY}"' and KEY not in r.url for r in server.sent)


@pytest.mark.parametrize("status, message", [
    (401, "unauthorized - check the API key"),
    (403, "use an API key from Dashboard > API Keys"),
])
def test_a_rejected_key_says_why(app, server, status, message):
    server.routes["GET /System/Info"] = (status, {})
    with pytest.raises(JellyfinError, match=message):
        app.test_connection()


def test_a_server_without_the_backup_api_needs_a_newer_jellyfin(app, server):
    del server.routes["GET /Backup"]
    with pytest.raises(JellyfinError, match="10.11 or newer"):
        app.test_connection()


def test_the_backups_folder_must_be_set_and_exist(server, tmp_path):
    with pytest.raises(JellyfinError, match="set the backups folder"):
        JellyfinApp("http://jellyfin:8096", KEY).test_connection()
    with pytest.raises(JellyfinError, match="doesn't exist in this container"):
        JellyfinApp("http://jellyfin:8096", KEY, backup_dir=str(tmp_path / "missing")).test_connection()


def test_a_folder_missing_jellyfins_listed_backups_is_the_wrong_mount(app, server, folder):
    server.routes["GET /Backup"] = (200, [entry("jellyfin-backup-1.zip")])
    with pytest.raises(JellyfinError, match=f"doesn't hold the backups Jellyfin lists in {SERVER_DIR}"):
        app.test_connection()
    (folder / "jellyfin-backup-1.zip").write_bytes(b"zip")
    assert app.test_connection().endswith("backups folder OK")


def test_a_read_only_folder_still_passes_with_a_note(app, server, monkeypatch):
    monkeypatch.setattr(jellyfin.os, "access", lambda path, mode: mode != os.W_OK)
    assert app.test_connection() == "jellyfin 12.4.0 reachable; backups folder is read-only, so backups work but restores won't"


# --- backup ------------------------------------------------------------------

def created(folder, name="jellyfin-backup-20261028.zip", **kwargs):
    def create(_request):
        archive(folder / name, **kwargs)
        return entry(name)
    return create


def test_backup_asks_for_the_database_only_and_moves_the_archive_out(app, server, folder, tmp_path):
    server.routes["POST /Backup/Create"] = (200, created(folder))
    result = app.backup(str(tmp_path / "work"))
    assert result == str(tmp_path / "work" / "jellyfin-backup-20261028.zip")
    assert jellyfin.is_jellyfin_backup(result) and list(folder.iterdir()) == []  # Jellyfin's copy removed
    create = server.sent[-1]
    assert json.loads(create.body) == BACKUP_OPTIONS == {"Database": True, "Metadata": False, "Trickplay": False, "Subtitles": False}


def test_an_archive_jellyfin_wrote_elsewhere_points_at_the_mount(app, server, fast_clock, tmp_path):
    server.routes["POST /Backup/Create"] = (200, entry("jellyfin-backup-1.zip"))
    with pytest.raises(JellyfinError, match=f"wrote jellyfin-backup-1.zip to {SERVER_DIR}, but it isn't in"):
        app.backup(str(tmp_path / "work"))


def test_a_copy_jellyfin_keeps_is_only_a_warning(app, server, folder, tmp_path, monkeypatch, caplog):
    server.routes["POST /Backup/Create"] = (200, created(folder))

    def read_only(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(jellyfin.os, "remove", read_only)
    assert os.path.isfile(app.backup(str(tmp_path / "work")))
    assert "couldn't remove jellyfin-backup-20261028.zip from Jellyfin's backups folder (Permission denied)" in caplog.text


def test_an_archive_without_a_manifest_is_refused(app, server, folder, tmp_path):
    server.routes["POST /Backup/Create"] = (200, created(folder, manifest=False))
    with pytest.raises(JellyfinError, match="isn't a valid Jellyfin backup"):
        app.backup(str(tmp_path / "work"))


@pytest.mark.parametrize("path", ["/config/data/backups/../../../etc/passwd/..", "", None, "C:\\backups\\.."])
def test_a_server_path_that_isnt_a_plain_filename_is_refused(app, server, tmp_path, path):
    server.routes["POST /Backup/Create"] = (200, {"Path": path})
    with pytest.raises(JellyfinError, match="unexpected backup path"):
        app.backup(str(tmp_path / "work"))


def test_windows_server_paths_map_to_the_filename(app, server, folder, tmp_path):
    def create(_request):
        archive(folder / "jellyfin-backup-2.zip")
        return {"Path": "C:\\ProgramData\\Jellyfin\\Server\\data\\backups\\jellyfin-backup-2.zip"}
    server.routes["POST /Backup/Create"] = (200, create)
    assert app.backup(str(tmp_path / "work")).endswith("jellyfin-backup-2.zip")


# --- restore -----------------------------------------------------------------

def listing_once_copied(folder):
    return lambda _request: [entry(p.name) for p in folder.iterdir()]


def test_restore_copies_the_archive_in_and_starts_jellyfins_restore(app, server, folder, tmp_path):
    source = archive(tmp_path / "jellyfin_20261028_030000.zip")
    server.routes["GET /Backup"] = (200, listing_once_copied(folder))
    server.routes["POST /Backup/Restore"] = (204, {})
    app.restore_from_file(source)
    copied = folder / "jellyfin_20261028_030000.zip"
    assert copied.read_bytes() == open(source, "rb").read() and copied.stat().st_mode & 0o777 == 0o644
    restore = server.sent[-1]
    assert restore.path_url == "/Backup/Restore" and json.loads(restore.body) == {"ArchiveFileName": copied.name}


def test_restore_refuses_a_file_that_isnt_a_jellyfin_backup_before_any_call(app, server, folder, tmp_path):
    other = tmp_path / "jellyfin_x.zip"
    other.write_bytes(b"not a zip")
    with pytest.raises(JellyfinError, match="isn't a Jellyfin backup"):
        app.restore_from_file(str(other))
    assert server.sent == [] and list(folder.iterdir()) == []


def test_restore_needs_a_writable_folder(app, server, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(jellyfin.os, "access", lambda path, mode: mode != os.W_OK)
    with pytest.raises(JellyfinError, match="isn't writable"):
        app.restore_from_file(archive(tmp_path / "jellyfin_1.zip"))
    assert server.sent == []


def test_an_archive_jellyfin_doesnt_list_is_removed_and_not_restored(app, server, folder, tmp_path):
    with pytest.raises(JellyfinError, match="doesn't list jellyfin_1.zip"):
        app.restore_from_file(archive(tmp_path / "jellyfin_1.zip"))
    assert list(folder.iterdir()) == []
    assert all(r.path_url != "/Backup/Restore" for r in server.sent)


def test_a_failed_restore_call_is_reported(app, server, folder, tmp_path):
    server.routes["GET /Backup"] = (200, listing_once_copied(folder))
    server.routes["POST /Backup/Restore"] = (500, {})
    with pytest.raises(requests.exceptions.HTTPError):
        app.restore_from_file(archive(tmp_path / "jellyfin_1.zip"))
