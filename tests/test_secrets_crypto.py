"""Tests for secrets_crypto.encrypt()/decrypt() round-trip and the
documented "wrong key -> return empty string, don't crash" behavior."""
import pytest

import secrets_crypto


@pytest.fixture(autouse=True)
def isolated_key(tmp_path, monkeypatch):
    """Point KEY_PATH at a fresh tmp_path per test and clear the cached
    Fernet instance so each test gets its own independent key context."""
    monkeypatch.delenv("BACKUPARR_SECRETS_KEY", raising=False)
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "secrets.key"))
    monkeypatch.setattr(secrets_crypto, "_fernet", None)
    yield
    monkeypatch.setattr(secrets_crypto, "_fernet", None)


def test_encrypt_decrypt_roundtrip():
    original = "super-secret-api-key"
    encrypted = secrets_crypto.encrypt(original)

    assert encrypted != original
    assert encrypted.startswith(secrets_crypto.PREFIX)
    assert secrets_crypto.decrypt(encrypted) == original


def test_empty_string_passes_through_unchanged():
    assert secrets_crypto.encrypt("") == ""
    assert secrets_crypto.decrypt("") == ""


def test_decrypt_with_different_key_returns_empty_string(tmp_path, monkeypatch):
    encrypted = secrets_crypto.encrypt("secret-value")

    # Switch to a brand new key file/context - simulates a lost/rotated key.
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "other" / "secrets.key"))
    monkeypatch.setattr(secrets_crypto, "_fernet", None)

    assert secrets_crypto.decrypt(encrypted) == ""
