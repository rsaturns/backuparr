"""X-Forwarded-* headers from a TLS reverse proxy (BACKUPARR_FORCE_HTTPS).

Waitress used to apply these itself (--trusted-proxy); the server since
#177 passes headers through untouched, so this does the same job:

- Without BACKUPARR_FORCE_HTTPS none are used.
- With it, X-Forwarded-Proto and X-Forwarded-Host are used, so links and
  Google Drive's redirect URI come out as the proxy's https:// address.
- X-Forwarded-For (the client's address, which the login lockout counts) is
  used only from the proxy named in BACKUPARR_TRUSTED_PROXY. From anyone
  else it could be forged to dodge the lockout, so then every login counts
  against the proxy's address. With a proxy named, requests from other
  addresses get none of the three.

Each header's last value is the one the nearest proxy added."""
import os


def _last(value):
    return value.split(",")[-1].strip() if value else ""


class ForwardedHeaders:
    def __init__(self, app, enabled=None, trusted_proxy=None):
        self.app = app
        self.enabled = (os.environ.get("BACKUPARR_FORCE_HTTPS", "").lower() in ("1", "true", "yes")
                        if enabled is None else enabled)
        self.trusted_proxy = (os.environ.get("BACKUPARR_TRUSTED_PROXY", "").strip()
                              if trusted_proxy is None else trusted_proxy)

    def __call__(self, environ, start_response):
        use_proto_host, use_for = self._trust(environ.get("REMOTE_ADDR", ""))
        if use_proto_host:
            proto = _last(environ.get("HTTP_X_FORWARDED_PROTO")).lower()
            if proto in ("http", "https"):
                environ["wsgi.url_scheme"] = proto
            host = _last(environ.get("HTTP_X_FORWARDED_HOST"))
            if host:
                environ["HTTP_HOST"] = host
        if use_for:
            client = _last(environ.get("HTTP_X_FORWARDED_FOR"))
            if client:
                environ["REMOTE_ADDR"] = client
        return self.app(environ, start_response)

    def _trust(self, remote):
        """(use Proto and Host, use For) for a request from `remote`."""
        if not self.enabled:
            return False, False
        if not self.trusted_proxy:
            return True, False
        from_proxy = self.trusted_proxy == "*" or remote == self.trusted_proxy
        return from_proxy, from_proxy
