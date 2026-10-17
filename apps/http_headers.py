"""Global reverse-proxy credentials, scoped to each configured app origin."""
from urllib.parse import urlsplit
import re

import requests


class RequestHeadersError(RuntimeError):
    """Safe to display: messages never contain header values or target URLs."""


_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_TRANSPORT_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "trailer",
    "te", "upgrade", "proxy-connection", "expect",
}


def validate_request_headers(rows):
    """Validate the settings/API shape and return a case-preserving mapping."""
    if not isinstance(rows, list) or len(rows) > 32:
        raise RequestHeadersError("Request headers must be a list of at most 32 name/value pairs")
    result, seen = {}, set()
    total = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != {"name", "value"}:
            raise RequestHeadersError(f"Request header {index} must have a name and value")
        name, value = row["name"], row["value"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise RequestHeadersError(f"Request header {index} has an invalid name")
        if name.lower() in _TRANSPORT_HEADERS:
            raise RequestHeadersError(f"Request header {index} controls HTTP transport and cannot be customized")
        if name.lower() in seen:
            raise RequestHeadersError(f"Request header {index} duplicates another header (names are case insensitive)")
        if (not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 or ord(c) > 255 for c in value)
                or value != value.strip()):
            raise RequestHeadersError(f"Request header {index} has an invalid value; use a single line without surrounding whitespace")
        total += len(name) + len(value)
        if total > 16384:
            raise RequestHeadersError("Request headers exceed the 16 KiB limit")
        seen.add(name.lower())
        result[name] = value
    return result


def _origin(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError
        return parts.scheme, parts.hostname.lower(), parts.port or {"http": 80, "https": 443}[parts.scheme]
    except (ValueError, TypeError):
        raise RequestHeadersError("Use a valid HTTP or HTTPS app URL for request headers") from None


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
    return app


def app_connection_config(cfg, name):
    """Runtime-only snapshot; global secrets are never copied into app settings."""
    return {**cfg["apps"][name], "request_headers": [dict(row) for row in cfg.get("request_headers", [])]}
