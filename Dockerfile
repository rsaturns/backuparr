FROM python:3.14-alpine

# rclone from its own GitHub release, not apk - apk's build lags
# upstream and carries known CVEs in its bundled Go dependencies.
ARG RCLONE_VERSION=1.75.0
ARG TARGETARCH

# Make sure the community repo (bash, su-exec live there) is enabled.
# su-exec drops from root to the PUID/PGID entrypoint.sh resolves at
# startup. rclone's zip is checksum-verified against its own SHA256SUMS.
RUN sed -i 's/^#\(.*community.*\)/\1/' /etc/apk/repositories \
    && apk update \
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
RUN pip install --no-cache-dir -r requirements.txt

COPY backup.py restore.py restore_actions.py rclone_util.py config_store.py destination_util.py gdrive_oauth.py onedrive_oauth.py auth_store.py secrets_crypto.py entrypoint.sh VERSION /app/
COPY apps /app/apps
COPY webui /app/webui
RUN chmod +x /app/entrypoint.sh

ENV RCLONE_CONFIG=/config/backuparr/rclone.conf \
    BACKUPARR_CONFIG=/config/backuparr/config.json \
    BACKUPARR_LOG_DIR=/var/log/backuparr \
    WEBUI_PORT=8990 \
    PYTHONUNBUFFERED=1

EXPOSE 8990

# /login always returns 200 (or a 302 to /setup on first boot, which wget
# follows) with no auth needed either way - just confirms the web server
# itself is actually serving, regardless of setup/login state.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${WEBUI_PORT}/login" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
