"""atomic_file.write_private: Backuparr's own files are private and never half-written."""
import os
import stat

import pytest

import atomic_file


def test_writes_text_or_bytes_readable_by_this_user_only(tmp_path):
    atomic_file.write_private(str(tmp_path / "new" / "config.json"), "{}")
    atomic_file.write_private(str(tmp_path / "key"), b"\x00\x01")
    assert (tmp_path / "new" / "config.json").read_text() == "{}" and (tmp_path / "key").read_bytes() == b"\x00\x01"
    assert stat.S_IMODE((tmp_path / "key").stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_old_file_and_no_temporary_one(tmp_path, monkeypatch):
    target = tmp_path / "auth.json"
    target.write_text("old")
    monkeypatch.setattr(atomic_file.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        atomic_file.write_private(str(target), "new")
    assert target.read_text() == "old" and os.listdir(tmp_path) == ["auth.json"]
