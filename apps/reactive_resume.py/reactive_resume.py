"""Back up Reactive Resume, the self-hosted resume builder, over its API.

A backup is one account's export with uploads, saved as a zip in the same
layout as the app's own "Export everything" zip (packages/utils/src/account-zip.ts
in reactive-resume v6.1.1), so Reactive Resume's own restore steps apply as
written: each resume or cover letter is imported from its JSON file as a new
document.

The API key is a Read only key, sent in the x-api-key header, and it goes
only to the app's own host (SameHostSession). The export is a JSON document
(up to 64 MiB); its uploads ride as base64 (up to 16 MiB in all) and are
decoded into the zip. Files the export skipped (over its limits) are warned
about in the run log, and an expired key fails the backup with a plain
message. There is no restore over the API, so this driver only backs up.
"""

import base64
import binascii
import json
import logging
import os
import re
import unicodedata
import zipfile
from urllib.parse import quote

import requests

from apps.base import AppError, save_stream
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")


class ReactiveResumeError(AppError):
    pass


# The export's limits, as the app enforces them (accountBackupLimits in
# packages/utils/src/account-zip.ts). A zip over these is refused.
ARCHIVE_BYTES = 64 * 1024 * 1024
ARCHIVE_ENTRIES = 50_000
FILE_BYTES = 8 * 1024 * 1024
UPLOAD_BYTES = 16 * 1024 * 1024
UPLOAD_FILES = 500

# The most base64 a single file may carry: 8 MiB of bytes, encoded.
_MAX_BASE64_LEN = (FILE_BYTES + 2) // 3 * 4

_UPLOAD_MANIFEST_COVERAGE = (
    "Files referenced by exported records. External URLs and "
    "unreferenced/deleted files are not fetched."
)

_PART = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


def slugify_stem(value):
    """A URL-safe stem for a document's name, like the app's slugify."""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def component(value):
    """A path component, as the app's: URL-encoded, dots escaped."""
    return quote(str(value), safe="").replace(".", "%2E")


def file_name(name, doc_id):
    """The zip name for a resume or letter: slugified name, whole id."""
    stem = slugify_stem(name)
    if stem:
        return f"{stem}-{component(doc_id)}.json"
    return f"{component(doc_id)}.json"


def is_account_upload_key(key, user_id):
    """Whether an upload key is a storage path inside this account."""
    if not isinstance(key, str) or not isinstance(user_id, str):
        return False
    prefix = f"uploads/{user_id}/"
    if not key.startswith(prefix):
        return False
    rest = key[len(prefix):]
    if not rest:
        return False
    return all(_PART.match(part) for part in rest.split("/"))


def _json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _check_document(doc, kind):
    if not isinstance(doc, dict):
        raise ReactiveResumeError(
            f"reactive-resume: the export's {kind} is not an object"
        )
    if not isinstance(doc.get("id"), str) or not doc["id"]:
        raise ReactiveResumeError(
            f"reactive-resume: the export's {kind} has no id"
        )
    if not isinstance(doc.get("name"), str):
        raise ReactiveResumeError(
            f"reactive-resume: the export's {kind} has no name"
        )
    return doc


def _check_version(version, parent_field):
    if not isinstance(version, dict):
        raise ReactiveResumeError(
            "reactive-resume: the export's document version is not an object"
        )
    if not isinstance(version.get("id"), str) or not version["id"]:
        raise ReactiveResumeError(
            "reactive-resume: the export's document version has no id"
        )
    if not isinstance(version.get(parent_field), str) or not version[parent_field]:
        raise ReactiveResumeError(
            f"reactive-resume: the export's document version has no {parent_field}"
        )
    return version


