"""The backup manifest (#106): what each backup run records, how retention,
History delete and Reset keep it in step, and the backfill that adds backups
made before it existed."""
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import zipfile
from datetime import datetime

import pytest
import requests

import backup
import destination_util
import manifest
import manifest_backfill
import rclone_util
from apps.bazarr import BazarrApp
from apps.sabnzbd import SabnzbdApp
from apps.tautulli import TautulliApp


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------ versions ----

@pytest.mark.parametrize("app_type, raw, expected", [
    ("radarr", "v6.4.4.10685", "6.4.4.10685"),
    ("radarr", "6.4.4.10685", "6.4.4.10685"),
    ("tautulli", "v2.18.2", "2.18.2"),
    ("jellyfin", "12.4.0.0", "12.4.0"),
    ("jellyfin", "12.4.0", "12.4.0"),
    ("emby", "4.10.2.0", "4.10.2.0"),  # Emby's fourth part is real.
    ("lazylibrarian", "228ABD29", "228abd29"),
    ("plex", " 1.43.5.11079-b3b13e506 ", "1.43.5.11079-b3b13e506"),
    ("jellyfin", "?", None),
    ("sabnzbd", "", None),
    ("bazarr", None, None),
])
def test_versions_are_normalised_for_comparing(app_type, raw, expected):
    assert manifest.normalise_version(app_type, raw) == expected


# --------------------------------------------------------------- store ----

def test_record_merges_destinations_and_remove_drops_the_entry_with_the_last():
    manifest.record("radarr", "radarr_1.zip", ["local"], app_version="6.4", sha256="aa", size=3, backuparr_version="2.5.0")
    manifest.record("radarr", "radarr_1.zip", ["gdrive"], app_version="6.4", sha256="aa", size=3, backuparr_version="2.5.0")
    entry = manifest.get("radarr", "radarr_1.zip")
    assert entry["destinations"] == ["gdrive", "local"]
    assert entry["type"] == "radarr" and entry["backuparr_version"] == "2.5.0"
    manifest.remove("radarr", "radarr_1.zip", "local")
    assert manifest.get("radarr", "radarr_1.zip")["destinations"] == ["gdrive"]
    manifest.remove("radarr", "radarr_1.zip", "gdrive")
    assert manifest.get("radarr", "radarr_1.zip") is None


def test_an_instance_entry_keeps_its_app_type():
    manifest.record("radarr-4k", "radarr-4k_1.zip", ["local"], app_version=None, sha256="aa", size=1, backuparr_version="x")
    assert manifest.get("radarr-4k", "radarr-4k_1.zip")["type"] == "radarr"


def test_reconcile_drops_only_that_destinations_missing_files():
    manifest.record("radarr", "radarr_1.zip", ["local", "gdrive"], app_version=None, sha256="a", size=1, backuparr_version="x")
    manifest.record("radarr", "radarr_2.zip", ["local"], app_version=None, sha256="b", size=1, backuparr_version="x")
    manifest.record("sonarr", "sonarr_1.zip", ["local"], app_version=None, sha256="c", size=1, backuparr_version="x")
    manifest.reconcile("local", {"sonarr/sonarr_1.zip"})
    assert manifest.get("radarr", "radarr_1.zip")["destinations"] == ["gdrive"]
    assert manifest.get("radarr", "radarr_2.zip") is None
    assert manifest.get("sonarr", "sonarr_1.zip")["destinations"] == ["local"]


def test_the_file_is_private_and_an_unreadable_one_is_set_aside(manifest_in_tmp):
    manifest.record("radarr", "radarr_1.zip", ["local"], app_version=None, sha256="a", size=1, backuparr_version="x")
    assert os.stat(manifest_in_tmp).st_mode & 0o777 == 0o600
    with open(manifest_in_tmp, "w") as f:
        f.write("{not json")
    assert manifest.backups() == {}
    assert open(manifest_in_tmp + ".bad").read() == "{not json"
    assert not os.path.exists(manifest_in_tmp)


