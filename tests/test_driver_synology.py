"""Synology (#168): the driver against a fake DSM that answers as DSM 6.2.4
and 7.2.2 did in the lab - SYNO.API.Info, SYNO.API.Auth (2FA and device
tokens as Synology's DSM Login Web API Guide documents them), SYNO.DSM.Info
and the Control Panel's SYNO.Backup.Config.Backup calls."""
import io
import json
import logging
import urllib.parse

import pytest
import requests
from synology_backup import config_info, make_dss

import backup
import manifest_backfill
from apps import cert_pin, synology
from apps.synology import NEW_CODE, CodeRequiredError, SynologyApp, SynologyError, backup_version, read_backup
from config_store import always_encrypted, restore_supported

USER, PASSWORD = "backuparr", "dsm-secret"
SID = "sid-must-never-be-in-a-url"
DID = "device-token-" + "A" * 73
SERIAL = "SERIAL-MUST-NOT-LEAK"
TRUSTED = {"sha256": "ab" * 32, "subject": "synology", "not_after": "2036-11-08T20:54:00+00:00"}


@pytest.fixture
def dsm(monkeypatch):
    """A DSM at http://nas.lab:5000. `auth_path` "auth.cgi" answers as DSM 6,
    "entry.cgi" as DSM 7. Records every request."""
    state = type("DSM", (), {})()
    state.sent, state.logins, state.logouts = [], [], 0
    state.auth_path = "entry.cgi"
    state.version_string = "DSM 7.2.2-72806 Update 1"
    state.admin = True
    state.otp = None  # the current code when the account has 2FA
    state.second_steps = [{"type": "otp"}]
    state.trusted = set()  # device tokens DSM accepts
    state.polls_left = 1
    state.dss = make_dss()
    state.downloaded = False
    state.download_json = None
    state.no_backup_api = False

    def reply(request, status, body=b"", content_type="application/json"):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = body if isinstance(body, bytes) else body.encode()
        res.raw = io.BytesIO(res._content)
        res.headers["Content-Type"] = content_type
        return res

    def ok(request, data=None):
        return reply(request, 200, json.dumps({"success": True, **({"data": data} if data is not None else {})}))

    def error(request, code, **extra):
        return reply(request, 200, json.dumps({"success": False, "error": {"code": code, **extra}}))

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        path = request.path_url.split("?")[0]
        assert request.method == "POST" and "?" not in request.path_url  # nothing in a URL
        form = dict(urllib.parse.parse_qsl(request.body or ""))
        api, method = form.get("api"), form.get("method")
        if path == "/webapi/query.cgi" and api == "SYNO.API.Info":
            apis = {"SYNO.API.Auth": {"path": state.auth_path, "minVersion": 1, "maxVersion": 7},
                    "SYNO.DSM.Info": {"path": "entry.cgi", "minVersion": 2, "maxVersion": 2},
                    "SYNO.Backup.Config.Backup": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2}}
            if state.no_backup_api:
                del apis["SYNO.Backup.Config.Backup"]
            return ok(request, apis)
        if path == f"/webapi/{state.auth_path}" and api == "SYNO.API.Auth":
            if method == "logout":
                state.logouts += 1
                return ok(request)
            state.logins.append(form)
            if (form.get("account"), form.get("passwd")) != (USER, PASSWORD):
                return error(request, 400)
            if form.get("session") != "webui" and not state.admin:
                return error(request, 402)
            data = {"sid": SID, "is_portal_port": False}
            if state.otp:
                if form.get("otp_code"):
                    if form["otp_code"] != state.otp:
                        return error(request, 404)
                    if form.get("enable_device_token") == "yes":
                        state.trusted.add(DID)
                        data["did"] = DID
                elif form.get("device_id") not in state.trusted:
                    return error(request, 403, errors={"token": "jwt", "types": state.second_steps})
                else:
                    data["did"] = form["device_id"]
            return ok(request, data)
        if path == "/webapi/entry.cgi":
            if form.get("_sid") != SID:
                return error(request, 119)
            if api == "SYNO.DSM.Info" and method == "getinfo":
                return ok(request, {"model": "DS413", "serial": SERIAL, "version_string": state.version_string})
            if api == "SYNO.Backup.Config.Backup":
                if not state.admin:
                    return error(request, 105)
                if method == "list":
                    return ok(request, {"services": [{"id": "user", "text": "controlpanel:leaf_user"}]})
                if method == "start":
                    return ok(request, {"task_id": "confbkp_bkp_1"})
                if method == "status":
                    assert form["task_id"] == '"confbkp_bkp_1"'  # JSON, as the Control Panel sends it
                    state.polls_left -= 1
                    return ok(request, {"finish": state.polls_left <= 0, "task_id": "confbkp_bkp_1"})
                if method == "download":
                    if state.download_json or state.downloaded:
                        return error(request, state.download_json or 4401)
                    state.downloaded = True
                    return reply(request, 200, state.dss, "application/octet-stream")
        return reply(request, 404, "<html>404</html>", "text/html")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(synology, "_POLL_INTERVAL", 0)
    return state


