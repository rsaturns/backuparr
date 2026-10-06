#!/usr/bin/env bash
# Starts a built Backuparr image and checks that it really works: every
# module is in the image, the container turns healthy, pages carry the
# security headers, first-run setup and login succeed, and the web process
# runs with the private umask. Usage: scripts/smoke_test.sh <image>
set -euo pipefail

IMAGE="${1:?usage: smoke_test.sh <image>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="backuparr-smoke-$$"
DATA="$(mktemp -d)"
JAR="$DATA/cookies"

fail() {
    echo "SMOKE TEST FAILED: $*" >&2
    if docker inspect "$NAME" >/dev/null 2>&1; then docker logs "$NAME" 2>&1 | tail -40 >&2; fi
    exit 1
}
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$DATA"
}
trap cleanup EXIT

echo "1/6 every module is in the image"
MODULES="$(cd "$ROOT" && for f in *.py; do printf '%s ' "${f%.py}"; done)webui.app"
docker run --rm --entrypoint python "$IMAGE" -c \
    "import importlib, sys; [importlib.import_module(m) for m in sys.argv[1:]]" $MODULES \
    || fail "an application module is missing from the image (check the Dockerfile COPY list)"

echo "2/6 container starts and turns healthy"
docker run -d --name "$NAME" -p 127.0.0.1::8990 -e PUID="$(id -u)" -e PGID="$(id -g)" \
    --health-interval=2s --health-start-period=1s --health-retries=3 \
    -v "$DATA:/config/backuparr" "$IMAGE" >/dev/null
PORT="$(docker port "$NAME" 8990/tcp | head -1 | sed 's/.*://')"
BASE="http://127.0.0.1:$PORT"
for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] && break
    [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ] || fail "container exited"
    sleep 2
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] || fail "container never became healthy"

echo "3/6 pages are served with the security headers"
HEADERS="$(curl -fsS -D - -o /dev/null "$BASE/setup")" || fail "/setup is not served"
for header in "Content-Security-Policy:" "X-Frame-Options: DENY" "X-Content-Type-Options: nosniff"; do
    grep -qi "^$header" <<<"$HEADERS" || fail "missing header: $header"
done
grep -qi "script-src 'self'" <<<"$HEADERS" || fail "script-src is not restricted"
! grep -qi "script-src[^;]*unsafe-inline" <<<"$HEADERS" || fail "script-src allows inline scripts"

echo "4/6 first-run setup and login"
CRED='{"username":"smoke","password":"smoke-test-password","confirm":"smoke-test-password"}'
curl -fsS -X POST -H 'Content-Type: application/json' -d "$CRED" -c "$JAR" "$BASE/api/setup" >/dev/null || fail "setup failed"
curl -fsS -X POST -H 'Content-Type: application/json' -d "$CRED" -c "$JAR" "$BASE/api/login" >/dev/null || fail "login failed"

echo "5/6 the API works, is not cacheable, and the UI shows the release version"
API_HEADERS="$(curl -fsS -D - -o /dev/null -b "$JAR" "$BASE/api/config")" || fail "/api/config failed after login"
grep -qi "^Cache-Control: no-store" <<<"$API_HEADERS" || fail "API response is cacheable"
VERSION="$(tr -d '[:space:]' <"$ROOT/VERSION")"
curl -fsS -b "$JAR" "$BASE/" | grep -q "v$VERSION" || fail "UI does not show version $VERSION"

echo "6/6 the web process runs with the private umask"
UMASK_LINE="$(docker exec "$NAME" sh -c 'grep Umask /proc/$(pgrep -f waitress-serve | head -1)/status')"
grep -q "0077" <<<"$UMASK_LINE" || fail "unexpected umask: $UMASK_LINE"

echo "smoke test passed ($IMAGE, version $VERSION)"
