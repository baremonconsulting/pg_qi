#!/usr/bin/env bash

set -euo pipefail
export PG_QI_TESTING=1

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}
artifact_root=${WP7_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp7"}

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
    socket_dir="$run_dir/pg$major/wp7-socket"
    data_dir="$run_dir/pg$major/wp7-data"
    mkdir -p "$socket_dir"
    "$install_root/$major/bin/initdb" -D "$data_dir" --no-locale --encoding=UTF8 \
        >"$run_dir/pg$major/wp7-initdb.log" 2>&1
    {
        echo "unix_socket_directories = '$socket_dir'"
        echo "listen_addresses = ''"
        echo "shared_preload_libraries = 'pg_duckdb,pg_qi'"
    } >>"$data_dir/postgresql.conf"
    "$install_root/$major/bin/pg_ctl" -D "$data_dir" \
        -l "$run_dir/pg$major/wp7-server.log" -w start >/dev/null
    cleanup() {
        "$install_root/$major/bin/pg_ctl" -D "$data_dir" -m fast stop \
            >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    "$install_root/$major/bin/psql" -X -A -t -F '|' \
        -h "$socket_dir" -d postgres -v ON_ERROR_STOP=1 \
        >"$run_dir/pg$major/wp7-setup.sql.log" 2>&1 <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;
CREATE TABLE reentry_probe(id integer PRIMARY KEY, v integer);
INSERT INTO reentry_probe SELECT i, i * 10 FROM generate_series(1, 100) g(i);
SQL

    "$install_root/$major/bin/psql" -X -A -t -F '|' \
        -h "$socket_dir" -d postgres \
        >"$run_dir/pg$major/s6a1.sql.log" 2>&1 <<'SQL'
SET duckdb.force_execution = off;
SET pg_qi.spike_reentry_mode = propagate;
SET pg_qi.spike_reentry_inner_error = off;
SELECT qi.spike_reset_token();
SELECT qi.spike_arm_next();
SELECT count(*) FROM reentry_probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT depth, cleanup_broken, nested_masks, nested_pg_plans,
       nested_duckdb_plans, unwound, accounting_valid
FROM qi.test_frame_state();
SHOW pg_qi.spike_reentry_active;
SHOW duckdb.force_execution;
SQL

    grep -qx '100' "$run_dir/pg$major/s6a1.sql.log"
    grep -qx 'on' "$run_dir/pg$major/s6a1.sql.log"
    grep -qx '0|f|1|1|0|0|t' "$run_dir/pg$major/s6a1.sql.log"
    test "$(grep -cx 'off' "$run_dir/pg$major/s6a1.sql.log")" -ge 2

    "$install_root/$major/bin/psql" -X -A -t -F '|' \
        -h "$socket_dir" -d postgres \
        >"$run_dir/pg$major/s6a2.sql.log" 2>&1 <<'SQL'
SET duckdb.force_execution = off;
SET pg_qi.spike_reentry_mode = propagate;
SET pg_qi.spike_reentry_inner_error = on;
SELECT qi.spike_reset_token();
SELECT qi.spike_arm_next();
SELECT count(*) FROM reentry_probe;
SELECT depth, cleanup_broken, nested_masks, nested_pg_plans,
       nested_duckdb_plans, unwound, accounting_valid
FROM qi.test_frame_state();
SHOW pg_qi.spike_reentry_active;
SHOW duckdb.force_execution;
SQL

    grep -q 'ERROR:  pg_qi synthetic inner planner error' \
        "$run_dir/pg$major/s6a2.sql.log"
    grep -qx '0|f|1|0|0|2|t' "$run_dir/pg$major/s6a2.sql.log"
    test "$(grep -cx 'off' "$run_dir/pg$major/s6a2.sql.log")" -ge 2

    "$install_root/$major/bin/psql" -X -A -t -F '|' \
        -h "$socket_dir" -d postgres \
        >"$run_dir/pg$major/s6a3.sql.log" 2>&1 <<'SQL'
SET duckdb.force_execution = off;
SET pg_qi.spike_reentry_mode = subxact;
SET pg_qi.spike_reentry_inner_error = on;
SELECT qi.spike_reset_token();
SELECT qi.spike_arm_next();
SELECT count(*) FROM reentry_probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT depth, cleanup_broken, nested_masks, nested_pg_plans,
       nested_duckdb_plans, unwound, accounting_valid
FROM qi.test_frame_state();
SHOW pg_qi.spike_reentry_active;
SHOW duckdb.force_execution;
SQL

    grep -qx '100' "$run_dir/pg$major/s6a3.sql.log"
    grep -qx 'on' "$run_dir/pg$major/s6a3.sql.log"
    grep -qx '0|f|1|0|0|1|t' "$run_dir/pg$major/s6a3.sql.log"
    test "$(grep -cx 'off' "$run_dir/pg$major/s6a3.sql.log")" -ge 2

    cleanup
    trap - EXIT
    echo "PostgreSQL $major: WP7 S6a-1/S6a-2/S6a-3 PASS"
done

echo "$run_dir"
echo "WP7 matrix PASS: synthetic planner reentry on PostgreSQL 16, 17, and 18"