def test_clear_removes_the_file_and_stops_a_running_backfill(manifest_in_tmp):
    generation = manifest.generation()
    manifest.record("radarr", "radarr_1.zip", ["local"], app_version=None, sha256="a", size=1, backuparr_version="x")
    manifest.clear()
    assert not os.path.exists(manifest_in_tmp)
    assert manifest.add_found("radarr", "radarr_1.zip", "local", app_version=None, sha256="a", size=1,
                              expected_generation=generation) is False
    assert manifest.backups() == {}


# ---------------------------------------------------------- backup run ----

class FakeApp:
    def __init__(self, version="v6.4.4.10685"):
        self.version = version

    def backup(self, dest_dir):
        path = os.path.join(dest_dir, "out.zip")
        with open(path, "wb") as f:
            f.write(b"archive")
        return path

    def app_version(self):
        if isinstance(self.version, Exception):
            raise self.version
        return self.version


@pytest.fixture
def run(monkeypatch):
    state = type("Run", (), {"uploads": [], "fail": set(), "app": FakeApp()})()
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["radarr"])
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local", "gdrive"])
    monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: f"{dest_id}:")
    monkeypatch.setattr(backup, "build_app", lambda name, app_cfg: state.app)
    monkeypatch.setattr(backup, "apply_retention", lambda *args, **kwargs: None)

    def copyto(src, dst):
        if dst.split(":")[0] in state.fail:
            raise rclone_util.RcloneError("copyto failed", "quota exceeded")
        state.uploads.append(dst)

    monkeypatch.setattr(rclone_util, "copyto", copyto)
    state.cfg = {"apps": {"radarr": {"url": "http://radarr:7878", "api_key": "k", "encrypt": False}},
                 "destinations": {"local": {}, "gdrive": {}}}
    state.go = lambda: backup.run_backup(state.cfg)
    return state


def test_a_run_records_version_checksum_and_destinations(run):
    ok, failed = run.go()
    assert ok == ["radarr"] and failed == []
    [entry] = manifest.backups().values()
    assert entry["app"] == "radarr" and entry["file"].startswith("radarr_") and entry["file"].endswith(".zip")
    assert entry["destinations"] == ["gdrive", "local"]
    assert entry["app_version"] == "6.4.4.10685"
    assert entry["sha256"] == sha(b"archive") and entry["size"] == len(b"archive")
    assert entry["backuparr_version"] == backup.VERSION
    assert entry["encrypted"] is False


def test_a_run_records_whether_a_media_servers_backup_holds_metadata(run, monkeypatch):
    class Emby(FakeApp):
        def backup(self, dest_dir):
            path = os.path.join(dest_dir, "emby.zip")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("backuparr-emby.json", json.dumps({"emby_version": "4.10.2.0", "metadata": True}))
            return path

    run.app = Emby("4.10.2.0")
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: ["emby"])
    run.cfg["apps"] = {"emby": {"url": "http://emby:8096", "api_key": "k", "encrypt": True}}
    ok, failed = run.go()
    assert ok == ["emby"] and failed == []
    [entry] = manifest.backups().values()
    assert entry["metadata"] is True and entry["encrypted"] is True


def test_an_encrypted_backup_is_recorded_as_encrypted_with_the_stored_files_checksum(run, monkeypatch):
    stored = {}
    monkeypatch.setattr(rclone_util, "copyto", lambda src, dst: stored.setdefault("data", open(src, "rb").read()))
    run.cfg["apps"]["radarr"]["encrypt"] = True
    ok, failed = run.go()
    assert ok == ["radarr"] and failed == []
    [entry] = manifest.backups().values()
    assert entry["file"].endswith(".zip.enc") and entry["encrypted"] is True
    assert entry["sha256"] == sha(stored["data"]) and entry["size"] == len(stored["data"])


def test_only_destinations_that_got_the_upload_are_recorded(run):
    run.fail = {"gdrive"}
    ok, failed = run.go()
    assert failed and failed[0].startswith("radarr: failed on gdrive")
    [entry] = manifest.backups().values()
    assert entry["destinations"] == ["local"]


def test_nothing_is_recorded_when_no_upload_worked(run):
    run.fail = {"local", "gdrive"}
    run.go()
    assert manifest.backups() == {}


