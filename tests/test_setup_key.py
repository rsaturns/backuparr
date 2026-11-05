"""A missing login on an install that already holds settings is only
replaced with the encryption key (#123): deleting auth.json must not hand
the stored secrets to whoever reaches the setup screen next."""
import base64
import os

import pytest
from cryptography.fernet import Fernet

import auth_store
import config_store
import secrets_crypto
import self_backup

PASSWORD = "the-old-password"
NEW = "a-new-password"


@pytest.fixture
def lost_login(isolated_webui):
    """An install with Radarr's API key stored and the old admin signed in
    on a client - then auth.json deleted. Returns (old client, key)."""
    old = isolated_webui.app.test_client()
    assert old.post("/api/setup", json={"username": "olduser", "password": PASSWORD}).status_code == 200
    assert old.post("/api/config", json={"apps": {"radarr": {"enabled": True, "url": "http://radarr:7878",
                                                            "api_key": "radarr-secret-key"}}}).status_code == 200
    os.remove(auth_store.AUTH_PATH)
    return old, secrets_crypto.current_key()


def setup(client, key=None, **extra):
    body = {"username": "newuser", "password": NEW, **extra}
    if key is not None:
        body["key"] = key
    return client.post("/api/setup", json=body)


def test_a_new_install_needs_no_key(isolated_webui):
    client = isolated_webui.app.test_client()
    assert 'id="su-key"' not in client.get("/setup").get_data(as_text=True)
    assert setup(client).status_code == 200


def test_without_the_key_the_login_isnt_replaced(lost_login, isolated_webui):
    client = isolated_webui.app.test_client()
    assert 'id="su-key"' in client.get("/setup").get_data(as_text=True)
    response = setup(client)
    assert response.status_code == 400 and "enter your encryption key" in response.get_json()["error"]
    response = setup(client, key=Fernet.generate_key().decode())
    assert response.status_code == 403 and "isn't this Backuparr's encryption key" in response.get_json()["error"]
    assert not auth_store.has_credentials()


def test_the_key_replaces_the_login_and_keeps_the_settings(lost_login, isolated_webui):
    old, key = lost_login
    client = isolated_webui.app.test_client()
    assert setup(client, key=f"  {key}\n").status_code == 200
    assert auth_store.verify_password("newuser", NEW)
    assert config_store.load_config()["apps"]["radarr"]["api_key"] == "radarr-secret-key"
    assert client.get("/api/config").status_code == 200  # signed in, key prompt skipped
    assert old.get("/api/config").status_code == 401  # the session from before is gone


def test_a_confirmed_key_alone_counts_as_settings(isolated_webui):
    cfg = config_store.load_config()
    cfg["secrets_key_acknowledged"] = True
    config_store.save_config(cfg)
    assert setup(isolated_webui.app.test_client()).status_code == 400


def test_an_unreadable_config_counts_as_settings(isolated_webui):
    with open(config_store.CONFIG_PATH, "w") as f:
        f.write("{not json")
    assert config_store.holds_settings() is True


def test_wrong_keys_count_towards_the_login_lockout(lost_login, isolated_webui):
    client = isolated_webui.app.test_client()
    codes = [setup(client, key=Fernet.generate_key().decode()).status_code for _ in range(12)]
    assert codes[0] == 403 and codes[-1] == 429
    _old, key = lost_login
    assert setup(client, key=key).status_code == 429  # even the right key waits out the lockout


def test_the_key_from_the_environment_is_the_one_asked_for(lost_login, isolated_webui, monkeypatch):
    _old, key = lost_login
    monkeypatch.setenv("BACKUPARR_SECRETS_KEY", key)
    os.remove(secrets_crypto.KEY_PATH)  # the volume alone no longer holds it
    secrets_crypto.forget_key()
    client = isolated_webui.app.test_client()
    assert setup(client, key=Fernet.generate_key().decode()).status_code == 403
    assert setup(client, key=key).status_code == 200


def restore_upload(archive, key):
    return {"archive": base64.b64encode(archive).decode(), "key": key}


@pytest.fixture
def rclone(monkeypatch):
    import rclone_util
    monkeypatch.setattr(rclone_util, "config_dump", lambda: {})
    monkeypatch.setattr(rclone_util, "config_delete", lambda name: None)
    monkeypatch.setattr(rclone_util, "config_set", lambda *args, **kwargs: None)


def test_a_restore_on_the_setup_screen_needs_this_installs_key(lost_login, isolated_webui, rclone):
    """A backup made with another key can't replace the settings, and is
    refused before it's opened; one made with this install's key can."""
    _old, key = lost_login
    client = isolated_webui.app.test_client()
    other_key = Fernet.generate_key().decode()
    response = client.post("/api/setup/restore", json=restore_upload(Fernet(other_key.encode()).encrypt(b"x"), other_key))
    assert response.status_code == 403
    assert config_store.load_config()["apps"]["radarr"]["api_key"] == "radarr-secret-key"

    auth_store.set_credentials("olduser", PASSWORD)
    own = self_backup.build()
    os.remove(auth_store.AUTH_PATH)
    assert client.post("/api/setup/restore", json=restore_upload(own, key)).status_code == 200
    assert auth_store.verify_password("olduser", PASSWORD)
