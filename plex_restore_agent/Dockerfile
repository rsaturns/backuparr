FROM python:3.14-alpine

WORKDIR /app
COPY plex_restore_agent/requirements.txt /app/requirements.txt
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir --only-binary=ijson -r /app/requirements.txt
COPY apps/__init__.py apps/plex_archive.py /app/apps/
COPY plex_restore_agent /app/plex_restore_agent
# Fail the build if the mandatory native JSON parser cannot be loaded.
RUN python -c "import plex_restore_agent.manifest"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8991
HEALTHCHECK --interval=30s --timeout=6s --start-period=300s --retries=3 \
    CMD ["python", "-m", "plex_restore_agent.healthcheck"]
# Root is needed to preserve the Plex database's existing UID/GID. The agent
# is separate from Plex; neither its image nor its entrypoint is modified.
ENTRYPOINT ["python", "-m", "plex_restore_agent"]
