"""Cookie names and the Secure flag, per request (#177).

Backuparr answers on plain HTTP and on HTTPS. A cookie set over HTTPS is
Secure, and a browser won't let a plain-HTTP page overwrite a Secure cookie
of the same name (or even send it back), so signing in over HTTP after
HTTPS would quietly fail. Over HTTPS the cookies are named with the
__Host- prefix, which also pins them to this host and path; over HTTP they
keep their plain names.

Behind a TLS reverse proxy (BACKUPARR_FORCE_HTTPS) every cookie is Secure,
under its plain name, as before."""
import os

from flask import request

BEHIND_HTTPS_PROXY = os.environ.get("BACKUPARR_FORCE_HTTPS", "").lower() in ("1", "true", "yes")
HTTPS_PREFIX = "__Host-"


def _direct_https():
    return request.is_secure and not BEHIND_HTTPS_PROXY


def secure():
    """Whether this response's cookies are Secure."""
    return BEHIND_HTTPS_PROXY or request.is_secure


def name(base):
    """The cookie's name for this request."""
    return f"{HTTPS_PREFIX}{base}" if _direct_https() else base
