"""Google Drive as a Backuparr destination, connected via an OAuth
"Connect" button (see webui/app.py's /api/destinations/gdrive/* routes)
instead of rclone's interactive config wizard. Scoped to drive.file -
only files/folders this app created or the user picked via the Google
Picker widget, not the whole Drive.

Keeps a single rclone remote (REMOTE_NAME) in sync with the stored
refresh token, so the rest of the codebase just sees an ordinary rclone
remote. rclone.conf is encrypted (see entrypoint.sh), so it's written via
rclone_util's `rclone config` wrappers, not configparser directly.
"""
import json

import requests

import rclone_util

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"
REMOTE_NAME = "backuparr-gdrive"


class GDriveOAuthError(RuntimeError):
    pass


def build_auth_url(client_id, redirect_uri, state):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Forces Google to re-issue a refresh token even if this client_id
        # already granted one before (e.g. reconnecting after a disconnect).
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
    }, timeout=15)
    if res.status_code != 200:
        raise GDriveOAuthError(f"token exchange failed: {res.text}")
    data = res.json()
    if "refresh_token" not in data:
        raise GDriveOAuthError(
            "Google didn't return a refresh token - this usually means the app was already "
            "authorized without one. Revoke Backuparr's access at "
            "https://myaccount.google.com/permissions and try connecting again."
        )
    return data


def refresh_access_token(client_id, client_secret, refresh_token):
    res = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    if res.status_code != 200:
        raise GDriveOAuthError(f"token refresh failed: {res.text}")
    return res.json()


def get_access_token(dest_cfg):
    """Returns a short-lived access token for client-side use (the Google
    Picker widget) - the refresh token itself never leaves the server."""
    if not dest_cfg.get("refresh_token"):
        raise GDriveOAuthError("Google Drive is not connected")
    data = refresh_access_token(dest_cfg["client_id"], dest_cfg["client_secret"], dest_cfg["refresh_token"])
    return data["access_token"]


def remote_root(dest_cfg):
    """The rclone remote root for this destination. The target folder is
    set via the remote's own root_folder_id, not path syntax."""
    if not dest_cfg.get("refresh_token"):
        raise GDriveOAuthError("Google Drive is not connected - go to Settings and click Connect.")
    return f"{REMOTE_NAME}:"


def sync_rclone_remote(dest_cfg):
    """Writes (or removes) the REMOTE_NAME remote in rclone.conf to match
    destinations.gdrive. Always force=True (full rewrite) - safe since,
    unlike OneDrive, Google's refresh tokens don't rotate on use."""
    if not dest_cfg.get("refresh_token"):
        rclone_util.config_delete(REMOTE_NAME)
        return

    # access_token must be non-empty or rclone discards the token as
    # invalid entirely - dated already-expired so rclone refreshes it.
    token_json = json.dumps({
        "access_token": "placeholder",
        "token_type": "Bearer",
        "refresh_token": dest_cfg["refresh_token"],
        "expiry": "1970-01-01T00:00:00Z",
    })
    fields = {
        "client_id": dest_cfg.get("client_id", ""),
        "client_secret": dest_cfg.get("client_secret", ""),
        "scope": "drive.file",
        "token": token_json,
    }
    if dest_cfg.get("folder_id"):
        fields["root_folder_id"] = dest_cfg["folder_id"]
    rclone_util.config_set(REMOTE_NAME, "drive", fields, force=True)
