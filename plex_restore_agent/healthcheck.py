import os
from pathlib import Path
import urllib.request


def main():
    token = Path(os.environ.get("AGENT_TOKEN_FILE", "/run/secrets/plex_restore_token")).read_text().strip()
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if ":" in host:
        host = "[" + host + "]"
    port = int(os.environ.get("AGENT_PORT", "8991"))
    request = urllib.request.Request(f"http://{host}:{port}/v1/health", headers={"Authorization": "Bearer " + token})
    # Local health probes must not go through a configured outbound proxy.
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5).close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(1)
