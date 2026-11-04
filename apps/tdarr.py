"""Backup/restore driver for Tdarr 2 and 3 via its generic database endpoint.

Tdarr exposes its internal state through a single CRUD endpoint,
POST /api/v2/cruddb, keyed by collection name. Looping getAll over every
collection dumps Tdarr's config (library settings, flows, automations,
variables, dashboards, global settings, node registrations) and its
staged/output/statistics/job data as plain JSON - no filesystem access to the
server's data volume needed.

Checked against Tdarr 2.94.03 and 3.05.00:

- Auth: with Tdarr's authentication on - the default from 3.00.00, optional
  on 2.x - every cruddb call needs an API key (Tools > API Keys, `tapi_...`)
  in the `x-api-key` header; `Authorization: Bearer` is for login sessions
  and is refused ("Invalid token"). A 401's JSON says why: "No auth token
  provided", "Invalid API key", or "No users" with setupAdmin on a Tdarr 3
  whose admin account doesn't exist yet. /api/v2/status answers without auth.
- Collections: both serve the original eight plus Automations, Variables,
  Jobs and WorkerVerdictHistory; Dashboards is new in 3. A collection a
  version doesn't have is a 400 FST_ERR_VALIDATION, so each one is tried and
  skipped if missing - a Tdarr 3 backup restored onto Tdarr 2 leaves its
  dashboards out. Users and ApiKeys are 403 to the API and never touched.
"""
import json
import logging
import os

import requests

from apps.base import AppError
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")

# Every collection cruddb serves on Tdarr 2 or 3, settings first. The
# scan-state ones (FileJSONDB, StatisticsJSONDB, Jobs...) cost nothing extra.
COLLECTIONS = [
    "LibrarySettingsJSONDB",
    "SettingsGlobalJSONDB",
    "FlowsJSONDB",
    "AutomationsJSONDB",
    "VariablesJSONDB",
    "DashboardsJSONDB",
    "NodeJSONDB",
    "StagedJSONDB",
    "F2FOutputJSONDB",
    "StatisticsJSONDB",
    "FileJSONDB",
    "JobsJSONDB",
    "WorkerVerdictHistoryJSONDB",
]
# Not asked of older versions, which log the refusal as an error.
_SINCE_MAJOR = {"DashboardsJSONDB": 3}


def _major(version):
    try:
        return int(str(version).split(".")[0])
    except ValueError:
        return None


def _known_missing(collection, version):
    major = _major(version)
    return major is not None and major < _SINCE_MAJOR.get(collection, 0)


class TdarrError(AppError):
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


class _MissingCollection(Exception):
    """This Tdarr version has no such collection."""


class TdarrApp:
    def __init__(self, url, api_key=None, timeout=60):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update({"Accept": "application/json"})
        if api_key:
            self.session.headers.update({"x-api-key": api_key})

    def version(self):
        res = self.session.get(f"{self.url}/api/v2/status", timeout=10)
        res.raise_for_status()
        try:
            return res.json().get("version")
        except (ValueError, AttributeError):
            raise TdarrError("tdarr: /api/v2/status didn't return Tdarr's status - check the URL (Tdarr's server port, 8266)") from None

    def test_connection(self):
        version = self.version()
        # status answers without auth; this proves the key, or that none is needed.
        self.dump_collection("SettingsGlobalJSONDB")
        return f"tdarr {version or '?'} reachable"

    def _cruddb(self, payload):
        res = self.session.post(f"{self.url}/api/v2/cruddb", json={"data": payload}, timeout=self.timeout)
        if res.status_code == 401:
            raise TdarrError(self._unauthorized(res))
        if res.status_code == 400 and "collection must be equal to one of the allowed values" in res.text:
            raise _MissingCollection(payload["collection"])
        res.raise_for_status()
        if res.text.strip() == "":
            return None
        return res.json()

    def _unauthorized(self, res):
        try:
            body = res.json()
        except ValueError:
            body = {}
        message = body.get("message") if isinstance(body, dict) else None
        if isinstance(body, dict) and (body.get("setupAdmin") or message == "No users"):
            return ("tdarr: Tdarr has no admin account yet - create it in Tdarr's web UI, "
                    "then an API key under Tools > API Keys")
        if message == "No auth token provided" and "x-api-key" not in self.session.headers:
            return "tdarr: Tdarr's authentication is on - create an API key under Tools > API Keys and enter it"
        return "tdarr: unauthorized - check the API key (Tools > API Keys)"

    def dump_collection(self, collection):
        payload = self._cruddb({"collection": collection, "mode": "getAll"})
        return _extract_docs(payload) if payload is not None else []

    def backup(self, dest_dir):
        version = self.version()
        os.makedirs(dest_dir, exist_ok=True)
        for collection in COLLECTIONS:
            if _known_missing(collection, version):
                continue
            try:
                docs = self.dump_collection(collection)
            except _MissingCollection:
                logger.debug("tdarr: %s isn't in Tdarr %s, skipping", collection, version)
                continue
            with open(os.path.join(dest_dir, f"{collection}.json"), "w") as f:
                json.dump(docs, f)
            logger.info("tdarr: dumped %d doc(s) from %s", len(docs), collection)
        return dest_dir

    def restore_collection(self, collection, docs):
        # cruddb has no bulk insert (its modes: insert, update, getAll,
        # getById, getByIndex, removeOne, removeAll, removeByDB), so one
        # request per document - about 600 a second.
        self._cruddb({"collection": collection, "mode": "removeAll"})
        restored = 0
        for doc in docs:
            doc_id = doc.get("_id") or doc.get("id")
            if not doc_id:
                logger.warning("tdarr: skipping doc with no _id in %s", collection)
                continue
            try:
                self._cruddb({"collection": collection, "mode": "insert", "docID": doc_id, "obj": doc})
            except requests.RequestException as exc:
                raise TdarrError(
                    f"tdarr: the restore stopped partway through {collection} ({restored} of {len(docs)} "
                    f"documents put back) after {type(exc).__name__} - run the restore again to finish it"
                ) from exc
            restored += 1
        logger.info("tdarr: restored %d/%d doc(s) into %s", restored, len(docs), collection)

    def restore(self, backup_dir):
        # Every dump is read and checked before the first collection is
        # wiped, so a damaged file can't leave Tdarr half restored.
        dumps = {}
        for collection in COLLECTIONS:
            path = os.path.join(backup_dir, f"{collection}.json")
            if not os.path.isfile(path):
                logger.info("tdarr: %s isn't in the backup, left as it is", collection)
                continue
            try:
                with open(path) as f:
                    docs = json.load(f)
            except (OSError, ValueError) as exc:
                raise TdarrError(f"tdarr: {collection}.json in the backup can't be read ({exc}) - nothing was changed") from exc
            if not isinstance(docs, list) or not all(isinstance(doc, dict) for doc in docs):
                raise TdarrError(f"tdarr: {collection}.json in the backup isn't a list of documents - nothing was changed")
            dumps[collection] = docs
        if not dumps:
            raise TdarrError("tdarr: the backup holds no Tdarr collections - nothing was changed")
        version = self.version()
        for collection in list(dumps):
            try:
                if _known_missing(collection, version):
                    raise _MissingCollection(collection)
                self._cruddb({"collection": collection, "mode": "getById", "docID": "backuparr-probe"})
            except _MissingCollection:
                logger.warning("tdarr: skipping %s - this Tdarr version doesn't have it (the backup is from a newer Tdarr)",
                               collection)
                del dumps[collection]
        for collection, docs in dumps.items():
            self.restore_collection(collection, docs)
