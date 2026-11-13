"""Encrypted backup archives (#147): the format, and every place an encrypted
backup is made, restored, downloaded or read."""
import base64
import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import archive_crypto
import backup
import config_store
import manifest
import manifest_backfill
import restore_actions
import secrets_crypto
import self_backup

OTHER_KEY = Fernet.generate_key().decode("ascii")


def encrypt(data, key=None):
    return archive_crypto.encrypt_bytes(data, key)


@pytest.fixture
def small_chunks(monkeypatch):
    """Chunks of 16 bytes, so a short test file spans many of them."""
    monkeypatch.setattr(archive_crypto, "CHUNK_SIZE", 16)


# --- the format -------------------------------------------------------------

@pytest.mark.parametrize("data", [b"", b"x", b"a" * 16, b"b" * 17, os.urandom(1000)])
def test_anything_round_trips_whatever_its_size(small_chunks, data):
    sealed = encrypt(data)
    assert sealed.startswith(archive_crypto.MAGIC)
    if len(data) >= 16:
        assert data not in sealed
    assert archive_crypto.decrypt_bytes(sealed) == data


def test_the_same_file_encrypts_differently_each_time():
    assert encrypt(b"same") != encrypt(b"same")


def test_a_key_given_as_text_works_both_ways():
    sealed = encrypt(b"secret", OTHER_KEY)
    assert archive_crypto.decrypt_bytes(sealed, OTHER_KEY) == b"secret"
    assert archive_crypto.decrypt_bytes(sealed, f"  {OTHER_KEY}\n") == b"secret"


def test_another_key_is_told_apart_from_a_damaged_file():
    with pytest.raises(archive_crypto.WrongKeyError, match="different encryption key"):
        archive_crypto.decrypt_bytes(encrypt(b"secret"), OTHER_KEY)


def test_every_byte_of_the_key_counts():
    """Both halves of the 32-byte key go into the archive key: a key sharing
    either half with the real one doesn't open it."""
    raw = secrets_crypto.key_bytes()
    sealed = encrypt(b"secret")
    for changed in (bytes(16) + raw[16:], raw[:16] + bytes(16)):
        with pytest.raises(archive_crypto.WrongKeyError):
            archive_crypto.decrypt_bytes(sealed, base64.urlsafe_b64encode(changed).decode("ascii"))


@pytest.mark.parametrize("key", ["not a key", "A" * 44, base64.urlsafe_b64encode(b"short").decode()])
def test_text_that_isnt_a_key_is_refused_plainly(key):
    with pytest.raises(archive_crypto.WrongKeyError, match="44 characters ending in ="):
        archive_crypto.decrypt_bytes(encrypt(b"secret"), key)


def damage(sealed, index):
    return sealed[:index] + bytes([sealed[index] ^ 1]) + sealed[index + 1:]


def test_a_changed_byte_anywhere_is_refused(small_chunks):
    sealed = encrypt(os.urandom(100))
    for index in (8, 30, 60, len(sealed) - 1):  # salt, chunk size, a chunk, the last tag
        with pytest.raises(archive_crypto.ArchiveCryptoError):
            archive_crypto.decrypt_bytes(damage(sealed, index))


def test_a_file_cut_short_at_a_chunk_boundary_is_refused(small_chunks):
    sealed = encrypt(b"c" * 64)  # four full chunks and an empty last one
    sealed_chunk = 16 + archive_crypto.TAG_SIZE
    header = len(sealed) - 5 * sealed_chunk + 16  # the empty last chunk is only its tag
    for cut in (header + sealed_chunk, header + 2 * sealed_chunk):
        with pytest.raises(archive_crypto.ArchiveCryptoError, match="damaged"):
            archive_crypto.decrypt_bytes(sealed[:cut])


def test_anything_added_after_the_last_chunk_is_refused():
    with pytest.raises(archive_crypto.ArchiveCryptoError, match="damaged"):
        archive_crypto.decrypt_bytes(encrypt(b"data") + b"more")


def test_a_file_that_isnt_one_is_refused():
    with pytest.raises(archive_crypto.ArchiveCryptoError, match="isn't a backup encrypted by Backuparr"):
        archive_crypto.decrypt_bytes(b"PK\x03\x04 a plain zip")


