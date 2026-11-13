"""Reverse-proxy credentials, limited to chosen apps and each app's own origin."""
from urllib.parse import urlsplit
import re

import requests


class RequestHeadersError(RuntimeError):
    """Safe to display: messages never contain header values or target URLs."""


_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_TRANSPORT_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "trailer",
    "te", "upgrade", "proxy-connection", "expect",
}


def validate_request_headers(rows, app_ids=None):
    """Validate the settings/API shape and return a case-preserving mapping.

    With app_ids, a row may carry "apps" (a non-empty list of those ids) to
    limit where it is sent; rows without it go to every app. A name may repeat
    only on rows that never reach the same app. Without app_ids, rows are one
    app's already-scoped headers.
    """
    if not isinstance(rows, list) or len(rows) > 32:
        raise RequestHeadersError("Request headers must be a list of at most 32 name/value pairs")
    result, seen = {}, {}
    total = 0
    for index, row in enumerate(rows, 1):
        allowed = {"name", "value", "apps"} if app_ids is not None else {"name", "value"}
        if not isinstance(row, dict) or not {"name", "value"} <= set(row) <= allowed:
            raise RequestHeadersError(f"Request header {index} must have a name and value")
        name, value = row["name"], row["value"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise RequestHeadersError(f"Request header {index} has an invalid name")
        if name.lower() in _TRANSPORT_HEADERS:
            raise RequestHeadersError(f"Request header {index} controls HTTP transport and cannot be customized")
        if (not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 or ord(c) > 255 for c in value)
                or value != value.strip()):
            raise RequestHeadersError(f"Request header {index} has an invalid value; use a single line without surrounding whitespace")
        scope = [None] if app_ids is None else list(app_ids)
        if "apps" in row:
            apps = row["apps"]
            if (not isinstance(apps, list) or not apps or len(set(apps)) != len(apps)
                    or not all(isinstance(app, str) and app in app_ids for app in apps)):
                raise RequestHeadersError(f"Request header {index} must apply to at least one known app")
            scope = apps
        if any(name.lower() in seen.get(app, ()) for app in scope):
            raise RequestHeadersError(
                f"Request header {index} duplicates another header for the same app (names are case insensitive)"
            )
        total += len(name) + len(value)
        if total > 16384:
            raise RequestHeadersError("Request headers exceed the 16 KiB limit")
        for app in scope:
            seen.setdefault(app, set()).add(name.lower())
        result[name] = value
    return result


def headers_for_app(rows, name):
    """The name/value rows that apply to one app, without their scope."""
    return [{"name": row["name"], "value": row["value"]} for row in rows if name in row.get("apps", [name])]


def _origin(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError
        return parts.scheme, parts.hostname.lower(), parts.port or {"http": 80, "https": 443}[parts.scheme]
    except (ValueError, TypeError):
        raise RequestHeadersError("Use a valid HTTP or HTTPS app URL for request headers") from None


def _merge_cookies(current, defaults):
    """Fill missing cookie names, preserving app/server values and their encoding."""
    if not current:
        return defaults
    # Cookie pairs are separated by semicolons; names are case sensitive.
    names = {part.partition("=")[0].strip() for part in current.split(";")}
    missing = [part.strip() for part in defaults.split(";")
               if part.strip() and part.partition("=")[0].strip() not in names]
    return "; ".join([current, *missing])


class _HeadersAdapter(requests.adapters.BaseAdapter):
    """Wrap the existing transport, preserving its TLS, proxy and retry setup.

    Every redirect passes through send again. Credentials are added here, after
    requests prepares app auth and rebuilds proxy auth, and never sent off-origin.
    """
    def __init__(self, adapter, origin, headers):
        self.adapter, self.origin, self.headers = adapter, origin, dict(headers)

    def send(self, request, **kwargs):
        if _origin(request.url) != self.origin:
            raise RequestHeadersError(
                "Request blocked to protect global authentication headers: the app redirected to a different "
                "scheme, host or port. Use its final URL and check reverse-proxy authentication."
            )
        for name, value in self.headers.items():
            # Native API keys, Bearer/Basic auth and per-request fields take priority.
            if name.lower() == "cookie":
                # A routing/session cookie must not hide an unrelated auth cookie.
                request.headers[name] = _merge_cookies(request.headers.get(name), value)
            else:
                request.headers.setdefault(name, value)
        return self.adapter.send(request, **kwargs)

    def close(self):
        self.adapter.close()


def configure_app_headers(app, rows):
    headers = validate_request_headers(rows)
    if headers:
        origin = _origin(requests.Request("GET", app.url).prepare().url)
        for prefix, adapter in list(app.session.adapters.items()):
            app.session.mount(prefix, _HeadersAdapter(adapter, origin, headers))
        # A driver that also talks over something other than its session
        # (Home Assistant's WebSocket) sends them there too.
        if hasattr(app, "set_request_headers"):
            app.set_request_headers(headers)
    return app


# A reverse proxy refuses a request without the headers it wants with 401 or
# 403 - just as the app refuses a wrong API key - so the app's own "check
# the API key" alone can point the wrong way.
REFUSAL_HINT = ", or the Service Authentication headers if a proxy in front needs them"


def note_refusals(app):
    """Notes on the app when a request it sent was answered 401 or 403."""
    session = getattr(app, "session", None)
    if isinstance(session, requests.Session):
        def note(response, *args, **kwargs):
            if response.status_code in (401, 403):
                app.refused_by_server = True
        session.hooks["response"].append(note)
    return app


def refusal_hint(app, exc, rows):
    """REFUSAL_HINT for a failure after a 401/403 when headers are set up -
    not for a Servarr web UI asking for the app's own login (needs_login)."""
    if rows and getattr(app, "refused_by_server", False) and not getattr(exc, "needs_login", False):
        return REFUSAL_HINT
    return ""


def app_connection_config(cfg, name):
    """Runtime-only snapshot; header secrets are never copied into app settings."""
    return {**cfg["apps"][name], "request_headers": headers_for_app(cfg.get("request_headers", []), name)}