def build_export_zip(data, zip_path):
    """Write the account export as the app's "Export everything" zip.

    Returns (included, skipped): the upload files written, and the ones the
    export skipped. Raises ReactiveResumeError when the export is over the
    app's limits or holds a path that escapes the account.
    """
    if not isinstance(data, dict):
        raise ReactiveResumeError(
            "reactive-resume: the export is not a JSON object"
        )
    user = data.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise ReactiveResumeError(
            "reactive-resume: the export has no account id"
        )
    for field in ("exportedAt", "applications", "career", "aiContentReports"):
        if field not in data:
            raise ReactiveResumeError(
                f"reactive-resume: the export has no {field}"
            )
    resumes = data.get("resumes")
    letters = data.get("coverLetters")
    if not isinstance(resumes, list) or not isinstance(letters, list):
        raise ReactiveResumeError(
            "reactive-resume: the export has no resumes or cover letters"
        )
    for doc in resumes:
        _check_document(doc, "resume")
    for doc in letters:
        _check_document(doc, "cover letter")
    versions = data.get("documentVersions")
    if versions is not None:
        if not isinstance(versions, dict):
            raise ReactiveResumeError(
                "reactive-resume: the export's documentVersions are not an object"
            )
        for version in versions.get("resumes") or []:
            _check_version(version, "resumeId")
        for version in versions.get("letters") or []:
            _check_version(version, "coverLetterId")
    uploads = data.get("uploads")
    if uploads is not None and not isinstance(uploads, dict):
        raise ReactiveResumeError(
            "reactive-resume: the export's uploads are not an object"
        )
    files = (uploads or {}).get("files") or []
    skipped = (uploads or {}).get("skipped") or []
    if not isinstance(files, list) or not isinstance(skipped, list):
        raise ReactiveResumeError(
            "reactive-resume: the export's uploads have no files or skipped list"
        )
    for item in files:
        if not isinstance(item, dict):
            raise ReactiveResumeError(
                "reactive-resume: the export's upload is not an object"
            )
        if not isinstance(item.get("key"), str) or not isinstance(
            item.get("base64"), str
        ):
            raise ReactiveResumeError(
                "reactive-resume: the export's upload has no key or base64"
            )

    written = set()
    total_bytes = 0
    entries = 0
    included = []
    upload_total = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:

        def add(path, payload):
            nonlocal total_bytes, entries
            if path in written:
                raise ReactiveResumeError(
                    "reactive-resume: duplicate path in the export zip: "
                    f"{path}"
                )
            entries += 1
            if (
                total_bytes + len(payload) > ARCHIVE_BYTES
                or entries > ARCHIVE_ENTRIES
            ):
                raise ReactiveResumeError(
                    "reactive-resume: the account archive exceeds its size "
                    "or file-count limit"
                )
            total_bytes += len(payload)
            written.add(path)
            archive.writestr(path, payload)

        def add_json(path, value):
            add(path, _json_bytes(value))

        add_json("account.json", {"exportedAt": data["exportedAt"], "user": user})
        add_json("applications.json", data["applications"])
        if data.get("applicationPipeline") is not None:
            add_json("application-stages.json", data["applicationPipeline"])
        add_json("career.json", data["career"])
        if data.get("applicationProfile") is not None:
            add_json("application-profile.json", data["applicationProfile"])
        if versions is not None:
            add_json("document-versions.json", versions)
        if data.get("feedback") is not None:
            add_json("feedback.json", data["feedback"])
        add_json("ai-content-reports.json", data["aiContentReports"])
        for doc in resumes:
            add_json(f"resumes/{file_name(doc['name'], doc['id'])}", doc)
        for doc in letters:
            add_json(
                f"letters/{file_name(doc['name'], doc['id'])}",
                {**doc, "format": "reactive-resume-cover-letter", "version": 1},
            )
        if versions is not None:
            for version in versions.get("resumes") or []:
                add_json(
                    f"versions/resumes/{component(version['resumeId'])}"
                    f"/{component(version['id'])}.json",
                    version,
                )
            for version in versions.get("letters") or []:
                add_json(
                    f"versions/letters/{component(version['coverLetterId'])}"
                    f"/{component(version['id'])}.json",
                    version,
                )
        if uploads is not None:
            user_id = user["id"]
            for item in files:
                key = item["key"]
                if not is_account_upload_key(key, user_id) or key in written:
                    raise ReactiveResumeError(
                        "reactive-resume: invalid account upload path in "
                        f"the export: {key}"
                    )
                raw_b64 = item["base64"]
                if len(raw_b64) > _MAX_BASE64_LEN:
                    raise ReactiveResumeError(
                        "reactive-resume: the account archive exceeds its "
                        "size or file-count limit"
                    )
                try:
                    raw = base64.b64decode(raw_b64, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ReactiveResumeError(
                        "reactive-resume: the export's upload is not valid "
                        f"base64: {key}"
                    ) from exc
                upload_total += len(raw)
                if (
                    len(raw) > FILE_BYTES
                    or upload_total > UPLOAD_BYTES
                    or len(included) >= UPLOAD_FILES
                ):
                    raise ReactiveResumeError(
                        "reactive-resume: the account archive exceeds its "
                        "size or file-count limit"
                    )
                add(key, raw)
                included.append({"key": key, "path": key, "bytes": len(raw)})
            add_json(
                "upload-manifest.json",
                {
                    "format": 1,
                    "exportedAt": data["exportedAt"],
                    "limits": {
                        "fileBytes": FILE_BYTES,
                        "uploadBytes": UPLOAD_BYTES,
                        "files": UPLOAD_FILES,
                        "archiveBytes": ARCHIVE_BYTES,
                        "archiveEntries": ARCHIVE_ENTRIES,
                    },
                    "included": included,
                    "skipped": skipped,
                    "coverage": _UPLOAD_MANIFEST_COVERAGE,
                },
            )
    return included, skipped


class ReactiveResumeApp:
    """One Reactive Resume account, backed up over its API."""

    def __init__(self, url, api_key, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update(
            {"x-api-key": api_key, "Accept": "application/json"}
        )

    def _api(self, path):
        return f"{self.url}{path}"

    def _get(self, path, timeout=None, **kwargs):
        try:
            res = self.session.get(
                self._api(path),
                timeout=self.timeout if timeout is None else timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ReactiveResumeError(
                f"reactive-resume: {type(exc).__name__} calling {path}"
            ) from exc
        if res.status_code == 401:
            raise ReactiveResumeError(
                "reactive-resume: unauthorized - the API key may have "
                "expired; make a new Read only key"
            )
        return res

    def _health(self):
        res = self._get("/api/health")
        res.raise_for_status()
        try:
            data = res.json()
        except ValueError as exc:
            raise ReactiveResumeError(
                "reactive-resume: /api/health didn't return JSON"
            ) from exc
        if not isinstance(data, dict) or not data.get("version"):
            raise ReactiveResumeError(
                "reactive-resume: /api/health didn't return a version"
            )
        return data["version"]

    def app_version(self):
        return self._health()

    def _profile_name(self):
        res = self._get("/api/openapi/auth/profile")
        res.raise_for_status()
        try:
            profile = res.json()
        except ValueError as exc:
            raise ReactiveResumeError(
                "reactive-resume: /api/openapi/auth/profile didn't return JSON"
            ) from exc
        if not isinstance(profile, dict):
            raise ReactiveResumeError(
                "reactive-resume: /api/openapi/auth/profile didn't return "
                "a profile"
            )
        for field in ("name", "displayUsername", "username", "email"):
            value = profile.get(field)
            if isinstance(value, str) and value:
                return value
        return None

    def test_connection(self):
        version = self._health()
        name = self._profile_name()
        if name:
            return f"reactive-resume {version} reachable as {name}"
        return f"reactive-resume {version} reachable"

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        export_path = os.path.join(dest_dir, "export.json")
        zip_path = os.path.join(dest_dir, "reactive-resume.zip")
        try:
            res = self._get(
                "/api/openapi/auth/account/export",
                params={"includeUploads": "true"},
                stream=True,
                timeout=300,
            )
            res.raise_for_status()
            save_stream(res, export_path)
            with open(export_path, encoding="utf-8") as handle:
                try:
                    data = json.load(handle)
                except json.JSONDecodeError as exc:
                    raise ReactiveResumeError(
                        "reactive-resume: the export is not valid JSON"
                    ) from exc
            if isinstance(data, dict) and "uploads" not in data:
                logger.warning(
                    "reactive-resume: the export didn't include uploads"
                )
            included, skipped = build_export_zip(data, zip_path)
            if skipped:
                detail = ", ".join(
                    f"{item.get('key', '?')} ({item.get('reason', '?')})"
                    for item in skipped
                    if isinstance(item, dict)
                )
                logger.warning(
                    "reactive-resume: the export skipped %d file(s) over "
                    "the limits: %s",
                    len(skipped),
                    detail,
                )
            logger.info(
                "reactive-resume: backed up %d resume(s), %d cover "
                "letter(s) and %d upload(s)",
                len(data.get("resumes") or []),
                len(data.get("coverLetters") or []),
                len(included),
            )
            return zip_path
        finally:
            if os.path.exists(export_path):
                os.remove(export_path)