def test_names_and_detection(tmp_path):
    assert archive_crypto.plain_name("radarr_1.zip.enc") == "radarr_1.zip"
    assert archive_crypto.plain_name("radarr_1.zip") == "radarr_1.zip"
    assert archive_crypto.is_encrypted_name("radarr_1.tar.gz.enc") and not archive_crypto.is_encrypted_name("radarr_1.zip")
    sealed, plain = tmp_path / "a.enc", tmp_path / "a.zip"
    sealed.write_bytes(encrypt(b"x"))
    plain.write_bytes(b"PK")
    assert archive_crypto.is_encrypted(sealed) and not archive_crypto.is_encrypted(plain)
    assert not archive_crypto.is_encrypted(tmp_path / "missing")


def test_files_round_trip_and_a_failed_decrypt_leaves_nothing(tmp_path):
    src, sealed, out = tmp_path / "a.zip", tmp_path / "a.zip.enc", tmp_path / "out.zip"
    src.write_bytes(os.urandom(3 * 1024 * 1024 + 5))  # more than one real chunk
    digest = archive_crypto.encrypt_file(src, sealed)
    archive_crypto.check_file(sealed, digest)
    assert archive_crypto.decrypt_file(sealed, out) == digest and out.read_bytes() == src.read_bytes()
    out.unlink()
    with pytest.raises(archive_crypto.WrongKeyError):
        archive_crypto.decrypt_file(sealed, out, OTHER_KEY)
    assert not out.exists()


def test_the_check_before_upload_refuses_a_different_archive(tmp_path):
    sealed = tmp_path / "a.enc"
    sealed.write_bytes(encrypt(b"made"))
    with pytest.raises(archive_crypto.ArchiveCryptoError, match="didn't decrypt to the archive"):
        archive_crypto.check_file(sealed, "0" * 64)


# --- backups ----------------------------------------------------------------

class FakeApp:
    def backup(self, work_dir):
        path = Path(work_dir) / "radarr_backup.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("config.xml", "<Config><ApiKey>secret</ApiKey></Config>")
        return str(path)

    def app_version(self):
        return "6.0.0"


