"""Unraid (#152): the driver against a fake web UI that answers as Unraid
7.4.0 and 7.3.5 did in the lab (login.php, server-state.php, FlashBackup.php
on 7.4, Download.php's backup/unlink before it)."""
import io
import json
import logging
import urllib.parse
import zipfile

import pytest
import requests

import backup
import manifest_backfill
from apps import cert_pin
from apps.unraid import UnraidApp, UnraidError, backup_version, read_backup
from config_store import always_encrypted, restore_supported

PASSWORD = "unraid-secret"
API_KEY = "unraid-api-key-must-not-leak"
CSRF = "0123456789ABCDEF"
TRUSTED = {"sha256": "cd" * 32, "subject": "Backuparr-Test", "not_after": "2036-11-08T20:54:00+00:00"}
LOGIN_PAGE = '<form action="/login" method="POST"><input name="username"><input name="password" type="password"></form>'


def boot_zip(version="7.4.0", extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("changes.txt", f"# Version {version} 2026-11-09\n\n## Changes\n")
        zf.writestr("bzimage", b"\x00kernel" * 100)
        zf.writestr("config/super.dat", b"\x00" * 64)
        zf.writestr("config/shadow", "root:$6$hash:20403::::::\n")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def server(monkeypatch):
    """An Unraid web UI: `version` "7.4.0" streams FlashBackup.php, anything
    else makes the zip through Download.php. Records every request."""
    state = type("Server", (), {})()
    state.sent, state.signed_in, state.signed_out = [], False, False
    state.version = "7.4.0"
    state.zip = boot_zip()
    state.fails = 0
    state.locked = False
    state.no_password = False
    state.no_space = False
    state.on_server = {}  # what cmd=backup left on the server
    state.unlinked = []
    state.download_status = 200

    def reply(request, status, body=b"", headers=None):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = body if isinstance(body, bytes) else body.encode()
        res.raw = io.BytesIO(res._content)
        res.headers.update(headers or {})
        return res

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        path = request.path_url.split("?")[0]
        form = dict(urllib.parse.parse_qsl(request.body or "")) if request.method == "POST" else {}
        if path == "/login" and request.method == "POST":
            if state.no_password:
                return reply(request, 200, '<input id="confirmPassword" name="confirmPassword" type="password">')
            if state.locked or state.fails >= 3:
                return reply(request, 200, LOGIN_PAGE + "Too many previous login attempts.")
            if (form.get("username"), form.get("password")) == ("root", PASSWORD):
                state.signed_in = True
                return reply(request, 302, "", {"Location": "/Main"})
            state.fails += 1
            return reply(request, 200, LOGIN_PAGE + "Invalid username or password")
        if not state.signed_in:
            return reply(request, 302, "", {"Location": "/login"})
        if path == "/logout":
            state.signed_in, state.signed_out = False, True
            return reply(request, 302, "", {"Location": "/login"})
        if path == "/plugins/dynamix.my.servers/data/server-state.php":
            return reply(request, 200, json.dumps({"osVersion": state.version, "csrf": CSRF, "apiKey": API_KEY,
                                                   "name": "Backuparr-Test"}), {"Content-Type": "application/json"})
        if path == "/webGui/include/FlashBackup.php" and state.version >= "7.4":
            name = f"backuparr-test-v{state.version}-boot-backup-20261111-2118.zip"
            return reply(request, 200, state.zip, {"Content-Type": "application/zip",
                                                    "Content-Disposition": f'attachment; filename="{name}"'})
        if path == "/webGui/include/Download.php" and request.method == "POST":
            assert form.get("csrf_token") == CSRF
            if form.get("cmd") == "backup":
                if state.no_space:
                    return reply(request, 200, "")
                name = f"backuparr-test-v{state.version}-boot-backup-20261111-2345.zip"
                state.on_server[name] = state.zip
                return reply(request, 200, name)
            if form.get("cmd") == "unlink":
                state.unlinked.append(form["file"])
                state.on_server.pop(form["file"], None)
                return reply(request, 200, "")
        if path.lstrip("/") in state.on_server:
            if state.download_status != 200:
                return reply(request, state.download_status, "<html>error</html>", {"Content-Type": "text/html"})
            return reply(request, 200, state.on_server[path.lstrip("/")], {"Content-Type": "application/zip"})
        return reply(request, 404, "<html>404 Not Found</html>", {"Content-Type": "text/html"})

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


@pytest.fixture
def app():
    return UnraidApp("http://tower.lab/", password=PASSWORD)


def posts(server, path):
    return [r for r in server.sent if r.method == "POST" and r.path_url.startswith(path)]


def test_the_card_is_always_encrypted_restored_by_hand_and_pins_its_certificate():
    app = backup.build_app("unraid", {"url": "https://tower.lab", "password": PASSWORD, "trusted_cert": TRUSTED})
    assert isinstance(app, UnraidApp)
    assert isinstance(app.session.get_adapter("https://tower.lab/"), cert_pin.PinnedAdapter)
    assert always_encrypted("unraid") and not restore_supported("unraid")


def test_connection_signs_in_as_root_reads_the_version_and_signs_out(app, server):
    assert app.test_connection() == "unraid 7.4.0 reachable, signed in as root"
    login = posts(server, "/login")[0]
    assert dict(urllib.parse.parse_qsl(login.body)) == {"username": "root", "password": PASSWORD}
    assert PASSWORD not in login.url and server.signed_out


@pytest.mark.parametrize("version", ["7.4.0", "7.3.5"])
def test_backup_keeps_unraids_zip_as_made_and_signs_out(app, server, tmp_path, version):
    server.version, server.zip = version, boot_zip(version)
    path = app.backup(str(tmp_path))
    with open(path, "rb") as f:
        assert f.read() == server.zip
    assert read_backup(path) == version and server.signed_out
    # The version came with the backup: the manifest needs no second sign-in.
    logins = len(posts(server, "/login"))
    assert app.app_version() == version and len(posts(server, "/login")) == logins


def test_before_7_4_the_servers_copy_is_made_with_the_csrf_token_and_removed(app, server, tmp_path):
    server.version = "7.3.5"
    app.backup(str(tmp_path))
    commands = [dict(urllib.parse.parse_qsl(r.body))["cmd"] for r in posts(server, "/webGui/include/Download.php")]
    assert commands == ["backup", "unlink"] and server.on_server == {}


def test_before_7_4_the_servers_copy_is_removed_even_when_the_download_fails(app, server, tmp_path):
    server.version, server.download_status = "7.3.5", 500
    with pytest.raises(UnraidError, match="didn't send the backup"):
        app.backup(str(tmp_path))
    assert server.on_server == {} and server.signed_out


def test_no_room_for_the_zip_on_the_server_is_explained(app, server, tmp_path):
    server.version, server.no_space = "7.3.5", True
    with pytest.raises(UnraidError, match=r"needs free space for it \(about 1.5 times"):
        app.backup(str(tmp_path))
    assert server.signed_out


def test_a_wrong_password_is_said_once_and_not_retried(server):
    with pytest.raises(UnraidError, match="root password is wrong.*15 minutes after 3 failures"):
        UnraidApp("http://tower.lab", password="wrong").test_connection()
    assert len(posts(server, "/login")) == 1


def test_unraid_ignoring_sign_ins_says_to_wait(server):
    server.locked = True
    with pytest.raises(UnraidError, match="ignoring sign-ins from Backuparr after 3 failed ones - wait 15 minutes"):
        UnraidApp("http://tower.lab", password=PASSWORD).test_connection()


def test_a_server_without_a_root_password_yet_says_to_set_one(app, server):
    server.no_password = True
    with pytest.raises(UnraidError, match="set Unraid's root password first"):
        app.test_connection()


def test_no_password_is_explained_without_contacting_unraid(server):
    with pytest.raises(UnraidError, match="enter the root password"):
        UnraidApp("http://tower.lab").test_connection()
    assert server.sent == []


def test_something_else_at_the_url_is_reported(server, monkeypatch):
    def other(_adapter, request, **kwargs):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, 404
        res._content, res.raw = b"nope", io.BytesIO(b"nope")
        return res
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", other)
    with pytest.raises(UnraidError, match="no Unraid web UI at this URL"):
        UnraidApp("http://tower.lab", password=PASSWORD).test_connection()


def test_a_redirect_to_another_host_is_refused_without_naming_it(server, monkeypatch):
    def away(_adapter, request, **kwargs):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, 302
        res._content, res.raw = b"", io.BytesIO(b"")
        res.headers["Location"] = "https://abc123.myunraid.net/login"
        return res
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", away)
    with pytest.raises(UnraidError) as raised:
        UnraidApp("http://tower.lab", password=PASSWORD).test_connection()
    assert "redirects to another address" in str(raised.value) and "myunraid" not in str(raised.value)


def test_the_server_states_api_key_is_never_logged(app, server, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app.test_connection()
    app.backup(str(tmp_path))
    assert API_KEY not in caplog.text and PASSWORD not in caplog.text and CSRF not in caplog.text


@pytest.mark.parametrize("members, fragment", [
    ({"config/super.dat": b"x"}, r"no changes.txt"),
    ({"changes.txt": "# Version 7.4.0\n"}, r"no config/super.dat"),
])
def test_a_zip_without_unraids_files_fails_its_check(tmp_path, members, fragment):
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    with pytest.raises(UnraidError, match=fragment):
        read_backup(str(path))


def test_a_damaged_or_cut_short_zip_fails_its_check(tmp_path):
    data = bytearray(boot_zip(extra={"config/ident.cfg": "NAME=Tower\n" * 200}))
    cut = tmp_path / "cut.zip"
    cut.write_bytes(bytes(data[: len(data) // 2]))
    with pytest.raises(UnraidError, match="isn't a whole zip"):
        read_backup(str(cut))
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:  # stored, so a byte can be flipped
        zf.writestr("changes.txt", "# Version 7.4.0\n")
        zf.writestr("config/super.dat", b"A" * 4096)
    damaged = bytearray(plain.getvalue())
    damaged[damaged.index(b"A" * 64)] ^= 0xFF
    path = tmp_path / "damaged.zip"
    path.write_bytes(bytes(damaged))
    with pytest.raises(UnraidError, match="config/super.dat is damaged"):
        read_backup(str(path))


def test_the_version_backfill_reads_changes_txt(tmp_path):
    path = tmp_path / "unraid_20261111_031500.zip"
    path.write_bytes(boot_zip("7.3.5"))
    assert backup_version(str(path)) == "7.3.5"
    assert manifest_backfill.read_version("unraid", str(path)) == "7.3.5"


def test_the_root_password_is_encrypted_at_rest(authed_client, isolated_webui):
    card = {"enabled": True, "url": "http://tower.lab", "password": PASSWORD}
    assert authed_client.post("/api/config", json={"apps": {"unraid": card}}).status_code == 200
    assert authed_client.get("/api/config").get_json()["apps"]["unraid"]["password"] == PASSWORD
    with open(isolated_webui.auth.CONFIG_PATH) as f:
        assert PASSWORD not in f.read()
    missing = authed_client.post("/api/config", json={"apps": {"unraid": {**card, "password": ""}}})
    assert missing.status_code == 400 and "Root password is required" in missing.get_json()["error"]
