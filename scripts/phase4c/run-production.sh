#!/usr/bin/env bash

set -euo pipefail
source "$(dirname "$0")/common.sh"

major=${1:?usage: run-production.sh PG_MAJOR [ARTIFACT_PARENT]}
case "$major" in 16|17|18|19) ;; *) echo "unsupported PostgreSQL major: $major" >&2; exit 2;; esac
pgroot=${PG_INSTALL_ROOT:-/usr/lib/postgresql}/$major
pgbin=$pgroot/bin
test -x "$pgbin/pg_config" || { echo "PostgreSQL $major not found" >&2; exit 2; }
parent=${2:-${RUNNER_TEMP:-/tmp}/pgqi4c}
parent=$(realpath -m -- "$parent")
pgqi4c_require_safe_root "$parent/probe"
mkdir -m 700 -p "$parent"
pgqi4c_require_private_parent "$parent"
run_root="$parent/run-pg${major}-$(date -u +%Y%m%dT%H%M%SZ)-$$"

pgqi4c_sanitize_connection_env
make -C "$pgqi4c_repo" PG_CONFIG="$pgbin/pg_config" PG_QI_TESTING=0 clean >"${run_root}.build.log" 2>&1
make -C "$pgqi4c_repo" PG_CONFIG="$pgbin/pg_config" PG_QI_TESTING=0 -j2 >>"${run_root}.build.log" 2>&1
if strings "$pgqi4c_repo/pg_qi.so" | grep -E 'pg_qi\.spike_|pg_qi\.test_|qi_spike_|qi_test_|PG_QI_FINAL' >/dev/null; then
    echo "test-only surface leaked into production library" >&2; exit 1
fi
pgqi4c_verify_installed_production "$pgbin/pg_config" "$pgqi4c_repo/pg_qi.so"
libdir=$($pgbin/pg_config --pkglibdir)
pg_qi_pre_sha=$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$libdir/pg_qi.so")
pg_duckdb_pre_sha=$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$libdir/pg_duckdb.so")
pg_qi_built_sha=$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$pgqi4c_repo/pg_qi.so")
cases_sha=$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$pgqi4c_repo/tests/phase4c/cases.tsv")
driver_sha=$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$pgqi4c_repo/scripts/phase4c/production-driver.c")
setup_sha=$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$pgqi4c_repo/tests/phase4c/setup.sql")
source_patch_sha=$(git -C "$pgqi4c_repo" diff --binary | sha256sum | cut -d' ' -f1)
[[ "$source_patch_sha" =~ ^[0-9a-f]{64}$ ]]

