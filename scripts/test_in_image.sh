#!/usr/bin/env bash
# Runs the test suite in the image the Dockerfile builds on (its FROM line,
# digest included), as an ordinary user, so the tests see the Python and
# musl the app ships with and no second Python version has to be kept in
# step with the Dockerfile. CI and the release workflow both use it.
# Arguments go to pytest:
#   scripts/test_in_image.sh
#   scripts/test_in_image.sh tests/test_schedule.py -x
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$(sed -n 's/^FROM[[:space:]]\{1,\}\([^[:space:]]\{1,\}\).*/\1/p' "$ROOT/Dockerfile" | head -n 1)"
[ -n "$BASE" ] || { echo "test_in_image.sh: no FROM line in the Dockerfile" >&2; exit 1; }
TAG="backuparr-tests:$$"
trap 'docker rmi -f "$TAG" >/dev/null 2>&1 || true' EXIT

# bash for the entrypoint tests; a user with the caller's uid, so tests
# that expect a permission error get one (root would bypass it).
docker build -q -t "$TAG" --build-arg BASE="$BASE" --build-arg TEST_UID="$(id -u)" - >/dev/null <<'EOF'
ARG BASE
FROM ${BASE}
ARG TEST_UID
RUN apk add --no-cache bash \
    && { grep -q "^[^:]*:[^:]*:${TEST_UID}:" /etc/passwd || adduser -D -u "${TEST_UID}" tester; }
USER ${TEST_UID}
EOF

# The checkout is mounted read-only: the tests write only to their own
# temporary folders.
docker run --rm -v "$ROOT":/src:ro -w /src -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 "$TAG" sh -euc '
    python -m venv /tmp/venv
    /tmp/venv/bin/pip install -q --disable-pip-version-check --require-hashes \
        -r requirements.txt -r requirements-dev.txt
    exec /tmp/venv/bin/python -m pytest -q -p no:cacheprovider "$@"
' sh "$@"
