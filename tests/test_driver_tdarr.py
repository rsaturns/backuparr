import json

import pytest
import requests

from apps.tdarr import COLLECTIONS, TdarrApp, TdarrError, _extract_docs


class Reply:
    def __init__(self, data=None, status=200, text=None):
        self.data, self.status_code = data, status
        self.text = text if text is not None else ("" if data is None else json.dumps(data))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        return self.data


class Session:
    """Answers cruddb calls from per-collection data and records every call."""

    def __init__(self, collections=None):
        self.headers = {}
        self.collections = collections or {}
        self.requests = []

    def update_headers(self, headers):
        self.headers.update(headers)

    def get(self, url, **kwargs):
        self.requests.append(("GET", url))
        return Reply(status=self.status if hasattr(self, "status") else 200)

    def post(self, url, json=None, timeout=None):
        payload = json["data"]
        self.requests.append(("POST", url, payload))
        if payload["mode"] == "getAll":
            return Reply(self.collections.get(payload["collection"], []))
        return Reply(None, text="")


def app_with(collections=None, api_key=None):
    app = TdarrApp("http://tdarr:8266/", api_key=api_key)
    app.session = Session(collections)
    return app


def writes(app):
    return [call[2] for call in app.session.requests if call[0] == "POST" and call[2]["mode"] != "getAll"]


# --- construction and connection -------------------------------------------

def test_the_key_becomes_a_bearer_token_only_when_given():
    assert "Authorization" not in TdarrApp("http://t").session.headers
    assert TdarrApp("http://t", api_key="abc").session.headers["Authorization"] == "Bearer abc"
    assert TdarrApp("http://t").session.headers["Accept"] == "application/json"
    assert TdarrApp("http://tdarr:8266/").url == "http://tdarr:8266"


def test_connection_test_checks_status():
    app = app_with()
    assert app.test_connection() == "tdarr reachable"
    assert app.session.requests == [("GET", "http://tdarr:8266/api/v2/status")]


def test_a_401_suggests_checking_the_key():
    app = app_with()
    app.session.status = 401
    with pytest.raises(TdarrError, match="unauthorized"):
        app.test_connection()


def test_other_http_errors_propagate():
    app = app_with()
    app.session.status = 500
    with pytest.raises(requests.exceptions.HTTPError):
        app.test_connection()


# --- reading collections ---------------------------------------------------

@pytest.mark.parametrize("payload", [
    [{"_id": "a"}],
    {"data": [{"_id": "a"}]},
    {"array": [{"_id": "a"}]},
    {"result": [{"_id": "a"}]},
    {"results": [{"_id": "a"}]},
    {"docs": [{"_id": "a"}]},
])
def test_every_known_response_shape_is_understood(payload):
    assert _extract_docs(payload) == [{"_id": "a"}]


@pytest.mark.parametrize("payload", ["text", 5, {"unrelated": []}, {"data": "not a list"}, None])
def test_an_unknown_response_shape_is_an_error(payload):
    with pytest.raises(TdarrError, match="unrecognized cruddb response"):
        _extract_docs(payload)


def test_an_empty_reply_means_an_empty_collection():
    app = app_with()
    app.session.post = lambda url, json=None, timeout=None: Reply(None, text="  ")
    assert app.dump_collection("FlowsJSONDB") == []


def test_dump_asks_for_every_document_of_one_collection():
    app = app_with({"FlowsJSONDB": [{"_id": "f1"}]})
    assert app.dump_collection("FlowsJSONDB") == [{"_id": "f1"}]
    assert app.session.requests == [("POST", "http://tdarr:8266/api/v2/cruddb", {"collection": "FlowsJSONDB", "mode": "getAll"})]


# --- backup ----------------------------------------------------------------

def test_backup_writes_one_json_file_per_collection(tmp_path):
    data = {"FlowsJSONDB": [{"_id": "f1", "name": "flow"}], "LibrarySettingsJSONDB": [{"_id": "l1"}]}
    out = tmp_path / "dump"
    assert app_with(data).backup(str(out)) == str(out)
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{c}.json" for c in COLLECTIONS)
    assert json.loads((out / "FlowsJSONDB.json").read_text()) == data["FlowsJSONDB"]
    assert json.loads((out / "FileJSONDB.json").read_text()) == []


def test_backup_fails_loudly_on_an_unreadable_collection(tmp_path):
    app = app_with()
    app.session.post = lambda url, json=None, timeout=None: Reply("surprise")
    with pytest.raises(TdarrError):
        app.backup(str(tmp_path))


# --- restore ---------------------------------------------------------------

def test_restoring_a_collection_clears_it_then_inserts_each_document():
    app = app_with()
    app.restore_collection("FlowsJSONDB", [{"_id": "a", "n": 1}, {"id": "b", "n": 2}])
    assert writes(app) == [
        {"collection": "FlowsJSONDB", "mode": "removeAll"},
        {"collection": "FlowsJSONDB", "mode": "insert", "docID": "a", "obj": {"_id": "a", "n": 1}},
        {"collection": "FlowsJSONDB", "mode": "insert", "docID": "b", "obj": {"id": "b", "n": 2}},
    ]


def test_documents_without_an_id_are_skipped_not_fatal(caplog):
    app = app_with()
    app.restore_collection("FlowsJSONDB", [{"name": "orphan"}, {"_id": "ok"}])
    assert [w["mode"] for w in writes(app)] == ["removeAll", "insert"] and "no _id" in caplog.text


def test_restore_reads_every_dump_file_and_skips_missing_ones(tmp_path, caplog):
    (tmp_path / "FlowsJSONDB.json").write_text(json.dumps([{"_id": "f1"}]))
    (tmp_path / "NodeJSONDB.json").write_text("[]")
    app = app_with()
    app.restore(str(tmp_path))
    restored = [(w["collection"], w["mode"]) for w in writes(app)]
    assert restored == [("FlowsJSONDB", "removeAll"), ("FlowsJSONDB", "insert"), ("NodeJSONDB", "removeAll")]
    assert caplog.text.count("no dump found") == len(COLLECTIONS) - 2


def test_restore_follows_the_documented_collection_order(tmp_path):
    for collection in COLLECTIONS:
        (tmp_path / f"{collection}.json").write_text("[]")
    app = app_with()
    app.restore(str(tmp_path))
    assert [w["collection"] for w in writes(app)] == COLLECTIONS


def test_a_failing_write_stops_the_restore():
    app = app_with()

    def post(url, json=None, timeout=None):
        if json["data"]["mode"] == "insert":
            return Reply(status=500)
        return Reply(None, text="")

    app.session.post = post
    with pytest.raises(requests.exceptions.HTTPError):
        app.restore_collection("FlowsJSONDB", [{"_id": "a"}])
