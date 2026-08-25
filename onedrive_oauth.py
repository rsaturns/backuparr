"""OneDrive as a Backuparr destination, connected by pasting the token
`rclone authorize onedrive` prints - not our own OAuth flow. Avoids
requiring an Azure app registration (Microsoft has required new
registrations to live in a directory since June 2024, which most
personal accounts don't have); rclone's own built-in Microsoft app
predates that requirement. Personal Microsoft accounts only.

rclone.conf is encrypted (see entrypoint.sh), so it's written via
rclone_util's `rclone config` wrappers, not configparser directly.
"""
import base64
import binascii
import json
import re

import requests

import rclone_util

REMOTE_NAME = "backuparr-onedrive"

_PASTE_MARKERS = re.compile(
    r"Paste the following into your remote machine\s*--->\s*(.*?)\s*<---\s*End paste",
    re.DOTALL,
)

_BAD_TOKEN_MESSAGE = (
    "That doesn't look like a valid rclone token - paste the exact output of "
    "`rclone authorize onedrive`."
)


class OneDriveOAuthError(RuntimeError):
    pass


def parse_token_blob(pasted):
    """Accepts the full terminal block, just the base64 line, or raw
    token JSON. Returns (token_json, access_token); token_json is a
    compact single-line re-serialization, safe as an INI value."""
    text = (pasted or "").strip()
    if not text:
        raise OneDriveOAuthError("Paste the token `rclone authorize onedrive` printed first.")

    match = _PASTE_MARKERS.search(text)
    if match:
        text = match.group(1).strip()

    candidate = text
    if not text.lstrip().startswith("{"):
        try:
            candidate = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise OneDriveOAuthError(_BAD_TOKEN_MESSAGE) from exc

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise OneDriveOAuthError(_BAD_TOKEN_MESSAGE) from exc

    if not isinstance(data, dict) or not data.get("access_token") or not data.get("refresh_token"):
        raise OneDriveOAuthError(_BAD_TOKEN_MESSAGE)

    return json.dumps(data, separators=(",", ":")), data["access_token"]


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


def sync_rclone_remote(dest_cfg, force=False):
    """Writes (or removes) the REMOTE_NAME remote in rclone.conf to match
    destinations.onedrive. Unlike Google Drive, does NOT overwrite an
    existing token unless force=True or the remote doesn't exist yet -
    Microsoft rotates OneDrive refresh tokens on use, and rclone rewrites
    its own rotated token back into this file, so blindly replacing it
    from our (stale) config.json copy would clobber a valid token."""
    if not dest_cfg.get("token"):
        rclone_util.config_delete(REMOTE_NAME)
        return

    existing = REMOTE_NAME in rclone_util.config_dump()
    fields = {
        "drive_id": dest_cfg.get("drive_id", ""),
        "drive_type": dest_cfg.get("drive_type", "personal"),
        "root_folder_id": dest_cfg.get("item_id", ""),
    }
    if force or not existing:
        fields["token"] = dest_cfg["token"]
    rclone_util.config_set(REMOTE_NAME, "onedrive", fields, force=force)
