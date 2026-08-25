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

# Writes /etc/crontabs/root from config.json before crond starts, so the
# very first tick already has the right schedule (rather than waiting on
# webui.app's own startup, which happens a moment later via waitress).
python3 -c "from webui.app import init; init()"

echo "Backuparr: starting cron"
# -f (foreground) is required here, not optional - crond's own default
# daemonizing behavior (an internal double-fork) never survives in this
# container: the intermediate fork-child it leaves behind becomes a zombie
# once reparented to PID 1 (waitress, which has no wait()-loop to reap
# orphans the way a real init system would), and no live scheduler process
# ever ends up actually running. Confirmed directly: `crond -l 2 &` (no -f)
# left three zombie [crond] entries in `ps -eo pid,ppid,stat,args` (STAT=Z)
# across repeated container starts, and a scheduled test job never fired
# even after several minutes of waiting - meaning scheduled backups were
# never actually running on a real schedule. With -f, crond doesn't
# daemonize at all - bash's own `&` here is what backgrounds it instead -
# and the resulting process stays genuinely alive (STAT=S), confirmed to
# correctly fire a test job on schedule afterward.
crond -f -l 2 &

echo "Backuparr: starting web UI on :${WEBUI_PORT}"
exec waitress-serve --host=0.0.0.0 --port="${WEBUI_PORT}" --threads=6 webui.app:app
