"""The browsers signed in to the web UI, kept on the server.

Flask's session is a signed cookie and nothing more: signing out only told
that browser to forget it, so a copy of the cookie - read off plain HTTP,
say - kept working until it expired, up to 30 days after its last use. Now
each sign-in gets a random id, carried in the session and listed here, and
a session whose id isn't listed is signed out. Signing out takes this
browser's id off the list; a password change, a restore of Backuparr
and a reset end every session.

The file holds a SHA-256 of each id, never the id itself, with when it was
last used (updated at most once a day, so a request doesn't write a file).
An entry unused for longer than a session lasts is dropped."""
import hashlib
import json
import os
import secrets
import threading
import time

import atomic_file

SESSIONS_PATH = os.environ.get("BACKUPARR_SESSIONS", "/config/backuparr/sessions.json")
LIFETIME = 30 * 24 * 3600  # the session cookie's own (webui/app.py PERMANENT_SESSION_LIFETIME)
TOUCH_EVERY = 24 * 3600

_lock = threading.Lock()
_sessions = None  # sha256 of an id -> when it was last used (time.time())


def _key(sid):
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def _load():
    global _sessions
    if _sessions is None:
        try:
            with open(SESSIONS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            stored = data.get("sessions") if isinstance(data, dict) else None
            _sessions = {k: v for k, v in (stored or {}).items()
                         if isinstance(k, str) and isinstance(v, (int, float))}
        except (OSError, ValueError):
            _sessions = {}  # none readable: everyone signs in again
    return _sessions


def _save():
    atomic_file.write_private(SESSIONS_PATH, json.dumps({"sessions": _sessions}))


def _prune(now):
    for key in [k for k, seen in _sessions.items() if now - seen > LIFETIME + TOUCH_EVERY]:
        del _sessions[key]


def start():
    """A new session's id, listed."""
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _load()
        _prune(now)
        _sessions[_key(sid)] = now
        _save()
    return sid


def valid(sid):
    """Whether a session's id is listed - noting that it was used."""
    if not isinstance(sid, str) or not sid:
        return False
    now = time.time()
    with _lock:
        sessions = _load()
        key = _key(sid)
        seen = sessions.get(key)
        if seen is None or now - seen > LIFETIME + TOUCH_EVERY:
            return False
        if now - seen > TOUCH_EVERY:
            sessions[key] = now
            _save()
    return True


def end(sid):
    """Signs one session out on the server."""
    if not isinstance(sid, str) or not sid:
        return
    with _lock:
        if _load().pop(_key(sid), None) is not None:
            _save()


def end_all():
    """Signs every session out on the server."""
    with _lock:
        _load().clear()
        _save()


def forget():
    """Reset deleted the file: start again from it, not from memory."""
    global _sessions
    with _lock:
        _sessions = None
