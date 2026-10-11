import os
import socket
import threading

from cheroot.server import HTTPConnection, HTTPRequest
from cheroot.wsgi import Gateway_10, Server

from plex_restore_agent.server import from_environment


class HeaderLimitedRequest(HTTPRequest):
    def parse_request(self):
        # Socket inactivity alone is insufficient: a client can drip a byte at
        # a time forever. Bound the entire request line + headers instead.
        guard = threading.Lock()
        finished = expired = False

        def expire():
            nonlocal expired
            with guard:
                if finished:
                    return
                expired = True
                try:
                    self.conn.socket.shutdown(socket.SHUT_RD)
                except OSError:
                    pass  # The peer may already have closed the connection.

        timer = threading.Timer(self.server.header_timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            super().parse_request()
        finally:
            with guard:
                finished = True
                # EOF caused by the deadline must never turn partial headers
                # into an accepted request. Cancel before handing off to WSGI.
                if expired:
                    self.ready = False
            timer.cancel()


class AgentConnection(HTTPConnection):
    RequestHandlerClass = HeaderLimitedRequest


class AgentGateway(Gateway_10):
    def get_environ(self):
        environ = super().get_environ()
        # Only the authenticated, admitted upload handler enables this longer
        # inactivity timeout; no client header can set this WSGI extension.
        environ["plex_restore_agent.begin_upload"] = lambda: self.req.conn.socket.settimeout(
            self.req.server.upload_timeout)
        return environ


def create_server(app, host="0.0.0.0", port=8991, *, header_timeout=3, upload_timeout=900):
    # Cheroot passes a streaming body to Flask, so authentication and admission
    # checks run before any archive is read or written to the state volume.
    server = Server((host, port), app, numthreads=4, timeout=header_timeout,
                    accepted_queue_size=8, accepted_queue_timeout=0)
    server.ConnectionClass = AgentConnection
    server.gateway = AgentGateway
    server.header_timeout = header_timeout
    server.upload_timeout = upload_timeout
    server.max_request_body_size = app.config["MAX_CONTENT_LENGTH"]
    server.max_request_header_size = 64 * 1024
    # Close after each response: never drain an untrusted or rejected upload
    # just to reuse its connection (including duplicate restore PUTs).
    server.keep_alive_conn_limit = 0
    return server


if __name__ == "__main__":
    server = create_server(from_environment(), os.environ.get("AGENT_HOST", "0.0.0.0"),
                           int(os.environ.get("AGENT_PORT", "8991")))
    try:
        server.start()
    finally:
        server.stop()
