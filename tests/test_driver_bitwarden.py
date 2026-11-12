import io
import json
import logging
import os
import tarfile
import time
import zipfile

import pytest
import requests

import archive_check
import backup
import manifest_backfill
from apps import bitwarden
from apps.bitwarden import MANIFEST, BitwardenApp, BitwardenError
from config_store import always_encrypted, app_meta, restore_supported

VERSION = "2026.10.1"
BAK = b"TAPE" + b"\0" * 252  # SQL Server's backups start with an MTF "TAPE" block.
OLD = time.time() - 3600  # Long enough ago that SQL Server has finished the file.


def age(path, seconds_ago):
    when = time.time() - seconds_ago
    os.utime(path, (when, when))


@pytest.fixture
def bwdata(tmp_path):
    """A standard install's ./bwdata, laid out as bitwarden.sh 2026.10.1 makes it."""
    root = tmp_path / "bwdata"
    files = {
        "config.yml": "url: https://vault.lab\n",
        "env/global.override.env": "globalSettings__sqlServer__connectionString=...Password=db;\n",
        "env/mssql.override.env": "SA_PASSWORD=db\n",
        "env/uid.env": "LOCAL_UID=1000\nLOCAL_GID=1000\n",
        "core/attachments/cipher-1/file-1": "attachment",
        "core/aspnet-dataprotection/key-1.xml": "<key/>",
        "core/licenses/organization/org-1.json": "{}",
        "identity/identity.pfx": "pfx",
        "ssl/vault.lab/certificate.crt": "crt",
        "docker/docker-compose.yml": "services: {}\n",
        "mssql/data/vault.mdf": "live database",
        "mssql/backups/vault_FULL_20261110_000000.BAK": BAK,
        "mssql/backups/vault_FULL_20261111_000000.BAK": BAK,
        "logs/api/log.txt": "log",
    }
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
        os.utime(path, (OLD, OLD))
    return root


@pytest.fixture
def server(monkeypatch):
    """Answers each "METHOD /path" from `routes` (status, body); records requests."""
    state = type("Server", (), {})()
    state.sent = []
    state.routes = {"GET /api/config": (200, {"version": VERSION, "gitHash": "abc", "server": None})}

    def send(_adapter, request, **kwargs):
        state.sent.append(request.copy())
        status, body = state.routes.get(f"{request.method} {request.path_url.split('?')[0]}", (404, {}))
        res = requests.Response()
        res.request, res.url, res.status_code = request, request.url, status
        res._content = json.dumps(body).encode()
        res.raw = io.BytesIO(res._content)
        return res

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


def make_backup(app, tmp_path):
    result = app.backup(str(tmp_path / "work"))
    if os.path.isdir(result):
        archive = str(tmp_path / "bitwarden_20261112_030000.zip")
        backup.zip_dir(result, archive)
        return archive
    return result


# --- metadata and version ------------------------------------------------------

def test_it_has_no_key_is_always_encrypted_and_restored_by_hand(bwdata):
    app = backup.build_app("bitwarden", {"url": "http://bitwarden:8080", "api_key": "", "data_dir": str(bwdata)})
    assert isinstance(app, BitwardenApp) and app.data_dir == str(bwdata)
    assert always_encrypted("bitwarden") and not restore_supported("bitwarden")
    assert app_meta("bitwarden")["pins_certificate"] and app_meta("bitwarden")["category"] == "security"
    pinned = backup.build_app("bitwarden", {"url": "https://bitwarden:8443", "data_dir": str(bwdata),
                                            "trusted_cert": {"sha256": "ab" * 32}})
    assert pinned.session.get_adapter("https://bitwarden:8443").__class__.__name__ != "HTTPAdapter"
    titles = [group["title"] for group in app_meta("bitwarden")["manual_restore"]["steps"]]
    assert titles == ["Standard install", "Bitwarden lite"]


def test_the_version_comes_from_api_config(bwdata, server):
    app = BitwardenApp("http://bitwarden:8080/", data_dir=str(bwdata))
    assert app.app_version() == VERSION
    assert server.sent[0].url == "http://bitwarden:8080/api/config"


def test_vaultwarden_is_sent_to_its_own_card(bwdata, server):
    # As the lab's Vaultwarden 1.37.5 answers /api/config.
    server.routes["GET /api/config"] = (200, {"version": "2026.6.0", "server": {
        "name": "Vaultwarden", "url": "https://github.com/dani-garcia/vaultwarden"}})
    with pytest.raises(BitwardenError, match="is Vaultwarden - back it up as Vaultwarden instead"):
        BitwardenApp("http://vaultwarden", data_dir=str(bwdata)).test_connection()


