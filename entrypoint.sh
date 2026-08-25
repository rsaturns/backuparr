#!/usr/bin/env bash
set -euo pipefail

WEBUI_PORT="${WEBUI_PORT:-8990}"
RCLONE_CONFIG_PASS_FILE="${RCLONE_CONFIG_PASS_FILE:-/config/backuparr/rclone.pass}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Password for rclone's own config-file encryption (protects the Google
# Drive/OneDrive secrets rclone.conf mirrors from config.json). Separate
# from secrets_crypto.py's/auth_store.py's keys since only the rclone binary
# ever needs it. Auto-generated on first boot, same pattern as secret_key/
# secrets.key; override with RCLONE_CONFIG_PASS to keep it off the volume.
if [ -z "${RCLONE_CONFIG_PASS:-}" ]; then
    if [ ! -f "$RCLONE_CONFIG_PASS_FILE" ]; then
        mkdir -p "$(dirname "$RCLONE_CONFIG_PASS_FILE")"
        head -c 32 /dev/urandom | base64 > "$RCLONE_CONFIG_PASS_FILE"
        chmod 600 "$RCLONE_CONFIG_PASS_FILE"
    fi
    RCLONE_CONFIG_PASS="$(cat "$RCLONE_CONFIG_PASS_FILE")"
fi
export RCLONE_CONFIG_PASS

# Encrypts rclone.conf in place - a no-op after the first boot, since
# `encryption check` already succeeds once the file is encrypted with this
# password (also covers a fresh install where the file doesn't exist yet,
# since rclone doesn't encrypt a file it creates from scratch just because
# RCLONE_CONFIG_PASS is set - only reading an already-encrypted one honors it).
if ! rclone config encryption check </dev/null >/dev/null 2>&1; then
    echo "Backuparr: encrypting rclone.conf"
    printf '%s\n%s\n' "$RCLONE_CONFIG_PASS" "$RCLONE_CONFIG_PASS" | rclone config encryption set
fi

# Scheduling runs in-process inside webui/app.py (a background thread
# polling the live cron_schedule from config.json) rather than via an
# external cron daemon - crond/dcron in this container couldn't reliably
# stay alive as PID 1's child or hot-reload a changed crontab.

# Drops from root (needed above for apk/rclone setup) to PUID:PGID before
# the actual web server starts - same PUID/PGID env var convention as
# LinuxServer.io images, so the container's files land on the host volume
# owned by whichever user you already use for this kind of thing, instead
# of root. Resolved dynamically rather than baked in at build time: this
# runs on every start, so it also self-heals ownership on an existing
# volume the first time you upgrade onto this image (everything under
# /config/backuparr was root:root before), and again automatically if you
# ever change PUID/PGID later. Idempotent - getent lookups mean re-running
# this on a container that already has the right user/group is a no-op,
# not a failure.
if ! getent group "$PGID" >/dev/null 2>&1; then
    addgroup -g "$PGID" backuparr
fi
GROUP_NAME="$(getent group "$PGID" | cut -d: -f1)"

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    adduser -D -H -u "$PUID" -G "$GROUP_NAME" backuparr
fi
USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"

mkdir -p "$(dirname "$BACKUPARR_CONFIG")" "$BACKUPARR_LOG_DIR"
chown -R "$PUID:$PGID" "$(dirname "$BACKUPARR_CONFIG")" "$BACKUPARR_LOG_DIR"

echo "Backuparr: starting web UI on :${WEBUI_PORT} (as ${USER_NAME}:${GROUP_NAME}, ${PUID}:${PGID})"
exec su-exec "$PUID:$PGID" waitress-serve --host=0.0.0.0 --port="${WEBUI_PORT}" --threads=6 webui.app:app
