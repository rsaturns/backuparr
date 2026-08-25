"""OneDrive as a Backuparr destination, connected by pasting the token
`rclone authorize onedrive` prints - not our own OAuth flow.

The original design mirrored gdrive_oauth.py (Backuparr hosts its own
"Connect" button and OAuth client), but that requires the user to register
their own Azure app, and Microsoft has required every new app registration
to live in a directory since June 2024 - personal Microsoft accounts don't
have one by default, and not everyone can get one (the free Microsoft 365
Developer Program has its own eligibility checks).

rclone sidesteps this entirely: its onedrive backend ships with its own
built-in Microsoft app (client_id/client_secret "leave blank normally" per
`rclone help backend onedrive`), registered under rclone's own directory
long before this restriction existed. `rclone authorize onedrive` drives
that app's consent flow and prints a token blob - the user runs this once
on any machine with a browser (their own laptop, no Azure account needed,
does not have to be wherever Backuparr itself runs, since the redirect
Microsoft sends the browser back to is a local rclone webserver that only
makes sense on that same machine) and pastes the result here.

Scope is still personal-only in practice: rclone's built-in app is
registered for "Personal Microsoft accounts only", same restriction our
own app would have had.

rclone.conf itself is encrypted (see entrypoint.sh) via rclone's own
`rclone config encryption`, which means it can no longer be read/written
directly with Python's configparser - only the rclone binary itself, given
the RCLONE_CONFIG_PASS it was started with, can get at it. sync_rclone_remote
below goes through rclone_util's `rclone config create/update/delete`
wrappers instead.
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
    """Accepts whatever the user pastes from `rclone authorize onedrive` -
    the whole terminal block (markers included), just the base64 line
    between them, or even an already-decoded token JSON (e.g. copied out of
    an existing rclone.conf) - and returns (token_json, access_token).
    token_json is a compact, single-line re-serialization, safe to store as
    an INI value in rclone.conf."""
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
    """The rclone remote root to pass to rclone_util for this destination.
    Which folder that resolves to is set via the remote's own
    root_folder_id/drive_id/drive_type config (written by
    sync_rclone_remote), populated once at connect time from
    approot_metadata()."""
    if not dest_cfg.get("token"):
        raise OneDriveOAuthError("OneDrive is not connected - go to Settings and paste a token from `rclone authorize onedrive`.")
    return f"{REMOTE_NAME}:"


def sync_rclone_remote(dest_cfg, force=False):
    """Writes (or removes) the REMOTE_NAME remote in rclone.conf to match
    the current destinations.onedrive config.

    Deliberately does NOT overwrite an already-present remote's token on
    every call the way gdrive_oauth.sync_rclone_remote does, unless
    force=True (only passed by the connect route, right after a fresh
    paste) or the remote doesn't exist yet. Microsoft rotates OneDrive
    refresh tokens on every use, and rclone rewrites its own rotated token
    back into this file as it goes - blindly replacing it from our
    (comparatively stale) config.json copy on every routine sync() would
    eventually clobber a valid token with an invalidated one. Handled by
    rclone_util.config_set's create-vs-update choice: `create` (a full
    rewrite, so the token must be included) on a fresh/forced write,
    `update` (only touches the given keys, token left alone) otherwise."""
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
