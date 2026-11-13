"""OneDrive as a Backuparr destination, connected by pasting the token
`rclone authorize onedrive` prints - not our own OAuth flow. Avoids
requiring an Azure app registration (Microsoft has required new
registrations to live in a directory since June 2024, which most
personal accounts don't have); rclone's own built-in Microsoft app
predates that requirement. Personal Microsoft accounts only.
"""
import requests

import rclone_util

REMOTE_NAME = "backuparr-onedrive"
RCLONE_TYPE = "onedrive"
TOKEN_FIELD = "token"


class OneDriveOAuthError(RuntimeError):
    pass


def approot_metadata(access_token):
    """The app's dedicated special folder - created on first access if it
    doesn't exist yet. Its id and parentReference give us everything
    rclone's onedrive backend needs to address it directly (drive_id,
    drive_type, root_folder_id), in a single call."""
    res = requests.get(
        "https://graph.microsoft.com/v1.0/me/drive/special/approot",
        params={"$select": "id,name,parentReference"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if res.status_code != 200:
        raise OneDriveOAuthError(f"could not read the app folder: {res.text}")
    return res.json()


def remote_root(dest_cfg):
    """The rclone remote root for this destination. Target folder is set
    via drive_id/drive_type/root_folder_id, populated at connect time."""
    if not dest_cfg.get("token"):
        raise OneDriveOAuthError("OneDrive is not connected - go to Settings and paste a token from `rclone authorize onedrive`.")
    return f"{REMOTE_NAME}:"


def backup_target(dest_cfg):
    return "your OneDrive app folder"


def sync_rclone_remote(dest_cfg, force=False, remotes=None):
    """Writes (or removes) the REMOTE_NAME remote in rclone.conf to match
    destinations.onedrive. Unlike Google Drive, does NOT overwrite an
    existing token unless force=True or the remote doesn't exist yet -
    Microsoft rotates OneDrive refresh tokens on use, and rclone rewrites
    its own rotated token back into this file, so blindly replacing it
    from our (stale) config.json copy would clobber a valid token.
    `remotes` is a config_dump() the caller already has; nothing is
    written when the remote already matches."""
    remote = rclone_util.connected_remote(REMOTE_NAME, dest_cfg.get("token"), remotes)
    if not dest_cfg.get("token"):
        return

    fields = {
        "drive_id": dest_cfg.get("drive_id", ""),
        "drive_type": dest_cfg.get("drive_type", "personal"),
        "root_folder_id": dest_cfg.get("item_id", ""),
    }
    if not force and remote is not None and all(remote.get(k, "") == v for k, v in fields.items()):
        return
    if force or remote is None:
        fields["token"] = dest_cfg["token"]
    rclone_util.config_set(REMOTE_NAME, RCLONE_TYPE, fields, force=force)
