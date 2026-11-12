"""UniFi Network (#161): the driver against a fake UniFi OS and standalone app,
their replies copied from UniFi OS Server 5.1.42 (Network 10.5.67) and the
standalone app 10.6.109 in the lab, and .unf files made the way UniFi makes
them (a zip, AES-128-CBC encrypted with no padding, so its last bytes are cut
off)."""
import io
import json

import pytest
import requests

import archive_check
import backup
import manifest_backfill
from apps import cert_pin, unifi
from apps.unifi import UnifiApp, UnifiError
from config_store import always_encrypted, app_meta, restore_supported
from unifi_backup import make_unf

USER, PASSWORD = "backuparr", "unifi-secret"
TRUSTED = {"sha256": "cd" * 32, "subject": "unifi.local", "not_after": "2036-11-08T23:17:52+00:00"}
CSRF = "5a30556f-dd1a-4aa5-a3b1-37f18b0f5795"


def reply(request, status, body=b"", headers=None):
    res = requests.Response()
    res.request, res.url, res.status_code = request, request.url, status
    res._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    res.raw = io.BytesIO(res._content)
    res.headers.update(headers or {})
    return res


def ok(data=()):
    return {"meta": {"rc": "ok"}, "data": list(data)}


def error(msg):
    return {"meta": {"rc": "error", "msg": msg}, "data": []}


@pytest.fixture
def server(monkeypatch):
    """UniFi OS by default (state.kind = "standalone" for the old app). One
    session; the login, CSRF and permission rules seen in the lab. Records
    every request."""
    state = type("Server", (), {})()
    state.kind = "unifi-os"
    state.sent, state.signed_in, state.signed_out, state.logins = [], False, False, 0
    state.allowed = True
    state.login_reply = None
    state.unf = make_unf()
    state.download_url = None
    state.days = []

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        path = request.path_url
        body = json.loads(request.body) if request.body else None
        uos = state.kind == "unifi-os"
        if path == "/api/system":
            return reply(request, 200, {"hardware": {"shortname": "UOSSERVER"}, "name": "UOS Server"}) if uos else \
                reply(request, 401, error("api.err.LoginRequired"))
        if path == "/status":
            return reply(request, 200, b"<!doctype html><html></html>", {"Content-Type": "text/html"}) if uos else \
                reply(request, 200, {"meta": {"rc": "ok", "up": True, "server_version": "10.6.109"}, "data": []})
        login_path = "/api/auth/login" if uos else "/api/login"
        if path == login_path and request.method == "POST":
            state.logins += 1
            if state.login_reply:
                return reply(request, *state.login_reply)
            if (body.get("username"), body.get("password")) != (USER, PASSWORD):
                return reply(request, 403, {"message": "Invalid username or password",
                                            "code": "AUTHENTICATION_FAILED_INVALID_CREDENTIALS"}) if uos else \
                    reply(request, 400, error("api.err.Invalid"))
            if uos:
                assert body == {"username": USER, "password": PASSWORD, "token": "", "rememberMe": False}
            state.signed_in = True
            return reply(request, 200, {"username": USER}, {"X-Csrf-Token": CSRF} if uos else {})
        if path == ("/api/auth/logout" if uos else "/api/logout"):
            if uos:
                assert request.headers.get("X-CSRF-Token") == CSRF
            state.signed_in, state.signed_out = False, True
            return reply(request, 200, ok())
        prefix = "/proxy/network" if uos else ""
        assert path.startswith(prefix + "/"), path
        if not state.signed_in:
            return reply(request, 401, {"error": {"code": 401, "message": "Unauthorized"}} if uos else error("api.err.LoginRequired"))
        if uos and request.method == "POST" and request.headers.get("X-CSRF-Token") != CSRF:
            return reply(request, 403, {"error": {"code": 403, "message": "Forbidden"}})
        rest = path[len(prefix):]
        version = "10.5.67" if uos else "10.6.109"
        if rest == "/api/s/default/stat/sysinfo":
            return reply(request, 200, ok([{"version": version, "build": f"atag_{version}_35187"}]))
        if rest == "/api/s/default/cmd/backup":
            if not state.allowed:
                return reply(request, 403, error("api.err.NoPermission"))
            if body["cmd"] == "list-backups":
                return reply(request, 200, ok())
            assert body["cmd"] == "backup"
            state.days.append(body["days"])
            return reply(request, 200, ok([{"url": f"/dl/backup/{version}.unf" if state.download_url is None else state.download_url}]))
        if rest.startswith("/dl/backup/"):
            return reply(request, 200, state.unf, {"Content-Type": "application/octet-stream"}) if state.allowed else \
                reply(request, 403, error("api.err.NoPermission"))
        return reply(request, 404, error("api.err.NotFound"))

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


def make_app(**kwargs):
    return UnifiApp("https://unifi.lab:11443/", kwargs.pop("username", USER), kwargs.pop("password", PASSWORD),
                    trusted_cert=TRUSTED, **kwargs)


