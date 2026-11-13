#!/usr/bin/env bash
# Starts a built Backuparr image and checks that it really works: every
# module is in the image, the container turns healthy, pages carry the
# security headers on HTTP and HTTPS (#177), first-run setup and login succeed,
# the HTTPS login cookie is Secure, and the web process runs with the private
# umask. Usage: scripts/smoke_test.sh <image>
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

echo "1/7 every module is in the image"
MODULES="$(cd "$ROOT" && for f in *.py; do printf '%s ' "${f%.py}"; done)webui.app"
docker run --rm --entrypoint python "$IMAGE" -c \
    "import importlib, sys; [importlib.import_module(m) for m in sys.argv[1:]]" $MODULES \
    || fail "an application module is missing from the image (check the Dockerfile COPY lines)"

echo "2/7 container starts and turns healthy"
docker run -d --name "$NAME" -p 127.0.0.1::8990 -p 127.0.0.1::8991 -e PUID="$(id -u)" -e PGID="$(id -g)" \
    --health-interval=2s --health-start-period=1s --health-retries=3 \
    -v "$DATA:/config/backuparr" "$IMAGE" >/dev/null
PORT="$(docker port "$NAME" 8990/tcp | head -1 | sed 's/.*://')"
BASE="http://127.0.0.1:$PORT"
TLS_PORT="$(docker port "$NAME" 8991/tcp | head -1 | sed 's/.*://')"
TLS_BASE="https://127.0.0.1:$TLS_PORT"
for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] && break
    [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ] || fail "container exited"
    sleep 2
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] || fail "container never became healthy"

echo "3/7 pages are served with the security headers, over HTTP and HTTPS"
HEADERS="$(curl -fsS -D - -o /dev/null "$BASE/setup")" || fail "/setup is not served"
TLS_HEADERS="$(curl -fsSk -D - -o /dev/null "$TLS_BASE/setup")" || fail "/setup is not served over HTTPS"
grep -qi "^Content-Security-Policy:" <<<"$TLS_HEADERS" || fail "missing header over HTTPS: Content-Security-Policy"
for header in "Content-Security-Policy:" "X-Frame-Options: DENY" "X-Content-Type-Options: nosniff"; do
    grep -qi "^$header" <<<"$HEADERS" || fail "missing header: $header"
done
grep -qi "script-src 'self'" <<<"$HEADERS" || fail "script-src is not restricted"
! grep -qi "script-src[^;]*unsafe-inline" <<<"$HEADERS" || fail "script-src allows inline scripts"

echo "4/7 first-run setup and login"
CRED='{"username":"smoke","password":"smoke-test-password","confirm":"smoke-test-password"}'
curl -fsS -X POST -H 'Content-Type: application/json' -d "$CRED" -c "$JAR" "$BASE/api/setup" >/dev/null || fail "setup failed"
curl -fsS -X POST -H 'Content-Type: application/json' -d "$CRED" -c "$JAR" "$BASE/api/login" >/dev/null || fail "login failed"

echo "5/7 the API waits for the encryption key to be saved, then works, is not cacheable, and the UI shows the release version"
[ "$(curl -sS -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/api/config")" = 403 ] || fail "/api/config answered before the encryption key was saved"
KEY_HEADERS="$(curl -fsS -D - -b "$JAR" "$BASE/api/secrets-key")" || fail "/api/secrets-key failed"
grep -q '"key"' <<<"$KEY_HEADERS" || fail "/api/secrets-key didn't return the key"
grep -qi "^Cache-Control: no-store" <<<"$KEY_HEADERS" || fail "the encryption key response is cacheable"
curl -fsS -X POST -H 'Content-Type: application/json' -d '{"confirm":true}' -b "$JAR" "$BASE/api/secrets-key/acknowledge" >/dev/null || fail "saving the encryption key failed"
curl -fsS -b "$JAR" "$BASE/api/secrets-key" | grep -q '"acknowledged":true' || fail "the encryption key is still shown after it was saved"
API_HEADERS="$(curl -fsS -D - -o /dev/null -b "$JAR" "$BASE/api/config")" || fail "/api/config failed after login"
grep -qi "^Cache-Control: no-store" <<<"$API_HEADERS" || fail "API response is cacheable"
VERSION="$(tr -d '[:space:]' <"$ROOT/VERSION")"
curl -fsS -b "$JAR" "$BASE/" | grep -q "v$VERSION" || fail "UI does not show version $VERSION"

echo "6/7 HTTPS: a self-signed certificate, a Secure login cookie of its own, and HTTPS only closing HTTP live"
docker exec "$NAME" test -s /config/backuparr/tls/cert.pem || fail "no certificate in /config/backuparr/tls"
echo | openssl s_client -connect "127.0.0.1:$TLS_PORT" 2>/dev/null | grep -q "CN *= *Backuparr" || fail "HTTPS doesn't serve Backuparr's certificate"
TLS_JAR="$DATA/tls-cookies"
LOGIN_HEADERS="$(curl -fsSk -D - -o /dev/null -X POST -H 'Content-Type: application/json' -d "$CRED" -c "$TLS_JAR" "$TLS_BASE/api/login")" || fail "login over HTTPS failed"
grep -qi "^Set-Cookie: __Host-session=.*; Secure" <<<"$LOGIN_HEADERS" || fail "the HTTPS login cookie isn't a Secure __Host- cookie"
curl -fsSk -b "$TLS_JAR" "$TLS_BASE/api/https" | grep -q '"on_https":true' || fail "/api/https doesn't see the HTTPS port"
HTTPS_ONLY() { curl -fsSk -b "$TLS_JAR" -X POST -H 'Content-Type: application/json' -d "{\"enabled\":$1}" "$TLS_BASE/api/https/only" >/dev/null; }
HTTP_CODE() { curl -s -m 3 -o /dev/null -w '%{http_code}' "$BASE/login" || true; }
HTTPS_ONLY true || fail "turning on HTTPS only failed"
for _ in $(seq 1 20); do [ "$(HTTP_CODE)" = 000 ] && break; sleep 0.5; done
[ "$(HTTP_CODE)" = 000 ] || fail "HTTPS only didn't close HTTP"
[ "$(docker exec "$NAME" python /app/healthcheck.py && echo ok)" = ok ] || fail "unhealthy with HTTPS only on"
HTTPS_ONLY false || fail "turning off HTTPS only failed"
for _ in $(seq 1 20); do [ "$(HTTP_CODE)" = 200 ] && break; sleep 0.5; done
[ "$(HTTP_CODE)" = 200 ] || fail "HTTP didn't open again after HTTPS only"

echo "7/7 the web process runs with the private umask"
UMASK_LINE="$(docker exec "$NAME" sh -c 'grep Umask /proc/$(pgrep -f webui.serve | head -1)/status')"
grep -q "0077" <<<"$UMASK_LINE" || fail "unexpected umask: $UMASK_LINE"

echo "smoke test passed ($IMAGE, version $VERSION)"
