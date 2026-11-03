import json

import pytest
import requests

from apps.tdarr import COLLECTIONS, TdarrApp, TdarrError, _extract_docs


MISSING = '{"statusCode":400,"code":"FST_ERR_VALIDATION","error":"Bad Request","message":"body/data/collection must be equal to one of the allowed values"}'


class Reply:
    def __init__(self, data=None, status=200, text=None):
        self.data, self.status_code = data, status
        self.text = text if text is not None else ("" if data is None else json.dumps(data))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        if self.data is None and self.text:
            return json.loads(self.text)
        return self.data


class Session:
    """Answers like a Tdarr server: /status, and cruddb calls from
    per-collection data. `missing` are collections this version lacks;
    `denied` is the JSON body of a 401 for every cruddb call."""

    def __init__(self, collections=None, version="3.05.00", missing=(), denied=None):
        self.headers = {}
        self.collections = collections or {}
        self.version, self.missing, self.denied = version, set(missing), denied
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url))
        status = getattr(self, "status", 200)
        return Reply({"status": "good", "version": self.version} if status == 200 else None, status=status)

    def post(self, url, json=None, timeout=None):
        payload = json["data"]
        self.requests.append(("POST", url, payload))
        if self.denied is not None:
            return Reply(self.denied, status=401)
        if payload["collection"] in self.missing:
            return Reply(None, status=400, text=MISSING)
        if payload["mode"] == "getAll":
            return Reply(self.collections.get(payload["collection"], []))
        return Reply(None, text="")


def app_with(collections=None, api_key=None, **server):
    app = TdarrApp("http://tdarr:8266/", api_key=api_key)
    app.session = Session(collections, **server)
    return app


def writes(app):
    return [call[2] for call in app.session.requests
            if call[0] == "POST" and call[2]["mode"] not in ("getAll", "getById")]


# Tdarr 2.94 has every collection but dashboards.
TDARR_2 = {"version": "2.94.03", "missing": {"DashboardsJSONDB"}}


# --- construction and connection -------------------------------------------

def test_the_key_goes_in_x_api_key_only_when_given():
    # Tdarr 2 and 3 both refuse it as a Bearer token ("Invalid token").
    assert "x-api-key" not in TdarrApp("http://t").session.headers
    headers = TdarrApp("http://t", api_key="tapi_abc").session.headers
    assert headers["x-api-key"] == "tapi_abc" and "Authorization" not in headers
    assert TdarrApp("http://t").session.headers["Accept"] == "application/json"
    assert TdarrApp("http://tdarr:8266/").url == "http://tdarr:8266"


@pytest.mark.parametrize("server", [{}, TDARR_2])
def test_connection_test_checks_status_and_that_the_api_answers(server):
    app = app_with(**server)
    assert app.test_connection() == f"tdarr {server.get('version', '3.05.00')} reachable"
    assert app.session.requests == [
        ("GET", "http://tdarr:8266/api/v2/status"),
        ("POST", "http://tdarr:8266/api/v2/cruddb", {"collection": "SettingsGlobalJSONDB", "mode": "getAll"}),
    ]


@pytest.mark.parametrize("body, api_key, message", [
    ({"status": 401, "message": "No users", "setupAdmin": True}, None, "no admin account yet - create it in Tdarr's web UI"),
    ({"status": 401, "message": "No auth token provided"}, None, "authentication is on - create an API key under Tools > API Keys"),
    ({"status": 401, "message": "Invalid API key"}, "tapi_wrong", r"unauthorized - check the API key \(Tools > API Keys\)"),
    ({"status": 401, "message": "Invalid token"}, "tapi_wrong", "unauthorized - check the API key"),
])
def test_each_401_says_what_tdarr_needs(body, api_key, message):
    with pytest.raises(TdarrError, match=message):
        app_with(api_key=api_key, denied=body).test_connection()


def test_other_http_errors_propagate():
    app = app_with()
    app.session.status = 500
    with pytest.raises(requests.exceptions.HTTPError):
        app.test_connection()


def test_something_else_at_the_url_is_explained():
    app = app_with()
    app.session.get = lambda url, **kwargs: Reply(None, text="<html>web UI</html>")
    with pytest.raises(TdarrError, match="didn't return Tdarr's status - check the URL"):
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
    data = {"FlowsJSONDB": [{"_id": "f1", "name": "flow"}], "LibrarySettingsJSONDB": [{"_id": "l1"}],
            "VariablesJSONDB": [{"_id": "v1", "key": "k", "value": "v"}], "DashboardsJSONDB": [{"_id": "d1"}]}
    out = tmp_path / "dump"
    assert app_with(data).backup(str(out)) == str(out)
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{c}.json" for c in COLLECTIONS)
    assert json.loads((out / "FlowsJSONDB.json").read_text()) == data["FlowsJSONDB"]
    assert json.loads((out / "VariablesJSONDB.json").read_text()) == data["VariablesJSONDB"]
    assert json.loads((out / "FileJSONDB.json").read_text()) == []


