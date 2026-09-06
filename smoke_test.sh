#!/bin/sh
# Regression check for the NAS control-plane services. Run this after every
# deploy - it's the only safety net available with no CI and no second
# device to test against. Exits non-zero on the first failure.
#
# Usage: ./smoke_test.sh [host]   (defaults to localhost - pass a hostname
# or IP to test a NAS remotely instead of running this on the box itself)
#
# If a login is configured, set NAS_CP_TEST_USER and NAS_CP_TEST_PASSWORD so
# this can log in first and carry the session cookie through every other
# check - otherwise every check below would just see a 200 login page
# instead of real content and (wrongly) still say PASS, since they only look
# at status codes.

set -u
HOST="${1:-localhost}"
FAIL=0
COOKIE_JAR=""
cleanup() { [ -n "$COOKIE_JAR" ] && rm -f "$COOKIE_JAR"; }
trap cleanup EXIT

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
    if [ -n "$COOKIE_JAR" ]; then
        curl -s -o /dev/null -w '%{http_code}' -b "$COOKIE_JAR" --max-time 5 "$@"
    else
        curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$@"
    fi
}

body_contains() {
    needle="$1"; shift
    if [ -n "$COOKIE_JAR" ]; then
        curl -s -b "$COOKIE_JAR" --max-time 5 "$@" | grep -qF "$needle" && echo yes || echo no
    else
        curl -s --max-time 5 "$@" | grep -qF "$needle" && echo yes || echo no
    fi
}

echo "=== Smoke test against $HOST ==="

if [ -n "${NAS_CP_TEST_USER:-}" ] && [ -n "${NAS_CP_TEST_PASSWORD:-}" ]; then
    check "Unauthenticated request is gated behind login" yes \
        body_contains "Sign in" "http://$HOST:8093/"

    COOKIE_JAR=$(mktemp)
    curl -s -c "$COOKIE_JAR" -X POST \
        --data-urlencode "username=$NAS_CP_TEST_USER" \
        --data-urlencode "password=$NAS_CP_TEST_PASSWORD" \
        "http://$HOST:8095/login" -o /dev/null

    check "Login succeeds and the session works on a different port" yes \
        body_contains '"port_files"' "http://$HOST:8095/api/config"
fi

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
