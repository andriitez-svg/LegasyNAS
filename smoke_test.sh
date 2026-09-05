#!/bin/sh
# Regression check for the NAS control-plane services. Run this after every
# deploy - it's the only safety net available with no CI and no second
# device to test against. Exits non-zero on the first failure.
#
# Usage: ./smoke_test.sh [host]   (defaults to the live NAS)

set -u
HOST="${1:-192.168.8.110}"
FAIL=0

check() {
    desc="$1"; expected="$2"; shift 2
    actual=$("$@")
    if [ "$actual" = "$expected" ]; then
        echo "[PASS] $desc"
    else
        echo "[FAIL] $desc (expected $expected, got $actual)"
        FAIL=1
    fi
}

http_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$@"
}

echo "=== Smoke test against $HOST ==="

check "Files app root listing responds" 200 \
    http_code "http://$HOST:8093/"

check "Fetcher UI responds" 200 \
    http_code "http://$HOST:8092/"

check "Desktop shell responds" 200 \
    http_code "http://$HOST:8095/desktop.html"

check "Desktop shell exposes its config to the client" 200 \
    http_code "http://$HOST:8095/api/config"

check "Glances proxy responds" 200 \
    http_code "http://$HOST:8095/glances/cpu"

check "Power endpoint refuses missing confirm header" 403 \
    http_code -X POST "http://$HOST:8095/system/restart"

check "Power endpoint 404s on unknown path" 404 \
    http_code -X POST "http://$HOST:8095/system/nonexistent"

check "GET on power path 404s (POST-only, falls through to static-file miss)" 404 \
    http_code "http://$HOST:8095/system/restart"

echo
if [ "$FAIL" -eq 0 ]; then
    echo "ALL CHECKS PASSED"
else
    echo "ONE OR MORE CHECKS FAILED - do not consider this deploy good"
fi
exit "$FAIL"