def test_backup_of_tdarr_2_skips_the_collections_it_doesnt_have(tmp_path):
    out = tmp_path / "dump"
    app_with({"AutomationsJSONDB": [{"_id": "a1"}]}, **TDARR_2).backup(str(out))
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{c}.json" for c in COLLECTIONS if c != "DashboardsJSONDB")
    assert json.loads((out / "AutomationsJSONDB.json").read_text()) == [{"_id": "a1"}]


def test_tdarr_2_is_never_asked_for_dashboards(tmp_path):
    # It would log the refusal as an error on every backup.
    app = app_with(**TDARR_2)
    app.backup(str(tmp_path / "dump"))
    asked = {call[2]["collection"] for call in app.session.requests if call[0] == "POST"}
    assert "DashboardsJSONDB" not in asked and asked == set(COLLECTIONS) - {"DashboardsJSONDB"}


def test_an_unknown_version_still_falls_back_to_tdarrs_answer(tmp_path):
    out = tmp_path / "dump"
    app_with(version="nightly", missing={"DashboardsJSONDB"}).backup(str(out))
    assert "DashboardsJSONDB.json" not in {p.name for p in out.iterdir()}


def test_users_and_api_keys_are_never_backed_up():
    # Tdarr answers 403 for them; restoring them could lock Backuparr out.
    assert not {"UsersJSONDB", "ApiKeysJSONDB"} & set(COLLECTIONS)


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
    caplog.set_level("INFO")
    (tmp_path / "FlowsJSONDB.json").write_text(json.dumps([{"_id": "f1"}]))
    (tmp_path / "NodeJSONDB.json").write_text("[]")
    app = app_with()
    app.restore(str(tmp_path))
    restored = [(w["collection"], w["mode"]) for w in writes(app)]
    assert restored == [("FlowsJSONDB", "removeAll"), ("FlowsJSONDB", "insert"), ("NodeJSONDB", "removeAll")]
    assert caplog.text.count("isn't in the backup, left as it is") == len(COLLECTIONS) - 2


def test_restore_follows_the_documented_collection_order(tmp_path):
    for collection in COLLECTIONS:
        (tmp_path / f"{collection}.json").write_text("[]")
    app = app_with()
    app.restore(str(tmp_path))
    assert [w["collection"] for w in writes(app)] == COLLECTIONS


@pytest.mark.parametrize("failure", [Reply(status=500), requests.exceptions.ConnectionError("reset")])
def test_a_failing_write_stops_the_restore_and_says_where(failure):
    app = app_with()
    inserted = []

    def post(url, json=None, timeout=None):
        if json["data"]["mode"] == "insert":
            if len(inserted) == 2:
                if isinstance(failure, Exception):
                    raise failure
                return failure
            inserted.append(json["data"]["docID"])
        return Reply(None, text="")

    app.session.post = post
    with pytest.raises(TdarrError, match=r"partway through FileJSONDB \(2 of 4 documents put back\).*run the restore again"):
        app.restore_collection("FileJSONDB", [{"_id": i} for i in "abcd"])
    assert inserted == ["a", "b"]


@pytest.mark.parametrize("bad", ["{not json", '{"a": 1}', "[1, 2]"])
def test_a_damaged_dump_stops_the_restore_before_anything_is_wiped(tmp_path, bad):
    for collection in COLLECTIONS:
        (tmp_path / f"{collection}.json").write_text(json.dumps([{"_id": collection}]))
    (tmp_path / f"{COLLECTIONS[-1]}.json").write_text(bad)
    app = app_with()
    with pytest.raises(TdarrError, match="nothing was changed"):
        app.restore(str(tmp_path))
    assert writes(app) == []


def test_a_tdarr_3_backup_restored_onto_tdarr_2_leaves_out_only_dashboards(tmp_path, caplog):
    for collection in COLLECTIONS:
        (tmp_path / f"{collection}.json").write_text(json.dumps([{"_id": collection}]))
    app = app_with(**TDARR_2)
    app.restore(str(tmp_path))
    assert [w["collection"] for w in writes(app) if w["mode"] == "removeAll"] == [c for c in COLLECTIONS if c != "DashboardsJSONDB"]
    assert "skipping DashboardsJSONDB - this Tdarr version doesn't have it" in caplog.text


def test_an_older_backup_restored_onto_tdarr_3_leaves_the_newer_collections_alone(tmp_path):
    # Backups made before automations, variables and dashboards were included.
    old = ["LibrarySettingsJSONDB", "SettingsGlobalJSONDB", "FlowsJSONDB", "NodeJSONDB",
           "StagedJSONDB", "F2FOutputJSONDB", "StatisticsJSONDB", "FileJSONDB"]
    for collection in old:
        (tmp_path / f"{collection}.json").write_text("[]")
    app = app_with()
    app.restore(str(tmp_path))
    assert [w["collection"] for w in writes(app)] == old


def test_a_backup_with_no_collections_is_refused(tmp_path):
    app = app_with()
    with pytest.raises(TdarrError, match="holds no Tdarr collections"):
        app.restore(str(tmp_path))
    assert writes(app) == []
