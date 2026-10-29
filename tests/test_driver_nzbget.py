import base64
import io
import json
import zipfile

import pytest
import requests

import backup
import restore_actions
from apps.nzbget import CONFIG, MANIFEST, NzbgetApp, NzbgetError, format_options, parse_options
from config_store import restore_supported

AUTH = "Basic " + base64.b64encode(b"nzbget:control-pass").decode()

OPTIONS = [
    {"Name": "MainDir", "Value": "/downloads"},
    {"Name": "DestDir", "Value": "${MainDir}/completed"},
    {"Name": "ControlUsername", "Value": "nzbget"},
    {"Name": "ControlPassword", "Value": "control-pass"},
    {"Name": "ControlPort", "Value": "6789"},
    {"Name": "Server1.Host", "Value": "news.example.com"},
    {"Name": "Server1.Password", "Value": "usenet = secret"},
    {"Name": "Category1.Name", "Value": "Movies"},
    {"Name": "VideoSort:MoviesFormat", "Value": "%t (%y)"},
    {"Name": "Server1.Group", "Value": ""},
]


@pytest.fixture
def nzbget(monkeypatch):
    """A JSON-RPC NZBGet: records calls, answers from `options` and `version`."""
    state = type("Nzbget", (), {})()
    state.sent, state.calls = [], []
    state.options = [dict(o) for o in OPTIONS]
    state.version = "26.3"
    state.saved = None
    state.status = 200
    state.errors = {}  # method -> error object or Exception

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        body = json.loads(request.body)
        method = body["method"]
        state.calls.append(method)
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, state.status
        error = state.errors.get(method)
        if isinstance(error, Exception):
            raise error
        if error:
            payload = {"version": "1.1", "error": error}
        elif method == "version":
            payload = {"version": "1.1", "result": state.version}
        elif method == "loadconfig":
            payload = {"version": "1.1", "result": state.options}
        elif method == "saveconfig":
            state.saved = body["params"][0]
            payload = {"version": "1.1", "result": True}
        elif method == "reload":
            payload = {"version": "1.1", "result": True}
        else:
            payload = {"version": "1.1", "error": {"name": "JSONRPCError", "code": 1, "message": "Invalid procedure"}}
        res._content = json.dumps(payload).encode()
        res.raw = io.BytesIO(res._content)
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


@pytest.fixture
def app():
    return NzbgetApp("http://nzbget:6789/", "nzbget", "control-pass")


def make_backup(app, tmp_path):
    archive = str(tmp_path / "nzbget_20261029_030000.zip")
    backup.zip_dir(app.backup(str(tmp_path / "work")), archive)
    return archive


# --- connection --------------------------------------------------------------

def test_basic_auth_goes_in_the_header_never_the_url(app, nzbget):
    assert app.test_connection() == "nzbget 26.3 reachable, full access OK"
    assert nzbget.calls == ["version", "loadconfig"]
    request = nzbget.sent[0]
    assert request.url == "http://nzbget:6789/jsonrpc" and request.headers["Authorization"] == AUTH
    assert "control-pass" not in request.url


def test_no_credentials_sends_no_authorization(nzbget):
    NzbgetApp("http://nzbget:6789").test_connection()
    assert "Authorization" not in nzbget.sent[0].headers


def test_wrong_credentials_say_where_they_live(app, nzbget):
    nzbget.status = 401
    with pytest.raises(NzbgetError, match="check the username and password .*ControlUsername"):
        app.test_connection()


def test_the_restricted_user_is_refused(app, nzbget):
    for option in nzbget.options:
        if "Password" in option["Name"] or option["Name"] == "ControlPort":
            option["Value"] = "***"
    with pytest.raises(NzbgetError, match="restricted user, which can't see passwords"):
        app.test_connection()


def test_the_add_only_user_is_refused(app, nzbget):
    nzbget.errors["loadconfig"] = {"name": "JSONRPCError", "code": 401, "message": "Access denied"}
    with pytest.raises(NzbgetError, match="access denied - use the Control username"):
        app.test_connection()


def test_a_url_that_isnt_nzbget_is_explained(app, nzbget, monkeypatch):
    def send(_adapter, request, **kwargs):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, 200
        res._content = b"<html>login</html>"
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(NzbgetError, match="didn't answer like NZBGet's API"):
        app.test_connection()


def test_redirects_are_not_followed_with_credentials(app, nzbget):
    nzbget.status = 302
    with pytest.raises(NzbgetError, match="redirects"):
        app.test_connection()
    assert len(nzbget.sent) == 1


def test_connection_failures_are_left_for_the_shared_explanation(app, nzbget):
    nzbget.errors["version"] = requests.ConnectionError("refused")
    with pytest.raises(requests.ConnectionError):
        app.test_connection()


# --- backup ------------------------------------------------------------------

def test_backup_holds_every_option_as_nzbget_stores_it(app, nzbget, tmp_path):
    with zipfile.ZipFile(make_backup(app, tmp_path)) as zf:
        assert set(zf.namelist()) == {CONFIG, MANIFEST}
        text = zf.read(CONFIG).decode()
        manifest = json.loads(zf.read(MANIFEST))
    assert "DestDir=${MainDir}/completed\n" in text and "Server1.Password=usenet = secret\n" in text
    assert parse_options(text) == OPTIONS
    assert manifest == {"nzbget_version": "26.3", "options": len(OPTIONS)}


