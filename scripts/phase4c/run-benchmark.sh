#!/usr/bin/env bash

set -euo pipefail
source "$(dirname "$0")/common.sh"

major=${1:?usage: run-benchmark.sh PG_MAJOR [WARMUP] [SAMPLES] [ARTIFACT_PARENT]}
case "$major" in 16|17|18|19) ;; *) echo "unsupported PostgreSQL major: $major" >&2; exit 2;; esac
warmup=${2:-25}; samples=${3:-250}; parent=${4:-${RUNNER_TEMP:-/tmp}/pgqi4c-benchmark}
case "$warmup:$samples" in *[!0-9:]*|:*) echo "invalid sample counts" >&2; exit 2;; esac
test "$samples" -gt 0 || exit 2
smoke_root=$("$(dirname "$0")/run-production.sh" "$major" "$parent")
pgbin=${PG_INSTALL_ROOT:-/usr/lib/postgresql}/$major/bin
pgqi4c_sanitize_connection_env
socket_dir="/tmp/pgqi4c-s-$major-$$"
started=0
benchmark_status=1
printf 'RUNNING\n' >"$smoke_root/BENCHMARK_STATUS"
cleanup() { local rc=$?; if test "$started" -eq 1 && ! pgqi4c_stop "$pgbin" "$smoke_root/benchmark-cluster"; then printf 'INCOMPLETE cleanup_failed\n' >"$smoke_root/BENCHMARK_STATUS"; rc=1; fi; rmdir "$socket_dir" 2>/dev/null || true; test "$benchmark_status" -eq 0 || printf 'INCOMPLETE\n' >"$smoke_root/BENCHMARK_STATUS"; exit "$rc"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
started=1
pgqi4c_start "$pgbin" "$smoke_root/benchmark-cluster" "pg_duckdb,pg_qi" "$socket_dir"
"$pgbin/psql" -X -v ON_ERROR_STOP=1 -h "$socket_dir" -d postgres -f "$pgqi4c_repo/tests/phase4c/setup.sql" >"$smoke_root/benchmark-setup.log" 2>&1
for mode in disabled off allowlist; do
    "$smoke_root/production-driver" \
        "host=$socket_dir dbname=postgres connect_timeout=10 application_name=pgqi4c_benchmark_$mode" \
        benchmark "$mode" "$smoke_root/$mode.tsv" "$warmup" "$samples"
done
pgqi4c_stop "$pgbin" "$smoke_root/benchmark-cluster"
started=0
rmdir "$socket_dir"
manifest_pg_qi_sha=$(sed -n 's/^pg_qi_installed_sha256=//p' "$smoke_root/manifest.txt")
manifest_pg_duckdb_sha=$(sed -n 's/^pg_duckdb_installed_sha256=//p' "$smoke_root/manifest.txt")
test "$(sha256sum "$($pgbin/pg_config --pkglibdir)/pg_qi.so" | cut -d' ' -f1)" = "$manifest_pg_qi_sha"
test "$(sha256sum "$($pgbin/pg_config --pkglibdir)/pg_duckdb.so" | cut -d' ' -f1)" = "$manifest_pg_duckdb_sha"
printf 'mode\tp50_us\tp95_us\tp99_us\n' >"$smoke_root/summary.tsv"
for mode in disabled off allowlist; do
    values=$(awk -F '\t' '$1 ~ /^[0-9]+$/ {print $2}' "$smoke_root/$mode.tsv" | sort -n)
    count=$(printf '%s\n' "$values" | wc -l)
    pick() { printf '%s\n' "$values" | sed -n "$(( (count * $1 + 99) / 100 ))p"; }
    printf '%s\t%s\t%s\t%s\n' "$mode" "$(pick 50)" "$(pick 95)" "$(pick 99)" >>"$smoke_root/summary.tsv"
done
sha256sum "$smoke_root"/*.tsv >"$smoke_root/benchmark-evidence.sha256"
printf 'PASS\n' >"$smoke_root/BENCHMARK_STATUS"
benchmark_status=0
trap - EXIT INT TERM
echo "$smoke_root"
