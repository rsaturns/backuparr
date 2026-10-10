import os

from cheroot.wsgi import Server

from plex_restore_agent.server import from_environment


def create_server(app, host="0.0.0.0", port=8991):
    # Cheroot passes a streaming body to Flask, so authentication and admission
    # checks run before any archive is read or written to the state volume.
    server = Server((host, port), app, numthreads=4, timeout=900)
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
