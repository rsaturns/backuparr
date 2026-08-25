#!/usr/bin/env bash
set -euo pipefail

WEBUI_PORT="${WEBUI_PORT:-8990}"

# Writes /etc/crontabs/root from config.json before crond starts, so the
# very first tick already has the right schedule (rather than waiting on
# webui.app's own startup, which happens a moment later via waitress).
python3 -c "from webui.app import init; init()"

echo "Backuparr: starting cron"
crond -l 2 &

echo "Backuparr: starting web UI on :${WEBUI_PORT}"
exec waitress-serve --host=0.0.0.0 --port="${WEBUI_PORT}" --threads=6 webui.app:app
