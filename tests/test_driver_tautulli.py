import json

import pytest
import requests

from apps.tautulli import TautulliApp, TautulliError

KEY = "tautulli-secret-key"


class Reply:
    def __init__(self, status=200, data=None, content=b"", text=None):
        self.status_code = status
        self._data = data
        self.content = content
        self.text = text if text is not None else json.dumps(data) if data is not None else ""

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} for http://tautulli/api/v2?apikey={KEY}")


class Session:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def request(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def app_with(*replies):
    app = TautulliApp("http://tautulli:8181/", KEY)
    app.session = Session(*replies)
    return app


def ok(message="done"):
    return Reply(data={"response": {"result": "success", "message": message}})


# --- connection ------------------------------------------------------------

def test_the_url_is_normalised_and_the_key_sent_as_a_parameter():
    app = app_with(ok())
    assert app.test_connection() == "tautulli reachable"
    method, url, kwargs = app.session.sent[0]
    assert (method, url) == ("GET", "http://tautulli:8181/api/v2")
    assert kwargs["params"] == {"apikey": KEY, "cmd": "get_settings", "key": "General"} and kwargs["timeout"] == 10


def test_an_error_result_is_reported_with_tautullis_message():
    app = app_with(Reply(data={"response": {"result": "error", "message": "Invalid apikey"}}))
    with pytest.raises(TautulliError, match="Invalid apikey"):
        app.test_connection()


def test_an_empty_response_is_reported_as_unexpected():
    with pytest.raises(TautulliError, match="unexpected response"):
        app_with(Reply(data={})).test_connection()


def test_a_401_means_a_wrong_key():
    with pytest.raises(TautulliError, match="unauthorized"):
        app_with(Reply(401)).test_connection()


def test_other_http_errors_name_the_command_but_not_the_url():
    with pytest.raises(TautulliError) as caught:
        app_with(Reply(500)).test_connection()
    assert "HTTP 500" in str(caught.value) and "get_settings" in str(caught.value)
    assert KEY not in str(caught.value) and KEY not in str(caught.value.__cause__ or "")


# --- backup ----------------------------------------------------------------

def test_backup_downloads_the_database_and_the_config(tmp_path):
    app = app_with(Reply(content=b"sqlite bytes"), Reply(content=b"[General]\n"))
    out = tmp_path / "nested" / "dump"
    assert app.backup(str(out)) == str(out)
    assert (out / "tautulli.db").read_bytes() == b"sqlite bytes" and (out / "config.ini").read_bytes() == b"[General]\n"
    assert [kw["params"]["cmd"] for _, _, kw in app.session.sent] == ["download_database", "download_config"]
    assert all(kw["timeout"] == 120 for _, _, kw in app.session.sent)


def test_backup_stops_when_a_download_fails(tmp_path):
    app = app_with(Reply(content=b"db"), Reply(500))
    with pytest.raises(TautulliError, match="download_config"):
        app.backup(str(tmp_path))


# --- restore ---------------------------------------------------------------

def folder(tmp_path, db=True, config=True):
    if db:
        (tmp_path / "tautulli.db").write_bytes(b"db")
    if config:
        (tmp_path / "config.ini").write_bytes(b"cfg")
    return str(tmp_path)


def test_restore_uploads_both_files_with_the_right_form_fields(tmp_path):
    app = app_with(ok("database imported"), ok("config import started"), Reply())
    summary = app.restore(folder(tmp_path))
    assert summary == {"database": "database imported", "config": "config import started"}
    (m1, _, db_call), (m2, _, cfg_call), _start = app.session.sent
    assert m1 == m2 == "POST"
    assert db_call["params"] == {"apikey": KEY, "cmd": "import_database"}
    assert db_call["data"] == {"app": "tautulli", "method": "overwrite", "backup": "true"}
    assert list(db_call["files"]) == ["database_file"] and db_call["files"]["database_file"][0] == "tautulli.db"
    assert cfg_call["params"]["cmd"] == "import_config" and cfg_call["data"] == {"backup": "true"}
    assert list(cfg_call["files"]) == ["config_file"]


def test_the_known_upstream_database_bug_is_skipped_but_the_config_still_restores(tmp_path):
    bug = Reply(400, data={"response": {"result": "error", "message": "No app specified for import."}})
    summary = app_with(bug, ok("started"), Reply()).restore(folder(tmp_path))
    assert summary == {"database_skipped": "not restorable via API - see README", "config": "started"}


def test_any_other_database_error_is_not_swallowed(tmp_path):
    other = Reply(400, data={"response": {"result": "error", "message": "disk full"}})
    app = app_with(other)
    with pytest.raises(TautulliError, match="disk full"):
        app.restore(folder(tmp_path))
    assert len(app.session.sent) == 1  # the config was not touched after a real failure


def test_older_backups_with_only_one_file_still_restore(tmp_path):
    assert app_with(ok("cfg ok"), Reply()).restore(folder(tmp_path, db=False)) == {"config": "cfg ok"}


def test_a_backup_with_neither_file_is_an_error(tmp_path):
    with pytest.raises(TautulliError, match="neither"):
        app_with().restore(str(tmp_path))


@pytest.mark.parametrize("reply, message", [
    (Reply(401), "unauthorized"),
    (Reply(400, text="plain text failure"), "plain text failure"),
    (Reply(400, data={"response": {}}, text=""), "import failed"),
    (Reply(400, data={"response": {}}), "response"),
    (Reply(500), "HTTP 500"),
    (Reply(data={"response": {"result": "error", "message": "bad file"}}), "bad file"),
    (Reply(data={"response": {"result": "error"}}), "import failed"),
])
def test_import_failures_are_explained(tmp_path, reply, message):
    with pytest.raises(TautulliError, match=message):
        app_with(reply).restore(folder(tmp_path, db=False))


def test_network_errors_during_upload_do_not_leak_the_key(tmp_path):
    app = app_with(requests.exceptions.ConnectionError(f"Max retries with url: /api/v2?apikey={KEY}"))
    with pytest.raises(requests.exceptions.ConnectionError) as caught:
        app.restore(folder(tmp_path, db=False))
    assert KEY not in str(caught.value)


# --- finishing the config import --------------------------------------------
# import_config only stages the file; /restart_import_config runs the import.

STAGED_URL = "http://tautulli:8181/restart_import_config"


def test_without_a_login_the_staged_import_is_started_with_the_api_key_in_a_header(tmp_path):
    app = app_with(ok("staged"), Reply(200))
    summary = app.restore(folder(tmp_path, db=False))
    assert summary == {"config": "staged"}
    method, url, call = app.session.sent[-1]
    assert (method, url) == ("GET", STAGED_URL)
    assert call["headers"] == {"X-Api-Key": KEY} and call["allow_redirects"] is False
    assert KEY not in url and "params" not in call


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 403])
def test_with_a_login_the_config_stays_staged_and_the_user_is_told_how_to_finish(tmp_path, status, caplog):
    summary = app_with(ok("staged"), Reply(status)).restore(folder(tmp_path, db=False))
    assert summary["config"] == "staged"
    message = summary["config_staged"]
    assert message.startswith("Tautulli has a login set up, so Backuparr can't start the import. The config is staged.")
    assert message.endswith(f"log in to Tautulli if asked, then open {STAGED_URL}")
    assert KEY not in message
    assert message in caplog.text


def test_an_unexpected_answer_is_reported_with_the_same_instructions(tmp_path):
    message = app_with(ok("staged"), Reply(500)).restore(folder(tmp_path, db=False))["config_staged"]
    assert message.startswith("Tautulli answered HTTP 500 when asked to start the import")
    assert message.endswith(STAGED_URL)


def test_the_database_is_still_reported_when_the_config_stays_staged(tmp_path):
    summary = app_with(ok("db done"), ok("staged"), Reply(303)).restore(folder(tmp_path))
    assert summary["database"] == "db done" and "config_staged" in summary


def test_a_network_error_starting_the_import_does_not_leak_the_key(tmp_path):
    app = app_with(ok("staged"), requests.exceptions.ConnectionError(f"refused {STAGED_URL}?apikey={KEY}"))
    with pytest.raises(requests.exceptions.ConnectionError) as caught:
        app.restore(folder(tmp_path, db=False))
    assert KEY not in str(caught.value) and caught.value.__cause__ is None
