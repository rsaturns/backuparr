"""The decrypt command in the image (#147, #162)."""
import pytest
from cryptography.fernet import Fernet

import archive_crypto
import decrypt
import secrets_crypto

KEY = Fernet.generate_key().decode("ascii")


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv("BACKUPARR_SECRETS_KEY", KEY)


def test_an_app_backup_is_written_beside_it_without_enc(tmp_path, key_env, capsys):
    sealed = tmp_path / "radarr_20261108_030000.zip.enc"
    sealed.write_bytes(archive_crypto.encrypt_bytes(b"PK radarr", KEY))
    decrypt.main([str(sealed)])
    assert (tmp_path / "radarr_20261108_030000.zip").read_bytes() == b"PK radarr"
    assert "decrypted to" in capsys.readouterr().out


def test_backuparrs_own_backup_becomes_a_zip(tmp_path, key_env):
    sealed = tmp_path / "backuparr_20261108_030000.enc"
    sealed.write_bytes(archive_crypto.encrypt_bytes(b"PK new", KEY))
    decrypt.main([str(sealed)])
    assert (tmp_path / "backuparr_20261108_030000.zip").read_bytes() == b"PK new"


def test_backuparrs_own_backup_from_before_3_0_says_to_use_2_5_7(tmp_path, monkeypatch):
    """Said before any key is asked for."""
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "missing.key"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    old = tmp_path / "backuparr_20261101_030000.enc"
    old.write_bytes(Fernet(KEY.encode()).encrypt(b"PK old"))  # as 2.5.x wrote it
    with pytest.raises(SystemExit, match=r"from before Backuparr 3\.0\. Restore it with Backuparr 2\.5\.7, then update"):
        decrypt.main([str(old)])
    assert not (tmp_path / "backuparr_20261101_030000.zip").exists()


def test_a_file_that_isnt_an_encrypted_backup_says_so(tmp_path, key_env):
    plain = tmp_path / "radarr_1.zip.enc"
    plain.write_bytes(b"PK a plain zip")
    with pytest.raises(SystemExit, match="isn't an encrypted Backuparr backup"):
        decrypt.main([str(plain)])
    with pytest.raises(SystemExit, match="not found"):
        decrypt.main([str(tmp_path / "missing.zip.enc")])


def test_the_wrong_key_says_so_and_writes_nothing(tmp_path, key_env):
    sealed = tmp_path / "radarr_1.zip.enc"
    sealed.write_bytes(archive_crypto.encrypt_bytes(b"PK", Fernet.generate_key().decode()))
    with pytest.raises(SystemExit, match="different encryption key"):
        decrypt.main([str(sealed)])
    assert not (tmp_path / "radarr_1.zip").exists()


def test_it_never_overwrites_a_file(tmp_path, key_env):
    sealed = tmp_path / "radarr_1.zip.enc"
    sealed.write_bytes(archive_crypto.encrypt_bytes(b"PK", KEY))
    (tmp_path / "radarr_1.zip").write_bytes(b"mine")
    with pytest.raises(SystemExit, match="already exists"):
        decrypt.main([str(sealed)])
    assert (tmp_path / "radarr_1.zip").read_bytes() == b"mine"


def test_without_a_key_or_a_terminal_it_says_how_to_give_one(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "missing.key"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    sealed = tmp_path / "radarr_1.zip.enc"
    sealed.write_bytes(archive_crypto.encrypt_bytes(b"PK", KEY))
    with pytest.raises(SystemExit, match="BACKUPARR_SECRETS_KEY"):
        decrypt.main([str(sealed)])
    assert not (tmp_path / "missing.key").exists()  # never makes a key
