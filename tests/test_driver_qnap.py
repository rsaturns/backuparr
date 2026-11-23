"""QNAP QuTS hero (#169): the driver against a fake NAS that answers as QuTS
hero h5.2.11 did when tested - authLogin.cgi (passwords, 2-step
verification codes and remembered sign-ins), sysRequest.cgi firm_update,
the Control Panel's /qmigrate/v1/backup/ calls and authLogout.cgi."""
import base64
import io
import json
import logging
import urllib.parse

import pytest
import requests
from qnap_backup import make_bin

import backup
import manifest_backfill
from apps import cert_pin
from apps.qnap import NEW_CODE, CodeRequiredError, QnapApp, QnapError, check_backup, parse_version
from config_store import always_encrypted, restore_supported

USER, PASSWORD = "backuparr", "qnap-secret"
SID = "sidneverinaurlmessage"
TOKEN = "0123456789abcdef0123456789abcdef"
MODEL = "MODEL-MUST-NOT-LEAK"
TRUSTED = {"sha256": "ab" * 32, "subject": "qnap", "not_after": "2036-11-08T20:54:00+00:00"}


def cdata(value):
    return f"<![CDATA[{value}]]>"


@pytest.fixture
def nas(monkeypatch):
    """A QuTS hero NAS at http://nas.lab:8080. Records every request."""
    state = type("NAS", (), {})()
    state.sent, state.logins, state.logouts = [], [], []
    state.admin, state.roles = True, []
    state.code = None  # the current 2-step code when the account has 2-step verification
    state.tokens = set()  # remembered sign-ins the NAS accepts
    state.os_name, state.version = "QuTS hero", "h5.2.11"
    state.no_qmigrate = False
    state.bin = make_bin()
    state.downloads = 0
    state.download_error = None

    def reply(request, status, body=b"", content_type="text/xml"):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = body if isinstance(body, bytes) else body.encode()
        res.raw = io.BytesIO(res._content)
        res.headers["Content-Type"] = content_type
        return res

    def xml(request, inner):
        return reply(request, 200, f'<?xml version="1.0" encoding="UTF-8" ?>\n<QDocRoot version="1.0">{inner}</QDocRoot>')

    def api(request, code, message="", data=None):
        body = {"error_code": code, "error_message": message, **({"data": data} if data is not None else {})}
        return reply(request, 200, json.dumps(body, indent=4), 'application/json; charset="UTF-8"')

    def signed_in(request, form):
        return form.get("sid") == SID

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        url = urllib.parse.urlsplit(request.url)
        path, query = url.path, dict(urllib.parse.parse_qsl(url.query))
        form = dict(urllib.parse.parse_qsl(request.body or "")) if request.method == "POST" else {}
        if path == "/cgi-bin/authLogin.cgi":
            assert request.method == "POST" and not url.query  # the password never goes in a URL
            state.logins.append(form)
            if form.get("qtoken"):
                if form["user"] != USER or form["qtoken"] not in state.tokens:
                    return xml(request, f"<authPassed>{cdata(0)}</authPassed><errorValue>{cdata(-1)}</errorValue>")
            else:
                if form.get("user") != USER or base64.b64decode(form.get("pwd", "")).decode() != PASSWORD:
                    return xml(request, f"<authPassed>{cdata(0)}</authPassed><errorValue>{cdata(-1)}</errorValue>")
                if state.code and form.get("security_code") != state.code:
                    return xml(request, f"<authPassed>{cdata(0)}</authPassed><need_2sv>{cdata(1)}</need_2sv>"
                                        f"<lost_phone>{cdata(1)}</lost_phone>")
            extra = ""
            if state.code and form.get("security_code") and form.get("remme") == "1":
                state.tokens.add(TOKEN)
                extra = f"<qtoken>{cdata(TOKEN)}</qtoken>"
            roles = "".join(f"<role><name>{cdata('r')}</name><id>{cdata(r)}</id></role>" for r in state.roles)
            return xml(request, f"{extra}<authPassed>{cdata(1)}</authPassed><authSid>{cdata(SID)}</authSid>"
                                f"<isAdmin>{cdata(int(state.admin))}</isAdmin><username>{cdata(USER)}</username>"
                                f"<role_delegation>{roles}</role_delegation>")
        if path == "/cgi-bin/authLogout.cgi":
            assert "del_client" not in query and "qtoken" not in query  # keeps a remembered sign-in
            state.logouts.append(query.get("sid"))
            return xml(request, f"<authPassed>{cdata(1)}</authPassed>")
        allowed = state.admin or "1" in state.roles
        if path == "/cgi-bin/sys/sysRequest.cgi":
            assert form.get("subfunc") == "firm_update" and not url.query
            if not signed_in(request, form) or not allowed:
                return xml(request, f"<authPassed>{cdata(0)}</authPassed>")
            return xml(request, f"<authPassed>{cdata(1)}</authPassed><model><modelName>{cdata(MODEL)}</modelName></model>"
                                f"<firmware><name>{cdata(state.os_name)}</name><version>{cdata(state.version)}</version>"
                                f"<number>{cdata(3661)}</number><build>{cdata(20261023)}</build>"
                                f"<patch>{cdata(0)}</patch></firmware>")
        if path.startswith("/qmigrate/"):
            if state.no_qmigrate:
                return reply(request, 404, "<html>404</html>", "text/html")
            if query.get("sid") != SID:
                return api(request, -2, "Authentication Fail")
            if not allowed:
                return api(request, -3, "Permission Denied")
            if path == "/qmigrate/v1/backup/schedule":
                return api(request, 0, "Success", {"enable": False, "backup_path": "", "frequency": "daily"})
            if path == "/qmigrate/v1/backup/download":
                if state.download_error:
                    return api(request, state.download_error, "Failed")
                state.downloads += 1
                res = reply(request, 200, state.bin, "application/x-compressed")
                res.headers["Content-Disposition"] = "attachment; filename=NAS_settings_2026-11-12.bin"
                return res
        return reply(request, 404, "<html>404</html>", "text/html")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


