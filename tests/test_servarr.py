"""Exercise API-key-only downloads, redirects and protected web routes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
from urllib.parse import urlsplit
import zipfile

import pytest

from apps.servarr import ServarrApp, ServarrError


@pytest.fixture
def server():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("config.xml", "<Config />")
    state = {"mode": "plain", "requests": [], "deleted": [], "archive": archive.getvalue()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=b"", **headers):
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(body)

        def record(self, body=b""):
            state["requests"].append((self.command, self.path, dict(self.headers), body))

        def do_GET(self):
            self.record()
            path = urlsplit(self.path).path
            if path.endswith("/system/status"):
                return self.reply(200, b'{"version":"fixture"}')
            if path.endswith("/command/7"):
                return self.reply(200, b'{"id":7,"status":"completed"}')
            if path.endswith("/system/backup"):
                return self.reply(200, json.dumps([{
                    "id": 8, "type": "manual", "path": "/backup/manual/current.zip",
                }]).encode())
            assert path.startswith("/arr/backup/"), path
            mode = state["mode"]
            if mode == "forms":
                return self.reply(302, Location="/arr/login?returnUrl=%2Farr%2Fbackup%2Fmanual%2Fcurrent.zip")
            if mode == "basic":
                return self.reply(401, WWW_Authenticate='Basic realm="Servarr"')
            if mode == "file-redirect" and path.endswith("current.zip"):
                return self.reply(302, Location="final.zip")
            if mode == "external":
                return self.reply(302, Location="https://login.example/login?token=do-not-leak")
            if mode == "proxy":
                return self.reply(302, Location="/sso/login?token=do-not-leak")
            if mode == "loop":
                return self.reply(302, Location=self.path)
            if mode == "html":
                return self.reply(200, b"<html>secret login page</html>")
            if mode == "denied":
                return self.reply(403, b"private response")
            return self.reply(200, state["archive"])

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.record(body)
            assert self.path.endswith("/command")
            return self.reply(200, b'{"id":7}')

        def do_DELETE(self):
            self.record()
            state["deleted"].append(self.path)
            return self.reply(200)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}/arr", state
    http.shutdown()
    http.server_close()
    thread.join()


def driver(server):
    return ServarrApp("radarr", server[0], "test-api-key")


def test_api_key_downloads_zip_and_cleans_up_server_backup(server, tmp_path):
    instance = driver(server)
    assert instance.test_connection() == "radarr fixture reachable"
    assert not any(r[0] == "POST" for r in server[1]["requests"])
    dest = instance.backup(tmp_path)
    assert zipfile.is_zipfile(dest)
    assert len(server[1]["deleted"]) == 1
    assert all(r[2].get("X-Api-Key") == "test-api-key" for r in server[1]["requests"])
    assert not any(r[1] == "/arr/login" for r in server[1]["requests"])
    assert not list(tmp_path.glob(".backup-*"))


def test_local_file_redirect_preserves_api_key(server, tmp_path):
    server[1]["mode"] = "file-redirect"
    dest = tmp_path / "backup.zip"
    assert driver(server).download_backup("/backup/manual/current.zip", dest) == dest
    assert zipfile.is_zipfile(dest)
    assert len(server[1]["requests"]) == 2
    assert all(r[2].get("X-Api-Key") == "test-api-key" for r in server[1]["requests"])
    assert all("apikey" not in r[1].lower() for r in server[1]["requests"])


@pytest.mark.parametrize("mode,reason", [("forms", "API key cannot authenticate"), ("basic", "HTTP 401")])
def test_protected_backup_never_logs_in_and_preserves_files(server, tmp_path, mode, reason):
    server[1]["mode"] = mode
    instance = driver(server)
    existing = tmp_path / "current.zip"
    existing.write_bytes(b"existing file")
    with pytest.raises(ServarrError, match=reason):
        instance.backup(tmp_path)
    assert existing.read_bytes() == b"existing file"
    assert not server[1]["deleted"]
    assert not list(tmp_path.glob(".backup-*"))
    assert all("Authorization" not in r[2] and "Cookie" not in r[2] for r in server[1]["requests"])
    assert not any(r[1].startswith("/arr/login") for r in server[1]["requests"])


@pytest.mark.parametrize("mode,reason", [
    ("external", "reverse proxy"), ("proxy", "reverse proxy"),
    ("html", "valid ZIP"), ("denied", "HTTP 403"), ("loop", "too many"),
])
def test_invalid_downloads_fail_without_credentials_leaks_or_deleting_backup(server, tmp_path, mode, reason):
    server[1]["mode"] = mode
    with pytest.raises(ServarrError, match=reason) as error:
        driver(server).backup(tmp_path)
    assert not server[1]["deleted"]
    assert not list(tmp_path.iterdir())
    assert not any(r[1].startswith("/sso") or r[1] == "/arr/login" for r in server[1]["requests"])
    assert all("Authorization" not in r[2] for r in server[1]["requests"])
    assert "do-not-leak" not in str(error.value)
    assert "secret login page" not in str(error.value)
    assert "private response" not in str(error.value)


def test_size_limit_removes_partial_download(server, tmp_path):
    server[1]["mode"] = "plain"
    with pytest.raises(ServarrError, match="too large"):
        driver(server).download_backup("/backup/manual/current.zip", tmp_path / "backup.zip", max_bytes=10)
    assert not list(tmp_path.iterdir())
