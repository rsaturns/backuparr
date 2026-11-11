"""Home Assistant (#155): the driver against a fake Home Assistant that
speaks its WebSocket protocol and backup REST views the way 2026.11.1 does,
on a Container (backup.local) or HA OS (hassio.local) install."""
import http.client
import io
import json
import logging
import re

import pytest
import requests
import websocket

import archive_check
import backup
import manifest_backfill
import restore_actions
from apps import base, cert_pin, homeassistant
from apps.homeassistant import HomeAssistantApp, HomeAssistantError
from config_store import always_encrypted, app_meta, restore_supported
from ha_backup import make_ha_backup

TOKEN = "ha-long-lived-token"
URL = "http://homeassistant:8123"


class Clock:
    """Stands in for time.monotonic/sleep: waits pass at once."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeWS:
    def __init__(self, ha):
        self.ha = ha
        self.queue = [{"type": "auth_required", "ha_version": ha.version}]
        self.closed = False
        self.timeout = None
        self.authed = False
        self.sent = []

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        self.ha.messages.append(message)
        if message["type"] == "auth":
            ok = message["access_token"] in self.ha.tokens
            self.authed = ok
            self.queue.append({"type": "auth_ok" if ok else "auth_invalid", "ha_version": self.ha.version})
            if not ok:
                self.closed = True
            return
        assert self.authed, "a command before auth"
        self.ha.command(self, message)

    def result(self, msg_id, result=None):
        self.queue.append({"id": msg_id, "type": "result", "success": True, "result": result})

    def error(self, msg_id, code, text):
        self.queue.append({"id": msg_id, "type": "result", "success": False, "error": {"code": code, "message": text}})

    def event(self, msg_id, **event):
        self.queue.append({"id": msg_id, "type": "event", "event": event})

    def recv(self):
        if self.queue:
            return json.dumps(self.queue.pop(0))
        if self.closed:
            raise websocket.WebSocketConnectionClosedException("closed")
        self.ha.clock.sleep(self.timeout or 1)
        raise websocket.WebSocketTimeoutException("timed out")

    def close(self, timeout=None):
        self.closed = True


class FakeSocket:
    closed = False

    def close(self):
        self.closed = True


class _Raw(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self._original_response = type("Original", (), {"msg": http.client.HTTPMessage()})()


class FakeHA:
    def __init__(self, clock):
        self.clock = clock
        self.version = "2026.11.1"
        self.install = "core"
        self.tokens = {TOKEN}
        self.admin = True
        self.messages = []
        self.sockets = []
        self.http = []
        self.backups = {"user0001": {"backup_id": "user0001", "name": "Automatic backup 2026.11.1",
                                     "agents": {"backup.local": {}}}}
        self.addons = [{"slug": "core_ssh", "name": "Terminal & SSH"}, {"slug": "a0d7b954_vscode", "name": "Studio Code Server"}]
        self.generate = "ok"  # "fail", "busy"
        self.generated = None
        self.download_status = 200
        self.delete_errors = {}
        self.restored = None
        self.down_for = 0  # connections refused after a restart
        self.after_restart = {"state": "completed", "tokens": None}
        self.onboarded = True
        self.fresh_after = "done"  # what onboarding/backup/info says after a new install's restore
        self.uploaded_tar = None
        self.views_after = 0  # onboarding backup calls that 404 before its backup views are there
        self.unlisted_polls = 0
        self.no_backup = False  # started after a restore without its backup integration  # backup/info calls after a restart that don't list the uploaded copy yet

    @property
    def agent(self):
        return "hassio.local" if self.install == "supervisor" else "backup.local"

    # ------------------------------------------------------- websocket --
    def connect(self):
        if self.down_for:
            self.down_for -= 1
            if not self.down_for:
                self._restarted()
            raise ConnectionRefusedError(111, "Connection refused")
        ws = FakeWS(self)
        self.sockets.append(ws)
        return ws

    def _restarted(self):
        self.no_backup = self.after_restart.get("no_backup", False)
        if self.after_restart["tokens"] is not None:
            self.tokens = set(self.after_restart["tokens"])
        self.last_action = {"manager_state": "restore_backup", "stage": None,
                            "state": self.after_restart["state"], "reason": self.after_restart.get("reason")}

    def command(self, ws, message):
        kind, msg_id = message["type"], message["id"]
        if not self.admin:
            return ws.error(msg_id, "unauthorized", "Unauthorized")
        if kind == "backup/agents/info":
            return ws.result(msg_id, {"agents": [{"agent_id": self.agent, "name": "This system"}]})
        if kind == "supervisor/api":
            assert self.install == "supervisor" and message["endpoint"] == "/addons" and message["method"] == "get"
            return ws.result(msg_id, {"addons": self.addons})
        if kind == "get_config":
            return ws.result(msg_id, {"state": "RUNNING", "components": ["http"] + ([] if self.no_backup else ["backup"])})
        if kind == "backup/info" and self.no_backup:
            return ws.error(msg_id, "unknown_command", "Unknown command.")
        if kind == "backup/info":
            listed = dict(self.backups)
            if self.unlisted_polls:
                self.unlisted_polls -= 1
                listed.pop("abc12345", None)
            return ws.result(msg_id, {"backups": list(listed.values()), "state": "idle",
                                      "last_action_event": getattr(self, "last_action", None)})
        if kind == "backup/subscribe_events":
            ws.event(msg_id, manager_state="idle")
            ws.subscription = msg_id
            return ws.result(msg_id)
        if kind == "backup/generate":
            return self._generate(ws, message)
        if kind == "backup/delete":
            if not self.unlisted_polls:  # Home Assistant can't delete what it hasn't listed yet
                self.backups.pop(message["backup_id"], None)
            return ws.result(msg_id, {"agent_errors": self.delete_errors})
        if kind == "backup/restore":
            return self._restore(ws, message)
        return ws.error(msg_id, "unknown_command", "Unknown command.")

    def _generate(self, ws, message):
        msg_id, sub = message["id"], ws.subscription
        if self.generate == "busy":
            return ws.error(msg_id, "home_assistant_error", "Backup manager busy: create_backup")
        if self.install == "core" and (message.get("include_folders") or message.get("include_addons")):
            return ws.error(msg_id, "home_assistant_error", "Addons and folders are not supported by core backup")
        self.generated = message
        ws.event(sub, manager_state="create_backup", stage=None, state="in_progress", reason=None)
        ws.result(msg_id, {"backup_job_id": "job1"})
        ws.event(sub, manager_state="create_backup", stage="home_assistant", state="in_progress", reason=None)
        if self.generate == "fail":
            ws.event(sub, manager_state="create_backup", stage=None, state="failed", reason="upload_failed")
        else:
            self.backups["new12345"] = {"backup_id": "new12345", "name": message["name"], "agents": {self.agent: {}},
                                        "failed_addons": [], "failed_folders": []}
            ws.event(sub, manager_state="create_backup", stage=None, state="completed", reason=None)
        ws.event(sub, manager_state="idle")

    def _restore(self, ws, message):
        self.restored = message
        if message["backup_id"] not in self.backups:
            return ws.error(message["id"], "backup_not_found", "Backup not found")
        ws.event(ws.subscription, manager_state="restore_backup", stage=None, state="in_progress", reason=None)
        if self.install == "supervisor":
            ws.event(ws.subscription, manager_state="restore_backup", stage="addons", state="in_progress", reason=None)
        else:
            ws.result(message["id"])
        ws.closed = True  # Home Assistant restarts
        self.down_for = 3

    # ------------------------------------------------------------ http --
    def handle(self, request):
        self.http.append(request)
        path = request.path_url.split("?")[0]
        query = request.path_url.partition("?")[2]
        auth = request.headers.get("Authorization")
        if path == "/api/onboarding":
            if self.onboarded:
                return 404, b"404: Not Found"  # an onboarded Home Assistant has no onboarding views after a restart
            return 200, [{"step": "user", "done": False}, {"step": "core_config", "done": False}]
        if path == "/api/onboarding/installation_type":
            return 200, {"installation_type": "Home Assistant OS" if self.install == "supervisor" else "Home Assistant Container"}
        if path.startswith("/api/onboarding/backup/"):
            assert auth is None
            if self.onboarded:
                return 404, b"404: Not Found"
            if self.views_after:  # added a few seconds after Home Assistant starts
                self.views_after -= 1
                return 404, b"404: Not Found"
            return self._onboarding(request, path, query)
        if auth != f"Bearer {TOKEN}" or TOKEN not in self.tokens:
            return 401, b""
        if path.startswith("/api/backup/download/"):
            backup_id = path.rsplit("/", 1)[1]
            assert query == f"agent_id={self.agent.replace('.', '.')}"
            if self.download_status != 200:
                return self.download_status, b""
            return (200, self.tar) if backup_id in self.backups else (404, b"")
        if path == "/api/backup/upload":
            return self._upload(request, query)
        return 404, b""

    def _upload(self, request, query):
        assert query == f"agent_id={self.agent}"
        body = request.body if isinstance(request.body, bytes) else request.body.read()
        assert re.search(rb'name="file"; filename="homeassistant.tar"', body)
        self.uploaded_tar = body
        self.backups["abc12345"] = {"backup_id": "abc12345", "name": "Backuparr 1a2b3c4d", "agents": {self.agent: {}}}
        return 201, {"backup_id": "abc12345"}

    def _onboarding(self, request, path, query):
        if path == "/api/onboarding/backup/upload":
            return self._upload(request, query)
        if path == "/api/onboarding/backup/restore":
            self.restored = json.loads(request.body)
            self.onboarded = self.fresh_after == "done"
            self.down_for = 2
            if self.fresh_after == "failed":
                self.last_action = {"manager_state": "restore_backup", "state": "failed", "reason": "unknown_error"}
            return 200, b""
        if path == "/api/onboarding/backup/info":
            if self.down_for:
                self.down_for -= 1
                raise requests.ConnectionError("restarting")
            return 200, {"backups": [], "state": "idle", "last_action_event": getattr(self, "last_action", None)}
        return 404, b""


@pytest.fixture
def ha(monkeypatch, tmp_path):
    clock = Clock()
    fake = FakeHA(clock)
    fake.tar = open(make_ha_backup(tmp_path / "made.tar", database=b""), "rb").read()

    def send(_adapter, request, **kwargs):
        status, body = fake.handle(request)
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res.raw = _Raw(body if isinstance(body, bytes) else json.dumps(body).encode())
        res.headers["Content-Type"] = "application/json"
        return res

    def create_connection(url, **options):
        assert url == "ws://homeassistant:8123/api/websocket"
        assert options["redirect_limit"] == 0 and isinstance(options["socket"], FakeSocket)
        fake.ws_options = options
        return fake.connect()

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(cert_pin, "connect_socket", lambda url, app, trusted=None, timeout=10: FakeSocket())
    monkeypatch.setattr(homeassistant.websocket, "create_connection", create_connection)
    monkeypatch.setattr(homeassistant.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(homeassistant.time, "sleep", clock.sleep)
    monkeypatch.setattr(base.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(base.time, "sleep", clock.sleep)
    return fake


def make(**options):
    return HomeAssistantApp(URL, TOKEN, **options)


def commands(ha, kind):
    return [m for m in ha.messages if m["type"] == kind]


# ------------------------------------------------------------------ card --
def test_the_card_is_an_always_encrypted_pinned_multi_instance_home_automation_app():
    meta = app_meta("homeassistant-cabin")
    assert meta["category"] == "home-automation" and meta["multi_instance"] and meta["pins_certificate"]
    assert always_encrypted("homeassistant") and restore_supported("homeassistant")
    assert meta["key_label"] == "Access token" and meta["key_required"]
    options = {f["name"] for f in meta["extra_fields"] if f.get("option")}
    assert options == {"settings", "history", "media", "share", "local_apps", "apps", "chosen_apps", "install_type"}


def test_build_app_keeps_a_switch_that_is_off():
    built = backup.build_app("homeassistant", {"url": URL, "api_key": TOKEN, "history": False, "media": True,
                                               "apps": "chosen", "chosen_apps": [{"slug": "core_ssh", "name": "SSH"}]})
    assert isinstance(built, HomeAssistantApp)
    assert built.history is False and built.folders == ["media"] and built.chosen_apps == ["core_ssh"]
    assert backup.build_app("homeassistant", {"url": URL, "api_key": TOKEN}).history is True


def test_request_headers_go_on_the_websocket_too(ha):
    app = backup.build_app("homeassistant", {"url": URL, "api_key": TOKEN,
                                             "request_headers": [{"name": "X-Proxy-Auth", "value": "secret"}]})
    app.app_version()
    assert ha.ws_options["header"] == ["X-Proxy-Auth: secret"]


# ------------------------------------------------------------ connection --
def test_the_version_comes_from_the_greeting_without_logging_in(ha):
    assert make().app_version() == "2026.11.1"
    assert not ha.messages  # no auth message: the token isn't even sent


def test_connection_on_a_container_install(ha):
    app = make()
    assert app.test_connection() == ("Home Assistant 2026.11.1 reachable (Container or Core), the token can back up "
                                     "and restore, 1 backup in Home Assistant")
    assert app.card_info == {"install": "core", "apps": []}
    assert [m["type"] for m in ha.messages] == ["auth", "backup/agents/info", "backup/info"]


def test_connection_on_ha_os_lists_the_apps_by_name(ha):
    ha.install = "supervisor"
    app = make()
    assert "(HA OS or Supervised)" in app.test_connection()
    assert app.card_info == {"install": "supervisor", "apps": [{"slug": "a0d7b954_vscode", "name": "Studio Code Server"},
                                                               {"slug": "core_ssh", "name": "Terminal & SSH"}]}


def test_a_refused_token_or_a_user_who_isnt_an_admin_says_so(ha):
    with pytest.raises(HomeAssistantError, match="the token was refused - make a long-lived access token"):
        HomeAssistantApp(URL, "wrong").test_connection()
    ha.admin = False
    with pytest.raises(HomeAssistantError, match="the token must belong to an administrator"):
        make().test_connection()


def test_an_unreachable_home_assistant_is_a_clean_message(monkeypatch):
    def refused(url, app, trusted=None, timeout=10):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(cert_pin, "connect_socket", refused)
    with pytest.raises(HomeAssistantError) as error:
        make().test_connection()
    assert str(error.value) == ("homeassistant: couldn't connect - connection refused by homeassistant:8123; "
                                "is Home Assistant running, and is the port right?")
    assert backup.describe_failure(error.value) == str(error.value)


@pytest.mark.parametrize("version, ok", [("2025.4.4", False), ("2025.1.4", False), ("2024.12.5", False),
                                         ("2025.5.0", True), ("2025.12.0b3", True), ("2026.11.1", True)])
def test_home_assistant_must_be_2025_5_or_newer(ha, tmp_path, version, ok):
    ha.version = version
    if ok:
        assert make().test_connection().startswith(f"Home Assistant {version} reachable")
        return
    for call in (make().test_connection, lambda: make().backup(str(tmp_path))):
        with pytest.raises(HomeAssistantError, match=f"Home Assistant {version} is too old .* update it to 2025.5 or newer"):
            call()
    assert make().app_version() == version  # the version check on the Restore tab can still say which it is
    assert not ha.messages  # not even logged in
    ha.onboarded = False
    with pytest.raises(HomeAssistantError, match="too old"):
        make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))
    assert not ha.http[1:] or not any("upload" in r.path_url for r in ha.http)


def test_something_that_isnt_home_assistant_is_refused(ha, monkeypatch):
    def not_ha(url, **options):
        raise websocket.WebSocketBadStatusException("Handshake status %d %s", 404, "Not Found")

    monkeypatch.setattr(homeassistant.websocket, "create_connection", not_ha)
    with pytest.raises(HomeAssistantError, match="no Home Assistant WebSocket API at this URL"):
        make().app_version()


# ---------------------------------------------------------------- backup --
def test_a_container_backup_is_made_downloaded_and_deleted_in_home_assistant(ha, tmp_path):
    path = make().backup(str(tmp_path / "work"))
    assert open(path, "rb").read() == ha.tar and path.endswith("homeassistant.tar")
    generated = ha.generated
    assert re.fullmatch(r"Backuparr [0-9a-f]{8}", generated["name"])
    assert {k: v for k, v in generated.items() if k not in ("id", "type", "name")} == {
        "agent_ids": ["backup.local"], "include_homeassistant": True, "include_database": True}
    assert "password" not in generated
    assert [m["backup_id"] for m in commands(ha, "backup/delete")] == ["new12345"]
    assert set(ha.backups) == {"user0001"}  # the user's own backup stays
    downloads = [r for r in ha.http if "/download/" in r.path_url]
    assert downloads[0].headers["Authorization"] == f"Bearer {TOKEN}" and TOKEN not in downloads[0].url


def test_history_off_leaves_the_database_out(ha, tmp_path):
    make(history=False).backup(str(tmp_path))
    assert ha.generated["include_database"] is False


def test_ha_os_options_are_left_out_on_a_container_install(ha, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        make(media=True, apps="all").backup(str(tmp_path))
    assert "include_folders" not in ha.generated and "include_all_addons" not in ha.generated
    assert "only on HA OS and Supervised installs" in caplog.text


def test_an_ha_os_backup_takes_the_folders_and_apps_chosen(ha, tmp_path, caplog):
    ha.install = "supervisor"
    make(media=True, share=True, local_apps=False, apps="all").backup(str(tmp_path))
    assert ha.generated["agent_ids"] == ["hassio.local"]
    assert ha.generated["include_folders"] == ["ssl", "media", "share"]
    assert ha.generated["include_all_addons"] is True and "include_addons" not in ha.generated
    chosen = [{"slug": "core_ssh", "name": "SSH"}, {"slug": "gone_app", "name": "Gone"}]
    with caplog.at_level(logging.WARNING):
        make(apps="chosen", chosen_apps=chosen).backup(str(tmp_path))
    assert ha.generated["include_folders"] == ["ssl"] and ha.generated["include_addons"] == ["core_ssh"]
    assert "gone_app is no longer installed - left out" in caplog.text


def test_a_failed_or_refused_backup_says_why(ha, tmp_path):
    ha.generate = "fail"
    with pytest.raises(HomeAssistantError, match=r"Home Assistant's backup failed \(upload failed\) - its log says why"):
        make().backup(str(tmp_path))
    ha.generate = "busy"
    with pytest.raises(HomeAssistantError, match="already busy with a backup or restore"):
        make().backup(str(tmp_path))


def test_the_copy_is_deleted_even_when_the_download_fails(ha, tmp_path):
    ha.download_status = 500
    with pytest.raises(requests.HTTPError):
        make().backup(str(tmp_path))
    assert "new12345" not in ha.backups


def test_a_copy_home_assistant_wont_delete_is_logged_not_failed(ha, tmp_path, caplog):
    ha.delete_errors = {"backup.local": "disk is read-only"}
    with caplog.at_level(logging.WARNING):
        make().backup(str(tmp_path))
    assert "couldn't delete Backuparr's copy (new12345)" in caplog.text and "Settings > System > Backups" in caplog.text


def test_an_untrusted_certificate_stops_before_anything_is_sent(monkeypatch):
    def untrusted(url, app, trusted=None, timeout=10):
        raise cert_pin.CertificateUntrusted(app, {"sha256": "a" * 64}, trusted)

    monkeypatch.setattr(cert_pin, "connect_socket", untrusted)
    with pytest.raises(cert_pin.CertificateUntrusted) as error:
        HomeAssistantApp("https://ha.local:8123", TOKEN).test_connection()
    assert str(error.value) == ("homeassistant: Home Assistant's certificate isn't trusted. Check it and trust it in "
                                "Settings to back it up.")


# ----------------------------------------------------------------- check --
@pytest.mark.archive_check
def test_a_whole_backup_passes_its_check_and_names_its_version(tmp_path):
    path = make_ha_backup(tmp_path / "homeassistant_20261111_210000.tar", database=b"", folders=["ssl"])
    archive_check.check("homeassistant", path)
    assert manifest_backfill.read_version("homeassistant", path) == "2026.11.1"


@pytest.mark.archive_check
@pytest.mark.parametrize("kwargs, fragment", [
    ({"meta": {"protected": True}}, "has a password"),
    ({"settings": False}, "homeassistant.tar.gz is missing"),
    ({"meta": {"homeassistant": {"exclude_database": True}}}, "doesn't say which Home Assistant made it"),
    ({"extra": {"./share.tar.gz": b"\x1f\x8bnot really gzip"}}, "damaged"),
    ({"database": b"not a database at all, just bytes" * 200}, "failed SQLite's quick check"),
])
def test_a_backup_that_isnt_whole_fails_its_check(tmp_path, kwargs, fragment):
    path = make_ha_backup(tmp_path / "ha.tar", **kwargs)
    with pytest.raises(archive_check.ArchiveCheckError, match=fragment):
        archive_check.check("homeassistant", path)


@pytest.mark.archive_check
def test_something_that_isnt_a_home_assistant_backup_fails_its_check(tmp_path):
    (tmp_path / "x.tar").write_bytes(b"plain text")
    with pytest.raises(archive_check.ArchiveCheckError, match="can't be opened as a Home Assistant backup"):
        archive_check.check("homeassistant", str(tmp_path / "x.tar"))


# --------------------------------------------------------------- restore --
def test_a_container_restore_uploads_restores_and_deletes_the_copy(ha, tmp_path):
    path = make_ha_backup(tmp_path / "ha.tar", database=b"")
    assert make().restore_from_file(path) == {"fresh": False, "token_ok": True, "cleaned": True}
    restore = commands(ha, "backup/restore")[0]
    assert {k: v for k, v in restore.items() if k not in ("id", "type")} == {
        "backup_id": "abc12345", "agent_id": "backup.local", "restore_database": True, "restore_homeassistant": True}
    assert "abc12345" not in ha.backups  # the uploaded copy
    upload = next(r for r in ha.http if r.path_url.startswith("/api/backup/upload"))
    assert upload.headers["Authorization"] == f"Bearer {TOKEN}"
    assert upload.headers["Content-Type"].startswith("multipart/form-data; boundary=")


def test_the_uploaded_copy_is_deleted_once_home_assistant_lists_it_again(ha, tmp_path):
    ha.unlisted_polls = 4
    assert make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))["cleaned"] is True
    assert "abc12345" not in ha.backups


def test_a_copy_still_listed_after_the_delete_is_logged(ha, tmp_path, caplog):
    ha.delete_errors = {}
    original = ha.command

    def keep(ws, message):
        if message["type"] == "backup/delete":
            return ws.result(message["id"], {"agent_errors": {}})  # answers, deletes nothing
        return original(ws, message)

    ha.command = keep
    with caplog.at_level(logging.WARNING):
        assert make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))["cleaned"] is False
    assert "still listed after the delete" in caplog.text


def test_a_backup_without_history_restores_without_the_database(ha, tmp_path):
    make().restore_from_file(make_ha_backup(tmp_path / "ha.tar", database=None, meta={"homeassistant": {
        "version": "2026.11.1", "exclude_database": True}}))
    assert commands(ha, "backup/restore")[0]["restore_database"] is False


def test_an_ha_os_restore_brings_back_its_apps_and_folders(ha, tmp_path):
    ha.install = "supervisor"
    make().restore_from_file(make_ha_backup(tmp_path / "ha.tar", folders=["ssl", "media"], addons=["core_ssh"]))
    restore = commands(ha, "backup/restore")[0]
    assert restore["agent_id"] == "hassio.local"
    assert restore["restore_folders"] == ["ssl", "media"] and restore["restore_addons"] == ["core_ssh"]


def test_a_backup_from_ha_os_is_refused_onto_a_container_install_before_any_upload(ha, tmp_path):
    path = make_ha_backup(tmp_path / "ha.tar", meta={"supervisor_version": "2026.11.0"}, folders=["ssl"])
    with pytest.raises(HomeAssistantError, match="is from HA OS or Supervised.*Restore it onto HA OS"):
        make().restore_from_file(path)
    assert not commands(ha, "backup/restore") and not any("upload" in r.path_url for r in ha.http)
    ha.onboarded = False
    with pytest.raises(HomeAssistantError, match="is from HA OS or Supervised"):
        make().restore_from_file(path)
    ha.install, ha.onboarded = "supervisor", True
    assert make().restore_from_file(path)["token_ok"] is True


def test_home_assistant_back_without_its_backup_integration_says_so(ha, tmp_path):
    ha.after_restart = {"state": "completed", "tokens": None, "no_backup": True}
    with pytest.raises(HomeAssistantError, match="without its backup integration"):
        make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))


def test_the_supervisors_apps_local_folder_restores_as_addons_local(ha, tmp_path):
    ha.install = "supervisor"
    make().restore_from_file(make_ha_backup(tmp_path / "ha.tar", folders=["apps/local", "media", "ssl"]))
    assert commands(ha, "backup/restore")[0]["restore_folders"] == ["addons/local", "media", "ssl"]


def test_a_restore_home_assistant_reports_failed_is_an_error(ha, tmp_path):
    ha.after_restart = {"state": "failed", "reason": "You need at least Home Assistant version 2026.12.0", "tokens": None}
    with pytest.raises(HomeAssistantError, match="couldn't restore the backup .*started with its settings as they were"):
        make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))


def test_a_token_the_restore_removed_says_make_a_new_one(ha, tmp_path, caplog):
    ha.after_restart = {"state": "completed", "tokens": ["an-older-token"]}
    with caplog.at_level(logging.WARNING):
        result = make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))
    assert result == {"fresh": False, "token_ok": False, "cleaned": False}
    assert "uploaded copy (abc12345) was left in Home Assistant" in caplog.text
    staged = restore_actions._homeassistant(type("App", (), {"restore_from_file": lambda self, p: result})(), None, "x", {})
    assert staged["attention"] is True
    assert restore_actions.restore_message("homeassistant", staged) == "Restored. Make a new token."


def test_the_restore_messages():
    assert restore_actions.restore_step("homeassistant-cabin") == \
        "restoring homeassistant-cabin, Home Assistant restarts to finish it"
    assert restore_actions.restore_message("homeassistant", {"token_ok": True}) == \
        "homeassistant restore complete, Home Assistant restarted with the restored settings"


def test_a_new_install_restores_through_its_onboarding(ha, tmp_path):
    ha.onboarded = False
    result = make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))
    assert result == {"fresh": True, "token_ok": True, "cleaned": True}
    assert ha.restored == {"backup_id": "abc12345", "agent_id": "backup.local", "restore_database": True}
    onboarding = [r for r in ha.http if r.path_url.startswith("/api/onboarding/backup/")]
    assert onboarding and not any("Authorization" in r.headers for r in onboarding)


def test_a_new_install_waits_for_its_onboarding_backup_api(ha, tmp_path):
    ha.onboarded, ha.views_after = False, 3
    assert make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))["fresh"] is True


def test_a_new_install_restore_that_failed_says_so(ha, tmp_path):
    ha.onboarded, ha.fresh_after = False, "failed"
    with pytest.raises(HomeAssistantError, match=r"couldn't restore the backup \(unknown error\)"):
        make().restore_from_file(make_ha_backup(tmp_path / "ha.tar"))


def test_a_damaged_backup_is_never_uploaded(ha, tmp_path):
    with pytest.raises(HomeAssistantError, match="missing from the backup"):
        make().restore_from_file(make_ha_backup(tmp_path / "ha.tar", settings=False))
    assert not ha.http and not ha.messages


# -------------------------------------------------------------- settings --
CHOSEN = [{"slug": "core_ssh", "name": "Terminal & SSH"}]


def test_the_options_save_and_come_back(authed_client, isolated_webui):
    card = {"enabled": True, "url": URL, "api_key": TOKEN, "history": False, "media": True, "share": False,
            "local_apps": False, "apps": "chosen", "chosen_apps": CHOSEN, "install_type": "supervisor",
            "settings": False}
    assert authed_client.post("/api/config", json={"apps": {"homeassistant": card}}).get_json() == {"ok": True}
    saved = authed_client.get("/api/config").get_json()["apps"]["homeassistant"]
    assert {k: saved[k] for k in ("history", "media", "apps", "chosen_apps", "install_type")} == {
        "history": False, "media": True, "apps": "chosen", "chosen_apps": CHOSEN, "install_type": "supervisor"}
    assert "settings" not in saved  # always on: nothing to save
    with open(isolated_webui.auth.CONFIG_PATH) as f:
        assert TOKEN not in f.read()  # the token is encrypted like any API key


@pytest.mark.parametrize("field, value, fragment", [
    ("history", "yes", "history must be true or false"),
    ("apps", "some", "apps must be one of none, all, chosen"),
    ("install_type", "windows", "install_type isn't a value Backuparr knows"),
    ("chosen_apps", [{"slug": "Bad Slug!", "name": "x"}], "chosen_apps must be a list of apps"),
    ("chosen_apps", [{"slug": "ok", "name": "x", "extra": 1}], "chosen_apps must be a list of apps"),
    ("chosen_apps", "core_ssh", "chosen_apps must be a list of apps"),
])
def test_options_that_arent_well_formed_are_refused(authed_client, field, value, fragment):
    res = authed_client.post("/api/config", json={"apps": {"homeassistant": {field: value}}})
    assert res.status_code == 400 and fragment in res.get_json()["error"]


def test_a_test_connection_hands_the_card_what_it_found(authed_client, isolated_webui, monkeypatch):
    class Found:
        card_info = {"install": "supervisor", "apps": CHOSEN}

        def test_connection(self):
            return "homeassistant 2026.11.1 reachable"

    monkeypatch.setattr(isolated_webui.settings, "build_app", lambda name, cfg: Found())
    body = authed_client.post("/api/test/homeassistant", json={"url": URL, "api_key": TOKEN}).get_json()
    assert body == {"ok": True, "message": "homeassistant 2026.11.1 reachable", "card": Found.card_info}


def test_the_card_renders_its_options(authed_client):
    html = authed_client.get("/").get_data(as_text=True)
    card = html[html.index('data-app="homeassistant"'):]
    card = card[:card.index('class="instance-add"')]
    assert '<input type="checkbox" checked disabled>' in card  # Settings: always on
    assert 'data-field="history" data-default="true"' in card
    assert card.count('data-only-install="supervisor"') == 5
    assert '<select class="f-extra" data-field="apps" data-default="none">' in card
    assert '<input type="hidden" class="f-extra" data-field="install_type">' in card
    assert "Always encrypted for safety." in card and 'class="f-encrypt"' not in card
