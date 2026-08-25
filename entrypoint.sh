#!/usr/bin/env bash
set -euo pipefail

WEBUI_PORT="${WEBUI_PORT:-8990}"
RCLONE_CONFIG_PASS_FILE="${RCLONE_CONFIG_PASS_FILE:-/config/backuparr/rclone.pass}"

# rclone's own config-file encryption (scrypt + AES, verified against the
# actual bundled binary) protects the Google Drive/OneDrive secrets
# rclone.conf mirrors from config.json. This password is deliberately
# separate from secrets_crypto.py's/auth_store.py's key material - it only
# ever needs to be read by the rclone binary itself, never by Python, so
# there's no reason to route it through that machinery. Auto-generated into
# its own file on first boot, same pattern as secret_key/secrets.key;
# override with RCLONE_CONFIG_PASS directly (e.g. a Docker secret) to keep
# it off the volume entirely.
if [ -z "${RCLONE_CONFIG_PASS:-}" ]; then
    if [ ! -f "$RCLONE_CONFIG_PASS_FILE" ]; then
        mkdir -p "$(dirname "$RCLONE_CONFIG_PASS_FILE")"
        head -c 32 /dev/urandom | base64 > "$RCLONE_CONFIG_PASS_FILE"
        chmod 600 "$RCLONE_CONFIG_PASS_FILE"
    fi
    RCLONE_CONFIG_PASS="$(cat "$RCLONE_CONFIG_PASS_FILE")"
fi
export RCLONE_CONFIG_PASS

# Encrypts rclone.conf in place the first time this runs - also covers the
# fresh-install case where the file doesn't exist yet at all, since a config
# file rclone creates from scratch is NOT encrypted by default just because
# RCLONE_CONFIG_PASS happens to be set; only *reading* an already-encrypted
# one is. `encryption check` succeeds once it's already encrypted with this
# password, so this is a no-op (and doesn't rewrite the file) on every start
# after the first - verified live that re-running `encryption set`
# unconditionally would instead try to *change* the password every time,
# which is wasteful and needs the current password to already be correct.
if ! rclone config encryption check </dev/null >/dev/null 2>&1; then
    echo "Backuparr: encrypting rclone.conf"
    printf '%s\n%s\n' "$RCLONE_CONFIG_PASS" "$RCLONE_CONFIG_PASS" | rclone config encryption set
fi

# Scheduling is handled entirely in-process by webui/app.py (a background
# thread polling the live cron_schedule from config.json every ~20s) rather
# than by an external cron daemon. This replaced an earlier crond/dcron
# setup after two confirmed problems: crond's own daemonizing double-fork
# zombified once reparented to PID 1 (this script exec's into waitress,
# which has no wait()-loop to reap orphans), and - even once made to stay
# genuinely alive by running it in the foreground - dcron does not
# hot-reload a changed crontab. Both a live in-place crontab edit and a
# SIGHUP to a live crond process were tested directly and neither picked up
# a schedule change without a full container restart. An in-process
# scheduler has no separate crontab file for anything to fail to notice.

echo "Backuparr: starting web UI on :${WEBUI_PORT}"
exec waitress-serve --host=0.0.0.0 --port="${WEBUI_PORT}" --threads=6 webui.app:app