def app(**kwargs):
    return SynologyApp("http://nas.lab:5000/", **{"username": USER, "password": PASSWORD, **kwargs})


def calls(dsm, api=None):
    forms = [dict(urllib.parse.parse_qsl(r.body or "")) for r in dsm.sent]
    return [(f["api"], f["method"]) for f in forms if api is None or f["api"] == api]


# ------------------------------------------------------------- the card ----
def test_the_card_is_always_encrypted_restored_by_hand_and_pins_its_certificate():
    built = backup.build_app("synology", {"url": "https://nas.lab:5001", "username": USER, "password": PASSWORD,
                                          "device_token": DID, "trusted_cert": TRUSTED})
    assert isinstance(built, SynologyApp) and built.device_token == DID and not built.otp_code
    assert isinstance(built.session.get_adapter("https://nas.lab:5001/"), cert_pin.PinnedAdapter)
    assert always_encrypted("synology") and not restore_supported("synology")


# ---------------------------------------------------- test and backup ----
@pytest.mark.parametrize("auth_path, version_string, version", [
    ("auth.cgi", "DSM 6.2.4-25556 Update 8", "6.2.4-25556 Update 8"),
    ("entry.cgi", "DSM 7.2.2-72806", "7.2.2-72806"),
])
def test_connection_finds_the_sign_in_path_checks_the_account_can_export_and_signs_out(dsm, auth_path,
                                                                                         version_string, version):
    dsm.auth_path, dsm.version_string = auth_path, version_string
    assert app().test_connection() == f"synology {version} reachable, signed in as {USER}, who can export its configuration"
    assert calls(dsm) == [("SYNO.API.Info", "query"), ("SYNO.API.Auth", "login"), ("SYNO.DSM.Info", "getinfo"),
                          ("SYNO.Backup.Config.Backup", "list"), ("SYNO.API.Auth", "logout")]
    login = dsm.logins[0]
    assert login["session"] == "webui" and login["format"] == "sid" and login["version"] == "6"
    assert "otp_code" not in login and "device_id" not in login
    assert not dsm.downloaded  # a test never exports


def test_backup_exports_waits_downloads_dsms_file_as_made_and_signs_out(dsm, tmp_path):
    dsm.polls_left = 3
    driver = app()
    path = driver.backup(str(tmp_path))
    with open(path, "rb") as f:
        assert f.read() == dsm.dss
    assert path.endswith("synology-configuration.dss") and backup.archive_suffix(path) == ".dss"
    assert calls(dsm, "SYNO.Backup.Config.Backup") == [("SYNO.Backup.Config.Backup", m)
                                                        for m in ("start", "status", "status", "status", "download")]
    assert dsm.logouts == 1 and read_backup(path) == "7.2-72806"
    # The version came with the backup: the manifest needs no second sign-in.
    assert driver.app_version() == "7.2.2-72806 Update 1" and len(dsm.logins) == 1


