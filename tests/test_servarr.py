"""Exercise downloads against HTTP, including Servarr's Forms login flow."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
from urllib.parse import parse_qs, urlsplit
import zipfile

import pytest

from apps.servarr import ServarrApp, ServarrError


@pytest.fixture
def server():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("config.xml", "<Config />")
    state = {"mode": "forms", "requests": [], "deleted": [], "archive": archive.getvalue()}

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
            if mode == "forms" and self.headers.get("Cookie") != "fixture-auth=valid":
                return self.reply(302, Location="/arr/login?returnUrl=%2Farr%2Fbackup%2Fmanual%2Fcurrent.zip")
            if mode == "basic":
                expected = "Basic " + base64.b64encode(b"demo: exact-password ").decode()
                if self.headers.get("Authorization") != expected:
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
            if self.path == "/arr/login":
                fields = parse_qs(body.decode())
                if fields.get("username") == ["demo"] and fields.get("password") == [" exact-password "]:
                    return self.reply(302, Location="/arr/", Set_Cookie="fixture-auth=valid; Path=/arr/")
                return self.reply(302, Location="/arr/login?loginFailed=true")
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


def driver(server, credentials=True):
    return ServarrApp("radarr", server[0], "test-api-key",
                      username="demo" if credentials else "",
                      password=" exact-password " if credentials else "")


def test_forms_login_downloads_zip_and_cleans_up_server_backup(server, tmp_path):
    instance = driver(server)
    assert instance.test_connection() == "radarr fixture reachable"
    assert not any(r[0] == "POST" for r in server[1]["requests"])
    dest = instance.backup(tmp_path)
    assert zipfile.is_zipfile(dest)
    assert len(server[1]["deleted"]) == 1
    logins = [r for r in server[1]["requests"] if r[1] == "/arr/login"]
    assert len(logins) == 1
    assert parse_qs(logins[0][3].decode())["password"] == [" exact-password "]
    assert not list(tmp_path.glob(".backup-*"))


@pytest.mark.parametrize("mode", ["file-redirect", "basic"])
def test_supported_download_redirect_or_basic_login(server, tmp_path, mode):
    server[1]["mode"] = mode
    dest = tmp_path / "backup.zip"
    assert driver(server).download_backup("/backup/manual/current.zip", dest) == dest
    assert zipfile.is_zipfile(dest)
    assert not any(r[1] == "/arr/login" for r in server[1]["requests"])


@pytest.mark.parametrize("credentials,reason", [(False, "requires web login"), (True, "web login failed")])
def test_login_failure_preserves_existing_file_and_server_backup(server, tmp_path, credentials, reason):
    instance = driver(server, credentials)
    if credentials:
        instance.password = "wrong-password"
    existing = tmp_path / "current.zip"
    existing.write_bytes(b"existing file")
    with pytest.raises(ServarrError, match=reason) as error:
        instance.backup(tmp_path)
    assert existing.read_bytes() == b"existing file"
    assert not server[1]["deleted"]
    assert not list(tmp_path.glob(".backup-*"))
    assert "wrong-password" not in str(error.value)


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
