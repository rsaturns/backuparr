"""Tests for config_store.load_config()/save_config() round-trip,
including that fields in _secret_fields() are actually encrypted at rest
and correctly decrypted back on load."""
import os

import pytest

import config_store
import secrets_crypto


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUPARR_SECRETS_KEY", raising=False)
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(secrets_crypto, "KEY_PATH", str(tmp_path / "secrets.key"))
    monkeypatch.setattr(secrets_crypto, "_fernet", None)
    yield
    monkeypatch.setattr(secrets_crypto, "_fernet", None)


def test_load_config_creates_defaults_when_missing():
    assert not os.path.exists(config_store.CONFIG_PATH)

    cfg = config_store.load_config()

    assert os.path.exists(config_store.CONFIG_PATH)
    assert cfg["retention_days"] == config_store.DEFAULTS["retention_days"]
    assert cfg["cron_schedule"] == config_store.DEFAULTS["cron_schedule"]
    assert cfg["apps"]["radarr"] == config_store.DEFAULT_APP
    assert cfg["destinations"]["local"] == config_store.DEFAULT_DEST["local"]


def test_save_and_load_roundtrip_plain_and_encrypted_fields():
    cfg = config_store.load_config()
    cfg["retention_days"] = 30
    cfg["apps"]["radarr"]["enabled"] = True
    cfg["apps"]["radarr"]["url"] = "http://radarr:7878"
    cfg["apps"]["radarr"]["api_key"] = "plain-radarr-key"
    config_store.save_config(cfg)

    reloaded = config_store.load_config()

    # Plain field.
    assert reloaded["retention_days"] == 30
    assert reloaded["apps"]["radarr"]["enabled"] is True
    assert reloaded["apps"]["radarr"]["url"] == "http://radarr:7878"
    # Field that gets encrypted at rest (config_store._secret_fields()).
    assert reloaded["apps"]["radarr"]["api_key"] == "plain-radarr-key"

    # Confirm it's genuinely encrypted on disk, not just round-tripped in
    # memory.
    with open(config_store.CONFIG_PATH) as f:
        raw_on_disk = f.read()
    assert "plain-radarr-key" not in raw_on_disk
    assert secrets_crypto.PREFIX in raw_on_disk


def test_a_config_saved_before_dropbox_existed_loads_with_an_empty_token():
    import json

    with open(config_store.CONFIG_PATH, "w") as f:
        json.dump({"destinations": {"dropbox": {"enabled": False}}}, f)

    cfg = config_store.load_config()

    assert cfg["destinations"]["dropbox"] == {"enabled": False, "token": ""}
    assert config_store.enabled_destinations(cfg) == ["local"]


def test_dropbox_token_is_encrypted_at_rest():
    cfg = config_store.load_config()
    cfg["destinations"]["dropbox"].update(enabled=True, token="plain-dropbox-token")
    config_store.save_config(cfg)

    with open(config_store.CONFIG_PATH) as f:
        assert "plain-dropbox-token" not in f.read()
    reloaded = config_store.load_config()
    assert reloaded["destinations"]["dropbox"]["token"] == "plain-dropbox-token"
    assert config_store.enabled_destinations(reloaded) == ["local", "dropbox"]


@pytest.mark.parametrize("content", ["{not json", "", "[1, 2]", '"text"'])
def test_an_unusable_config_file_raises_a_message_that_says_what_to_do(content):
    with open(config_store.CONFIG_PATH, "w") as f:
        f.write(content)
    with pytest.raises(config_store.ConfigError) as caught:
        config_store.load_config()
    message = str(caught.value)
    assert config_store.CONFIG_PATH in message and "delete the file to start over" in message
