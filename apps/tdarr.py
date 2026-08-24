"""Backup/restore driver for Tdarr via its generic database endpoint.

Tdarr exposes all of its internal state through a single CRUD endpoint,
POST /api/v2/cruddb, keyed by collection name. Looping getAll over every
collection dumps Tdarr's entire config (library settings, flows, global
settings, node registrations, staged/output/statistics data) as plain JSON -
no filesystem access to the server's data volume needed.

Tdarr's own auth-token feature (if enabled in server settings) is not
publicly documented as of this writing; TDARR_API_KEY is sent as a bearer
token, which is the common convention, but verify against your Tdarr
version if you have API auth turned on.
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

# All 8 known collections. FileJSONDB and StatisticsJSONDB are more "scan
# state" than configuration (they get rebuilt as libraries are rescanned),
# but there's no extra cost to including them since it's the same call
# shape for every collection.
COLLECTIONS = [
    "LibrarySettingsJSONDB",
    "SettingsGlobalJSONDB",
    "FlowsJSONDB",
    "NodeJSONDB",
    "StagedJSONDB",
    "F2FOutputJSONDB",
    "StatisticsJSONDB",
    "FileJSONDB",
]


class TdarrError(RuntimeError):
    pass


def _extract_docs(payload):
    """cruddb's getAll response shape isn't formally documented; handle a
    plain list or a dict wrapping one under a common key."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "array", "result", "results", "docs"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise TdarrError(f"tdarr: unrecognized cruddb response shape: {type(payload).__name__}")


class TdarrApp:
    def __init__(self, url, api_key=None, timeout=60):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def test_connection(self):
        res = self.session.get(f"{self.url}/api/v2/status", timeout=10)
        if res.status_code == 401:
            raise TdarrError("tdarr: unauthorized - check the API key, or the header format may not match your version")
        res.raise_for_status()
        return "tdarr reachable"

    def _cruddb(self, payload):
        res = self.session.post(f"{self.url}/api/v2/cruddb", json={"data": payload}, timeout=self.timeout)
        res.raise_for_status()
        if res.text.strip() == "":
            return None
        return res.json()

    def dump_collection(self, collection):
        payload = self._cruddb({"collection": collection, "mode": "getAll"})
        return _extract_docs(payload) if payload is not None else []

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        for collection in COLLECTIONS:
            docs = self.dump_collection(collection)
            with open(os.path.join(dest_dir, f"{collection}.json"), "w") as f:
                json.dump(docs, f)
            logger.info("tdarr: dumped %d doc(s) from %s", len(docs), collection)
        return dest_dir

    def restore_collection(self, collection, docs):
        self._cruddb({"collection": collection, "mode": "removeAll"})
        restored = 0
        for doc in docs:
            doc_id = doc.get("_id") or doc.get("id")
            if not doc_id:
                logger.warning("tdarr: skipping doc with no _id in %s", collection)
                continue
            self._cruddb({"collection": collection, "mode": "insert", "docID": doc_id, "obj": doc})
            restored += 1
        logger.info("tdarr: restored %d/%d doc(s) into %s", restored, len(docs), collection)

    def restore(self, backup_dir):
        for collection in COLLECTIONS:
            path = os.path.join(backup_dir, f"{collection}.json")
            if not os.path.isfile(path):
                logger.warning("tdarr: no dump found for %s, skipping", collection)
                continue
            with open(path) as f:
                docs = json.load(f)
            self.restore_collection(collection, docs)
