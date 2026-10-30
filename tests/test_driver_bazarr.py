"""Bazarr restore: it restarts inside the restore request, so a dropped
connection - or a reverse proxy's gateway error for it - means success."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.bazarr import BazarrApp


def reply(status):
    res = requests.Response()
    res.status_code = status
    return res


@pytest.fixture
def restore(tmp_path):
    source = tmp_path / "bazarr_20261028_210819.zip"
    source.write_bytes(b"zip")
    target_dir = tmp_path / "bazarr-backup"
    target_dir.mkdir()
    app = BazarrApp("http://bazarr.example", "fake-api-key")

    def run(get=None, patch_result=None):
        get = get or MagicMock(return_value=MagicMock(json=lambda: {"data": []}))
        sent = patch_result if isinstance(patch_result, Exception) else None
        with patch.object(app.session, "get", get), \
             patch.object(app.session, "patch", side_effect=sent, return_value=patch_result) as patched:
            return app.restore_from_file(str(source), str(target_dir)), patched

    run.target_dir = target_dir
    return run


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_proxy_gateway_error_during_the_restart_counts_as_success(restore, status, caplog):
    caplog.set_level("INFO")
    result, _ = restore(patch_result=reply(status))
    assert result is None and f"HTTP {status}" in caplog.text


def test_a_dropped_connection_during_the_restart_counts_as_success(restore):
    result, _ = restore(patch_result=requests.exceptions.ConnectionError("reset"))
    assert result is None


def test_other_errors_from_the_restore_request_still_fail(restore):
    with pytest.raises(requests.exceptions.HTTPError):
        restore(patch_result=reply(500))


def test_an_unreachable_bazarr_fails_before_anything_is_placed_or_sent(restore):
    down = MagicMock(side_effect=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(requests.exceptions.ConnectionError):
        restore(get=down)
    assert list(restore.target_dir.iterdir()) == []


# --- backup: the downloaded zip must read back intact ---------------------------

def zip_bytes(corrupt=False):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("bazarr.db", b"A" * 100)
        archive.writestr("config.yaml", b"general: {}\n")
    data = buffer.getvalue()
    # Same central directory, one changed byte in a member: a CRC mismatch.
    return data.replace(b"A" * 100, b"A" * 99 + b"B", 1) if corrupt else data


class FakeBazarr:
    def __init__(self, downloads):
        self.downloads = downloads
        self.listed = 0
        self.deleted = []

    def get(self, url, **kwargs):
        res = reply(200)
        if url.endswith("/api/system/backups"):
            self.listed += 1
            files = [] if self.listed == 1 else [{"filename": "bazarr_backup_v1.zip"}]
            res._content = requests.compat.json.dumps({"data": files}).encode()
        else:
            res._content = self.downloads.pop(0)
        return res

    def post(self, url, **kwargs):
        return reply(200)

    def delete(self, url, params=None, **kwargs):
        self.deleted.append(params["filename"])
        return reply(200)


def run_backup(tmp_path, downloads, timeout_s=180):
    app = BazarrApp("http://bazarr.example", "fake-api-key")
    fake = FakeBazarr(downloads)
    with patch.object(app, "session", fake):
        return app.backup(str(tmp_path), poll_interval=0, timeout_s=timeout_s), fake


def test_a_download_with_a_bad_crc_is_fetched_again(tmp_path):
    with patch("apps.bazarr.time.sleep"):
        path, fake = run_backup(tmp_path, [zip_bytes(corrupt=True), zip_bytes()])
    assert open(path, "rb").read() == zip_bytes() and fake.downloads == []
    assert fake.deleted == ["bazarr_backup_v1.zip"]


def test_a_download_that_never_reads_back_fails_and_keeps_the_server_copy(tmp_path):
    from apps.bazarr import BazarrError
    downloads = [zip_bytes(corrupt=True)] * 3
    clock = iter([0, 0])  # the deadline, the listing loop; then it has passed
    fake_time = SimpleNamespace(time=lambda: next(clock, 1000), sleep=lambda seconds: None)
    with patch("apps.bazarr.time", fake_time):
        with pytest.raises(BazarrError, match="never finished writing"):
            run_backup(tmp_path, downloads)
    assert len(downloads) == 2  # one download, then the deadline had passed