def test_the_backup_file_matches_nzbgets_own_backup_format():
    # NZBGet's Backup settings writes "Name=Value" lines; Restore settings
    # splits each at the first "=".
    assert format_options(OPTIONS[:2]) == "MainDir=/downloads\nDestDir=${MainDir}/completed\n"


def test_backup_by_the_restricted_user_is_refused(app, nzbget, tmp_path):
    next(o for o in nzbget.options if o["Name"] == "ControlPort")["Value"] = "***"
    with pytest.raises(NzbgetError, match="restricted user"):
        app.backup(str(tmp_path / "work"))


def test_backup_is_built_through_the_app_registry():
    app = backup.build_app("nzbget", {"url": "http://nzbget:6789", "username": "u", "password": "p"})
    assert isinstance(app, NzbgetApp) and app.session.auth == ("u", "p")
    assert restore_supported("nzbget")


# --- restore -----------------------------------------------------------------

def test_restore_saves_the_backups_options_in_order_then_reloads(app, nzbget, tmp_path):
    archive = make_backup(app, tmp_path)
    nzbget.options = [{"Name": "MainDir", "Value": "/elsewhere"}, {"Name": "ControlPort", "Value": "6789"}]
    nzbget.calls.clear()
    app.restore_from_file(archive)
    assert nzbget.calls == ["loadconfig", "saveconfig", "reload"]
    assert nzbget.saved == OPTIONS
    # NZBGet reads each entry's strings in order: Name, then Value.
    assert all(list(o) == ["Name", "Value"] for o in nzbget.saved)


def test_a_reload_that_drops_the_connection_still_counts(app, nzbget, tmp_path):
    archive = make_backup(app, tmp_path)
    nzbget.errors["reload"] = requests.ConnectionError("reset by peer")
    app.restore_from_file(archive)
    assert nzbget.saved == OPTIONS


def test_restore_without_full_access_writes_nothing(app, nzbget, tmp_path):
    archive = make_backup(app, tmp_path)
    nzbget.errors["loadconfig"] = {"name": "JSONRPCError", "code": 401, "message": "Access denied"}
    with pytest.raises(NzbgetError, match="access denied"):
        app.restore_from_file(archive)
    assert "saveconfig" not in nzbget.calls


def test_a_failed_save_is_reported(app, nzbget, tmp_path, monkeypatch):
    archive = make_backup(app, tmp_path)
    nzbget.errors["saveconfig"] = {"name": "JSONRPCError", "code": 1, "message": "Could not write"}
    with pytest.raises(NzbgetError, match=r"saveconfig failed \(Could not write\)"):
        app.restore_from_file(archive)
    assert "reload" not in nzbget.calls


def test_saveconfig_returning_false_is_reported(app, nzbget, tmp_path, monkeypatch):
    archive = make_backup(app, tmp_path)
    real = app._call
    monkeypatch.setattr(app, "_call", lambda method, *a, **kw: False if method == "saveconfig" else real(method, *a, **kw))
    with pytest.raises(NzbgetError, match="couldn't write its settings file"):
        app.restore_from_file(archive)


def test_restore_refuses_a_file_that_isnt_an_nzbget_backup(app, nzbget, tmp_path):
    other = tmp_path / "nzbget_x.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr(CONFIG, "MainDir=/downloads\n")
    with pytest.raises(NzbgetError, match="isn't an NZBGet backup made by Backuparr"):
        app.restore_from_file(str(other))
    assert nzbget.sent == []


@pytest.mark.parametrize("text, message", [
    ("just some text\n", "unexpected line"),
    ("=value\n", "unexpected line"),
    ("Foo=bar\n", "doesn't hold NZBGet's settings"),
    ("MainDir=/d\nControlPort=***\n", "made by the restricted user"),
])
def test_restore_refuses_a_broken_settings_file_before_any_call(app, nzbget, tmp_path, text, message):
    archive = tmp_path / "nzbget_bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(MANIFEST, json.dumps({"nzbget_version": "26.3"}))
        zf.writestr(CONFIG, text)
    with pytest.raises(NzbgetError, match=message):
        app.restore_from_file(str(archive))
    assert nzbget.sent == []


def test_comments_and_blank_lines_are_skipped():
    assert parse_options("# NZBGet backup\n\nMainDir=/d\r\n") == [{"Name": "MainDir", "Value": "/d"}]


def test_the_restore_action_uses_the_username_and_password(app, nzbget, tmp_path):
    archive = make_backup(app, tmp_path)
    nzbget.sent.clear()
    cfg = {"url": "http://nzbget:6789", "api_key": "", "username": "nzbget", "password": "control-pass"}
    assert restore_actions.restore_app("nzbget", cfg, str(tmp_path), archive) == {"kind": "nzbget"}
    assert nzbget.saved == OPTIONS and all(r.headers["Authorization"] == AUTH for r in nzbget.sent)
