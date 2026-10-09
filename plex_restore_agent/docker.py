"""Small Docker Engine client. Never shells into or executes inside Plex."""
import http.client
import json
import re
import socket
from urllib.parse import quote


class AgentError(RuntimeError):
    pass


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    def __init__(self, socket_path="/var/run/docker.sock"):
        self.socket_path = socket_path
        self.api = ""

    def request(self, method, path, data=None, *, timeout=90):
        connection = UnixConnection(self.socket_path, timeout)
        try:
            connection.request(method, path, body=json.dumps(data) if data is not None else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read(4 * 1024 * 1024)
            if response.status not in (200, 201, 204, 304):
                raise AgentError(f"Docker API request failed (HTTP {response.status}); check the agent's container/socket configuration")
            return json.loads(body) if body else {}
        except (OSError, http.client.HTTPException, ValueError):
            raise AgentError("Could not communicate with the local Docker Engine") from None
        finally:
            connection.close()

    def inspect(self, container):
        if not self.api:
            version = self.request("GET", "/version").get("ApiVersion", "")
            if not re.fullmatch(r"1\.[0-9]+", version):
                raise AgentError("Unsupported Docker Engine API version")
            self.api = "/v" + version
        return self.request("GET", f"{self.api}/containers/{quote(container, safe='')}/json")

    def stop(self, container):
        self.request("POST", f"{self.api}/containers/{container}/stop?t=60")
        self.assert_stopped(container)

    def assert_stopped(self, container):
        state = self.inspect(container)["State"]
        if state.get("Running") or state.get("Restarting") or state.get("Paused"):
            raise AgentError("Plex is still running; database files were not changed")

    def start(self, container):
        self.request("POST", f"{self.api}/containers/{container}/start")

    def restart_policy(self, container, policy):
        self.request("POST", f"{self.api}/containers/{container}/update", {"RestartPolicy": policy})
