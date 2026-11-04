#!/usr/bin/env bash

set -euo pipefail
export PG_QI_TESTING=1

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}
artifact_root=${WP5_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp5"}

mkdir -p "$artifact_root"

for major in 16 17 18; do
    make -C "$repo_dir" PG_CONFIG="$install_root/$major/bin/pg_config" clean \
        >/dev/null
    make -C "$repo_dir" PG_CONFIG="$install_root/$major/bin/pg_config" -j2 \
        >"$artifact_root/build-pg$major.log" 2>&1
    make -C "$repo_dir" PG_CONFIG="$install_root/$major/bin/pg_config" install \
        >"$artifact_root/install-pg$major.log" 2>&1
done

WP23_ARTIFACT_DIR="$artifact_root" \
    "$repo_dir/scripts/spike1/wp2-wp3-matrix.sh" | tee "$artifact_root/base-matrix.log"

run_dir=$(awk '/\/run\.[[:alnum:]]+$/ {print $0}' "$artifact_root/base-matrix.log" | tail -1)
test -n "$run_dir"

for major in 16 17 18; do
    socket_dir="$run_dir/pg$major/wp5-error-socket"
    data_dir="$run_dir/pg$major/wp5-error-data"
    mkdir -p "$socket_dir"
    "$install_root/$major/bin/initdb" -D "$data_dir" --no-locale --encoding=UTF8 \
        >"$run_dir/pg$major/wp5-error-initdb.log" 2>&1
    {
        echo "unix_socket_directories = '$socket_dir'"
        echo "listen_addresses = ''"
        echo "shared_preload_libraries = 'pg_duckdb,pg_qi'"
    } >>"$data_dir/postgresql.conf"
    "$install_root/$major/bin/pg_ctl" -D "$data_dir" \
        -l "$run_dir/pg$major/wp5-error-server.log" -w start >/dev/null
    cleanup() {
        "$install_root/$major/bin/pg_ctl" -D "$data_dir" -m fast stop \
            >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    "$install_root/$major/bin/psql" -X -A -t -F '|' \
        -h "$socket_dir" -d postgres \
        >"$run_dir/pg$major/wp5-error.sql.log" 2>&1 <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;
SET duckdb.force_execution = off;
SELECT 1 / 0;
SELECT depth, cleanup_broken, unwound, accounting_valid
FROM qi.test_frame_state();
SHOW duckdb.force_execution;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;
SQL

    grep -q 'ERROR:  division by zero' "$run_dir/pg$major/wp5-error.sql.log"
    grep -qx '0|f|1|t' "$run_dir/pg$major/wp5-error.sql.log"
    test "$(grep -cx 'off' "$run_dir/pg$major/wp5-error.sql.log")" -ge 2
    grep -qx '0' "$run_dir/pg$major/wp5-error.sql.log"

    cleanup
    trap - EXIT
    echo "PostgreSQL $major: WP5 protected downstream ERROR PASS"
done

echo "$run_dir"
echo "WP5 matrix PASS: canonical planner wrapper on PostgreSQL 16, 17, and 18"