def app(**kwargs):
    return QnapApp("http://nas.lab:8080/", **{"username": USER, "password": PASSWORD, **kwargs})


def paths(nas):
    return [urllib.parse.urlsplit(r.url).path for r in nas.sent]


# ------------------------------------------------------------- the card ----
def test_the_card_is_always_encrypted_restored_by_hand_and_pins_its_certificate():
    built = backup.build_app("qnap", {"url": "https://nas.lab", "username": USER, "password": PASSWORD,
                                      "device_token": TOKEN, "trusted_cert": TRUSTED})
    assert isinstance(built, QnapApp) and built.device_token == TOKEN and not built.otp_code
    assert isinstance(built.session.get_adapter("https://nas.lab/"), cert_pin.PinnedAdapter)
    assert always_encrypted("qnap") and not restore_supported("qnap")


# ---------------------------------------------------- test and backup ----
def test_connection_reads_the_version_and_the_backup_settings_and_signs_out(nas):
    assert app().test_connection() == (f"qnap h5.2.11.3661 build 20261023 reachable, signed in as {USER}, "
                                       "who can export its settings")
    assert paths(nas) == ["/cgi-bin/authLogin.cgi", "/cgi-bin/sys/sysRequest.cgi", "/qmigrate/v1/backup/schedule",
                          "/cgi-bin/authLogout.cgi"]
    login = nas.logins[0]
    assert login["serviceKey"] == "1" and "security_code" not in login and "qtoken" not in login
    assert nas.logouts == [SID] and not nas.downloads  # a test never exports


def test_backup_downloads_the_nas_file_as_made_and_signs_out(nas, tmp_path):
    driver = app()
    path = driver.backup(str(tmp_path))
    with open(path, "rb") as f:
        assert f.read() == nas.bin
    assert path.endswith("qnap-settings.bin") and backup.archive_suffix(path) == ".bin"
    assert paths(nas)[-2:] == ["/qmigrate/v1/backup/download", "/cgi-bin/authLogout.cgi"]
    # The version came with the backup: the manifest needs no second sign-in.
    assert driver.app_version() == "h5.2.11.3661 build 20261023" and len(nas.logins) == 1


def test_the_session_id_and_password_never_reach_a_message_or_the_log(nas, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="backuparr")
    driver = app()
    driver.backup(str(tmp_path))
    assert MODEL not in json.dumps(vars(driver), default=str)
    assert PASSWORD not in caplog.text and SID not in caplog.text

    def refuse(_adapter, request, **kwargs):
        raise requests.exceptions.ConnectionError(f"Max retries exceeded with url: {request.url}")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    with pytest.raises(requests.exceptions.ConnectionError) as error:
        app().backup(str(tmp_path))
    assert "nas.lab" not in str(error.value) and SID not in str(error.value) and error.value.__cause__ is None
    assert error.value.__suppress_context__


