"""Exercise API-key downloads, redirects, protected web routes and signing
in to them with the app's own login."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
from urllib.parse import parse_qs, urlsplit
import zipfile

import pytest

from apps.servarr import LoginRequiredError, ServarrApp, ServarrError

BASIC_OK = "Basic " + base64.b64encode(b"admin:s3cret").decode()


@pytest.fixture
def server():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("config.xml", "<Config />")
    # host_method: what config/host reports (None = the call fails).
    state = {"mode": "plain", "requests": [], "deleted": [], "archive": archive.getvalue(), "host_method": "auto"}

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

        def signed_in(self):
            """Like Servarr: the Forms cookie, or a valid Basic header."""
            if state["mode"] == "forms":
                return "RadarrAuth=ok" in self.headers.get("Cookie", "")
            if state["mode"] == "basic":
                return self.headers.get("Authorization") == BASIC_OK
            return False

        def do_GET(self):
            self.record()
            path = urlsplit(self.path).path
            if path.endswith("/system/status"):
                return self.reply(200, b'{"version":"fixture"}')
            if path.endswith("/config/host"):
                method = state["mode"] if state["host_method"] == "auto" else state["host_method"]
                if method is None:
                    return self.reply(500)
                return self.reply(200, json.dumps({"authenticationMethod": method, "authenticationRequired": "enabled"}).encode())
            if self.signed_in():
                if path == "/arr/":
                    return self.reply(200, b"<html>app</html>")
                return self.reply(200, state["archive"])
            if path.endswith("/command/7"):
                return self.reply(200, b'{"id":7,"status":"completed"}')
            if path.endswith("/system/backup"):
                return self.reply(200, json.dumps([{
                    "id": 8, "type": "manual", "path": "/backup/manual/current.zip",
                }]).encode())
            mode = state["mode"]
            if path == "/arr/":  # the web UI's front page, behind the same login as /backup/
                if mode == "forms":
                    return self.reply(302, Location="/arr/login?returnUrl=%2Farr%2F")
                if mode == "basic":
                    return self.reply(401, WWW_Authenticate='Basic realm="Servarr"')
                if mode == "external":
                    return self.reply(302, Location="https://login.example/login?token=do-not-leak")
                if mode == "down":
                    self.close_connection = True
                    return None
                return self.reply(200, b"<html>app</html>")
            assert path.startswith("/arr/backup/"), path
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
            if self.path == "/arr/login":  # Servarr's AuthenticationController
                form = parse_qs(body.decode())
                if form.get("username") == ["admin"] and form.get("password") == ["s3cret"]:
                    return self.reply(302, Location="/arr/", Set_Cookie="RadarrAuth=ok; path=/; httponly")
                return self.reply(302, Location="/arr/login?returnUrl=&loginFailed=true")
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


def driver(server, **login):
    return ServarrApp("radarr", server[0], "test-api-key", **login)


def logins(state):
    return [r for r in state["requests"] if r[0] == "POST" and r[1] == "/arr/login"]


def sent_credentials(state):
    return [r for r in state["requests"] if "Authorization" in r[2] or "Cookie" in r[2]]


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


@pytest.mark.parametrize("mode", ["forms", "basic"])
def test_protected_backup_without_a_login_asks_for_one_and_preserves_files(server, tmp_path, mode):
    server[1]["mode"] = mode
    instance = driver(server)
    existing = tmp_path / "current.zip"
    existing.write_bytes(b"existing file")
    with pytest.raises(LoginRequiredError) as error:
        instance.backup(tmp_path)
    assert str(error.value) == ("radarr: the app asks for a login to download backups. "
                                "Enter its username and password in Settings.")
    assert error.value.needs_login
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


@pytest.mark.parametrize("mode", ["forms", "basic"])
def test_connection_asks_for_the_apps_login_when_it_wants_one(server, mode):
    """The API key works, but the app asks every address to log in, so no
    backup could be downloaded: Test connection asks for its username and
    password (and the card shows those fields)."""
    server[1]["mode"] = mode
    with pytest.raises(LoginRequiredError) as error:
        driver(server).test_connection()
    assert str(error.value) == "radarr fixture asks for a login. Enter its username and password."
    assert error.value.needs_login
    assert not any(r[1].startswith("/arr/login") for r in server[1]["requests"])
    assert all("Authorization" not in r[2] and "Cookie" not in r[2] for r in server[1]["requests"])


@pytest.mark.parametrize("mode", ["plain", "external", "down"])
def test_connection_passes_when_the_web_ui_isnt_the_apps_own_login(server, mode):
    """Open (External or Disabled for Local Addresses), another host's login
    (a reverse proxy - the backup itself reports that), or no answer at all."""
    server[1]["mode"] = mode
    assert driver(server).test_connection() == "radarr fixture reachable"



@pytest.mark.parametrize("mode", ["forms", "basic"])
def test_signs_in_with_the_apps_login_and_backs_up(server, tmp_path, mode):
    server[1]["mode"] = mode
    instance = driver(server, username="admin", password="s3cret")
    assert instance.test_connection() == "radarr fixture reachable, signed in"
    instance = driver(server, username="admin", password="s3cret")  # a fresh run
    dest = instance.backup(tmp_path)
    assert zipfile.is_zipfile(dest)
    assert len(server[1]["deleted"]) == 1
    requests_ = server[1]["requests"]
    if mode == "forms":
        assert len(logins(server[1])) == 2  # one per run
        assert parse_qs(logins(server[1])[0][3].decode()) == {"username": ["admin"], "password": ["s3cret"]}
        assert not any("Authorization" in r[2] for r in requests_)
    else:
        assert not logins(server[1])
        # Basic goes only to the web UI, never to the API.
        assert all(not r[1].startswith("/arr/api/") for r in requests_ if "Authorization" in r[2])
        assert any(r[1].startswith("/arr/backup/") and r[2].get("Authorization") == BASIC_OK for r in requests_)


@pytest.mark.parametrize("mode", ["forms", "basic"])
def test_login_method_falls_back_to_the_response_without_config_host(server, tmp_path, mode):
    server[1]["mode"] = mode
    server[1]["host_method"] = None
    assert zipfile.is_zipfile(driver(server, username="admin", password="s3cret").backup(tmp_path))


@pytest.mark.parametrize("mode", ["forms", "basic"])
def test_wrong_login_fails_once_without_retrying(server, tmp_path, mode):
    server[1]["mode"] = mode
    with pytest.raises(LoginRequiredError) as error:
        driver(server, username="admin", password="wrong").backup(tmp_path)
    assert str(error.value) == "radarr: wrong username or password. Check them in Settings."
    assert not server[1]["deleted"]
    assert not list(tmp_path.iterdir())
    attempts = logins(server[1]) if mode == "forms" else [r for r in server[1]["requests"] if "Authorization" in r[2]]
    assert len(attempts) == 1  # each refused login is an Auth-Failure in the app's log


@pytest.mark.parametrize("mode", ["forms", "basic"])
def test_connection_reports_a_wrong_login(server, mode):
    server[1]["mode"] = mode
    with pytest.raises(LoginRequiredError, match="wrong username or password"):
        driver(server, username="admin", password="wrong").test_connection()


def test_unsupported_login_method_is_never_tried(server, tmp_path):
    """e.g. Whisparr v2's develop branch adds OIDC, which no password signs in to."""
    server[1]["mode"] = "forms"
    server[1]["host_method"] = "oidc"
    with pytest.raises(ServarrError, match="its oidc login can't be used by Backuparr") as error:
        driver(server, username="admin", password="s3cret").backup(tmp_path)
    assert "Disabled for Local Addresses" in str(error.value)
    assert not logins(server[1]) and not sent_credentials(server[1])


@pytest.mark.parametrize("mode", ["plain", "external", "proxy"])
def test_a_saved_login_is_only_used_when_the_app_itself_asks(server, tmp_path, mode):
    """Open apps (Disabled for Local Addresses) and other hosts' or a proxy's
    login pages never get the username and password."""
    server[1]["mode"] = mode
    instance = driver(server, username="admin", password="s3cret")
    try:
        instance.backup(tmp_path)
    except ServarrError:
        assert mode != "plain"
    assert not logins(server[1]) and not sent_credentials(server[1])
    assert not instance.signed_in
