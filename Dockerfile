FROM python:3.12-alpine

# Make sure the community repo (rclone lives there) is enabled.
RUN sed -i 's/^#\(.*community.*\)/\1/' /etc/apk/repositories \
    && apk update \
    && apk add --no-cache bash rclone tzdata

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

ENTRYPOINT ["/app/entrypoint.sh"]
