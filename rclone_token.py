"""Parsing for the token `rclone authorize <backend>` prints, shared by the
destinations that connect by pasting it (OneDrive, Dropbox)."""
import base64
import binascii
import json

_PASTE_START = "Paste the following into your remote machine"


def _terminal_block(text):
    """The token between rclone's "--->" and "<---End paste" markers, or
    None. Plain string scans, so it stays linear on any paste."""
    start = text.find(_PASTE_START)
    if start < 0:
        return None
    rest = text[start + len(_PASTE_START):].lstrip()
    if not rest.startswith("--->"):
        return None
    token, found, tail = rest[len("--->"):].partition("<---")
    return token.strip() if found and tail.lstrip().startswith("End paste") else None


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

    block = _terminal_block(text)
    if block is not None:
        text = block

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
