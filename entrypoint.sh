#!/usr/bin/env bash
set -euo pipefail

WEBUI_PORT="${WEBUI_PORT:-8990}"

# Writes /etc/crontabs/root from config.json before crond starts, so the
# very first tick already has the right schedule (rather than waiting on
# webui.app's own startup, which happens a moment later via waitress).
python3 -c "from webui.app import init; init()"

echo "arr-backup: starting cron"
crond -l 2 &

if [ "${RUN_ON_START:-false}" = "true" ]; then
    echo "RUN_ON_START=true, running an initial backup now..."
    (cd /app && python3 backup.py) || echo "Initial backup run failed (see above). Will retry on schedule."
fi

echo "arr-backup: starting web UI on :${WEBUI_PORT}"
exec waitress-serve --host=0.0.0.0 --port="${WEBUI_PORT}" --threads=6 webui.app:app
