import json

import pytest
import requests

from apps import sabnzbd
from apps.sabnzbd import MASKED, SabnzbdApp, SabnzbdError

KEY = "sabnzbd-secret-key"


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        return self.data


@pytest.fixture
def api(monkeypatch):
    """Records every POST and answers from `replies` (default: {"status": True})."""
    sent, replies = [], []

    def post(url, data=None, timeout=None, **kwargs):
        sent.append({"url": url, "data": data, "timeout": timeout, **kwargs})
        return replies.pop(0) if replies else Reply({"status": True})

    monkeypatch.setattr(sabnzbd.requests, "post", post)
    return type("Api", (), {"sent": sent, "replies": replies})


@pytest.fixture
def app():
    return SabnzbdApp("http://sab:8080/", KEY)


def config(**sections):
    return {"config": sections}


# --- calls -----------------------------------------------------------------

def test_calls_post_the_key_in_the_body_never_in_the_url(app, api):
    api.replies.append(Reply({"config": {}}))
    app.get_config()
    call = api.sent[0]
    assert call["url"] == "http://sab:8080/api" and KEY not in call["url"]
    assert call["data"] == {"apikey": KEY, "output": "json", "mode": "get_config"}


def test_test_connection_uses_an_authenticated_call(app, api):
    api.replies.append(Reply({"config": {}}))
    assert app.test_connection() == "sabnzbd reachable, API key OK"
    assert api.sent[0]["data"]["mode"] == "get_config"  # mode=version would not check the key


def test_a_rejected_key_raises_with_sabnzbds_reason(app, api):
    api.replies.append(Reply({"status": False, "error": "API Key Incorrect"}))
    with pytest.raises(SabnzbdError, match="API Key Incorrect"):
        app.test_connection()


def test_http_errors_propagate(app, api):
    api.replies.append(Reply({}, status=500))
    with pytest.raises(requests.exceptions.HTTPError):
        app.get_config()


def test_a_response_without_config_is_unexpected(app, api):
    api.replies.append(Reply({"version": "4.5"}))
    with pytest.raises(SabnzbdError, match="unexpected get_config"):
        app.get_config()


# --- backup ----------------------------------------------------------------

def test_backup_saves_the_config_and_a_warning_about_masked_passwords(app, api, tmp_path, caplog):
    stored = config(misc={"port": "8080"}, servers=[{"name": "news", "password": MASKED}])
    api.replies.append(Reply(stored))
    out = tmp_path / "dump"
    assert app.backup(str(out)) == str(out)
    assert json.loads((out / "sabnzbd_config.json").read_text()) == stored
    note = (out / "_READ_ME_FIRST.txt").read_text()
    assert "masks password fields" in note and "Restore tab" in note and "restore.py" not in note
    assert "masked" in caplog.text


# --- restore ---------------------------------------------------------------

def calls(api):
    return [{k: v for k, v in c["data"].items() if k not in ("apikey", "output")} for c in api.sent]


def never(name, server):
    raise AssertionError("no password should be requested")


def test_servers_with_masked_passwords_ask_for_them(app, api):
    asked = []
    cfg = config(servers=[{"name": "news", "host": "news.example", "port": 563, "ssl": True, "password": MASKED, "username": "u"}])
    summary = app.restore(cfg, lambda name, server: asked.append(name) or "real-password")
    assert asked == ["news"] and summary["servers_restored"] == ["news"] and summary["servers_missing_password"] == []
    assert calls(api) == [{"mode": "set_config", "section": "servers", "keyword": "news",
                           "host": "news.example", "port": 563, "ssl": True, "username": "u", "password": "real-password"}]


@pytest.mark.parametrize("answer", [None, "", False])
def test_a_skipped_password_leaves_the_servers_existing_one_untouched(app, api, answer):
    summary = app.restore(config(servers=[{"name": "news", "host": "h", "password": MASKED}]), lambda *a: answer)
    assert summary["servers_missing_password"] == ["news"] and summary["servers_restored"] == ["news"]
    assert "password" not in calls(api)[0]


def test_servers_without_a_masked_password_are_never_prompted_for(app, api):
    summary = app.restore(config(servers=[{"name": "a", "password": ""}, {"name": "b"}]), never)
    assert summary["servers_restored"] == ["a", "b"] and summary["servers_missing_password"] == []


def test_nameless_servers_are_skipped_and_none_values_dropped(app, api, caplog):
    summary = app.restore(config(servers=[{"host": "orphan"}, {"name": "ok", "host": "h", "priority": None, "enable": False}]), never)
    assert summary["servers_restored"] == ["ok"] and "no name" in caplog.text
    sent = calls(api)
    assert len(sent) == 1 and "priority" not in sent[0] and sent[0]["enable"] is False  # False is a real value


def test_plain_sections_restore_key_by_key(app, api):
    summary = app.restore(config(misc={"bandwidth_perc": "41", "port": "8080"}, logging={"log_level": "1"}), never)
    assert summary["misc_keys_restored"] == ["misc.bandwidth_perc", "misc.port", "logging.log_level"]
    assert calls(api)[0] == {"mode": "set_config", "section": "misc", "keyword": "bandwidth_perc", "value": "41"}


def test_the_credentials_the_restore_itself_uses_are_never_rewritten(app, api, caplog):
    summary = app.restore(config(misc={"api_key": "new", "nzb_key": "new", "port": "8080"}), never)
    assert summary["misc_keys_restored"] == ["misc.port"] and "api_key" in caplog.text
    assert all(c.get("keyword") not in ("api_key", "nzb_key") for c in calls(api))


def test_other_masked_values_are_not_written_back(app, api, caplog):
    summary = app.restore(config(misc={"email_pwd": MASKED, "port": "8080"}, prowl={"prowl_apikey": MASKED}), never)
    assert summary["misc_keys_restored"] == ["misc.port"] and "email_pwd" in caplog.text
    assert not any(c.get("keyword") in ("email_pwd", "prowl_apikey") for c in calls(api))


def test_a_key_named_api_key_in_another_section_is_still_restored(app, api):
    app.restore(config(prowl={"api_key": "keep-me"}), never)
    assert calls(api)[0]["keyword"] == "api_key"


def test_categories_rss_and_sorters_are_reported_only_when_they_have_content(app, api):
    summary = app.restore(config(categories=[{"name": "tv"}], rss={}, sorters=[]), never)
    assert summary["sections_skipped"] == ["categories"] and api.sent == []


def test_non_section_values_are_ignored(app, api):
    assert app.restore(config(version="4.5", misc={}), never)["misc_keys_restored"] == [] and api.sent == []


def test_an_empty_backup_restores_nothing(app, api):
    summary = app.restore({}, never)
    assert summary == {"servers_restored": [], "servers_missing_password": [], "misc_keys_restored": [], "sections_skipped": []}


def test_a_failing_call_aborts_the_restore(app, api):
    api.replies.extend([Reply({"status": True}), Reply({"status": False, "error": "invalid value"})])
    with pytest.raises(SabnzbdError, match="invalid value"):
        app.restore(config(misc={"a": "1", "b": "2", "c": "3"}), never)
    assert len(api.sent) == 2