def test_only_the_version_is_kept_from_dsms_info_never_the_serial(dsm, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    driver = app()
    driver.backup(str(tmp_path))
    assert SERIAL not in caplog.text and SERIAL not in json.dumps(vars(driver), default=str)
    assert PASSWORD not in caplog.text and SID not in caplog.text


def test_a_non_admin_account_is_told_it_needs_the_administrators_group(dsm, tmp_path):
    dsm.admin = False
    for run in (app().test_connection, lambda: app().backup(str(tmp_path))):
        with pytest.raises(SynologyError, match="isn't in the administrators group"):
            run()
    assert dsm.logouts == 2


def test_a_wrong_password_is_said_once_and_not_retried(dsm):
    with pytest.raises(SynologyError, match="refused the username or password") as error:
        app(password="nope").test_connection()
    assert len(dsm.logins) == 1 and not getattr(error.value, "needs_login", False)
    assert "nas.lab" not in str(error.value)


def test_no_login_is_explained_without_signing_in(dsm):
    with pytest.raises(SynologyError, match="enter the username and password"):
        app(password="").test_connection()
    assert not dsm.logins


@pytest.mark.parametrize("code, text", [
    (401, "disabled"), (402, "doesn't let this account sign in"), (406, "isn't set up"),
    (407, "Auto Block"), (409, "password has expired"), (499, "error 499"),
])
def test_each_refused_sign_in_is_explained(code, text):
    assert text in str(synology._login_error(code, [], False))


def test_a_download_dsm_refuses_is_reported_not_saved(dsm, tmp_path):
    dsm.download_json = 4401
    with pytest.raises(SynologyError, match=r"send the configuration export \(error 4401\)"):
        app().backup(str(tmp_path))
    assert not (tmp_path / "synology-configuration.dss").exists() and dsm.logouts == 1


def test_an_export_that_never_finishes_gives_up(dsm, tmp_path, monkeypatch):
    dsm.polls_left = 10**9
    monkeypatch.setattr(synology, "_EXPORT_TIMEOUT", 0)
    with pytest.raises(SynologyError, match="didn't finish"):
        app().backup(str(tmp_path))
    assert dsm.logouts == 1


def test_a_dsm_without_the_configuration_backup_api_is_explained(dsm):
    dsm.no_backup_api = True
    with pytest.raises(SynologyError, match="no configuration backup"):
        app().test_connection()


def test_something_else_at_the_url_is_reported(monkeypatch):
    def send(_adapter, request, **kwargs):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, 200
        res._content = b"<html>router</html>"
        res.raw = io.BytesIO(res._content)
        return res
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(SynologyError, match="didn't answer like DSM"):
        app().test_connection()


def test_a_redirect_is_refused_without_naming_it(monkeypatch):
    def send(_adapter, request, **kwargs):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, 301
        res.headers["Location"] = "https://elsewhere.example:5001/webapi/query.cgi"
        res._content, res.raw = b"", io.BytesIO(b"")
        return res
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(SynologyError, match="redirects") as error:
        app().test_connection()
    assert "elsewhere" not in str(error.value)


# ------------------------------------------------------------- 2FA ----
def test_an_account_with_2fa_asks_for_a_code_on_the_card(dsm):
    dsm.otp = "123456"
    with pytest.raises(CodeRequiredError, match="asks for a 2FA code") as error:
        app().test_connection()
    assert error.value.needs_login and len(dsm.logins) == 1


def test_a_code_signs_in_once_and_dsm_remembers_the_device_for_the_card(dsm):
    dsm.otp = "123456"
    driver = app(otp_code=" 123 456 ")
    assert driver.test_connection().endswith(". 2FA: this device is remembered - Save to keep it")
    login = dsm.logins[0]
    assert (login["otp_code"], login["enable_device_token"], login["device_name"]) == ("123456", "yes", "Backuparr")
    assert driver.card_info == {"device_token": DID}


def test_the_saved_device_token_signs_in_without_a_code(dsm, tmp_path):
    dsm.otp, dsm.trusted = "123456", {DID}
    driver = app(device_token=DID)
    driver.backup(str(tmp_path))
    login = dsm.logins[0]
    assert login["device_id"] == DID and login["device_name"] == "Backuparr" and "otp_code" not in login
    assert driver.card_info is None


def test_a_device_dsm_no_longer_trusts_asks_for_a_new_code(dsm, tmp_path):
    dsm.otp = "123456"
    with pytest.raises(CodeRequiredError) as error:
        app(device_token=DID).backup(str(tmp_path))
    assert str(error.value) == f"synology: {NEW_CODE}" and error.value.needs_login
    assert not dsm.downloaded


def test_a_wrong_code_says_so(dsm):
    dsm.otp = "123456"
    with pytest.raises(CodeRequiredError, match="wrong or expired"):
        app(otp_code="000000").test_connection()


def test_a_second_step_other_than_a_code_is_explained(dsm):
    dsm.otp, dsm.second_steps = "123456", [{"type": "approve_sign_in"}]
    with pytest.raises(SynologyError, match="second sign-in step Backuparr can't give") as error:
        app().test_connection()
    assert not getattr(error.value, "needs_login", False)


# ---------------------------------------------------------- the file ----
def test_the_version_backfill_reads_config_info(tmp_path):
    path = tmp_path / "synology_20261112_030000.dss"
    path.write_bytes(make_dss(info=config_info("6", "2", "25556")))
    assert backup_version(str(path)) == "6.2-25556"
    assert manifest_backfill.read_version("synology", str(path)) == "6.2-25556"


@pytest.mark.parametrize("kwargs, fragment", [
    ({"drop": ("ConfigBkp/config_info",)}, "no ConfigBkp/config_info"),
    ({"drop": ("ConfigBkp/_Syno_ConfBkp.db",)}, "no ConfigBkp/_Syno_ConfBkp.db"),
    ({"info": config_info(os_name="SRM")}, "doesn't name DSM"),
])
def test_a_file_without_dsms_parts_fails_its_check(tmp_path, kwargs, fragment):
    path = tmp_path / "x.dss"
    path.write_bytes(make_dss(**kwargs))
    with pytest.raises(SynologyError, match=fragment):
        synology.check_backup(str(path))


def test_a_cut_short_or_foreign_file_fails_its_check(tmp_path):
    path = tmp_path / "x.dss"
    for data in (make_dss()[:200], b"PK\x03\x04not a dss"):
        path.write_bytes(data)
        with pytest.raises(SynologyError, match="isn't a whole xz tar"):
            synology.check_backup(str(path))


def test_the_settings_database_gets_sqlites_quick_check(tmp_path):
    import archive_check
    path = tmp_path / "synology-configuration.dss"
    path.write_bytes(make_dss())
    archive_check._check("synology", str(path))
    path.write_bytes(make_dss(database=b"SQLite format 3\x00" + b"\x00" * 200))
    with pytest.raises(archive_check.ArchiveCheckError, match="_Syno_ConfBkp.db"):
        archive_check._check("synology", str(path))


# -------------------------------------------------------- Settings ----
def test_the_2fa_code_is_never_saved_and_the_device_token_is_encrypted_at_rest(authed_client, isolated_webui):
    card = {"enabled": True, "url": "https://nas.lab:5001", "username": USER, "password": PASSWORD,
            "otp_code": "123456", "device_token": DID}
    assert authed_client.post("/api/config", json={"apps": {"synology": card}}).status_code == 200
    saved = authed_client.get("/api/config").get_json()["apps"]["synology"]
    assert saved["device_token"] == DID and saved["password"] == PASSWORD and "otp_code" not in saved
    with open(isolated_webui.auth.CONFIG_PATH) as f:
        text = f.read()
    assert DID not in text and PASSWORD not in text and "123456" not in text


def test_a_device_token_must_be_short_text(authed_client):
    card = {"enabled": True, "url": "https://nas.lab:5001", "username": USER, "password": PASSWORD}
    for bad in ("x" * 513, 42):
        response = authed_client.post("/api/config", json={"apps": {"synology": {**card, "device_token": bad}}})
        assert response.status_code == 400 and "device_token" in response.get_json()["error"]


def test_test_connection_hands_the_card_the_device_token_and_shows_the_code_field(authed_client, isolated_webui,
                                                                                   monkeypatch):
    class FakeApp:
        def __init__(self, code):
            self.card_info = {"device_token": DID} if code else None
            self.code = code

        def test_connection(self):
            if not self.code:
                raise CodeRequiredError("synology: the account has 2-factor sign-in - enter a code")
            return "synology 7.2.2-72806 reachable"

    monkeypatch.setattr(isolated_webui.settings, "build_app", lambda name, cfg: FakeApp(cfg.get("otp_code")))
    card = {"url": "https://nas.lab:5001", "username": USER, "password": PASSWORD}
    asked = authed_client.post("/api/test/synology", json=card).get_json()
    assert asked["ok"] is False and asked["needs_login"] is True
    done = authed_client.post("/api/test/synology", json={**card, "otp_code": "123456"}).get_json()
    assert done["ok"] is True and done["card"] == {"device_token": DID}
    assert done["message"].startswith("Synology DSM 7.2.2")
