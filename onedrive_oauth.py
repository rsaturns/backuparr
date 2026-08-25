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
"""
import base64
import binascii
import configparser
import json
import os
import re
import tempfile

import requests

REMOTE_NAME = "backuparr-onedrive"

RCLONE_CONFIG_PATH = os.environ.get("RCLONE_CONFIG", "/config/backuparr/rclone.conf")

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
    """Writes (or removes) the REMOTE_NAME section of rclone.conf to match
    the current destinations.onedrive config.

    Deliberately does NOT overwrite an already-present section's token on
    every call the way gdrive_oauth.sync_rclone_remote does, unless
    force=True (only passed by the connect route, right after a fresh
    paste). Microsoft rotates OneDrive refresh tokens on every use - rclone
    refreshes and rewrites its own token back into this same file as it
    goes, so blindly replacing it here from our own (comparatively stale)
    config.json copy on every routine sync() call would eventually clobber
    a valid, already-rotated token with an invalidated one. config.json's
    copy is only ever needed as the initial seed."""
    parser = configparser.ConfigParser()
    if os.path.exists(RCLONE_CONFIG_PATH):
        parser.read(RCLONE_CONFIG_PATH)

    if dest_cfg.get("token"):
        section_exists = parser.has_section(REMOTE_NAME)
        if not section_exists:
            parser.add_section(REMOTE_NAME)
        if force or not section_exists:
            parser.set(REMOTE_NAME, "type", "onedrive")
            parser.set(REMOTE_NAME, "token", dest_cfg["token"])
        parser.set(REMOTE_NAME, "drive_id", dest_cfg.get("drive_id", ""))
        parser.set(REMOTE_NAME, "drive_type", dest_cfg.get("drive_type", "personal"))
        parser.set(REMOTE_NAME, "root_folder_id", dest_cfg.get("item_id", ""))
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
