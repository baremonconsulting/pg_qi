#!/usr/bin/env bash

set -euo pipefail
source "$(dirname "$0")/common.sh"

major=${1:?usage: run-rollback-drill.sh PG_MAJOR [ARTIFACT_PARENT]}
case "$major" in 16|17|18|19) ;; *) echo "unsupported PostgreSQL major: $major" >&2; exit 2;; esac
pgbin=${PG_INSTALL_ROOT:-/usr/lib/postgresql}/$major/bin
parent=${2:-${RUNNER_TEMP:-/tmp}/pgqi4c-rollback}
parent=$(realpath -m -- "$parent")
pgqi4c_require_safe_root "$parent/probe"
mkdir -m 700 -p "$parent"
pgqi4c_require_private_parent "$parent"
root="$parent/run-pg${major}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
pgqi4c_sanitize_connection_env
installed="$($pgbin/pg_config --pkglibdir)/pg_qi.so"
installed_duckdb="$($pgbin/pg_config --pkglibdir)/pg_duckdb.so"
test -f "$installed" || { echo "pg_qi production library is not installed" >&2; exit 1; }
test -f "$installed_duckdb" || { echo "pg_duckdb library is not installed" >&2; exit 1; }
if strings "$installed" | grep -E 'pg_qi\.spike_|pg_qi\.test_|qi_spike_|qi_test_|PG_QI_FINAL' >/dev/null; then echo "test-only installed library" >&2; exit 1; fi
installed_pre_sha=$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$installed")
duckdb_pre_sha=$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$installed_duckdb")
started=0
drill_status=1
cleanup() { local rc=$?; if test "$started" -eq 1 && ! pgqi4c_stop "$pgbin" "$root"; then printf 'INCOMPLETE cleanup_failed\n' >"$root/STATUS"; rc=1; fi; test "$drill_status" -eq 0 || printf 'INCOMPLETE\n' >"$root/STATUS"; exit "$rc"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
started=1
pgqi4c_start "$pgbin" "$root"
psql=("$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 -h "$root/socket" -d postgres)
"${psql[@]}" -f "$pgqi4c_repo/tests/phase4c/setup.sql" >"$root/setup.log" 2>&1
pgqi4c_build_driver "$pgbin/pg_config" "$root/production-driver"
"$root/production-driver" "host=$root/socket dbname=postgres connect_timeout=10 application_name=pgqi4c_rollback" rollback "$root/session-disable.txt"

reconnect=$("${psql[@]}" -c 'SHOW pg_qi.routing_mode; SHOW duckdb.force_execution;')
test "$reconnect" = $'off\noff'
printf '%s\n' "$reconnect" >"$root/reconnect.txt"
{
 printf 'run_id=%s\npostgresql=%s\npg_config=%s\n' "$(basename "$root")" "$($pgbin/postgres --version)" "$($pgbin/pg_config --configure)"
 printf 'source_commit=%s\nsource_dirty=%s\n' "$(git -C "$pgqi4c_repo" rev-parse HEAD)" "$(test -z "$(git -C "$pgqi4c_repo" status --porcelain)" && echo false || echo true)"
 printf 'pg_qi_installed=%s\npg_qi_sha256=%s\npg_duckdb_sha256=%s\nbuild_mode=production_boundary_checked\n' "$installed" "$installed_pre_sha" "$duckdb_pre_sha"
 if test -d "${RUNNER_TEMP:-}/pg_duckdb-$major/.git"; then
   printf 'observed_pg_duckdb_source_sha=%s\nobserved_duckdb_source_sha=%s\n' "$(git -C "$RUNNER_TEMP/pg_duckdb-$major" rev-parse HEAD)" "$(git -C "$RUNNER_TEMP/pg_duckdb-$major/third_party/duckdb" rev-parse HEAD)"
 else
   printf 'observed_pg_duckdb_source_sha=unknown\nobserved_duckdb_source_sha=unknown\n'
 fi
} >"$root/manifest.txt"
pgqi4c_stop "$pgbin" "$root"
started=0
test "$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$installed")" = "$installed_pre_sha"
test "$(bash "$pgqi4c_repo/.github/scripts/ci-sha256.sh" "$installed_duckdb")" = "$duckdb_pre_sha"
sha256sum "$root"/*.txt "$root/manifest.txt" >"$root/evidence.sha256"
test -s "$root/evidence.sha256" && test -s "$root/session-disable.txt"
printf 'PASS\n' >"$root/STATUS"
drill_status=0
trap - EXIT INT TERM
echo "$root"
