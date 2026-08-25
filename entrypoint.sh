#!/usr/bin/env bash
set -euo pipefail

WEBUI_PORT="${WEBUI_PORT:-8990}"
RCLONE_CONFIG_PASS_FILE="${RCLONE_CONFIG_PASS_FILE:-/config/backuparr/rclone.pass}"

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

echo "Backuparr: starting web UI on :${WEBUI_PORT}"
exec waitress-serve --host=0.0.0.0 --port="${WEBUI_PORT}" --threads=6 webui.app:app