def test_an_account_with_the_system_management_role_is_enough(nas, tmp_path):
    nas.admin, nas.roles = False, ["1", "2", "3", "4", "5", "6", "7", "8"]
    app().test_connection()
    app().backup(str(tmp_path))


@pytest.mark.parametrize("roles", [[], ["7", "8"]])
def test_an_account_without_the_right_is_told_what_it_needs_and_signed_out(nas, tmp_path, roles):
    nas.admin, nas.roles = False, roles
    for run in (app().test_connection, lambda: app().backup(str(tmp_path))):
        with pytest.raises(QnapError, match="administrator, or an account with the System Management role"):
            run()
    assert nas.logouts == [SID, SID] and not nas.downloads


def test_a_wrong_password_is_said_once_and_not_retried(nas):
    with pytest.raises(QnapError, match="refused the username or password") as error:
        app(password="nope").test_connection()
    assert len(nas.logins) == 1 and not getattr(error.value, "needs_login", False)
    assert "nas.lab" not in str(error.value)


def test_no_login_is_explained_without_signing_in(nas):
    with pytest.raises(QnapError, match="enter the username and password"):
        app(password="").test_connection()
    assert not nas.logins


@pytest.mark.parametrize("os_name, version, text", [
    ("QTS", "5.2.7", "runs QTS - Backuparr backs up QuTS hero h5.2 or later for now"),
    ("QuTS hero", "h5.1.8", "needs QuTS hero h5.2 or later - this NAS runs h5.1.8.3661 build 20261023"),
])
def test_another_os_or_an_older_version_is_explained(nas, os_name, version, text):
    nas.os_name, nas.version = os_name, version
    with pytest.raises(QnapError, match=text):
        app().test_connection()
    assert nas.logouts == [SID]


def test_newer_versions_are_accepted(nas):
    nas.version = "h6.0.0"
    assert "h6.0.0.3661" in app().test_connection()


def test_a_nas_without_the_export_is_explained(nas):
    nas.no_qmigrate = True
    with pytest.raises(QnapError, match="no settings export"):
        app().test_connection()


def test_a_download_the_nas_refuses_is_reported_not_saved(nas, tmp_path):
    nas.download_error = -9
    with pytest.raises(QnapError, match=r"export the settings \(error -9\)"):
        app().backup(str(tmp_path))
    assert not (tmp_path / "qnap-settings.bin").exists() and nas.logouts == [SID]


def test_a_download_that_isnt_qnaps_file_fails_the_backup(nas, tmp_path):
    nas.bin = b"<html>" + b"x" * 4096
    with pytest.raises(QnapError, match="no QNAP footer"):
        app().backup(str(tmp_path))


def test_something_else_at_the_url_is_reported(monkeypatch):
    def send(_adapter, request, **kwargs):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, 200
        res._content = b"<html>router</html>"
        res.raw = io.BytesIO(res._content)
        return res
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(QnapError, match="didn't answer like a QNAP NAS"):
        app().test_connection()


def test_a_redirect_is_refused_without_naming_it(monkeypatch):
    def send(_adapter, request, **kwargs):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, 302
        res.headers["Location"] = "https://elsewhere.example/cgi-bin/"
        res._content, res.raw = b"", io.BytesIO(b"")
        return res
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(QnapError, match="redirects") as error:
        app().test_connection()
    assert "elsewhere" not in str(error.value)


# ------------------------------------------------------------- 2FA ----
def test_an_account_with_2fa_asks_for_a_code_on_the_card(nas):
    nas.code = "123456"
    with pytest.raises(CodeRequiredError, match="asks for a 2FA code") as error:
        app().test_connection()
    assert error.value.needs_login and len(nas.logins) == 1


def test_a_code_signs_in_once_and_qnap_remembers_the_sign_in_for_the_card(nas):
    nas.code = "123456"
    driver = app(otp_code=" 123 456 ")
    assert driver.test_connection().endswith(". 2FA: the sign-in is remembered - Save to keep it")
    login = nas.logins[0]
    assert (login["security_code"], login["remme"]) == ("123456", "1")
    assert driver.card_info == {"device_token": TOKEN}


def test_the_saved_token_signs_in_without_the_password_or_a_code(nas, tmp_path):
    nas.code, nas.tokens = "123456", {TOKEN}
    driver = app(device_token=TOKEN)
    driver.backup(str(tmp_path))
    login = nas.logins[0]
    assert login["qtoken"] == TOKEN and "pwd" not in login and "security_code" not in login
    assert driver.card_info is None


