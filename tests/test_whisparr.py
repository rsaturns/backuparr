"""Whisparr runs on the shared Servarr driver, against API v3 (v2 and v3 alike)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import threading
from urllib.parse import urlsplit
import zipfile

import pytest

import backup
import config_store
import restore_actions
from apps.servarr import ServarrApp
from apps.whisparr import WhisparrApp


@pytest.fixture
def whisparr():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("config.xml", "<Config />")
        output.writestr("whisparr2.db", b"SQLite format 3\x00")
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
            if path == "/api/v3/system/status":
                return self.reply(200, b'{"version":"2.2.0.404"}')
            if path == "/api/v3/command/12":
                return self.reply(200, b'{"id":12,"status":"completed"}')
            if path == "/api/v3/system/backup":
                return self.reply(200, json.dumps([
                    {"id": 3, "type": "scheduled", "path": "/backup/scheduled/old.zip", "time": "2026-11-01T00:00:00Z"},
                    {"id": 4, "type": "manual", "path": "/backup/manual/whisparr_backup_v2.2.0.404_2026.11.03_08.00.00.zip",
                     "time": "2026-11-03T08:00:00Z"},
                ]).encode())
            if path == "/backup/manual/whisparr_backup_v2.2.0.404_2026.11.03_08.00.00.zip":
                return self.reply(200, state["archive"])
            return self.reply(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.record(body)
            path = urlsplit(self.path).path
            if path == "/api/v3/command":
                return self.reply(201, b'{"id":12}')
            if path == "/api/v3/system/backup/restore/upload":
                return self.reply(200, b'{"RestartRequired":true}')
            if path == "/api/v3/system/restart":
                return self.reply(200, b"{}")
            return self.reply(404)

        def do_DELETE(self):
            self.record()
            return self.reply(200 if urlsplit(self.path).path == "/api/v3/system/backup/4" else 404, b"{}")

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", state
    http.shutdown()
    http.server_close()
    thread.join()


def test_whisparr_is_a_multi_instance_app_restored_by_upload():
    meta = config_store.app_meta("whisparr-v3")
    assert meta["id"] == "whisparr" and meta["multi_instance"] is True
    assert config_store.key_required("whisparr") is True
    assert config_store.restore_supported("whisparr-v3") is True
    assert "whisparr" in restore_actions.UPLOAD_RESTORE_APPS


def test_build_app_makes_a_whisparr_for_each_instance():
    for name in ("whisparr", "whisparr-v3"):
        app = backup.build_app(name, {"url": "http://whisparr:6969/", "api_key": "k"})
        assert isinstance(app, WhisparrApp) and app.name == "whisparr" and app.url == "http://whisparr:6969"


def test_backup_downloads_the_new_manual_backup_and_removes_it(whisparr, tmp_path):
    url, state = whisparr
    app = backup.build_app("whisparr", {"url": url, "api_key": "whisparr-key"})
    assert app.test_connection() == "whisparr 2.2.0.404 reachable"
    path = app.backup(str(tmp_path))
    with zipfile.ZipFile(path) as archive:
        assert sorted(archive.namelist()) == ["config.xml", "whisparr2.db"]
    calls = [(method, path) for method, path, _key, _body in state["requests"]]
    assert calls == [
        ("GET", "/api/v3/system/status"),
        ("POST", "/api/v3/command"),
        ("GET", "/api/v3/command/12"),
        ("GET", "/api/v3/system/backup"),
        ("GET", "/backup/manual/whisparr_backup_v2.2.0.404_2026.11.03_08.00.00.zip"),
        ("DELETE", "/api/v3/system/backup/4"),
    ]
    assert all(key == "whisparr-key" for _method, _path, key, _body in state["requests"])
    assert json.loads(state["requests"][1][3]) == {"name": "Backup"}


def test_restore_uploads_the_archive_and_restarts_whisparr(whisparr, tmp_path):
    url, state = whisparr
    archive = tmp_path / "whisparr_20261103_080000.zip"
    archive.write_bytes(state["archive"])
    assert restore_actions.restore_app("whisparr-v3", {"url": url, "api_key": "whisparr-key"}, str(tmp_path), str(archive)) == {"kind": "upload"}
    calls = [(method, path) for method, path, _key, _body in state["requests"]]
    assert calls == [("POST", "/api/v3/system/backup/restore/upload"), ("POST", "/api/v3/system/restart")]
    assert b'filename="whisparr_20261103_080000.zip"' in state["requests"][0][3]


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return str(path)


def _uploaded(monkeypatch):
    seen = []

    def capture(self, file_path):
        with zipfile.ZipFile(file_path) as archive:
            seen.append((os.path.basename(file_path), {n: archive.read(n) for n in archive.namelist()}))
        return {"RestartRequired": True}

    monkeypatch.setattr(ServarrApp, "restore_upload", capture)
    return seen


def test_v3_backup_is_uploaded_with_the_database_name_v3_accepts(monkeypatch, tmp_path):
    # Whisparr v3 backs up whisparr3.db but only restores nzbdrone.db or whisparr.db.
    seen = _uploaded(monkeypatch)
    archive = _zip(tmp_path / "whisparr-v3_20261103_080000.zip",
                   {"config.xml": b"<Config />", "whisparr3.db": b"v3 database", "whisparr3.db-journal": b"", "INFO": b"v3"})
    WhisparrApp("http://whisparr:6969", "k").restore_upload(archive)
    name, members = seen[0]
    assert name == "whisparr-v3_20261103_080000.zip"
    assert members == {"config.xml": b"<Config />", "whisparr.db": b"v3 database", "whisparr3.db-journal": b"", "INFO": b"v3"}
    assert os.listdir(tmp_path) == ["whisparr-v3_20261103_080000.zip"], "the renamed copy must be removed"


@pytest.mark.parametrize("members", [
    {"config.xml": b"<Config />", "whisparr2.db": b"v2 database"},
    {"config.xml": b"<Config />", "whisparr.db": b"db", "whisparr3.db": b"db"},
])
def test_other_backups_are_uploaded_unchanged(monkeypatch, tmp_path, members):
    seen = _uploaded(monkeypatch)
    archive = _zip(tmp_path / "whisparr_20261103_080000.zip", members)
    WhisparrApp("http://whisparr:6969", "k").restore_upload(archive)
    assert seen == [("whisparr_20261103_080000.zip", members)]
