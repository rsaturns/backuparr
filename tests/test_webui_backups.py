"""The per-destination storage-space endpoint."""


def test_cloud_storage_space_is_unknown_when_rclone_about_fails(authed_client, isolated_webui, monkeypatch):
    cfg = isolated_webui.settings.load_config()
    cfg["destinations"]["s3"].update(
        enabled=True, provider="aws", bucket="my-bucket", access_key_id="AKID", secret_access_key="s3-secret")
    isolated_webui.settings.save_config(cfg)

    called = []

    def reject(root):
        called.append(root)
        raise isolated_webui.backups.rclone_util.RcloneError(
            "rclone about backuparr-s3:my-bucket/Backuparr failed")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "about", reject)
    response = authed_client.get("/api/storage/s3/space")
    assert response.status_code == 200
    assert response.get_json() == {"total": None, "free": None}
    assert called == ["backuparr-s3:my-bucket/Backuparr"]


def test_a_failed_space_check_is_an_error_for_other_destinations(authed_client, isolated_webui, monkeypatch):
    cfg = isolated_webui.settings.load_config()
    cfg["destinations"]["gdrive"].update(enabled=True, refresh_token="rt")
    isolated_webui.settings.save_config(cfg)

    called = []

    def reject(root):
        called.append(root)
        raise isolated_webui.backups.rclone_util.RcloneError(
            "rclone about backuparr-gdrive: failed: invalid_grant", "invalid_grant (reconnect it in Settings)")

    monkeypatch.setattr(isolated_webui.backups.rclone_util, "about", reject)
    response = authed_client.get("/api/storage/gdrive/space")
    assert response.status_code == 502
    assert response.get_json() == {"error": "invalid_grant (reconnect it in Settings)"}
    assert called == ["backuparr-gdrive:"]
