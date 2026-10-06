"""Tests for the os.path.basename() sanitization applied to API-sourced
backup filenames in apps/bazarr.py and apps/profilarr.py, so a malicious
filename reported by the remote app's own API can't be used to write
outside the intended destination directory."""
import io
import os
import zipfile
from unittest.mock import MagicMock, patch

from apps.bazarr import BazarrApp
from apps.profilarr import ProfilarrApp


def _valid_zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bazarr.db", b"fake db content")
    return buf.getvalue()


def test_bazarr_backup_sanitizes_malicious_filename(tmp_path):
    app = BazarrApp("http://bazarr.example", "fake-api-key")
    malicious_name = "../../etc/evil.zip"

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.content = _valid_zip_bytes()
    fake_response.raise_for_status = MagicMock()

    with patch.object(app, "trigger_backup", return_value=None), patch.object(
        app, "list_backups", side_effect=[[], [{"filename": malicious_name}]]
    ), patch.object(app.session, "get", return_value=fake_response), patch.object(
        app, "delete_backup", return_value=None
    ):
        dest = app.backup(str(tmp_path))

    # os.path.basename() must strip the traversal - the file lands squarely
    # inside dest_dir, never above it.
    assert os.path.dirname(dest) == str(tmp_path)
    assert os.path.basename(dest) == "evil.zip"
    assert os.path.exists(dest)
    assert not (tmp_path.parent / "evil.zip").exists()


def test_profilarr_download_latest_sanitizes_malicious_filename(tmp_path):
    app = ProfilarrApp("http://profilarr.example", "fake-api-key")
    malicious_name = "/etc/evil.zip"

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.content = b"fake profilarr backup bytes"
    fake_response.raise_for_status = MagicMock()

    with patch.object(
        app, "list_backups", return_value=[{"filename": malicious_name}]
    ), patch.object(app.session, "get", return_value=fake_response):
        dest, filename = app.download_latest(str(tmp_path))

    assert filename == "evil.zip"
    assert os.path.dirname(dest) == str(tmp_path)
    assert os.path.exists(dest)


def test_bazarr_restore_file_stays_readable_by_bazarr_under_a_private_umask(tmp_path):
    source = tmp_path / "bazarr_20261006_171108.zip"
    source.write_bytes(_valid_zip_bytes())
    bazarr_dir = tmp_path / "bazarr-backup"
    bazarr_dir.mkdir()
    app = BazarrApp("http://bazarr.example", "fake-api-key")

    previous = os.umask(0o077)
    try:
        with patch.object(app.session, "patch", return_value=MagicMock()):
            app.restore_from_file(str(source), str(bazarr_dir))
    finally:
        os.umask(previous)

    placed = bazarr_dir / "bazarr_backup_vbazarr_20261006_171108.zip"
    assert placed.read_bytes() == source.read_bytes()
    assert placed.stat().st_mode & 0o777 == 0o644
