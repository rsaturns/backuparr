#!/usr/bin/env bash
set -euo pipefail

# `decrypt <file.enc>` decrypts a backup and exits, before any setup: it
# runs as whoever started it and touches nothing but the files named.
if [ "${1:-}" = "decrypt" ]; then
    shift
    exec python /app/decrypt.py "$@"
fi

WEBUI_HOST="${WEBUI_HOST:-0.0.0.0}"
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
# Only what isn't PUID:PGID's already, so a start doesn't rewrite every
# local backup. -h changes a symlink itself, never what it points to.
find "$(dirname "$BACKUPARR_CONFIG")" "$BACKUPARR_LOG_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) \
    -exec chown -h "$PUID:$PGID" {} +

# HTTPS (#177) and any reverse proxy's X-Forwarded-* headers are handled in
# Python: webui/serve.py and webui/forwarded.py.
WEBUI_HTTPS_PORT="${WEBUI_HTTPS_PORT:-8991}"
export WEBUI_HOST WEBUI_PORT WEBUI_HTTPS_PORT

# Backups hold API keys and credentials, so new files are owner-only
# (0600, directories 0700) unless UMASK says otherwise, e.g. UMASK=022.
UMASK="${UMASK:-077}"
case "$UMASK" in
    [0-7][0-7][0-7]|0[0-7][0-7][0-7]) ;;
    *) echo "Backuparr: ignoring invalid UMASK '$UMASK', using 077"; UMASK=077 ;;
esac
umask "$UMASK"

echo "Backuparr: starting web UI on ${WEBUI_HOST}, HTTP ${WEBUI_PORT} and HTTPS ${WEBUI_HTTPS_PORT} (as ${USER_NAME}:${GROUP_NAME}, ${PUID}:${PGID})"
cd /app
exec su-exec "$PUID:$PGID" python -m webui.serve
