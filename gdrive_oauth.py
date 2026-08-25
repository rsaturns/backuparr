"""Google Drive as a Backuparr destination, connected via a normal OAuth
"Connect" button instead of the rclone CLI's interactive config wizard.

The web UI drives the standard OAuth2 authorization-code flow (see
webui/app.py's /api/destinations/gdrive/* routes): the user creates their
own OAuth client in Google Cloud Console (unavoidable - every app talking to
Google APIs needs one), pastes the client ID/secret into Settings, then
clicks through Google's consent screen scoped to drive.file - narrower than
the full `drive` scope most rclone Google Drive setups end up using, since
it only ever grants access to files/folders this app created or the user
explicitly picked via the Google Picker widget.

Once connected, this module keeps a single rclone remote (REMOTE_NAME) in
sync with the stored refresh token, so backup.py/restore_actions.py/
rclone_util.py don't need to know OAuth happened at all - they just see an
ordinary rclone remote, the same way a hand-configured one would look.

rclone.conf itself is encrypted (see entrypoint.sh) via rclone's own
`rclone config encryption`, which means it can no longer be read/written
directly with Python's configparser - only the rclone binary itself, given
the RCLONE_CONFIG_PASS it was started with, can get at it. sync_rclone_remote
below goes through rclone_util's `rclone config create/update/delete`
wrappers instead.
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
    """The rclone remote root to pass to rclone_util for this destination.
    Which Drive folder that resolves to is set via the remote's own
    root_folder_id config (written by sync_rclone_remote), not path syntax -
    rclone has no `remote:{id}` shorthand for scoping to a folder by ID."""
    if not dest_cfg.get("refresh_token"):
        raise GDriveOAuthError("Google Drive is not connected - go to Settings and click Connect.")
    return f"{REMOTE_NAME}:"


def sync_rclone_remote(dest_cfg):
    """Writes (or removes) the REMOTE_NAME remote in rclone.conf to match
    the current destinations.gdrive config. Called before any rclone
    operation that might touch this destination, and right after connect/
    disconnect, so the file on disk never drifts from config.json.

    Always force=True (a full rewrite via `rclone config create`, not an
    incremental `update`) - safe here because Google's refresh tokens don't
    rotate on use the way Microsoft's OneDrive ones do (contrast with
    onedrive_oauth.sync_rclone_remote), so config.json's copy is always the
    same value already in rclone.conf, never staler than it."""
    if not dest_cfg.get("refresh_token"):
        rclone_util.config_delete(REMOTE_NAME)
        return

    # rclone refreshes this itself using client_id/client_secret once it
    # expires - we don't need to keep it current from our side, just seed it
    # with a token shaped the way rclone expects, dated already expired so
    # it refreshes on first use. access_token must be non-empty (verified
    # against a real rclone binary) - an empty string makes rclone discard
    # the whole token as invalid instead of just treating it as expired, and
    # it then reports "no refresh token" even though one's right there.
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