status=1
started=0
cleanup() { local rc=$?; if test "$started" -eq 1; then if ! pgqi4c_stop "$pgbin" "$run_root"; then printf 'INCOMPLETE cleanup_failed\n' >"$run_root/STATUS"; rc=1; fi; fi; test "$status" -eq 0 || printf 'INCOMPLETE\n' >"$run_root/STATUS"; exit "$rc"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
started=1
pgqi4c_start "$pgbin" "$run_root"

observed_pg_duckdb=unknown
observed_duckdb=unknown
if test -d "${RUNNER_TEMP:-}/pg_duckdb-$major/.git"; then
    observed_pg_duckdb=$(git -C "$RUNNER_TEMP/pg_duckdb-$major" rev-parse HEAD)
    observed_duckdb=$(git -C "$RUNNER_TEMP/pg_duckdb-$major/third_party/duckdb" rev-parse HEAD)
fi
if test -n "${PG_DUCKDB_SHA:-}" && test "$observed_pg_duckdb" != unknown; then test "$observed_pg_duckdb" = "$PG_DUCKDB_SHA"; fi
if test -n "${DUCKDB_SHA:-}" && test "$observed_duckdb" != unknown; then test "$observed_duckdb" = "$DUCKDB_SHA"; fi
if test "${CI:-false}" = true; then
    test "$observed_pg_duckdb" != unknown && test "$observed_duckdb" != unknown || {
        echo "CI acceptance requires observed dependency source identities" >&2
        exit 1
    }
fi
{
    printf 'run_id=%s\npostgresql=%s\nsource_commit=%s\nsource_dirty=%s\n' "$(basename "$run_root")" "$($pgbin/postgres --version)" "$(git -C "$pgqi4c_repo" rev-parse HEAD)" "$(test -z "$(git -C "$pgqi4c_repo" status --porcelain)" && echo false || echo true)"
    printf 'pg_qi_installed=%s\npg_qi_built_sha256=%s\npg_qi_installed_sha256=%s\npg_duckdb_installed_sha256=%s\n' "$libdir/pg_qi.so" "$pg_qi_built_sha" "$pg_qi_pre_sha" "$pg_duckdb_pre_sha"
    printf 'expected_pg_duckdb_sha=%s\nexpected_duckdb_sha=%s\n' "${PG_DUCKDB_SHA:-unknown}" "${DUCKDB_SHA:-unknown}"
    printf 'observed_pg_duckdb_source_sha=%s\nobserved_duckdb_source_sha=%s\nprovenance_status=%s\n' "$observed_pg_duckdb" "$observed_duckdb" "$(test "$observed_pg_duckdb" != unknown && test "$observed_duckdb" != unknown && echo complete || echo exploratory_incomplete)"
    printf 'pg_config=%s\ncases_sha256=%s\n' "$($pgbin/pg_config --configure)" "$cases_sha"
    printf 'driver_sha256=%s\nsetup_sha256=%s\nsource_patch_sha256=%s\n' "$driver_sha" "$setup_sha" "$source_patch_sha"
    printf 'started_after_install=true\nnetwork_listener=false\nbuild_mode=production\n'
} >"$run_root/manifest.txt"

"$pgbin/psql" -X -v ON_ERROR_STOP=1 -h "$run_root/socket" -d postgres -f "$pgqi4c_repo/tests/phase4c/setup.sql" >"$run_root/setup.log" 2>&1
pgqi4c_build_driver "$pgbin/pg_config" "$run_root/production-driver"
conninfo="host=$run_root/socket dbname=postgres connect_timeout=10 application_name=pgqi4c"
"$run_root/production-driver" "$conninfo" smoke "$run_root/smoke.tsv"
expected_cases=$(awk -F '\t' '!/^#/ && NF && $1!="rollback_reconnect" {print $1}' "$pgqi4c_repo/tests/phase4c/cases.tsv" | sort)
actual_cases=$(awk -F '\t' '$1=="case" && ($3=="PASS" || $3=="NOT_RUN") {print $2}' "$run_root/smoke.tsv" | sort)
test "$actual_cases" = "$expected_cases" || { printf 'scenario completeness failure:\n%s\n' "$actual_cases" >&2; exit 1; }
if test "${PG_QI_REJECT_NOT_RUN:-0}" = 1 && \
    awk -F '\t' '$1=="case" && $3=="NOT_RUN" {found=1} END {exit !found}' "$run_root/smoke.tsv"; then
    echo "production acceptance forbids NOT_RUN cases" >&2
    exit 1
fi
"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 -h "$run_root/socket" -d postgres \
    -c 'SHOW shared_preload_libraries; SHOW pg_qi.enabled; SHOW pg_qi.routing_mode; SHOW duckdb.force_execution;' \
    >"$run_root/runtime-preflight.txt"
pgqi4c_stop "$pgbin" "$run_root"
started=0
test "$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$libdir/pg_qi.so")" = "$pg_qi_pre_sha"
test "$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$libdir/pg_duckdb.so")" = "$pg_duckdb_pre_sha"
sha256sum "$run_root"/manifest.txt "$run_root"/setup.log "$run_root"/smoke.tsv >"$run_root/evidence.sha256"
test -s "$run_root/evidence.sha256" && test -s "$run_root/runtime-preflight.txt"
printf 'PASS\n' >"$run_root/STATUS"
status=0
trap - EXIT INT TERM
echo "$run_root"
