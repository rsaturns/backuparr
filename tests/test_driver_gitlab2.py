import io
import json
import logging
import os
import shutil
import tarfile
import time
import zipfile

import pytest
import requests

import backup
from apps.gitlab import GitLabApp, GitLabError, _parse_backup_information, archive_version, check_backup
from config_store import app_meta, restore_supported

KEY = "glpat-gitlab-test-key"
VERSION = "18.5.0-ce"
BACKUP_ID = "1764000000_2026_11_25_18.5.0-ce"
ARCHIVE = f"{BACKUP_ID}_gitlab_backup.tar"
CONFIG = "gitlab_config_1764000000_2026_11_25.tar"
INFO = f"gitlab_version: {VERSION}\n"


def make_tar(path, members):
    """A tar holding `members` {name: bytes}."""
    with tarfile.open(path, "w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def make_archive(path, info_text=INFO, extra=None):
    """A *_gitlab_backup.tar whose backup_information.yml says `info_text`."""
    members = {"backup_information.yml": info_text.encode()}
    members.update(extra or {})
    make_tar(path, members)


def make_config(path):
    """A gitlab_config_*.tar holding the secrets file."""
    make_tar(path, {"gitlab-secrets.json": b'{"db_key_base": "secret"}'})


def set_old(path, days=1):
    """Backdate `path` so GitLab isn't still writing it (and, past two days, stale)."""
    old = time.time() - days * 86400
    os.utime(path, (old, old))


@pytest.fixture
def folder(tmp_path):
    """A GitLab backups folder: one complete archive and its config tar."""
    root = tmp_path / "backups"
    root.mkdir()
    archive = root / ARCHIVE
    make_archive(archive)
    set_old(archive)
    config_dir = root / "config"
    config_dir.mkdir()
    config = config_dir / CONFIG
    make_config(config)
    set_old(config)
    return root


@pytest.fixture
def server(monkeypatch):
    """Answers each "METHOD /path" from `routes` (status, body); records requests."""
    state = type("Server", (), {})()
    state.sent = []
    state.routes = {
        "GET /api/v4/metadata": (200, {"version": VERSION}),
    }

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        status, body = state.routes.get(f"{request.method} {request.path_url.split('?')[0]}", (404, {}))
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
def app(folder):
    return GitLabApp("http://gitlab:8080/", KEY, data_dir=str(folder))


def make_zip(path, tmp_path, archive_name=ARCHIVE, config_name=CONFIG, info_text=INFO):
    """A finished backup zip: the archive at its root, the config tar under config/."""
    archive = tmp_path / "a.tar"
    make_archive(archive, info_text=info_text)
    config = tmp_path / "c.tar"
    make_config(config)
    with zipfile.ZipFile(path, "w") as zf:
        zf.write(archive, archive_name)
        zf.write(config, f"config/{config_name}")
    return path


# --- the backup id and its information file -------------------------------


def test_archive_version_comes_from_the_backup_id():
    assert archive_version("1493107454_2018_04_25_10.6.4-ce_gitlab_backup.tar") == "10.6.4-ce"
    assert archive_version("1493107454_2018_04_25_10.6.4-ee_gitlab_backup.tar") == "10.6.4-ee"
    assert archive_version("mybackup_gitlab_backup.tar") is None
    assert archive_version("not-a-backup.tar") is None


def test_backup_information_is_read_without_yaml():
    version, skipped = _parse_backup_information(
        "gitlab_version: 18.5.0-ce\nskipped:\n  - db\n  - uploads\n"
    )
    assert version == "18.5.0-ce"
    assert skipped == ["db", "uploads"]
    version, skipped = _parse_backup_information("gitlab_version: 18.5.0-ce\nskipped: [db, uploads]\n")
    assert version == "18.5.0-ce"
    assert skipped == ["db", "uploads"]
    version, skipped = _parse_backup_information("gitlab_version: 18.5.0-ce\nskipped: []\n")
    assert version == "18.5.0-ce"
    assert skipped == []
    version, skipped = _parse_backup_information("nothing here\n")
    assert version is None
    assert skipped == []


def test_the_version_falls_back_to_backup_information_yml(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    custom = "mybackup_gitlab_backup.tar"
    make_archive(root / custom)
    set_old(root / custom)
    app = GitLabApp("http://gitlab:8080", KEY, data_dir=str(root))
    assert archive_version(custom) is None
    assert app.app_version() == VERSION


# --- connection ------------------------------------------------------------


def test_the_token_goes_in_its_own_header_never_the_url(app, server):
    assert app.test_connection() == f"gitlab {VERSION} reachable, newest backup {ARCHIVE} ({VERSION})"
    request = server.sent[0]
    assert request.url == "http://gitlab:8080/api/v4/metadata"
    assert request.headers["PRIVATE-TOKEN"] == KEY
    assert KEY not in request.url and "Authorization" not in request.headers


def test_a_rejected_key_says_it_needs_only_read_user(app, server):
    server.routes["GET /api/v4/metadata"] = (401, {})
    with pytest.raises(GitLabError, match="unauthorized.*read_user"):
        app.test_connection()


def test_a_version_mismatch_warns_that_a_restore_needs_the_backups_version(app, server, caplog):
    server.routes["GET /api/v4/metadata"] = (200, {"version": "18.6.0-ce"})
    with caplog.at_level(logging.WARNING, logger="backuparr.apps.gitlab"):
        assert app.test_connection() == (
            f"gitlab 18.6.0-ce reachable, newest backup {ARCHIVE} ({VERSION})"
        )
    assert "a restore needs the backup's version" in caplog.text


def test_the_backups_folder_must_be_set_and_exist(server, tmp_path):
    with pytest.raises(GitLabError, match="set the backups folder"):
        GitLabApp("http://gitlab:8080", KEY).test_connection()
    with pytest.raises(GitLabError, match="doesn't exist in this container"):
        GitLabApp("http://gitlab:8080", KEY, data_dir=str(tmp_path / "missing")).test_connection()


def test_a_connection_failure_says_it_couldnt_reach_gitlab(app, server):
    server.routes["GET /api/v4/metadata"] = (0, requests.ConnectionError("refused"))
    with pytest.raises(GitLabError, match="couldn't reach /api/v4/metadata"):
        app.test_connection()


# --- backup ----------------------------------------------------------------


def test_backup_copies_the_archive_and_config_in_the_same_layout(app, folder, tmp_path):
    dest = tmp_path / "out"
    assert app.backup(str(dest)) == str(dest)
    assert (dest / ARCHIVE).read_bytes() == (folder / ARCHIVE).read_bytes()
    assert (dest / "config" / CONFIG).read_bytes() == (folder / "config" / CONFIG).read_bytes()


def test_the_newest_complete_archive_is_picked(app, folder, tmp_path):
    older = "1493107454_2018_04_25_10.6.4-ce_gitlab_backup.tar"
    make_archive(folder / older)
    set_old(folder / older, days=2)
    dest = tmp_path / "out"
    app.backup(str(dest))
    assert (dest / ARCHIVE).exists()
    assert not (dest / older).exists()


def test_an_archive_still_being_written_is_skipped(app, folder, tmp_path):
    fresh = "1764000001_2026_11_25_18.5.0-ce_gitlab_backup.tar"
    make_archive(folder / fresh)  # Its mtime is now: GitLab is still writing it.
    dest = tmp_path / "out"
    app.backup(str(dest))
    assert (dest / ARCHIVE).exists()
    assert not (dest / fresh).exists()


def test_a_non_tar_archive_is_skipped(app, folder, tmp_path):
    fake = folder / "1764000002_2026_11_25_18.5.0-ce_gitlab_backup.tar"
    fake.write_bytes(b"not a tar archive")
    set_old(fake)
    dest = tmp_path / "out"
    app.backup(str(dest))
    assert (dest / ARCHIVE).exists()
    assert not (dest / fake.name).exists()


def test_backup_without_a_config_tar_still_works_but_warns(app, folder, tmp_path, caplog):
    shutil.rmtree(folder / "config")
    dest = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger="backuparr.apps.gitlab"):
        assert app.backup(str(dest)) == str(dest)
    assert (dest / ARCHIVE).exists()
    assert not (dest / "config").exists()
    assert "no config tar" in caplog.text


def test_a_backup_with_skipped_parts_warns(app, folder, tmp_path, caplog):
    make_archive(
        folder / ARCHIVE,
        info_text="gitlab_version: 18.5.0-ce\nskipped:\n  - db\n  - uploads\n",
    )
    set_old(folder / ARCHIVE)
    dest = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger="backuparr.apps.gitlab"):
        app.backup(str(dest))
    assert (dest / ARCHIVE).exists()
    assert "the newest backup skipped db, uploads" in caplog.text


