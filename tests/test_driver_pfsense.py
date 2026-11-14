"""pfSense (#153): the driver against a fake web UI, its pages cut down from
pfSense's own (authgui.inc, diag_backup.php, system_information.widget.php)."""
import io
import json
import urllib.parse

import pytest
import requests

import backup
from apps import cert_pin
from apps.pfsense import PfsenseApp, PfsenseError
from config_store import always_encrypted, restore_supported

USER, PASSWORD = "backuparr", "pf-secret"
TRUSTED = {"sha256": "ab" * 32, "subject": "pfSense-6530c9e8b3d4", "not_after": "2027-12-13T06:16:39+00:00"}
CONFIG = b'<?xml version="1.0"?>\n<pfsense>\n\t<version>23.3</version>\n\t<system><hostname>pfSense</hostname></system>\n</pfsense>\n'


def login_page(token):
    return f"""<html><form class="login" method="post"><input type='hidden' name='__csrf_magic' value="{token}" />
<input name="usernamefld" id="usernamefld" type="text"/><input name="passwordfld" id="passwordfld" type="password"/>
<input type="submit" name="login" value="Sign In"/></form></html>"""


def dashboard(token, version="2.7.2-RELEASE"):
    return f"""<html><script>var csrfMagicToken = "{token}";</script><table><tr>
<th>Version</th>
<td>
\t<strong>{version}</strong>
\t(amd64)</td></tr></table></html>"""


def backup_page(token):
    return f"""<html><form method="post"><input type='hidden' name='__csrf_magic' value="{token}" />
<input name="donotbackuprrd" type="checkbox" checked/>
<button class="btn btn-primary" type="submit" name="download" value="Download configuration as XML">Download</button></form></html>"""


@pytest.fixture
def server(monkeypatch):
    """A pfSense web UI: one session, a fresh CSRF token per page, and the
    privileges given in `allowed`. Records every request."""
    state = type("Server", (), {})()
    state.sent, state.signed_in, state.counter = [], False, 0
    state.allowed = {"diag_backup.php", "index.php"}
    state.version = "2.7.2-RELEASE"
    state.config = CONFIG
    state.signed_out = False
    state.wizard = False

    def token():
        state.counter += 1
        return f"sid:tok{state.counter},1794379441"

    def reply(request, status, body, headers=None):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = body if isinstance(body, bytes) else body.encode()
        res.raw = io.BytesIO(res._content)
        res.headers.update(headers or {})
        return res

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        path = request.path_url
        form = dict(urllib.parse.parse_qsl(request.body or "")) if request.method == "POST" else {}
        if not state.signed_in:
            if request.method == "POST" and form.get("login") and form.get("__csrf_magic"):
                if (form.get("usernamefld"), form.get("passwordfld")) == (USER, PASSWORD):
                    state.signed_in = True
                    return reply(request, 200, dashboard(token(), state.version))
            return reply(request, 200, login_page(token()))
        page = path.split("?")[0].lstrip("/") or "index.php"
        if page not in state.allowed:
            first = sorted(state.allowed)[0] if state.allowed else "index.php"
            return reply(request, 302, "", {"Location": f"/{first}"})
        if page == "index.php" and request.method == "POST" and "logout" in path:
            state.signed_in, state.signed_out = False, True
            return reply(request, 302, "", {"Location": "/"})
        if page == "index.php":
            if state.wizard:
                return reply(request, 200, f'<meta http-equiv="refresh" content="1;url=wizard.php?xml=setup_wizard.xml" />'
                                           f'<script>var csrfMagicToken = "{token()}";</script>')
            return reply(request, 200, dashboard(token(), state.version))
        if request.method == "POST" and form.get("download") and form.get("__csrf_magic"):
            assert form.get("donotbackuprrd") == "yes"
            return reply(request, 200, state.config, {"Content-Disposition": "attachment; filename=config-pfSense.home.arpa-20261111.xml"})
        return reply(request, 200, backup_page(token()))

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


@pytest.fixture
def app():
    return PfsenseApp("https://pf.lab/", USER, PASSWORD, trusted_cert=TRUSTED)


def test_the_card_is_always_encrypted_restored_by_hand_and_pins_its_certificate():
    app = backup.build_app("pfsense", {"url": "https://pf.lab", "username": USER, "password": PASSWORD, "trusted_cert": TRUSTED})
    assert isinstance(app, PfsenseApp)
    assert isinstance(app.session.get_adapter("https://pf.lab/"), cert_pin.PinnedAdapter)
    assert always_encrypted("pfsense") and not restore_supported("pfsense")


