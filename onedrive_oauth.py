"""OneDrive as a Backuparr destination, connected via a normal OAuth
"Connect" button instead of the rclone CLI's interactive config wizard -
same approach as gdrive_oauth.py.

Scoped to personal Microsoft accounts only (the /consumers/ tenant
endpoint, rather than /common/ or /organizations/) - this project has no
interest in the extra complexity of work/school (Microsoft 365) accounts,
so the OAuth flow itself refuses those at sign-in instead of just
documenting "personal accounts only" and hoping.

Uses the Files.ReadWrite.AppFolder scope, which - unlike Google Drive -
means there's no folder to pick: Microsoft Graph gives every app its own
single, dedicated special folder (/Apps/Backuparr in the user's OneDrive),
created automatically the first time it's touched. That removes an entire
picker widget's worth of frontend work compared to Google Drive; the
tradeoff is the destination folder isn't user-choosable.
"""
import configparser
import json
import os
import tempfile

import requests

AUTH_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPE = "Files.ReadWrite.AppFolder offline_access"
REMOTE_NAME = "backuparr-onedrive"

RCLONE_CONFIG_PATH = os.environ.get("RCLONE_CONFIG", "/config/backuparr/rclone.conf")


class OneDriveOAuthError(RuntimeError):
    pass


def build_auth_url(client_id, redirect_uri, state):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": SCOPE,
        # Forces Microsoft to re-prompt for consent even if this client_id
        # already granted access before (e.g. reconnecting after a
        # disconnect) - mirrors gdrive_oauth's use of Google's own
        # equivalent "prompt=consent".
        "prompt": "consent",
        "state": state,
    }
    query = "&".join(f"{k}={requests.utils.quote(v, safe='')}" for k, v in params.items())
    return f"{AUTH_URL}?{query}"


def exchange_code(client_id, client_secret, redirect_uri, code):
    res = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
        "grant_type": "authorization_code",
        "scope": SCOPE,
    }, timeout=15)
    if res.status_code != 200:
        raise OneDriveOAuthError(f"token exchange failed: {res.text}")
    data = res.json()
    if "refresh_token" not in data:
        raise OneDriveOAuthError(
            "Microsoft didn't return a refresh token - this usually means the app was already "
            "authorized without one. Remove Backuparr's access at "
            "https://account.live.com/consent/Manage and try connecting again."
        )
    return data


def refresh_access_token(client_id, client_secret, refresh_token):
    res = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": SCOPE,
    }, timeout=15)
    if res.status_code != 200:
        raise OneDriveOAuthError(f"token refresh failed: {res.text}")
    return res.json()


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
    """The rclone remote root to pass to rclone_util for this destination.
    Which folder that resolves to is set via the remote's own
    root_folder_id/drive_id/drive_type config (written by
    sync_rclone_remote), populated once at connect time from
    approot_metadata()."""
    if not dest_cfg.get("refresh_token"):
        raise OneDriveOAuthError("OneDrive is not connected - go to Settings and click Connect.")
    return f"{REMOTE_NAME}:"


def sync_rclone_remote(dest_cfg):
    """Writes (or removes) the REMOTE_NAME section of rclone.conf to match
    the current destinations.onedrive config. Called before any rclone
    operation that might touch this destination, and right after connect/
    disconnect, so the file on disk never drifts from config.json."""
    parser = configparser.ConfigParser()
    if os.path.exists(RCLONE_CONFIG_PATH):
        parser.read(RCLONE_CONFIG_PATH)

    if dest_cfg.get("refresh_token"):
        if not parser.has_section(REMOTE_NAME):
            parser.add_section(REMOTE_NAME)
        parser.set(REMOTE_NAME, "type", "onedrive")
        parser.set(REMOTE_NAME, "client_id", dest_cfg.get("client_id", ""))
        parser.set(REMOTE_NAME, "client_secret", dest_cfg.get("client_secret", ""))
        parser.set(REMOTE_NAME, "drive_id", dest_cfg.get("drive_id", ""))
        parser.set(REMOTE_NAME, "drive_type", dest_cfg.get("drive_type", "personal"))
        parser.set(REMOTE_NAME, "root_folder_id", dest_cfg.get("item_id", ""))
        # Same expired-placeholder-token trick as gdrive_oauth.sync_rclone_remote -
        # rclone refreshes it itself using client_id/client_secret/refresh_token
        # once it notices the expiry is in the past, and access_token must be
        # non-empty or rclone discards the whole token as invalid.
        token_json = json.dumps({
            "access_token": "placeholder",
            "token_type": "Bearer",
            "refresh_token": dest_cfg["refresh_token"],
            "expiry": "1970-01-01T00:00:00Z",
        })
        parser.set(REMOTE_NAME, "token", token_json)
    elif parser.has_section(REMOTE_NAME):
        parser.remove_section(REMOTE_NAME)

    config_dir = os.path.dirname(RCLONE_CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)
    # Uniquely-named tmp file (not a fixed "<path>.tmp") - see
    # gdrive_oauth.sync_rclone_remote for the concurrent-write collision this
    # avoids (reproduced under load: FileNotFoundError on a shared tmp path).
    fd, tmp_path = tempfile.mkstemp(dir=config_dir, prefix=".rclone.conf.")
    try:
        with os.fdopen(fd, "w") as f:
            parser.write(f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, RCLONE_CONFIG_PATH)
    except BaseException:
        os.remove(tmp_path)
        raise