def test_a_token_qnap_no_longer_accepts_asks_for_a_new_code(nas, tmp_path):
    nas.code = "123456"
    with pytest.raises(CodeRequiredError) as error:
        app(device_token=TOKEN).backup(str(tmp_path))
    assert str(error.value) == f"qnap: {NEW_CODE}" and error.value.needs_login
    assert len(nas.logins) == 1 and not nas.downloads


def test_a_wrong_code_says_so(nas):
    nas.code = "123456"
    with pytest.raises(CodeRequiredError, match="wrong or expired"):
        app(otp_code="000000").test_connection()


# ---------------------------------------------------------- the file ----
@pytest.mark.parametrize("data, fragment", [
    (b"\0" * 100, "only 100 bytes"),
    (make_bin()[:-1], "no QNAP footer"),
    (make_bin(encrypted=0), "doesn't fit"),
    (make_bin(size=4096, encrypted=5000), "doesn't fit"),
])
def test_a_file_without_qnaps_footer_fails_its_check(tmp_path, data, fragment):
    path = tmp_path / "x.bin"
    path.write_bytes(data)
    with pytest.raises(QnapError, match=fragment):
        check_backup(str(path))


def test_the_pre_upload_check_takes_qnaps_file(tmp_path):
    import archive_check
    path = tmp_path / "qnap-settings.bin"
    path.write_bytes(make_bin(size=2 * 1024 * 1024, encrypted=1024 * 1024))
    archive_check._check("qnap", str(path))
    path.write_bytes(b"PK\x03\x04" + b"\0" * 2000)
    with pytest.raises(archive_check.ArchiveCheckError, match="no QNAP footer"):
        archive_check._check("qnap", str(path))


def test_the_file_holds_no_version_for_the_backfill(tmp_path):
    path = tmp_path / "qnap_20261112_030000.bin"
    path.write_bytes(make_bin())
    assert manifest_backfill.read_version("qnap", str(path)) is None


@pytest.mark.parametrize("fields, expected", [
    (("QuTS hero", "h5.2.11", "3661", "20261023"), ("QuTS hero", "h5.2.11.3661 build 20261023")),
    (("QTS", "5.2.7", "", ""), ("QTS", "5.2.7")),
    (("QuTS hero", "h5.2.11", "x;rm", "<b>"), ("QuTS hero", "h5.2.11")),
    (("QuTS hero", "", "3661", "20261023"), None),
    (("QuTS hero", "five", "", ""), None),
])
def test_the_version_is_read_from_qnaps_fields(fields, expected):
    assert parse_version(*fields) == expected


# -------------------------------------------------------- Settings ----
def test_the_2fa_code_is_never_saved_and_the_token_is_encrypted_at_rest(authed_client, isolated_webui):
    card = {"enabled": True, "url": "https://nas.lab", "username": USER, "password": PASSWORD,
            "otp_code": "123456", "device_token": TOKEN}
    assert authed_client.post("/api/config", json={"apps": {"qnap": card}}).status_code == 200
    saved = authed_client.get("/api/config").get_json()["apps"]["qnap"]
    assert saved["device_token"] == TOKEN and saved["password"] == PASSWORD and "otp_code" not in saved
    with open(isolated_webui.auth.CONFIG_PATH) as f:
        text = f.read()
    assert TOKEN not in text and PASSWORD not in text and "123456" not in text


def test_test_connection_hands_the_card_the_token_and_shows_the_code_field(authed_client, isolated_webui,
                                                                            monkeypatch):
    class FakeApp:
        def __init__(self, code):
            self.card_info = {"device_token": TOKEN} if code else None
            self.code = code

        def test_connection(self):
            if not self.code:
                raise CodeRequiredError("qnap: the NAS asks for a 2FA code")
            return "qnap h5.2.11.3661 build 20261023 reachable"

    monkeypatch.setattr(isolated_webui.settings, "build_app", lambda name, cfg: FakeApp(cfg.get("otp_code")))
    card = {"url": "https://nas.lab", "username": USER, "password": PASSWORD}
    asked = authed_client.post("/api/test/qnap", json=card).get_json()
    assert asked["ok"] is False and asked["needs_login"] is True
    done = authed_client.post("/api/test/qnap", json={**card, "otp_code": "123456"}).get_json()
    assert done["ok"] is True and done["card"] == {"device_token": TOKEN}
    assert done["message"].startswith("QNAP QuTS hero h5.2.11")