def test_connection_signs_in_reads_the_version_and_signs_out(app, server):
    assert app.test_connection() == "pfsense 2.7.2-RELEASE reachable, signed in as backuparr with access to Backup & Restore"
    login = next(r for r in server.sent if r.method == "POST")
    form = dict(urllib.parse.parse_qsl(login.body))
    assert form["usernamefld"] == USER and form["__csrf_magic"].startswith("sid:") and form["login"]
    assert PASSWORD not in login.url and server.signed_out


def test_a_wrong_password_is_said_once_and_not_retried(server):
    app = PfsenseApp("https://pf.lab", USER, "wrong")
    with pytest.raises(PfsenseError, match="username or password is wrong.*blocks the address"):
        app.test_connection()
    assert sum(1 for r in server.sent if r.method == "POST") == 1


def test_no_login_is_explained_without_contacting_pfsense(server):
    with pytest.raises(PfsenseError, match="enter the username and password"):
        PfsenseApp("https://pf.lab").test_connection()
    assert server.sent == []


def test_a_user_without_backup_and_restore_is_told_which_privileges(app, server):
    server.allowed = {"index.php"}
    with pytest.raises(PfsenseError, match='needs the "WebCfg - Diagnostics: Backup & Restore" and "WebCfg - System: Login / Logout / Dashboard"'):
        app.test_connection()
    assert server.signed_out


def test_a_user_without_the_dashboard_is_told_for_the_version(app, server):
    server.allowed = {"diag_backup.php"}
    with pytest.raises(PfsenseError, match='"WebCfg - System: Login / Logout / Dashboard" privilege too'):
        app.test_connection()


def test_a_firewall_still_in_its_setup_wizard_says_so(app, server):
    # A fresh pfSense shows the first-time setup wizard instead (2.8.1, lab).
    server.wizard = True
    with pytest.raises(PfsenseError, match="finish pfSense's setup wizard first"):
        app.test_connection()


@pytest.mark.parametrize("version", ["2.7.2-RELEASE", "2.8.1-RELEASE", "25.07.1-RELEASE", "24.11-RELEASE"])
def test_the_version_is_read_from_the_dashboard(app, server, version):
    server.version = version
    assert app.app_version() == version


def test_something_else_at_the_url_is_reported(server, monkeypatch):
    def send(_adapter, request, **kwargs):
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, 200
        res._content = b"<html>a router, but not pfSense</html>"
        res.raw = io.BytesIO(res._content)
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(PfsenseError, match="doesn't show pfSense's login page"):
        PfsenseApp("https://pf.lab", USER, PASSWORD).test_connection()


def test_backup_downloads_config_xml_without_rrd_data_and_signs_out(app, server, tmp_path):
    path = app.backup(str(tmp_path / "work"))
    assert path.endswith("config.xml") and open(path, "rb").read() == CONFIG
    assert backup.archive_suffix(path) == ".xml" and server.signed_out


@pytest.mark.parametrize("body, fragment", [
    (b"<html><body>Login</body></html>", "isn't a pfSense configuration"),
    (b"<pfsense><system>", "isn't valid XML"),
    (b'<?xml version="1.0"?><opnsense/>', "isn't a pfSense configuration"),
])
def test_a_download_that_isnt_a_whole_config_fails(app, server, tmp_path, body, fragment):
    server.config = body
    with pytest.raises(PfsenseError, match=fragment):
        app.backup(str(tmp_path / "work"))
    assert server.signed_out


def test_the_login_is_encrypted_at_rest_and_the_certificate_saved(authed_client, isolated_webui):
    card = {"enabled": True, "url": "https://pf.lab", "username": USER, "password": PASSWORD, "trusted_cert": TRUSTED}
    assert authed_client.post("/api/config", json={"apps": {"pfsense": card}}).status_code == 200
    saved = authed_client.get("/api/config").get_json()["apps"]["pfsense"]
    assert saved["trusted_cert"] == TRUSTED and saved["password"] == PASSWORD
    with open(isolated_webui.auth.CONFIG_PATH) as f:
        assert PASSWORD not in f.read()
    missing = authed_client.post("/api/config", json={"apps": {"pfsense": {**card, "password": ""}}})
    assert missing.status_code == 400 and "Password is required" in missing.get_json()["error"]


def test_the_trust_route_accepts_pfsense(authed_client, monkeypatch):
    presented = {**TRUSTED, "issuer": TRUSTED["subject"], "self_signed": True}
    monkeypatch.setattr(cert_pin, "fetch_certificate", lambda url: b"der")
    monkeypatch.setattr(cert_pin, "describe", lambda der: presented)
    body = authed_client.post("/api/trust-certificate/pfsense", json={"url": "https://pf.lab", "sha256": TRUSTED["sha256"]}).get_json()
    assert body == {"ok": True, "trusted_cert": TRUSTED}
    json.dumps(body)