def test_the_card_signs_in_is_always_encrypted_restored_by_hand_and_pins_its_certificate():
    app = backup.build_app("unifi-site2", {"url": "https://unifi.lab:11443", "username": USER, "password": PASSWORD,
                                           "history": "30", "trusted_cert": TRUSTED})
    assert isinstance(app, UnifiApp) and app.history == "30"
    meta = app_meta("unifi")
    assert always_encrypted("unifi") and not restore_supported("unifi")
    assert meta["pins_certificate"] and meta["multi_instance"] and meta["no_api_key"]
    assert meta["category"] == "network" and meta["icon"] == "ubiquiti-unifi.svg"
    history = next(f for f in meta["extra_fields"] if f["name"] == "history")
    assert history["default"] == "settings" and {c["value"] for c in history["choices"]} == set(unifi.HISTORY_DAYS)
    steps = meta["manual_restore"]["steps"]
    assert steps and all(group["title"] and group["steps"] for group in steps)


def test_unifi_os_is_found_signed_in_with_its_csrf_token_and_signed_out(server):
    message = make_app().test_connection()
    assert message == "UniFi Network 10.5.67 reachable (UniFi OS), signed in as backuparr with permission to make backups"
    paths = [r.path_url for r in server.sent]
    assert paths == ["/api/system", "/api/auth/login", "/proxy/network/api/s/default/stat/sysinfo",
                     "/proxy/network/api/s/default/cmd/backup", "/api/auth/logout"]
    assert json.loads(server.sent[3].body) == {"cmd": "list-backups"}  # changes nothing
    assert server.signed_out and server.days == []


def test_the_standalone_app_is_found_and_uses_its_own_paths(server):
    server.kind = "standalone"
    message = make_app().test_connection()
    assert message == "UniFi Network 10.6.109 reachable (standalone app), signed in as backuparr with permission to make backups"
    assert [r.path_url for r in server.sent] == ["/api/system", "/status", "/api/login", "/api/s/default/stat/sysinfo",
                                                 "/api/s/default/cmd/backup", "/api/logout"]


@pytest.mark.archive_check
@pytest.mark.parametrize("history, days", [(None, 0), ("settings", 0), ("7", 7), ("30", 30), ("90", 90),
                                           ("365", 365), ("all", -1), ("bogus", 0)])
def test_a_backup_keeps_the_history_chosen(server, tmp_path, history, days):
    app = make_app(history=history)
    path = app.backup(str(tmp_path))
    assert server.days == [days] and server.signed_out
    assert path == str(tmp_path / "10.5.67.unf")
    assert unifi.read_backup(path) == "10.5.67"
    archive_check.check("unifi", path)
    # The version comes from the backup's own sign-in: UniFi OS limits sign-ins.
    assert app.app_version() == "10.5.67" and server.logins == 1


def test_a_standalone_backup(server, tmp_path):
    server.kind, server.unf = "standalone", make_unf("10.6.109")
    app = make_app(history="all")
    assert unifi.read_backup(app.backup(str(tmp_path))) == "10.6.109"
    assert server.days == [-1] and "/dl/backup/10.6.109.unf" in [r.path_url for r in server.sent]


def test_the_version_alone_signs_in_once(server):
    assert make_app().app_version() == "10.5.67"
    assert server.logins == 1 and server.signed_out


@pytest.mark.parametrize("kind, fragment", [
    ("unifi-os", r"username or password is wrong \(UniFi limits sign-ins"),
    ("standalone", "username or password is wrong$"),
])
def test_a_wrong_password_is_said_and_never_retried(server, kind, fragment):
    server.kind = kind
    with pytest.raises(UnifiError, match=fragment):
        make_app(password="wrong").test_connection()
    assert server.logins == 1 and not server.signed_out


def test_unifi_os_limiting_sign_ins_is_said_plainly(server):
    server.login_reply = (429, {"message": "You've reached the login attempt limit", "code": "AUTHENTICATION_FAILED_LIMIT_REACHED"})
    with pytest.raises(UnifiError) as raised:
        make_app().test_connection()
    assert str(raised.value) == "unifi: UniFi is limiting sign-ins. Wait a minute and try again."
    assert server.logins == 1


def test_an_account_with_two_factor_sign_in_is_pointed_to_a_local_admin(server):
    server.login_reply = (499, {"code": "MFA_AUTH_REQUIRED", "message": "MFA required"})
    with pytest.raises(UnifiError, match="two-factor authentication - use a local admin"):
        make_app().test_connection()


def test_no_login_is_explained_without_contacting_unifi(server):
    with pytest.raises(UnifiError, match="enter the username and password of a local admin"):
        UnifiApp("https://unifi.lab", "", "").test_connection()
    assert server.sent == []


@pytest.mark.parametrize("kind", ["unifi-os", "standalone"])
def test_a_user_without_backup_rights_is_told_which_role_it_needs(server, kind):
    server.kind, server.allowed = kind, False
    with pytest.raises(UnifiError, match="on UniFi OS give it Network: Full Management; in the standalone app use the admin made during its setup"):
        make_app().test_connection()
    assert server.signed_out


