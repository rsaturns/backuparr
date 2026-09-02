#!/usr/bin/env bash
set -euo pipefail

WEBUI_PORT="${WEBUI_PORT:-8990}"
RCLONE_CONFIG_PASS_FILE="${RCLONE_CONFIG_PASS_FILE:-/config/backuparr/rclone.pass}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Password for rclone's own config-file encryption. Auto-generated on
# first boot; override with RCLONE_CONFIG_PASS to keep it off the volume.
if [ -z "${RCLONE_CONFIG_PASS:-}" ]; then
    if [ ! -f "$RCLONE_CONFIG_PASS_FILE" ]; then
        mkdir -p "$(dirname "$RCLONE_CONFIG_PASS_FILE")"
        head -c 32 /dev/urandom | base64 > "$RCLONE_CONFIG_PASS_FILE"
        chmod 600 "$RCLONE_CONFIG_PASS_FILE"
    fi
    RCLONE_CONFIG_PASS="$(cat "$RCLONE_CONFIG_PASS_FILE")"
fi
export RCLONE_CONFIG_PASS

# Encrypts rclone.conf in place - a no-op after the first boot.
if ! rclone config encryption check </dev/null >/dev/null 2>&1; then
    echo "Backuparr: encrypting rclone.conf"
    printf '%s\n%s\n' "$RCLONE_CONFIG_PASS" "$RCLONE_CONFIG_PASS" | rclone config encryption set
fi

# Drops from root to PUID:PGID before the web server starts. Resolved
# every start (not baked in), so it self-heals ownership on upgrade or
# if PUID/PGID changes later. getent guards make this idempotent.
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

WAITRESS_ARGS=(--host=0.0.0.0 --port="${WEBUI_PORT}" --threads=6)

# Same opt-in as webui/app.py's ProxyFix wrapping (BACKUPARR_FORCE_HTTPS).
# Without --trusted-proxy, waitress silently drops incoming
# X-Forwarded-Proto/-For/-Host headers before the WSGI app ever sees them
# - ProxyFix alone isn't enough, since it never gets a header to read.
FORCE_HTTPS_LOWER="$(printf '%s' "${BACKUPARR_FORCE_HTTPS:-}" | tr '[:upper:]' '[:lower:]')"
if [ "$FORCE_HTTPS_LOWER" = "1" ] || [ "$FORCE_HTTPS_LOWER" = "true" ] || [ "$FORCE_HTTPS_LOWER" = "yes" ]; then
    WAITRESS_ARGS+=(--trusted-proxy="*" --trusted-proxy-headers="x-forwarded-proto x-forwarded-for x-forwarded-host")
fi

echo "Backuparr: starting web UI on :${WEBUI_PORT} (as ${USER_NAME}:${GROUP_NAME}, ${PUID}:${PGID})"
exec su-exec "$PUID:$PGID" waitress-serve "${WAITRESS_ARGS[@]}" webui.app:app
