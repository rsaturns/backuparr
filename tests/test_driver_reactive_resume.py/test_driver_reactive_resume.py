"""Reactive Resume: the export zip, the API key, and the backup itself."""

import base64
import io
import json
import logging
import zipfile

import pytest
import requests

import backup
from apps import reactive_resume
from apps.reactive_resume import (
    ReactiveResumeApp,
    ReactiveResumeError,
    build_export_zip,
    component,
    file_name,
    is_account_upload_key,
    slugify_stem,
)
from apps.servarr import SameHostSession

KEY = "reactive-resume-read-only-key"
URL = "http://reactive-resume:3000"
USER_ID = "user-1"


def export_data(**overrides):
    """One account's export, as GET /api/openapi/auth/account/export returns it."""
    data = {
        "exportedAt": "2026-11-26T10:00:00.000Z",
        "user": {"id": USER_ID, "name": "Tarek", "email": "tarek@example.com"},
        "applications": [{"id": "app-1", "title": "Engineer"}],
        "applicationPipeline": {"stages": ["Applied"]},
        "career": {"jobs": []},
        "applicationProfile": {"headline": "Builder"},
        "documentVersions": {
            "resumes": [{"id": "v1", "resumeId": "res-1"}],
            "letters": [{"id": "v2", "coverLetterId": "let-1"}],
        },
        "feedback": {"rating": 5},
        "aiContentReports": [],
        "resumes": [{"id": "res-1", "name": "My Resume", "basics": {"name": "Tarek"}}],
        "coverLetters": [{"id": "let-1", "name": "Cover Letter"}],
        "uploads": {
            "files": [
                {
                    "key": f"uploads/{USER_ID}/photo.png",
                    "base64": base64.b64encode(b"png-bytes").decode(),
                }
            ],
            "skipped": [{"key": f"uploads/{USER_ID}/big.bin", "reason": "file-limit"}],
        },
    }
    data.update(overrides)
    return data


@pytest.fixture
def server(monkeypatch):
    """Answers each "METHOD /path" from `routes` (status, body); records requests."""
    state = type("Server", (), {})()
    state.sent = []
    state.routes = {
        "GET /api/health": (200, {"version": "6.1.1"}),
        "GET /api/openapi/auth/profile": (200, {"id": USER_ID, "name": "Tarek"}),
        "GET /api/openapi/auth/account/export": (200, export_data()),
    }

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        status, body = state.routes.get(
            f"{request.method} {request.path_url.split('?')[0]}", (404, {})
        )
        if isinstance(body, Exception):
            raise body
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = json.dumps(body).encode()
        res.raw = io.BytesIO(res._content)
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


@pytest.fixture
def app():
    return ReactiveResumeApp(URL, KEY)


# --- naming helpers ----------------------------------------------------------

def test_slugify_stem_turns_a_name_into_a_url_safe_stem():
    assert slugify_stem("My Resume") == "my-resume"
    assert slugify_stem("Café CV") == "cafe-cv"
    assert slugify_stem("  Spaces & Dashes-- ") == "spaces-dashes"
    assert slugify_stem("") == ""


def test_component_escapes_dots_and_slashes():
    assert component("a.b") == "a%2Eb"
    assert component("a/b") == "a%2Fb"
    assert component("abc-1") == "abc-1"


def test_file_name_keeps_the_whole_id_and_falls_back_to_it():
    assert file_name("My Resume", "res-1") == "my-resume-res-1.json"
    assert file_name("", "res-1") == "res-1.json"
    assert file_name("!!!", "res-1") == "res-1.json"


def test_an_upload_key_must_stay_inside_the_accounts_uploads():
    good = f"uploads/{USER_ID}/photo.png"
    assert is_account_upload_key(good, USER_ID)
    assert not is_account_upload_key("uploads/other/photo.png", USER_ID)
    assert not is_account_upload_key(f"uploads/{USER_ID}/../secret", USER_ID)
    assert not is_account_upload_key(f"uploads/{USER_ID}/", USER_ID)
    assert not is_account_upload_key(None, USER_ID)
    assert not is_account_upload_key(good, None)


# --- the export zip ----------------------------------------------------------

