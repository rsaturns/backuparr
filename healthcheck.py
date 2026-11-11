"""Check the web server at its configured bind address without an HTTP proxy:
HTTPS always, and plain HTTP unless HTTPS only has closed it (#177)."""
import http.client
import json
import os
import ssl

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _host():
    host = os.environ.get("WEBUI_HOST") or "0.0.0.0"
    return {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)


def https_only():
    """The same answer as webui.serve.https_only(), without importing the app."""
    env = os.environ.get("BACKUPARR_HTTPS_ONLY", "").strip().lower()
    if env in _TRUE:
        return True
    if env in _FALSE:
        return False
    try:
        with open(os.environ.get("BACKUPARR_CONFIG", "/config/backuparr/config.json"), encoding="utf-8") as f:
            return json.load(f).get("https_only") is True
    except (OSError, ValueError, AttributeError):
        return False


def _answers(connection):
    try:
        connection.request("GET", "/login")
        # First boot redirects to /setup; disabled local auth redirects to /.
        # No redirect needs following to confirm this local server is serving.
        return connection.getresponse().status in (200, 302)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def check_health():
    try:
        http_port = int(os.environ.get("WEBUI_PORT") or "8990")
        https_port = int(os.environ.get("WEBUI_HTTPS_PORT") or "8991")
    except ValueError:
        return False
    # Our own self-signed certificate: this only checks something answers.
    context = ssl._create_unverified_context()
    if not _answers(http.client.HTTPSConnection(_host(), https_port, timeout=3, context=context)):
        return False
    return https_only() or _answers(http.client.HTTPConnection(_host(), http_port, timeout=3))


if __name__ == "__main__":
    raise SystemExit(0 if check_health() else 1)