def test_a_version_lookup_that_fails_records_unknown_and_keeps_the_backup(run, caplog):
    run.app = FakeApp(requests.exceptions.ConnectionError("refused"))
    ok, failed = run.go()
    assert ok == ["radarr"]
    [entry] = manifest.backups().values()
    assert entry["app_version"] is None
    assert "couldn't read its version" in caplog.text


def test_a_manifest_that_cannot_be_written_does_not_fail_the_backup(run, monkeypatch, caplog):
    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(manifest.atomic_file, "write_private", full)
    ok, failed = run.go()
    assert ok == ["radarr"] and failed == []
    assert "couldn't write the backup record" in caplog.text


# ----------------------------------------------------------- retention ----

def test_retention_drops_what_it_deletes_and_entries_for_missing_files(monkeypatch):
    files = ["radarr/radarr_20261001_030000.zip", "radarr/radarr_20261029_030000.zip"]
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): [
        {"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False} for p in files])
    monkeypatch.setattr(rclone_util, "delete_file", lambda path: None)
    for name in ("radarr_20261001_030000.zip", "radarr_20261029_030000.zip", "radarr_20261015_030000.zip"):
        manifest.record("radarr", name, ["local", "gdrive"], app_version=None, sha256="a", size=1, backuparr_version="x")
    backup.apply_retention("dest:", 7, now=datetime(2026, 10, 29, 3), dest_id="local")
    recorded = manifest.backups()
    assert recorded["radarr/radarr_20261001_030000.zip"]["destinations"] == ["gdrive"]  # deleted by retention
    assert recorded["radarr/radarr_20261015_030000.zip"]["destinations"] == ["gdrive"]  # not on local any more
    assert recorded["radarr/radarr_20261029_030000.zip"]["destinations"] == ["gdrive", "local"]


# ------------------------------------------------- versions in archives ----

def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return str(path)


def make_tar(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return str(path)


def tautulli_db(tmp_path, version):
    db_path = tmp_path / "t.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE version_info (key TEXT UNIQUE, value TEXT)")
    if version:
        db.execute("INSERT INTO version_info VALUES ('version', ?)", (version,))
    db.commit()
    db.close()
    return db_path.read_bytes()


# Layouts as found in real backups from each app (lab, 2026-11-04).
def test_the_version_is_read_from_inside_each_apps_archive(tmp_path):
    cases = {
        "radarr": make_zip(tmp_path / "r.zip", {"config.xml": "", "radarr.db": "", "INFO": "v6.4.4.10685\n2026-11-04 20:18:44\n"}),
        "whisparr": make_zip(tmp_path / "w.zip", {"INFO": "v3.6.5.1886\n"}),
        "profilarr": make_tar(tmp_path / "p.tar.gz", {"data/INFO.json": json.dumps({"appVersion": "2.3.1"}).encode()}),
        "jellyfin": make_zip(tmp_path / "j.zip", {"manifest.json": json.dumps({"ServerVersion": "12.4.0.0"})}),
        "tautulli": make_zip(tmp_path / "t.zip", {"tautulli.db": tautulli_db(tmp_path, "v2.18.2"), "config.ini": ""}),
        "lazylibrarian": make_tar(tmp_path / "l.tgz", {"config.ini": b"[General]\nx = 1\n[Git]\ncurrent_version = 228abd29\n"}),
        "plex": make_zip(tmp_path / "x.zip", {"backuparr-plex.json": json.dumps({"plex_version": "1.43.5.11079-b3b13e506"})}),
        "emby": make_zip(tmp_path / "e.zip", {"backuparr-emby.json": json.dumps({"emby_version": "4.10.2.0"})}),
        "seerr": make_zip(tmp_path / "s.zip", {"backuparr-seerr.json": json.dumps({"seerr_version": "3.5.1"})}),
        "nzbget": make_zip(tmp_path / "n.zip", {"backuparr-nzbget.json": json.dumps({"nzbget_version": "26.3"})}),
        "vaultwarden": make_zip(tmp_path / "vw.zip", {"backuparr-vaultwarden.json": json.dumps({"vaultwarden_version": "1.37.5"})}),
        # Pi-hole's own Teleporter zip, first lines as FTL v6.8 writes them (lab, 2026-11-10).
        "pihole": make_zip(tmp_path / "ph.zip", {"etc/pihole/pihole.toml": "# Pi-hole configuration file (v6.8)\n# Encoding: UTF-8\n"}),
    }
    found = {app: manifest_backfill.read_version(app, path) for app, path in cases.items()}
    assert found == {
        "radarr": "v6.4.4.10685", "whisparr": "v3.6.5.1886", "profilarr": "2.3.1", "jellyfin": "12.4.0.0",
        "tautulli": "v2.18.2", "lazylibrarian": "228abd29", "plex": "1.43.5.11079-b3b13e506",
        "emby": "4.10.2.0", "seerr": "3.5.1", "nzbget": "26.3", "pihole": "FTL v6.8", "vaultwarden": "1.37.5",
    }


# Jellyfin's manifest.json as 12.4 writes it (lab, 2026-11-12); Backuparr's own for Emby and Plex (#196).
def test_whether_a_media_servers_backup_holds_metadata_is_read_from_inside_it(tmp_path):
    def jellyfin(name, options):
        return make_zip(tmp_path / name, {"manifest.json": json.dumps({"ServerVersion": "12.4.0.0", "Options": options})})
    with_metadata = {"Metadata": True, "Trickplay": False, "Subtitles": False, "Database": True}
    assert manifest_backfill.read_metadata("jellyfin", jellyfin("j1.zip", with_metadata)) is True
    assert manifest_backfill.read_metadata("jellyfin", jellyfin("j2.zip", {**with_metadata, "Metadata": False})) is False
    assert manifest_backfill.read_metadata("jellyfin", make_zip(tmp_path / "j3.zip", {"manifest.json": "{}"})) is False
    for app in ("emby", "plex"):
        own = f"backuparr-{app}.json"
        assert manifest_backfill.read_metadata(app, make_zip(tmp_path / f"{app}1.zip", {own: json.dumps({"metadata": True})})) is True
        # Made before the switch: no "metadata" in it.
        assert manifest_backfill.read_metadata(app, make_zip(tmp_path / f"{app}2.zip", {own: json.dumps({})})) is False
        assert manifest_backfill.read_metadata(app, make_zip(tmp_path / f"{app}3.zip", {"other": "x"})) is None
    assert manifest_backfill.read_metadata("radarr", make_zip(tmp_path / "r.zip", {"INFO": "v6\n"})) is None
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    assert manifest_backfill.read_metadata("plex", str(broken)) is None


def test_apps_whose_archives_hold_no_version_and_broken_archives_give_none(tmp_path):
    assert manifest_backfill.read_version("bazarr", make_zip(tmp_path / "b.zip", {"bazarr.db": "", "config.yaml": ""})) is None
    assert manifest_backfill.read_version("sabnzbd", make_zip(tmp_path / "s.zip", {"sabnzbd_config.json": "{}"})) is None
    assert manifest_backfill.read_version("tdarr", make_zip(tmp_path / "t.zip", {"FlowsJSONDB.json": "[]"})) is None
    assert manifest_backfill.read_version("radarr", make_zip(tmp_path / "r.zip", {"radarr.db": ""})) is None
    assert manifest_backfill.read_version("pihole", make_zip(tmp_path / "ph.zip", {"etc/pihole/pihole.toml": "[dns]\n"})) is None
    assert manifest_backfill.read_version("tautulli", make_zip(tmp_path / "t2.zip", {"tautulli.db": tautulli_db(tmp_path, None)})) is None
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    assert manifest_backfill.read_version("radarr", str(broken)) is None


# ------------------------------------------------------------- backfill ----

@pytest.fixture
def destinations(tmp_path, monkeypatch):
    """A real local folder, plus a fake "gdrive:" remote whose files are
    served by copyto from a dict."""
    local = tmp_path / "local"
    remote = {}
    downloads = []

    def lsjson(root, recursive=False, include=()):
        if root == "gdrive:":
            return [{"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False, "Size": len(d)} for p, d in remote.items()]
        out = []
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                full = os.path.join(dirpath, name)
                out.append({"Path": os.path.relpath(full, root), "Name": name, "IsDir": False, "Size": os.path.getsize(full)})
        return out

    def copyto(src, dst):
        path = src.removeprefix("gdrive:/")
        if path not in remote:
            raise rclone_util.RcloneError("copyto failed", "object not found")
        downloads.append(path)
        with open(dst, "wb") as f:
            f.write(remote[path])

    monkeypatch.setattr(rclone_util, "lsjson", lsjson)
    monkeypatch.setattr(rclone_util, "copyto", copyto)
    monkeypatch.setattr(destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(destination_util, "remote_root", lambda dest_id, cfg: str(local) if dest_id == "local" else "gdrive:")
    cfg = {"destinations": {"local": {"enabled": True}, "gdrive": {"enabled": True}}}
    monkeypatch.setattr(manifest_backfill, "enabled_destinations", lambda c: ["local", "gdrive"])
    return type("Dests", (), {"local": local, "remote": remote, "downloads": downloads, "cfg": cfg})


def radarr_zip(version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("INFO", f"v{version}\n")
    return buf.getvalue()


def test_backfill_records_existing_backups_with_version_and_checksum(destinations):
    old = radarr_zip("6.4.4.10685")
    (destinations.local / "radarr").mkdir(parents=True)
    (destinations.local / "radarr" / "radarr_20261101_030000.zip").write_bytes(old)
    (destinations.local / "radarr" / "notes.txt").write_text("not a backup")
    destinations.remote["radarr/radarr_20261101_030000.zip"] = old  # the same run's copy
    destinations.remote["sonarr-4k/sonarr-4k_20261101_030000.zip"] = radarr_zip("4.0.20.3014")

    manifest_backfill.backfill(destinations.cfg)

    recorded = manifest.backups()
    assert set(recorded) == {"radarr/radarr_20261101_030000.zip", "sonarr-4k/sonarr-4k_20261101_030000.zip"}
    radarr = recorded["radarr/radarr_20261101_030000.zip"]
    assert radarr["destinations"] == ["gdrive", "local"]
    assert radarr["app_version"] == "6.4.4.10685" and radarr["sha256"] == sha(old)
    assert radarr["backuparr_version"] is None
    assert recorded["sonarr-4k/sonarr-4k_20261101_030000.zip"]["type"] == "sonarr"
    # The Drive copy of radarr wasn't downloaded: the local one was read already.
    assert destinations.downloads == ["sonarr-4k/sonarr-4k_20261101_030000.zip"]


def test_backfill_skips_what_is_recorded_so_an_interrupted_pass_resumes(destinations):
    destinations.remote["radarr/radarr_20261101_030000.zip"] = radarr_zip("6.4")
    destinations.remote["radarr/radarr_20261102_030000.zip"] = radarr_zip("6.5")
    stops = iter([False, True])
    manifest_backfill.backfill(destinations.cfg, should_stop=lambda: next(stops, True))
    assert len(manifest.backups()) == 1
    manifest_backfill.backfill(destinations.cfg)
    assert len(manifest.backups()) == 2
    assert destinations.downloads == ["radarr/radarr_20261101_030000.zip", "radarr/radarr_20261102_030000.zip"]


def test_backfill_records_whether_a_media_server_backup_holds_metadata(destinations, tmp_path):
    plain = make_zip(tmp_path / "jellyfin.zip", {"manifest.json": json.dumps({"ServerVersion": "12.4.0.0", "Options": {"Metadata": True}})})
    data = open(plain, "rb").read()
    destinations.remote["jellyfin/jellyfin_20261101_030000.zip"] = data
    manifest_backfill.backfill(destinations.cfg)
    entry = manifest.get("jellyfin", "jellyfin_20261101_030000.zip")
    assert entry["metadata"] is True and entry["app_version"] == "12.4.0"


def test_a_run_record_is_kept_over_what_the_backfill_reads(destinations):
    data = radarr_zip("6.4")
    destinations.remote["radarr/radarr_20261101_030000.zip"] = data
    manifest.record("radarr", "radarr_20261101_030000.zip", ["local"], app_version="6.4.9", sha256=sha(data),
                    size=len(data), backuparr_version="2.5.0")
    manifest_backfill.backfill(destinations.cfg)
    entry = manifest.get("radarr", "radarr_20261101_030000.zip")
    assert entry["app_version"] == "6.4.9" and entry["backuparr_version"] == "2.5.0"
    assert entry["destinations"] == ["gdrive", "local"]


def test_a_download_that_fails_is_retried_next_time(destinations, monkeypatch, caplog):
    destinations.remote["radarr/radarr_20261101_030000.zip"] = radarr_zip("6.4")
    real_copyto = rclone_util.copyto

    def flaky(src, dst):
        raise rclone_util.RcloneError("copyto failed", "connection reset")

    monkeypatch.setattr(rclone_util, "copyto", flaky)
    manifest_backfill.backfill(destinations.cfg)
    assert manifest.backups() == {}
    assert "connection reset" in caplog.text
    monkeypatch.setattr(rclone_util, "copyto", real_copyto)
    manifest_backfill.backfill(destinations.cfg)
    assert len(manifest.backups()) == 1


def test_a_destination_that_cannot_be_listed_does_not_stop_the_others(destinations, monkeypatch, caplog):
    destinations.remote["radarr/radarr_20261101_030000.zip"] = radarr_zip("6.4")
    real_lsjson = rclone_util.lsjson

    def lsjson(root, recursive=False, include=()):
        if root != "gdrive:":
            raise rclone_util.RcloneError("lsjson failed", "permission denied")
        return real_lsjson(root, recursive, include)

    monkeypatch.setattr(rclone_util, "lsjson", lsjson)
    manifest_backfill.backfill(destinations.cfg)
    assert "couldn't check the backups on local" in caplog.text
    assert len(manifest.backups()) == 1


# ------------------------------------------------- version lookups ----

class Reply:
    def __init__(self, data):
        self.data, self.status_code = data, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.fixture
def http(monkeypatch):
    sent = []

    def answer(self, method, url, **kwargs):
        sent.append((method, url, kwargs.get("params") or kwargs.get("data")))
        return http.reply

    monkeypatch.setattr(requests.Session, "request", answer)
    http.sent = sent
    return http


# Answers as the real apps gave them (lab, 2026-11-04).
def test_bazarr_reports_its_version_from_system_status(http):
    http.reply = Reply({"data": {"bazarr_version": "1.6.2", "package_version": "v1.6.2-ls371"}})
    assert BazarrApp("http://bazarr:6767", "k").app_version() == "1.6.2"
    assert http.sent[0][:2] == ("GET", "http://bazarr:6767/api/system/status")


def test_sabnzbd_reports_its_version_from_mode_version(http):
    http.reply = Reply({"version": "5.1.3"})
    assert SabnzbdApp("http://sab:8080", "k").app_version() == "5.1.3"
    assert http.sent[0][2]["mode"] == "version"


def test_tautulli_reports_its_version_from_get_tautulli_info(http):
    http.reply = Reply({"response": {"result": "success", "data": {"tautulli_version": "v2.18.2"}}})
    assert TautulliApp("http://tautulli:8181", "k").app_version() == "v2.18.2"
    assert http.sent[0][2]["cmd"] == "get_tautulli_info"


def test_every_driver_can_report_its_version():
    missing = [name for name, driver in backup.DRIVERS.items() if not callable(getattr(driver, "app_version", None))]
    assert missing == []


# --------------------------------------------------------------- web UI ----

def test_history_shows_each_backups_version_and_delete_updates_the_manifest(authed_client, isolated_webui, monkeypatch):
    files = ["radarr/radarr_20261101_030000.zip", "radarr/radarr_20261102_030000.zip", "sonarr/sonarr_20261101_030000.zip"]
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): [
        {"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False, "Size": 1, "ModTime": p[-19:-4]} for p in files])
    deleted = []
    monkeypatch.setattr(rclone_util, "delete_file", deleted.append)
    authed_client.put("/api/config", json={"destinations": {"local": {"enabled": True}}})
    manifest.record("radarr", "radarr_20261102_030000.zip", ["local"], app_version="6.4.4.10685", sha256="a",
                    size=1, backuparr_version="2.5.0")
    manifest.record("sonarr", "sonarr_20261101_030000.zip", ["local"], app_version=None, sha256="b",
                    size=1, backuparr_version="2.5.0")

    history = authed_client.get("/api/history/local").get_json()
    radarr = {e["name"]: (e["app_version"], e["recorded"]) for e in history["radarr"]}
    assert radarr == {"radarr_20261102_030000.zip": ("6.4.4.10685", True), "radarr_20261101_030000.zip": (None, False)}
    assert [(e["app_version"], e["recorded"]) for e in history["sonarr"]] == [(None, True)]

    response = authed_client.delete("/api/history/local/radarr/radarr_20261102_030000.zip")
    assert response.status_code == 200 and len(deleted) == 1
    assert manifest.get("radarr", "radarr_20261102_030000.zip") is None


def test_history_says_which_backups_hold_metadata(authed_client, isolated_webui, monkeypatch):
    files = ["plex/plex_20261101_030000.zip", "plex/plex_20261102_030000.zip", "plex/plex_20261031_030000.zip",
             "plex/plex_20261030_030000.zip", "radarr/radarr_20261101_030000.zip"]
    monkeypatch.setattr(rclone_util, "lsjson", lambda root, recursive=False, include=(): [
        {"Path": p, "Name": p.rsplit("/", 1)[-1], "IsDir": False, "Size": 1, "ModTime": p[-19:-4]} for p in files])
    authed_client.put("/api/config", json={"destinations": {"local": {"enabled": True}}})
    manifest.record("plex", "plex_20261102_030000.zip", ["local"], app_version="1.43", sha256="a", size=1,
                    backuparr_version="3.0.0", metadata=True)
    manifest.record("plex", "plex_20261101_030000.zip", ["local"], app_version="1.43", sha256="b", size=1,
                    backuparr_version="3.0.0", metadata=False)
    manifest.record("plex", "plex_20261031_030000.zip", ["local"], app_version="1.43", sha256="d", size=1,
                    backuparr_version="3.0.0-beta")  # recorded before the switch existed
    manifest.record("radarr", "radarr_20261101_030000.zip", ["local"], app_version="6.4", sha256="c", size=1,
                    backuparr_version="3.0.0")
    history = authed_client.get("/api/history/local").get_json()
    assert [(e["name"], e["metadata"]) for e in history["plex"]] == [
        ("plex_20261102_030000.zip", True), ("plex_20261101_030000.zip", False),
        ("plex_20261031_030000.zip", False), ("plex_20261030_030000.zip", None)]  # the last not read yet
    assert history["radarr"][0]["metadata"] is None
    assert "metadata" not in manifest.get("radarr", "radarr_20261101_030000.zip")


def test_reset_deletes_the_manifest(authed_client, isolated_webui, manifest_in_tmp):
    manifest.record("radarr", "radarr_1.zip", ["gdrive"], app_version=None, sha256="a", size=1, backuparr_version="x")
    response = authed_client.post("/api/reset", json={"confirm": isolated_webui.auth.RESET_CONFIRM_PHRASE})
    assert response.status_code == 200
    assert not os.path.exists(manifest_in_tmp)


def test_backfill_waits_while_a_backup_or_restore_runs(destinations, monkeypatch):
    destinations.remote["radarr/radarr_20261101_030000.zip"] = radarr_zip("6.4")
    monkeypatch.setattr(manifest_backfill, "_PAUSE_POLL_SECONDS", 0)
    busy = iter([True, True, True])  # busy for the first three checks

    def should_pause():
        paused = next(busy, False)
        if paused:
            assert destinations.downloads == []  # nothing downloaded while busy
        return paused

    manifest_backfill.backfill(destinations.cfg, should_pause=should_pause)
    assert destinations.downloads == ["radarr/radarr_20261101_030000.zip"]


def test_the_web_app_pauses_the_backfill_during_runs(isolated_webui):
    runs = isolated_webui.runs
    assert runs.busy() is False
    runs.RUN_STATE["running"] = True
    try:
        assert runs.busy() is True
    finally:
        runs.RUN_STATE["running"] = False