def test_build_export_zip_lays_the_files_out_like_export_everything(tmp_path):
    data = export_data()
    zip_path = tmp_path / "reactive-resume.zip"
    included, skipped = build_export_zip(data, str(zip_path))
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == [
            "account.json",
            "applications.json",
            "application-stages.json",
            "career.json",
            "application-profile.json",
            "document-versions.json",
            "feedback.json",
            "ai-content-reports.json",
            "resumes/my-resume-res-1.json",
            "letters/cover-letter-let-1.json",
            "versions/resumes/res-1/v1.json",
            "versions/letters/let-1/v2.json",
            f"uploads/{USER_ID}/photo.png",
            "upload-manifest.json",
        ]
        assert json.loads(zf.read("account.json")) == {
            "exportedAt": data["exportedAt"],
            "user": data["user"],
        }
        assert json.loads(zf.read("resumes/my-resume-res-1.json")) == data["resumes"][0]
        letter = json.loads(zf.read("letters/cover-letter-let-1.json"))
        assert letter["format"] == "reactive-resume-cover-letter"
        assert letter["version"] == 1
        assert letter["id"] == "let-1"
        assert zf.read(f"uploads/{USER_ID}/photo.png") == b"png-bytes"
        manifest = json.loads(zf.read("upload-manifest.json"))
        assert manifest["format"] == 1
        assert manifest["exportedAt"] == data["exportedAt"]
        assert manifest["limits"] == {
            "fileBytes": 8 * 1024 * 1024,
            "uploadBytes": 16 * 1024 * 1024,
            "files": 500,
            "archiveBytes": 64 * 1024 * 1024,
            "archiveEntries": 50_000,
        }
        assert manifest["included"] == [
            {
                "key": f"uploads/{USER_ID}/photo.png",
                "path": f"uploads/{USER_ID}/photo.png",
                "bytes": len(b"png-bytes"),
            }
        ]
        assert manifest["skipped"] == data["uploads"]["skipped"]
        assert "External URLs" in manifest["coverage"]
    assert included == manifest["included"]
    assert skipped == data["uploads"]["skipped"]


def test_build_export_zip_without_uploads_writes_no_manifest(tmp_path):
    data = export_data()
    del data["uploads"]
    zip_path = tmp_path / "reactive-resume.zip"
    included, skipped = build_export_zip(data, str(zip_path))
    with zipfile.ZipFile(zip_path) as zf:
        assert "upload-manifest.json" not in zf.namelist()
        assert not [name for name in zf.namelist() if name.startswith("uploads/")]
        assert "account.json" in zf.namelist()
    assert included == [] and skipped == []


def test_build_export_zip_refuses_an_upload_path_outside_the_account(tmp_path):
    data = export_data()
    data["uploads"]["files"] = [
        {
            "key": f"uploads/{USER_ID}/../../escape.txt",
            "base64": base64.b64encode(b"x").decode(),
        }
    ]
    with pytest.raises(ReactiveResumeError, match="invalid account upload path"):
        build_export_zip(data, str(tmp_path / "reactive-resume.zip"))


def test_build_export_zip_refuses_an_archive_over_the_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(reactive_resume, "ARCHIVE_BYTES", 10)
    with pytest.raises(ReactiveResumeError, match="exceeds its size"):
        build_export_zip(export_data(), str(tmp_path / "reactive-resume.zip"))


def test_build_export_zip_refuses_an_export_without_an_account(tmp_path):
    with pytest.raises(ReactiveResumeError, match="no account id"):
        build_export_zip({"exportedAt": "x"}, str(tmp_path / "reactive-resume.zip"))


# --- connection --------------------------------------------------------------

def test_the_key_goes_in_its_own_header_never_the_url(app, server):
    assert app.test_connection() == "reactive-resume 6.1.1 reachable as Tarek"
    request = server.sent[0]
    assert request.url == "http://reactive-resume:3000/api/health"
    assert request.headers["x-api-key"] == KEY
    assert KEY not in request.url
    assert "Authorization" not in request.headers


def test_an_expired_key_fails_with_a_plain_message(app, server):
    server.routes["GET /api/health"] = (401, {})
    with pytest.raises(ReactiveResumeError, match="expired"):
        app.test_connection()


def test_app_version_comes_from_health(app, server):
    assert app.app_version() == "6.1.1"


def test_connection_without_a_name_still_reports_the_version(app, server):
    server.routes["GET /api/openapi/auth/profile"] = (200, {"id": USER_ID})
    assert app.test_connection() == "reactive-resume 6.1.1 reachable"


# --- backup ------------------------------------------------------------------

def test_backup_writes_the_export_zip_and_warns_about_skipped_files(
    app, server, tmp_path, caplog
):
    caplog.set_level(logging.WARNING)
    zip_path = app.backup(str(tmp_path))
    assert zip_path == str(tmp_path / "reactive-resume.zip")
    assert not (tmp_path / "export.json").exists()
    export_request = [r for r in server.sent if "account/export" in r.url]
    assert export_request and "includeUploads=true" in export_request[0].url
    with zipfile.ZipFile(zip_path) as zf:
        assert "resumes/my-resume-res-1.json" in zf.namelist()
        assert f"uploads/{USER_ID}/photo.png" in zf.namelist()
    assert "skipped 1 file(s)" in caplog.text
    assert "big.bin" in caplog.text


def test_backup_is_built_through_the_app_registry():
    app = backup.build_app("reactive-resume", {"url": URL, "api_key": KEY})
    assert isinstance(app, ReactiveResumeApp)
    assert isinstance(app.session, SameHostSession)
    assert app.session.headers["x-api-key"] == KEY
