import os

from waitress import serve

from plex_restore_agent.server import from_environment


if __name__ == "__main__":
    app = from_environment()
    serve(app, host=os.environ.get("AGENT_HOST", "0.0.0.0"),
          port=int(os.environ.get("AGENT_PORT", "8991")), threads=4,
          max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
          channel_timeout=900)
