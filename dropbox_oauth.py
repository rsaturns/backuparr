"""Dropbox as a Backuparr destination, connected by pasting the token
`rclone authorize dropbox` prints - the same flow as OneDrive. rclone's own
built-in Dropbox app is used, so there's no Dropbox app registration to set
up; the trade-off is that it's shared by every rclone user.
"""
import requests

import rclone_util

REMOTE_NAME = "backuparr-dropbox"
RCLONE_TYPE = "dropbox"
TOKEN_FIELD = "token"

# Backups go in this folder instead of the Dropbox root, which the built-in
# app may be allowed to see in full.
BACKUP_FOLDER = "Backuparr"


class DropboxOAuthError(RuntimeError):
    pass


def verify_access_token(access_token):
    """Confirms Dropbox accepts the freshly authorized token before it's
    saved, so a bad paste fails at Connect instead of at the first backup."""
    res = requests.post(
        "https://api.dropboxapi.com/2/users/get_current_account",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if res.status_code != 200:
        try:
            reason = res.json().get("error_summary") or res.text
        except ValueError:
            reason = res.text
        raise DropboxOAuthError(f"Dropbox rejected that token ({reason[:200].rstrip('/')}) - run `rclone authorize dropbox` again.")


def remote_root(dest_cfg):
    """The rclone remote root for this destination."""
    if not dest_cfg.get("token"):
        raise DropboxOAuthError("Dropbox is not connected - go to Settings and paste a token from `rclone authorize dropbox`.")
    return f"{REMOTE_NAME}:{BACKUP_FOLDER}"


def backup_target(dest_cfg):
    return f"the {BACKUP_FOLDER} folder in your Dropbox"


def sync_rclone_remote(dest_cfg, force=False, remotes=None):
    """Writes (or removes) the REMOTE_NAME remote in rclone.conf to match
    destinations.dropbox. Only writes the token when force=True or the
    remote doesn't exist yet: Dropbox access tokens are short-lived and
    rclone rewrites the refreshed one into this file, so replacing it from
    our (older) config.json copy on every sync would throw that away.
    `remotes` is a config_dump() the caller already has."""
    remote = rclone_util.connected_remote(REMOTE_NAME, dest_cfg.get("token"), remotes)
    if not dest_cfg.get("token"):
        return
    if force or remote is None:
        rclone_util.config_set(REMOTE_NAME, RCLONE_TYPE, {"token": dest_cfg["token"]}, force=force)