def test_a_stale_backup_warns_but_still_backs_up(app, folder, tmp_path, caplog):
    set_old(folder / ARCHIVE, days=3)
    dest = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger="backuparr.apps.gitlab"):
        assert app.backup(str(dest)) == str(dest)
    assert (dest / ARCHIVE).exists()
    assert "the newest backup is 3 days old" in caplog.text


# --- checking a finished backup --------------------------------------------


def test_check_backup_accepts_a_sound_gitlab_backup(tmp_path):
    check_backup(str(make_zip(tmp_path / "gitlab.zip", tmp_path)))


def test_check_backup_refuses_a_zip_without_an_archive(tmp_path):
    path = tmp_path / "no-archive.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"config/{CONFIG}", b"x")
    with pytest.raises(GitLabError, match="no \\*_gitlab_backup\\.tar"):
        check_backup(str(path))


def test_check_backup_refuses_a_zip_without_a_config_tar(tmp_path):
    path = tmp_path / "no-config.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(ARCHIVE, b"x")
    with pytest.raises(GitLabError, match="no config/gitlab_config_"):
        check_backup(str(path))


def test_check_backup_refuses_a_file_that_isnt_a_zip(tmp_path):
    path = tmp_path / "gitlab.tar"
    path.write_bytes(b"not a zip")
    with pytest.raises(GitLabError, match="isn't a zip archive"):
        check_backup(str(path))


def test_check_backup_refuses_a_damaged_archive(tmp_path):
    path = tmp_path / "damaged.zip"
    config = tmp_path / "c.tar"
    make_config(config)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(ARCHIVE, b"x" * 1024)
        zf.write(config, f"config/{CONFIG}")
    with pytest.raises(GitLabError, match="is damaged"):
        check_backup(str(path))


# --- registry --------------------------------------------------------------


def test_backup_is_built_through_the_app_registry(folder):
    app = backup.build_app(
        "gitlab", {"url": "http://gitlab:8080", "api_key": KEY, "data_dir": str(folder)}
    )
    assert isinstance(app, GitLabApp) and app.data_dir == str(folder)
    assert app.app_version() == VERSION
    assert not restore_supported("gitlab")
    assert app_meta("gitlab")["always_encrypted"] is True