@pytest.fixture
def run(monkeypatch, tmp_path):
    """run_backup for Radarr with a fake driver; uploads land in tmp_path/dest."""
    dest = tmp_path / "dest"
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["radarr"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local"])
    monkeypatch.setattr(backup.destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(backup.destination_util, "remote_root", lambda dest_id, cfg: str(dest))
    monkeypatch.setattr(backup, "build_app", lambda name, cfg: FakeApp())
    monkeypatch.setattr(backup, "apply_retention", lambda *args, **kwargs: None)

    def copyto(src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        Path(dst).write_bytes(Path(src).read_bytes())

    monkeypatch.setattr(backup.rclone_util, "copyto", copyto)

    def go(**app):
        ok, failed = backup.run_backup({"apps": {"radarr": {"enabled": True, "url": "http://radarr", **app}},
                                        "destinations": {"local": {}}})
        assert ok == ["radarr"] and not failed
        [uploaded] = (dest / "radarr").iterdir()
        return uploaded

    return go


def test_a_backup_is_encrypted_by_default_and_decrypts_to_the_archive(run, tmp_path):
    uploaded = run()
    assert uploaded.name.endswith(".zip.enc") and archive_crypto.is_encrypted(uploaded)
    assert b"secret" not in uploaded.read_bytes()
    plain = tmp_path / "plain.zip"
    archive_crypto.decrypt_file(uploaded, plain)
    assert zipfile.ZipFile(plain).read("config.xml").startswith(b"<Config>")


def test_a_backup_with_encryption_turned_off_stays_a_plain_zip(run):
    uploaded = run(encrypt=False)
    assert uploaded.name.endswith(".zip") and zipfile.is_zipfile(uploaded)


def test_an_always_encrypted_app_ignores_encryption_being_turned_off(run, monkeypatch):
    monkeypatch.setitem(config_store.app_meta("radarr"), "always_encrypted", True)
    assert run(encrypt=False).name.endswith(".zip.enc")


def test_encrypts_reads_the_card_and_always_encrypted():
    assert config_store.encrypts({"apps": {"radarr": {}}}, "radarr") is True
    assert config_store.encrypts({"apps": {"radarr": {"encrypt": False}}}, "radarr") is False
    assert config_store.encrypts({"apps": {}}, "radarr-4k") is True


def test_a_backup_that_fails_its_encryption_check_is_not_uploaded(monkeypatch, run, tmp_path):
    def broken(path, digest, key=None):
        raise archive_crypto.ArchiveCryptoError("the encrypted backup didn't decrypt to the archive that was made")

    monkeypatch.setattr(archive_crypto, "check_file", broken)
    ok, failed = backup.run_backup({"apps": {"radarr": {"enabled": True, "url": "http://radarr"}}, "destinations": {"local": {}}})
    assert ok == [] and "didn't decrypt" in failed[0]
    assert not (tmp_path / "dest").exists()


# --- restore ----------------------------------------------------------------

def test_decrypted_gives_the_plain_archive_and_removes_the_encrypted_copy(tmp_path):
    sealed = tmp_path / "radarr_1.zip.enc"
    sealed.write_bytes(encrypt(b"PK plain"))
    plain = restore_actions.decrypted(str(tmp_path), str(sealed))
    assert plain == str(tmp_path / "radarr_1.zip") and Path(plain).read_bytes() == b"PK plain"
    assert not sealed.exists()


def test_decrypted_leaves_a_plain_backup_alone(tmp_path):
    plain = tmp_path / "radarr_1.zip"
    plain.write_bytes(b"PK")
    assert restore_actions.decrypted(str(tmp_path), str(plain)) == str(plain)


@pytest.fixture
def encrypted_restore(inline, authed_client, monkeypatch, tmp_path):
    """A Radarr restore whose download is an encrypted archive."""
    seen = {}

    def fetch(root, app, filename=None):
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        (work / "radarr_1.zip.enc").write_bytes(encrypt(b"PK radarr", seen.get("made_with")))
        return str(work), str(work / "radarr_1.zip.enc"), "radarr_1.zip.enc"

    def restore_app(app_name, app_cfg, tmp_dir, local_zip, **kwargs):
        seen["restored"] = (os.path.basename(local_zip), Path(local_zip).read_bytes())
        return {}

    monkeypatch.setattr(restore_actions, "fetch_backup", fetch)
    monkeypatch.setattr(restore_actions, "restore_app", restore_app)
    assert authed_client.post("/api/config", json={"apps": {"radarr": {"enabled": True, "url": "http://radarr:1", "api_key": "k"}}}).status_code == 200
    return seen


def restore(client, **body):
    assert client.post("/api/restore/local/radarr", json={"confirm": True, "file": "radarr_1.zip.enc", **body}).status_code == 200
    return client.get("/api/restore/status").get_json()


def test_an_encrypted_backup_is_decrypted_before_the_app_gets_it(authed_client, encrypted_restore):
    state = restore(authed_client)
    assert state["ok"] is True and state["key_needed"] is False
    assert encrypted_restore["restored"] == ("radarr_1.zip", b"PK radarr")


def test_a_backup_made_with_another_key_asks_for_it_then_restores_with_it(authed_client, encrypted_restore):
    encrypted_restore["made_with"] = OTHER_KEY
    state = restore(authed_client)
    assert state["ok"] is False and state["key_needed"] is True and "different encryption key" in state["error"]
    assert "restored" not in encrypted_restore
    state = restore(authed_client, key=OTHER_KEY)
    assert state["ok"] is True and state["key_needed"] is False
    assert encrypted_restore["restored"] == ("radarr_1.zip", b"PK radarr")


def test_the_key_given_for_a_restore_is_never_logged(authed_client, encrypted_restore, caplog):
    encrypted_restore["made_with"] = OTHER_KEY
    restore(authed_client, key=Fernet.generate_key().decode("ascii"))
    restore(authed_client, key=OTHER_KEY)
    assert OTHER_KEY not in caplog.text


def test_the_sabnzbd_preview_decrypts_and_asks_for_another_key(authed_client, monkeypatch, tmp_path):
    work = tmp_path / "preview"

    def fetch(root, app, filename=None):
        work.mkdir(exist_ok=True)
        (work / "sabnzbd_1.zip.enc").write_bytes(encrypt(b"PK", OTHER_KEY))
        return str(work), str(work / "sabnzbd_1.zip.enc"), "sabnzbd_1.zip.enc"

    monkeypatch.setattr(restore_actions, "fetch_backup", fetch)
    monkeypatch.setattr(restore_actions, "load_sabnzbd_config", lambda tmp, path: {"config": {"servers": []}})
    response = authed_client.post("/api/restore/local/sabnzbd/preview", json={"file": "sabnzbd_1.zip.enc"})
    assert response.status_code == 409 and response.get_json()["key_needed"] is True
    response = authed_client.post("/api/restore/local/sabnzbd/preview", json={"file": "sabnzbd_1.zip.enc", "key": OTHER_KEY})
    assert response.status_code == 200 and response.get_json()["servers"] == []


# --- History ----------------------------------------------------------------

def local_file(isolated_webui, app, name, data):
    folder = os.path.join(isolated_webui.backups.destination_util.DEFAULT_LOCAL_DIR, app)
    os.makedirs(folder, exist_ok=True)
    Path(folder, name).write_bytes(data)


def test_history_marks_encrypted_backups(authed_client, isolated_webui, monkeypatch):
    entries = [{"Path": f"radarr/{n}", "Name": n, "Size": 1, "ModTime": "2026-01-01T00:00:00Z", "IsDir": False}
               for n in ("radarr_1.zip", "radarr_2.zip.enc")]
    monkeypatch.setattr(isolated_webui.backups.rclone_util, "lsjson", lambda root, recursive=False, include=(): entries)
    history = authed_client.get("/api/history/local").get_json()
    assert {e["name"]: e["encrypted"] for e in history["radarr"]} == {"radarr_1.zip": False, "radarr_2.zip.enc": True}


def test_an_encrypted_backup_downloads_decrypted_under_its_plain_name(authed_client, isolated_webui):
    local_file(isolated_webui, "radarr", "radarr_1.zip.enc", encrypt(b"PK plain"))
    response = authed_client.get("/api/history/local/radarr/radarr_1.zip.enc/download")
    assert response.status_code == 200 and response.data == b"PK plain"
    assert "radarr_1.zip" in response.headers["Content-Disposition"] and ".enc" not in response.headers["Content-Disposition"]


def test_a_cloud_backup_downloads_decrypted_and_leaves_no_copy(authed_client, isolated_webui, monkeypatch):
    cfg = isolated_webui.backups.load_config()
    cfg["destinations"]["dropbox"].update(enabled=True, token="{}")
    isolated_webui.settings.save_config(cfg)
    monkeypatch.setattr(isolated_webui.backups.destination_util, "sync", lambda cfg: None)
    copies = []

    def copyto(src, dst):
        copies.append(dst)
        Path(dst).write_bytes(encrypt(b"PK cloud"))

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "copyto", copyto)
    response = authed_client.get("/api/history/dropbox/radarr/radarr_1.zip.enc/download")
    assert response.status_code == 200 and response.data == b"PK cloud"
    response.close()
    assert not os.path.exists(os.path.dirname(copies[0]))


def test_backuparrs_own_backup_downloads_as_it_is(authed_client, isolated_webui):
    sealed = encrypt(b"PK self")
    local_file(isolated_webui, "backuparr", "backuparr_20260101_030000.enc", sealed)
    response = authed_client.get("/api/history/local/backuparr/backuparr_20260101_030000.enc/download")
    assert response.status_code == 200 and response.data == sealed


def test_a_download_made_with_another_key_is_refused_plainly(authed_client, isolated_webui):
    local_file(isolated_webui, "radarr", "radarr_1.zip.enc", encrypt(b"PK", OTHER_KEY))
    response = authed_client.get("/api/history/local/radarr/radarr_1.zip.enc/download")
    assert response.status_code == 409 and "different encryption key" in response.get_json()["error"]


# --- the startup backfill ---------------------------------------------------

def radarr_zip(version):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("INFO", f"{version}\nradarr")
    return buffer.getvalue()


def test_the_backfill_reads_the_version_inside_an_encrypted_backup(tmp_path, monkeypatch):
    folder = tmp_path / "radarr"
    folder.mkdir()
    (folder / "radarr_20260101_030000.zip.enc").write_bytes(encrypt(radarr_zip("6.1.0")))
    (folder / "radarr_20260102_030000.zip.enc").write_bytes(encrypt(radarr_zip("6.2.0"), OTHER_KEY))
    monkeypatch.setattr(manifest_backfill.destination_util, "list_backups", lambda root: [
        {"Path": f"radarr/{p.name}", "Size": p.stat().st_size, "IsDir": False} for p in sorted(folder.iterdir())])
    assert manifest_backfill.backfill_destination("local", str(tmp_path), manifest.generation()) == 2
    found = manifest.backups()
    assert found["radarr/radarr_20260101_030000.zip.enc"]["app_version"] == "6.1.0"
    assert found["radarr/radarr_20260101_030000.zip.enc"]["encrypted"] is True
    assert found["radarr/radarr_20260102_030000.zip.enc"]["app_version"] is None  # another key
    assert sorted(os.listdir(folder)) == ["radarr_20260101_030000.zip.enc", "radarr_20260102_030000.zip.enc"]


def test_the_backfill_reads_whether_an_encrypted_backup_holds_metadata(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("backuparr-plex.json", json.dumps({"plex_version": "1.43.5", "metadata": True}))
    folder = tmp_path / "plex"
    folder.mkdir()
    (folder / "plex_20260101_030000.zip.enc").write_bytes(encrypt(buffer.getvalue()))
    monkeypatch.setattr(manifest_backfill.destination_util, "list_backups", lambda root: [
        {"Path": f"plex/{p.name}", "Size": p.stat().st_size, "IsDir": False} for p in sorted(folder.iterdir())])
    assert manifest_backfill.backfill_destination("local", str(tmp_path), manifest.generation()) == 1
    entry = manifest.get("plex", "plex_20260101_030000.zip.enc")
    assert entry["metadata"] is True and entry["app_version"] == "1.43.5" and entry["encrypted"] is True


# --- Backuparr's own backup ---------------------------------------------------

def self_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("config.json", "{}")
        z.writestr(self_backup.META, json.dumps({"format": 1}))
    return buffer.getvalue()


def test_backuparrs_own_backup_uses_the_shared_format(isolated_webui, monkeypatch):
    monkeypatch.setattr(self_backup.rclone_util, "config_dump", lambda: {})
    isolated_webui.backups.load_config()  # writes config.json
    data = self_backup.build()
    assert archive_crypto.is_encrypted_bytes(data)
    assert "config.json" in self_backup.open_archive(data)


def test_a_backup_of_backuparr_from_before_3_0_says_to_use_2_5_7():
    """Recognised, not opened - with the key it was made with or another."""
    legacy = Fernet(OTHER_KEY.encode()).encrypt(self_zip())  # as 2.5.x wrote it
    for key in (None, OTHER_KEY):
        with pytest.raises(self_backup.SelfBackupError, match=r"from before Backuparr 3\.0\. Restore it with Backuparr 2\.5\.7, then update"):
            self_backup.open_archive(legacy, key)


def test_backuparrs_own_backup_with_the_wrong_key_is_refused():
    with pytest.raises(self_backup.SelfBackupError, match="made with this key"):
        self_backup.open_archive(encrypt(self_zip()), OTHER_KEY)


# --- settings and the notice --------------------------------------------------

def test_encrypt_must_be_true_or_false(authed_client):
    response = authed_client.post("/api/config", json={"apps": {"radarr": {"encrypt": "no"}}})
    assert response.status_code == 400 and "encrypt must be true or false" in response.get_json()["error"]
    assert authed_client.post("/api/config", json={"apps": {"radarr": {"encrypt": False}}}).status_code == 200
    assert authed_client.get("/api/config").get_json()["apps"]["radarr"]["encrypt"] is False


NO_NOTICE = {"show": False, "self_backup_pending": False, "self_backup_made": False}


def test_a_new_install_never_sees_the_encryption_notice(authed_client, isolated_webui):
    assert authed_client.get("/api/encryption-notice").get_json() == NO_NOTICE
    assert isolated_webui.runs.start_upgrade_self_backup() is False  # no old backups of itself either


def updated_from_2_5(isolated_webui, self_backup_on=True, destination=True):
    """config.json as an install updated from 2.5.x has it."""
    cfg = config_store.load_config()
    cfg["self_backup"] = self_backup_on
    cfg["destinations"]["local"]["enabled"] = destination
    config_store.save_config(cfg)
    path = isolated_webui.auth.CONFIG_PATH
    with open(path) as f:
        data = json.load(f)
    for name in ("encryption_notice_acknowledged", "upgrade_self_backup_tried", "upgrade_self_backup_made"):
        del data[name]
    with open(path, "w") as f:
        json.dump(data, f)


def test_an_updated_install_sees_the_notice_once(authed_client, isolated_webui):
    updated_from_2_5(isolated_webui)
    assert authed_client.get("/api/encryption-notice").get_json() == {**NO_NOTICE, "show": True}
    assert authed_client.post("/api/encryption-notice/acknowledge", json={}).status_code == 200
    assert authed_client.get("/api/encryption-notice").get_json() == NO_NOTICE


@pytest.fixture
def first_start(isolated_webui, monkeypatch):
    """Starts the first start's backup of Backuparr with a stand-in run that
    succeeds or fails; returns what it ran, once it's done."""
    runs = isolated_webui.runs
    calls = []

    def start(succeeds=True):
        def fake_run(only):
            calls.append(only)
            assert runs.UPGRADE_SELF_BACKUP["running"] is True
            runs.RUN_STATE["ok" if succeeds else "failed"] = ["backuparr" if succeeds else "backuparr: failed on local: down"]

        monkeypatch.setattr(runs, "_backup_work", fake_run)
        started = runs.start_upgrade_self_backup()
        for _ in range(500):
            if not runs.RUN_STATE["running"]:
                break
            time.sleep(0.01)
        return started, calls

    return start


def test_the_first_start_of_3_0_backs_up_backuparr_once_and_the_notice_says_so(authed_client, isolated_webui, first_start):
    updated_from_2_5(isolated_webui)
    started, calls = first_start()
    assert started is True and calls == ["backuparr"]
    cfg = config_store.load_config()
    assert cfg["upgrade_self_backup_tried"] is True and cfg["upgrade_self_backup_made"] is True
    assert authed_client.get("/api/encryption-notice").get_json() == {**NO_NOTICE, "show": True, "self_backup_made": True}
    assert first_start() == (False, ["backuparr"])  # the next start leaves it to the runs


def test_a_failed_first_start_backup_isnt_tried_again_or_mentioned(authed_client, isolated_webui, first_start):
    updated_from_2_5(isolated_webui)
    assert first_start(succeeds=False) == (True, ["backuparr"])
    cfg = config_store.load_config()
    assert cfg["upgrade_self_backup_tried"] is True and cfg["upgrade_self_backup_made"] is False
    assert authed_client.get("/api/encryption-notice").get_json() == {**NO_NOTICE, "show": True}
    assert first_start() == (False, ["backuparr"])


@pytest.mark.parametrize("self_backup_on, destination", [(False, True), (True, False)])
def test_no_first_start_backup_without_self_backup_or_a_destination(isolated_webui, first_start, self_backup_on, destination):
    updated_from_2_5(isolated_webui, self_backup_on=self_backup_on, destination=destination)
    assert first_start() == (False, [])
    assert config_store.load_config()["upgrade_self_backup_tried"] is True


def test_the_notice_waits_while_the_first_start_backup_runs(authed_client, isolated_webui, monkeypatch):
    updated_from_2_5(isolated_webui)
    monkeypatch.setitem(isolated_webui.runs.UPGRADE_SELF_BACKUP, "running", True)
    assert authed_client.get("/api/encryption-notice").get_json()["self_backup_pending"] is True