def test_a_redirect_is_never_followed(server, monkeypatch, tmp_path):
    # A Hotspot Operator's download is sent to http://127.0.0.1/manage/hotspot (lab).
    send = requests.adapters.HTTPAdapter.send

    def redirecting(adapter, request, **kwargs):
        if request.path_url.startswith("/proxy/network/dl/"):
            server.sent.append(request.copy())
            return reply(request, 302, b"", {"Location": "http://127.0.0.1/manage/hotspot"})
        return send(adapter, request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", redirecting)
    with pytest.raises(UnifiError, match="the URL redirects"):
        make_app().backup(str(tmp_path))
    assert not any("manage/hotspot" in r.url for r in server.sent) and server.signed_out


def test_a_url_that_isnt_unifi_is_refused_before_signing_in(monkeypatch):
    sent = []

    def send(_adapter, request, **kwargs):
        sent.append(request.path_url)
        return reply(request, 404, b"<html>Not found</html>")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(UnifiError, match="no UniFi Network at this URL"):
        make_app().test_connection()
    assert sent == ["/api/system", "/status"]


@pytest.mark.parametrize("url", ["/dl/backup/../../etc/passwd", "https://elsewhere/x.unf", "/dl/backup/x.zip", ""])
def test_only_a_backup_path_unifi_named_is_downloaded(server, tmp_path, url):
    server.download_url = url
    with pytest.raises(UnifiError, match="didn't say where its backup is"):
        make_app().backup(str(tmp_path))
    assert not any("/dl/" in r.path_url for r in server.sent)


@pytest.mark.parametrize("pad", range(16))
def test_a_unf_is_read_however_much_of_its_zip_end_was_cut(tmp_path, pad):
    path = tmp_path / "10.5.67.unf"
    path.write_bytes(make_unf(pad=pad))
    assert unifi.read_backup(str(path)) == "10.5.67"
    assert [p.name for p in tmp_path.iterdir()] == ["10.5.67.unf"]  # its decrypted copy is gone


@pytest.mark.archive_check
@pytest.mark.parametrize("data, fragment", [
    (b"", "isn't whole AES blocks"),
    (b"x" * 17, "isn't whole AES blocks"),
    (b"<html>Login</html>".ljust(32), "has no zip directory"),
    (make_unf(members={"db.gz": None}), "db.gz is missing"),
    (make_unf(members={"db.gz": b"plain bytes"}), "isn't gzip"),
])
def test_a_file_that_isnt_a_whole_backup_is_refused(tmp_path, data, fragment):
    path = tmp_path / "x.unf"
    path.write_bytes(data)
    with pytest.raises(UnifiError, match=fragment):
        unifi.read_backup(str(path))
    with pytest.raises(archive_check.ArchiveCheckError):
        archive_check.check("unifi", str(path))


def test_a_damaged_unf_is_refused(tmp_path):
    data = bytearray(make_unf(pad=40))
    data[80:96] = bytes(16)  # one block of the compressed data
    path = tmp_path / "x.unf"
    path.write_bytes(bytes(data))
    with pytest.raises(UnifiError, match="damaged"):
        unifi.read_backup(str(path))


def test_the_backfill_reads_the_version_inside_a_backup(tmp_path):
    path = tmp_path / "unifi_20261111_120000.unf"
    path.write_bytes(make_unf("10.6.109"))
    assert manifest_backfill.read_version("unifi", str(path)) == "10.6.109"


def test_the_login_is_encrypted_at_rest_and_the_history_checked(authed_client, isolated_webui):
    card = {"enabled": True, "url": "https://unifi.lab:11443", "username": USER, "password": PASSWORD,
            "history": "90", "trusted_cert": TRUSTED}
    assert authed_client.post("/api/config", json={"apps": {"unifi": card}}).status_code == 200
    saved = authed_client.get("/api/config").get_json()["apps"]["unifi"]
    assert saved["trusted_cert"] == TRUSTED and saved["password"] == PASSWORD and saved["history"] == "90"
    with open(isolated_webui.auth.CONFIG_PATH) as f:
        assert PASSWORD not in f.read()
    bad = authed_client.post("/api/config", json={"apps": {"unifi": {**card, "history": "12"}}})
    assert bad.status_code == 400 and "history must be one of" in bad.get_json()["error"]
    missing = authed_client.post("/api/config", json={"apps": {"unifi": {**card, "password": ""}}})
    assert missing.status_code == 400 and "Password is required" in missing.get_json()["error"]


def test_the_trust_route_accepts_unifi_and_names_it(authed_client, monkeypatch):
    presented = {**TRUSTED, "issuer": TRUSTED["subject"], "self_signed": True}
    monkeypatch.setattr(cert_pin, "fetch_certificate", lambda url: b"der")
    monkeypatch.setattr(cert_pin, "describe", lambda der: presented)
    body = authed_client.post("/api/trust-certificate/unifi", json={"url": "https://unifi.lab:11443", "sha256": TRUSTED["sha256"]}).get_json()
    assert body == {"ok": True, "trusted_cert": TRUSTED}
    assert str(cert_pin.CertificateUntrusted("unifi", None)) == (
        "unifi: UniFi's certificate isn't trusted. Check it and trust it in Settings to back it up.")
