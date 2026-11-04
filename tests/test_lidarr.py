"""Lidarr runs on the shared Servarr driver, against API v1."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
from urllib.parse import urlsplit
import zipfile

import pytest

import backup
import config_store
import restore_actions
from apps.lidarr import LidarrApp


@pytest.fixture
def lidarr():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("config.xml", "<Config />")
        output.writestr("lidarr.db", b"SQLite format 3\x00")
    state = {"requests": [], "archive": archive.getvalue()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=b""):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def record(self, body=b""):
            state["requests"].append((self.command, urlsplit(self.path).path, self.headers.get("X-Api-Key"), body))

        def do_GET(self):
            self.record()
            path = urlsplit(self.path).path
            if path == "/api/v1/system/status":
                return self.reply(200, b'{"version":"3.1.6.5079"}')
            if path == "/api/v1/command/12":
                return self.reply(200, b'{"id":12,"status":"completed"}')
            if path == "/api/v1/system/backup":
                return self.reply(200, json.dumps([
                    {"id": 3, "type": "scheduled", "path": "/backup/scheduled/old.zip", "time": "2026-11-01T00:00:00Z"},
                    {"id": 4, "type": "manual", "path": "/backup/manual/lidarr_backup_v3.1.6.5079_2026.11.03_08.00.00.zip",
                     "time": "2026-11-03T08:00:00Z"},
                ]).encode())
            if path == "/backup/manual/lidarr_backup_v3.1.6.5079_2026.11.03_08.00.00.zip":
                return self.reply(200, state["archive"])
            return self.reply(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.record(body)
            path = urlsplit(self.path).path
            if path == "/api/v1/command":
                return self.reply(201, b'{"id":12}')
            if path == "/api/v1/system/backup/restore/upload":
                return self.reply(200, b'{"RestartRequired":true}')
            if path == "/api/v1/system/restart":
                return self.reply(200, b"{}")
            return self.reply(404)

        def do_DELETE(self):
            self.record()
            return self.reply(200 if urlsplit(self.path).path == "/api/v1/system/backup/4" else 404, b"{}")

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", state
    http.shutdown()
    http.server_close()
    thread.join()


def test_lidarr_is_a_multi_instance_app_restored_by_upload():
    meta = config_store.app_meta("lidarr-flac")
    assert meta["id"] == "lidarr" and meta["multi_instance"] is True
    assert config_store.key_required("lidarr") is True
    assert config_store.restore_supported("lidarr-flac") is True
    assert "lidarr" in restore_actions.UPLOAD_RESTORE_APPS


def test_build_app_makes_a_lidarr_for_each_instance():
    for name in ("lidarr", "lidarr-flac"):
        app = backup.build_app(name, {"url": "http://lidarr:8686/", "api_key": "k"})
        assert isinstance(app, LidarrApp) and app.name == "lidarr" and app.url == "http://lidarr:8686"


def test_backup_downloads_the_new_manual_backup_and_removes_it(lidarr, tmp_path):
    url, state = lidarr
    app = backup.build_app("lidarr", {"url": url, "api_key": "lidarr-key"})
    assert app.test_connection() == "lidarr 3.1.6.5079 reachable"
    path = app.backup(str(tmp_path))
    with zipfile.ZipFile(path) as archive:
        assert sorted(archive.namelist()) == ["config.xml", "lidarr.db"]
    calls = [(method, path) for method, path, _key, _body in state["requests"]]
    assert calls == [
        ("GET", "/api/v1/system/status"),
        ("POST", "/api/v1/command"),
        ("GET", "/api/v1/command/12"),
        ("GET", "/api/v1/system/backup"),
        ("GET", "/backup/manual/lidarr_backup_v3.1.6.5079_2026.11.03_08.00.00.zip"),
        ("DELETE", "/api/v1/system/backup/4"),
    ]
    assert all(key == "lidarr-key" for _method, _path, key, _body in state["requests"])
    assert json.loads(state["requests"][1][3]) == {"name": "Backup"}


def test_restore_uploads_the_archive_and_restarts_lidarr(lidarr, tmp_path):
    url, state = lidarr
    archive = tmp_path / "lidarr_20261103_080000.zip"
    archive.write_bytes(state["archive"])
    assert restore_actions.restore_app("lidarr-flac", {"url": url, "api_key": "lidarr-key"}, str(tmp_path), str(archive)) == {"kind": "upload"}
    calls = [(method, path) for method, path, _key, _body in state["requests"]]
    assert calls == [("POST", "/api/v1/system/backup/restore/upload"), ("POST", "/api/v1/system/restart")]
    assert b'filename="lidarr_20261103_080000.zip"' in state["requests"][0][3]
