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


@pytest.mark.parametrize("name", ["radarr", "sonarr", "prowlarr"])
@pytest.mark.parametrize("encrypted", [False, True])
def test_removed_web_credentials_are_cleared_on_load(name, encrypted):
    import json
    from backup import build_app

    cfg = config_store.load_config()
    cfg["apps"][name].update(url=f"http://{name}:9999", api_key="retained-api-key")
    config_store.save_config(cfg)
    with open(config_store.CONFIG_PATH) as source:
        old = json.load(source)
    obsolete_password = secrets_crypto.encrypt("obsolete-login-password") if encrypted else "obsolete-login-password"
    old["apps"][name].update(username="obsolete-user", password=obsolete_password)
    with open(config_store.CONFIG_PATH, "w") as target:
        json.dump(old, target)

    reloaded = config_store.load_config()
    assert reloaded["apps"][name]["username"] == ""
    assert reloaded["apps"][name]["password"] == ""
    assert reloaded["apps"][name]["api_key"] == "retained-api-key"
    with open(config_store.CONFIG_PATH) as source:
        disk = source.read()
    assert "obsolete-user" not in disk and obsolete_password not in disk
    instance = build_app(name, reloaded["apps"][name])
    assert not hasattr(instance, "username") and not hasattr(instance, "password")
    instance.session.close()


def test_save_drops_removed_credentials_and_preserves_bazarr_login():
    cfg = config_store.load_config()
    for name in ("radarr", "sonarr", "prowlarr"):
        cfg["apps"][name].update(username="unused-user", password="unused-password")
    cfg["apps"]["bazarr"].update(username="bazarr-user", password="bazarr-password")
    config_store.save_config(cfg)
    reloaded = config_store.load_config()
    assert reloaded["apps"]["bazarr"]["username"] == "bazarr-user"
    assert reloaded["apps"]["bazarr"]["password"] == "bazarr-password"
    with open(config_store.CONFIG_PATH) as source:
        disk = source.read()
    assert all(value not in disk for value in ("unused-user", "unused-password", "bazarr-password"))
