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


def test_the_file_and_its_folder_are_flushed_to_disk(tmp_path, monkeypatch):
    """A power cut can't leave the new name with nothing in it (#229)."""
    synced, real = [], os.fsync
    monkeypatch.setattr(atomic_file.os, "fsync", lambda fd: synced.append(os.path.realpath(f"/proc/self/fd/{fd}")) or real(fd))
    atomic_file.write_private(str(tmp_path / "secrets.key"), "key")
    assert len(synced) == 2 and synced[0].startswith(str(tmp_path / ".secrets.key.")) and synced[1] == str(tmp_path)
    assert (tmp_path / "secrets.key").read_text() == "key"


def test_a_filesystem_that_cant_flush_still_gets_the_file(tmp_path, monkeypatch):
    import errno

    def unsupported(fd):
        raise OSError(errno.EINVAL, "Invalid argument")
    monkeypatch.setattr(atomic_file.os, "fsync", unsupported)
    atomic_file.write_private(str(tmp_path / "config.json"), "{}")
    assert (tmp_path / "config.json").read_text() == "{}"


def test_a_failed_flush_leaves_the_old_file(tmp_path, monkeypatch):
    import errno
    target = tmp_path / "auth.json"
    target.write_text("old")

    def failing(fd):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(atomic_file.os, "fsync", failing)
    with pytest.raises(OSError):
        atomic_file.write_private(str(target), "new")
    assert target.read_text() == "old" and os.listdir(tmp_path) == ["auth.json"]
