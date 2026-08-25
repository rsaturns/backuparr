#!/usr/bin/env bash
set -euo pipefail

WEBUI_PORT="${WEBUI_PORT:-8990}"

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
