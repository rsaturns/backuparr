# Pinned by digest so every build uses the base image CI tested (and
# scripts/test_in_image.sh runs the tests in). Dependabot opens a PR when
# python:3.15-alpine moves, so Alpine and Python fixes arrive as tested PRs.
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd

# rclone from its own GitHub release, not apk - apk's build lags
# upstream and carries known CVEs in its bundled Go dependencies.
ARG RCLONE_VERSION=1.76.0
ARG TARGETARCH

# Alpine's security updates first: they ship between rebuilds of the python
# image (in November 2026 the pinned one still had zlib 1.3.2 and OpenSSL
# 3.5.9-r0, with fixes out in zlib 1.4 and 3.5.9-r1). scripts/test_in_image.sh
# installs the same updates, so the tests still run on what ships.
# su-exec (from the community repo, enabled in python:alpine) drops from root
# to the PUID/PGID entrypoint.sh resolves at startup. rclone's zip is
# checksum-verified against its own SHA256SUMS.
RUN apk upgrade --no-cache \
    && apk add --no-cache bash tzdata su-exec \
    && cd /tmp \
    && wget -q "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip" \
    && wget -q "https://downloads.rclone.org/v${RCLONE_VERSION}/SHA256SUMS" \
    && grep " rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip\$" SHA256SUMS > rclone.sha256 \
    && sha256sum -c rclone.sha256 \
    && python3 -m zipfile -e "rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip" . \
    && mv "rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}/rclone" /usr/local/bin/rclone \
    && chmod +x /usr/local/bin/rclone \
    && cd / && rm -rf /tmp/*

WORKDIR /app
COPY requirements.txt /app/
# Every package is pinned with its hashes (#230), so a build installs what
# CI tested. pip isn't needed once they're in, and it carries its own
# vendored copies of urllib3, msgpack and setuptools that scanners flag.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && pip uninstall -y pip

# The license travels with the image (/app/LICENSE).
COPY *.py entrypoint.sh VERSION LICENSE /app/
COPY apps /app/apps
COPY webui /app/webui
# Compiled here: the web server runs as PUID, which can't write /app, so it
# would otherwise compile every module again on each start.
RUN chmod +x /app/entrypoint.sh \
    && python -m compileall -q /app

ENV RCLONE_CONFIG=/config/backuparr/rclone.conf \
    BACKUPARR_CONFIG=/config/backuparr/config.json \
    BACKUPARR_LOG_DIR=/var/log/backuparr \
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=8990 \
    WEBUI_HTTPS_PORT=8991 \
    PYTHONUNBUFFERED=1

EXPOSE 8990 8991

# Check the configured bind address, mapping wildcard listeners to loopback.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["/app/entrypoint.sh"]
