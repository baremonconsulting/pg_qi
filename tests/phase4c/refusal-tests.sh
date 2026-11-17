#!/usr/bin/env bash

set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
source "$repo/scripts/phase4c/common.sh"

failures=0
expect_refusal() { if "$@" >/dev/null 2>&1; then echo "unexpected success: $*" >&2; failures=$((failures+1)); fi; }
expect_refusal pgqi4c_require_safe_root /
expect_refusal pgqi4c_require_safe_root /home
expect_refusal pgqi4c_require_safe_root "/tmp/unsafe path/hostaddr=127.0.0.1"
expect_refusal pgqi4c_require_safe_root "/tmp/unsafe'quote"
expect_refusal pgqi4c_require_safe_root '/tmp/unsafe\backslash'
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
ln -s "$tmp" "$tmp-link"
expect_refusal pgqi4c_require_safe_root "$tmp-link"
rm "$tmp-link"
mkdir "$tmp/existing"
expect_refusal pgqi4c_start /nonexistent "$tmp/existing"
expect_refusal pgqi4c_stop /nonexistent "$tmp"
expect_refusal "$repo/scripts/phase4c/run-benchmark.sh" 0 invalid 1 "$tmp"
printf 'pg_qi.spike_example\npg_qi.test_example\n' >"$tmp/test-surface"
strings "$tmp/test-surface" | grep -E 'pg_qi\.spike_|pg_qi\.test_' >/dev/null
printf 'pg_qi.production_example\n' >"$tmp/production-surface"
if strings "$tmp/production-surface" | grep -E 'pg_qi\.spike_|pg_qi\.test_' >/dev/null; then
    echo "production-surface pattern false positive" >&2
    failures=$((failures+1))
fi
test "$failures" -eq 0
echo "PGQI-4C refusal tests PASS"
