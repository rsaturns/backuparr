"""Parsing for the token `rclone authorize <backend>` prints, shared by the
destinations that connect by pasting it (OneDrive, Dropbox)."""
import base64
import binascii
import json
import re

_PASTE_MARKERS = re.compile(
    r"Paste the following into your remote machine\s*--->\s*(.*?)\s*<---\s*End paste",
    re.DOTALL,
)


class TokenError(ValueError):
    pass


def parse_token_blob(pasted, backend):
    """Accepts the full terminal block, just the base64 line, or raw
    token JSON. Returns (token_json, access_token); token_json is a
    compact single-line re-serialization, safe as an INI value."""
    command = f"rclone authorize {backend}"
    text = (pasted or "").strip()
    if not text:
        raise TokenError(f"Paste the token `{command}` printed first.")

    bad_token = TokenError(f"That doesn't look like a valid rclone token - paste the exact output of `{command}`.")

    match = _PASTE_MARKERS.search(text)
    if match:
        text = match.group(1).strip()

    candidate = text
    if not text.lstrip().startswith("{"):
        try:
            candidate = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise bad_token from exc

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise bad_token from exc

    if not isinstance(data, dict) or not data.get("access_token") or not data.get("refresh_token"):
        raise bad_token

    return json.dumps(data, separators=(",", ":")), data["access_token"]