@pytest.mark.parametrize("route", [(404, {}), (200, "<html>"), (200, {"object": "config"})])
def test_something_else_at_the_url_is_reported(bwdata, server, route):
    server.routes["GET /api/config"] = route
    with pytest.raises(BitwardenError, match="doesn't look like Bitwarden"):
        BitwardenApp("http://other", data_dir=str(bwdata)).app_version()


# --- the folder ----------------------------------------------------------------

def test_the_folder_must_be_set_and_mounted(tmp_path, server):
    with pytest.raises(BitwardenError, match="set the folder"):
        BitwardenApp("http://bitwarden").test_connection()
    with pytest.raises(BitwardenError, match="doesn't exist in this container"):
        BitwardenApp("http://bitwarden", data_dir=str(tmp_path / "missing")).test_connection()


def test_lites_config_volume_is_turned_away(tmp_path, server):
    (tmp_path / "data-protection").mkdir()
    (tmp_path / "identity.pfx").write_text("pfx")
    with pytest.raises(BitwardenError, match="is lite's /etc/bitwarden - mount lite's backups folder"):
        BitwardenApp("http://bitwarden", data_dir=str(tmp_path)).test_connection()


# --- standard install ----------------------------------------------------------

def test_standard_test_connection_names_the_newest_backup(bwdata, server):
    app = BitwardenApp("http://bitwarden", data_dir=str(bwdata))
    assert app.test_connection() == (f"bitwarden {VERSION} reachable (standard install), newest backup "
                                      "vault_FULL_20261111_000000.BAK")


@pytest.mark.archive_check
def test_standard_backup_takes_bwdata_and_the_newest_bak_only(bwdata, server, tmp_path):
    app = BitwardenApp("http://bitwarden", data_dir=str(bwdata))
    archive = make_backup(app, tmp_path)
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert json.loads(zf.read(MANIFEST)) == {"bitwarden_version": VERSION, "install": "standard",
                                                 "database_backup": "vault_FULL_20261111_000000.BAK"}
    assert names == {
        MANIFEST, "config.yml", "env/global.override.env", "env/mssql.override.env", "env/uid.env",
        "core/attachments/cipher-1/file-1", "core/aspnet-dataprotection/key-1.xml",
        "core/licenses/organization/org-1.json", "identity/identity.pfx", "ssl/vault.lab/certificate.crt",
        "docker/docker-compose.yml", "mssql/backups/vault_FULL_20261111_000000.BAK"}
    archive_check.check("bitwarden", archive)
    assert manifest_backfill.read_version("bitwarden", archive) == VERSION


def test_a_bak_still_being_written_is_skipped_for_the_one_before(bwdata, server, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="backuparr")
    age(bwdata / "mssql/backups/vault_FULL_20261111_000000.BAK", 30)
    archive = make_backup(BitwardenApp("http://bitwarden", data_dir=str(bwdata)), tmp_path)
    with zipfile.ZipFile(archive) as zf:
        assert "mssql/backups/vault_FULL_20261110_000000.BAK" in zf.namelist()
        assert "mssql/backups/vault_FULL_20261111_000000.BAK" not in zf.namelist()
    assert "may still be being written" in caplog.text


def test_the_only_bak_still_being_written_fails_the_backup(bwdata, server, tmp_path):
    (bwdata / "mssql/backups/vault_FULL_20261110_000000.BAK").unlink()
    age(bwdata / "mssql/backups/vault_FULL_20261111_000000.BAK", 30)
    with pytest.raises(BitwardenError, match="still being written - try again in a few minutes"):
        BitwardenApp("http://bitwarden", data_dir=str(bwdata)).backup(str(tmp_path / "work"))


def test_no_nightly_backup_yet_says_how_to_make_one(bwdata, server, tmp_path):
    for bak in (bwdata / "mssql/backups").iterdir():
        bak.unlink()
    with pytest.raises(BitwardenError, match="no nightly database backup.*docker exec -i bitwarden-mssql /backup-db.sh"):
        BitwardenApp("http://bitwarden", data_dir=str(bwdata)).test_connection()


def test_an_old_nightly_backup_is_warned_about(bwdata, server, tmp_path, caplog):
    age(bwdata / "mssql/backups/vault_FULL_20261111_000000.BAK", 5 * 86400)
    app = BitwardenApp("http://bitwarden", data_dir=str(bwdata))
    assert app.test_connection().endswith("(more than 2 days old)")
    make_backup(app, tmp_path)
    assert "is 5 days old - check that its nightly backups still run" in caplog.text


