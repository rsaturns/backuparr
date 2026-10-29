import os
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

import backup


def make_zip(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.txt", "x")
    return path


@pytest.mark.parametrize("name, expected", [
    ("backup-2026-10-06-172005.tar.gz", ".tar.gz"),
    ("backup.tar.xz", ".tar.xz"),
    ("dump.sqlite", ".sqlite"),
    ("no_extension", ".bin"),
])
def test_non_zip_archives_keep_their_own_suffix(tmp_path, name, expected):
    path = tmp_path / name
    path.write_bytes(b"\x1f\x8b not a zip")
    assert backup.archive_suffix(path) == expected


def test_real_zips_are_named_zip_whatever_their_name(tmp_path):
    assert backup.archive_suffix(make_zip(tmp_path / "radarr_backup_v6.zip")) == ".zip"
    assert backup.archive_suffix(make_zip(tmp_path / "odd.bak")) == ".zip"


def test_run_backup_names_each_archive_by_its_real_format(tmp_path, monkeypatch):
    class Fake:
        def __init__(self, kind):
            self.kind = kind

        def backup(self, work_dir):
            if self.kind == "tar":
                path = Path(work_dir) / "backup-2026-10-06-172005.tar.gz"
                path.write_bytes(b"\x1f\x8b profilarr")
            elif self.kind == "zip":
                path = make_zip(Path(work_dir) / "radarr_backup.zip")
            else:
                path = Path(work_dir) / "files"
                path.mkdir()
                (path / "a.json").write_text("{}")
            return str(path)

    kinds = {"profilarr": "tar", "radarr": "zip", "tdarr": "dir"}
    uploaded = []
    monkeypatch.setattr(backup, "enabled_apps", lambda cfg: list(kinds))
    monkeypatch.setattr(backup, "enabled_destinations", lambda cfg: ["local"])
    monkeypatch.setattr(backup.destination_util, "sync", lambda cfg: None)
    monkeypatch.setattr(backup.destination_util, "remote_root", lambda dest_id, cfg: "/dest")
    monkeypatch.setattr(backup, "build_app", lambda name, cfg: Fake(kinds[name]))
    monkeypatch.setattr(backup.rclone_util, "copyto", lambda src, dst: uploaded.append(dst))
    monkeypatch.setattr(backup, "apply_retention", lambda *args: None)

    ok, failed = backup.run_backup({"apps": {n: {} for n in kinds}, "destinations": {"local": {}}})

    assert not failed and sorted(ok) == sorted(kinds)
    names = {dst.split("/")[2]: dst.rsplit("/", 1)[1] for dst in uploaded}
    assert re.fullmatch(r"profilarr_\d{8}_\d{6}\.tar\.gz", names["profilarr"])
    assert re.fullmatch(r"radarr_\d{8}_\d{6}\.zip", names["radarr"])
    assert re.fullmatch(r"tdarr_\d{8}_\d{6}\.zip", names["tdarr"])


ENTRYPOINT = Path(__file__).resolve().parent.parent / "entrypoint.sh"
UMASK_BLOCK = re.search(r'UMASK="\$\{UMASK:-077\}".*?umask "\$UMASK"\n', ENTRYPOINT.read_text(), re.S).group(0)


def effective_umask(value):
    env = {k: v for k, v in os.environ.items() if k != "UMASK"}
    if value is not None:
        env["UMASK"] = value
    out = subprocess.run(["bash", "-c", UMASK_BLOCK + "umask"], env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("value, expected", [
    (None, "0077"),
    ("022", "0022"),
    ("0027", "0027"),
    ("77", "0077"),
    ("999", "0077"),
    ("rwx", "0077"),
])
def test_entrypoint_umask_defaults_private_and_rejects_bad_values(value, expected):
    assert effective_umask(value) == expected


PROXY_BLOCK = re.search(r'FORCE_HTTPS_LOWER=.*?\nfi\n', ENTRYPOINT.read_text(), re.S).group(0)


def waitress_args(**env):
    script = 'WAITRESS_ARGS=()\n' + PROXY_BLOCK + 'printf "%s\\n" "${WAITRESS_ARGS[@]}"'
    clean = {k: v for k, v in os.environ.items() if not k.startswith("BACKUPARR_")}
    out = subprocess.run(["bash", "-c", script], env={**clean, **env}, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def test_plain_http_trusts_no_proxy_headers():
    assert waitress_args() == []


def test_https_alone_never_trusts_a_forwarded_client_address():
    # Anyone reaching the port directly could forge X-Forwarded-For to dodge the login lockout.
    args = waitress_args(BACKUPARR_FORCE_HTTPS="true")
    assert args == ['--trusted-proxy=*', '--trusted-proxy-headers=x-forwarded-proto x-forwarded-host']


def test_a_named_proxy_is_the_only_source_of_client_addresses():
    args = waitress_args(BACKUPARR_FORCE_HTTPS="yes", BACKUPARR_TRUSTED_PROXY="172.18.0.5")
    assert args == ['--trusted-proxy=172.18.0.5', '--trusted-proxy-headers=x-forwarded-proto x-forwarded-for x-forwarded-host']
