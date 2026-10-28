"""Bazarr restore: it restarts inside the restore request, so a dropped
connection - or a reverse proxy's gateway error for it - means success."""
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
