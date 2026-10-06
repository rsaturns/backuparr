"""Check the web server at its configured bind address without an HTTP proxy."""
import http.client
import os


def check_health():
    host = os.environ.get("WEBUI_HOST") or "0.0.0.0"
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    connection = None
    try:
        port = int(os.environ.get("WEBUI_PORT") or "8990")
        connection = http.client.HTTPConnection(host, port, timeout=3)
        connection.request("GET", "/login")
        # First boot redirects to /setup; disabled local auth redirects to /.
        # No redirect needs following to confirm this local server is serving.
        return connection.getresponse().status in (200, 302)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(0 if check_health() else 1)