def test_a_file_that_isnt_a_sql_server_backup_is_refused(bwdata, server, tmp_path):
    (bwdata / "mssql/backups/vault_FULL_20261111_000000.BAK").write_bytes(b"<html>")
    age(bwdata / "mssql/backups/vault_FULL_20261111_000000.BAK", 3600)
    with pytest.raises(BitwardenError, match="isn't a SQL Server backup"):
        BitwardenApp("http://bitwarden", data_dir=str(bwdata)).backup(str(tmp_path / "work"))


def test_an_unreadable_file_says_whose_user_to_run_as(bwdata, server, monkeypatch):
    real = os.access
    blocked = str(bwdata / "env" / "global.override.env")
    monkeypatch.setattr(bitwarden.os, "access", lambda path, mode: real(path, mode) and str(path) != blocked)
    with pytest.raises(BitwardenError, match=f"can't read {blocked} - run Backuparr as the user that owns bwdata"):
        BitwardenApp("http://bitwarden", data_dir=str(bwdata)).test_connection()


def test_symlinks_in_bwdata_are_not_followed(bwdata, server, tmp_path):
    (tmp_path / "outside").write_text("not Bitwarden's")
    (bwdata / "core" / "link").symlink_to(tmp_path / "outside")
    (bwdata / "linked-dir").symlink_to(tmp_path)
    with zipfile.ZipFile(make_backup(BitwardenApp("http://bitwarden", data_dir=str(bwdata)), tmp_path)) as zf:
        assert not any("link" in name for name in zf.namelist())


@pytest.mark.archive_check
@pytest.mark.parametrize("drop, fragment", [
    ("mssql/backups/vault_FULL_20261111_000000.BAK", "is missing from the backup"),
    ("env/", "env/ is missing"),
    (MANIFEST, "is missing or unreadable"),
])
def test_the_archive_check_needs_the_database_env_and_manifest(bwdata, server, tmp_path, drop, fragment):
    archive = make_backup(BitwardenApp("http://bitwarden", data_dir=str(bwdata)), tmp_path)
    trimmed = str(tmp_path / "trimmed.zip")
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(trimmed, "w") as dst:
        for info in src.infolist():
            if not info.filename.startswith(drop):
                dst.writestr(info, src.read(info))
    with pytest.raises(archive_check.ArchiveCheckError, match=fragment):
        archive_check.check("bitwarden", trimmed)


# --- lite ----------------------------------------------------------------------

LITE_MANIFEST = """Bitwarden Lite backup
=====================

created:          2026-11-12 18:29:02+0000
deployment:       bitwarden-lite
db provider:      sqlite
lite image tag:   {tag}

contents
--------
database/vault_20261112_182902.db   database dump
config/                 /etc/bitwarden excluding logs/ (and, for sqlite, the live database file)
{settings}
"""


def make_lite(path, settings=False, tag="2026.10.1", data_protection=True, database=True):
    """A lite archive laid out as 2026.10.1's backup-db.sh writes it (checked
    against a real one, 2026-11-12): ./manifest.txt and ./database/ first,
    then /etc/bitwarden as config/."""
    text = LITE_MANIFEST.format(tag=tag, settings="settings.env            host settings file" if settings else "")
    members = [("./manifest.txt", text.encode()), ("./database/", None)]
    if database:
        members.append(("./database/vault_20261112_182902.db", b"SQLite format 3\0"))
    members += [("config/", None), ("config/identity.pfx", b"pfx")]
    if data_protection:
        members += [("config/data-protection/", None), ("config/data-protection/key-1.xml", b"<key/>")]
    if settings:
        members.append(("./settings.env", b"BW_INSTALLATION_ID=x\n"))
    with tarfile.open(path, "w:gz") as tar:
        for name, body in members:
            info = tarfile.TarInfo(name.rstrip("/"))
            if body is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
    os.utime(path, (OLD, OLD))
    return path


@pytest.fixture
def lite(tmp_path):
    """Lite's backups folder with two days' archives and a .partial in progress."""
    root = tmp_path / "backups"
    root.mkdir()
    make_lite(root / "bitwarden-lite_20261111_000000.tar.gz")
    make_lite(root / "bitwarden-lite_20261112_000000.tar.gz")
    (root / "bitwarden-lite_20261113_000000.tar.gz.partial").write_bytes(b"half")
    return root


def test_lite_test_connection_names_its_newest_archive(lite, server):
    assert BitwardenApp("http://bitwarden", data_dir=str(lite)).test_connection() == (
        f"bitwarden {VERSION} reachable (lite), newest backup bitwarden-lite_20261112_000000.tar.gz")


@pytest.mark.archive_check
def test_lite_backup_is_its_newest_archive_kept_whole(lite, server, tmp_path, caplog):
    result = make_backup(BitwardenApp("http://bitwarden", data_dir=str(lite)), tmp_path)
    assert os.path.basename(result) == "bitwarden-lite_20261112_000000.tar.gz"
    assert open(result, "rb").read() == (lite / "bitwarden-lite_20261112_000000.tar.gz").read_bytes()
    assert backup.archive_suffix(result) == ".tar.gz"
    archive_check.check("bitwarden", result)
    assert manifest_backfill.read_version("bitwarden", result) == "2026.10.1"
    assert "doesn't hold settings.env" in caplog.text and "BACKUP_INCLUDE_SETTINGS=true" in caplog.text


def test_a_lite_archive_with_settings_env_isnt_warned_about(lite, server, tmp_path, caplog):
    make_lite(lite / "bitwarden-lite_20261112_000000.tar.gz", settings=True)
    make_backup(BitwardenApp("http://bitwarden", data_dir=str(lite)), tmp_path)
    assert "settings.env" not in caplog.text


def test_a_lite_image_tag_that_isnt_a_version_is_recorded_as_unknown(tmp_path):
    archive = make_lite(tmp_path / "a.tar.gz", tag="unknown (TAG not set in the environment)")
    assert manifest_backfill.read_version("bitwarden", str(archive)) is None
    assert bitwarden.lite_image_tag(LITE_MANIFEST.format(tag="latest", settings="")) is None


def test_an_old_lite_archive_is_warned_about(lite, server, tmp_path, caplog):
    age(lite / "bitwarden-lite_20261112_000000.tar.gz", 3 * 86400)
    make_backup(BitwardenApp("http://bitwarden", data_dir=str(lite)), tmp_path)
    assert "newest lite backup (bitwarden-lite_20261112_000000.tar.gz) is 3 days old" in caplog.text


def test_an_empty_backups_folder_says_where_lite_writes(tmp_path, server):
    with pytest.raises(BitwardenError, match="no lite backups in .* lite 2026.10.1 and newer make one daily"):
        BitwardenApp("http://bitwarden", data_dir=str(tmp_path)).test_connection()


def test_a_lite_older_than_its_backups_is_told_to_update(tmp_path, server):
    server.routes["GET /api/config"] = (200, {"version": "2026.10.0", "server": None})
    with pytest.raises(BitwardenError, match=r"update Bitwarden lite to 2026.10.1 or newer \(2026.10.0 is installed\)"):
        BitwardenApp("http://bitwarden", data_dir=str(tmp_path)).test_connection()


def test_an_unreadable_lite_archive_says_how_to_share_it(lite, server, monkeypatch):
    # Lite writes its archives mode 600, owned by its user (911).
    real = os.access
    monkeypatch.setattr(bitwarden.os, "access", lambda path, mode: real(path, mode) and not str(path).endswith(".tar.gz"))
    with pytest.raises(BitwardenError, match="set BACKUP_FILE_MODE=640 in lite's settings.env and give lite and "
                                             "Backuparr the same PGID"):
        BitwardenApp("http://bitwarden", data_dir=str(lite)).test_connection()


@pytest.mark.archive_check
@pytest.mark.parametrize("kwargs, fragment", [
    ({"data_protection": False}, "no config/data-protection"),
    ({"database": False}, "no database dump"),
])
def test_the_archive_check_needs_lites_dump_and_keys(tmp_path, kwargs, fragment):
    archive = make_lite(tmp_path / "bitwarden_20261112_030000.tar.gz", **kwargs)
    with pytest.raises(archive_check.ArchiveCheckError, match=fragment):
        archive_check.check("bitwarden", str(archive))


@pytest.mark.archive_check
def test_a_truncated_lite_archive_fails_the_check(tmp_path):
    archive = make_lite(tmp_path / "bitwarden_20261112_030000.tar.gz")
    archive.write_bytes(archive.read_bytes()[:-20])
    with pytest.raises(archive_check.ArchiveCheckError, match="damaged"):
        archive_check.check("bitwarden", str(archive))


def test_an_untrusted_certificate_is_called_bitwardens():
    from apps import cert_pin
    exc = cert_pin.CertificateUntrusted("bitwarden", {"sha256": "ab" * 32})
    assert str(exc) == "bitwarden: Bitwarden's certificate isn't trusted. Check it and trust it in Settings to back it up."
