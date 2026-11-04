#!/usr/bin/env bash

set -euo pipefail
export PG_QI_TESTING=1

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}
artifact_root=${WP6_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp6"}

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
    socket_dir="$run_dir/pg$major/wp6-socket"
    data_dir="$run_dir/pg$major/wp6-data"
    mkdir -p "$socket_dir"
    "$install_root/$major/bin/initdb" -D "$data_dir" --no-locale --encoding=UTF8 \
        >"$run_dir/pg$major/wp6-initdb.log" 2>&1
    {
        echo "unix_socket_directories = '$socket_dir'"
        echo "listen_addresses = ''"
        echo "shared_preload_libraries = 'pg_duckdb,pg_qi'"
    } >>"$data_dir/postgresql.conf"
    "$install_root/$major/bin/pg_ctl" -D "$data_dir" \
        -l "$run_dir/pg$major/wp6-server.log" -w start >/dev/null
    cleanup() {
        "$install_root/$major/bin/pg_ctl" -D "$data_dir" -m fast stop \
            >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    "$install_root/$major/bin/psql" -X -A -t -F '|' \
        -h "$socket_dir" -d postgres \
        >"$run_dir/pg$major/wp6.sql.log" 2>&1 <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;
CREATE TABLE token_probe(id integer PRIMARY KEY, v integer);
INSERT INTO token_probe SELECT i, i * 10 FROM generate_series(1, 100) g(i);
SET duckdb.force_execution = off;
SELECT qi.spike_reset_token();
SELECT qi.spike_arm_next();
SELECT count(*) FROM token_probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT count(*) FROM token_probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT * FROM qi.test_token_state();
BEGIN;
SELECT qi.spike_arm_next();
SELECT 1 / 0;
ROLLBACK;
SELECT * FROM qi.test_token_state();
SELECT count(*) FROM token_probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
BEGIN;
SELECT qi.spike_arm_next();
SAVEPOINT armed;
SELECT 1 / 0;
ROLLBACK TO SAVEPOINT armed;
SELECT * FROM qi.test_token_state();
SELECT count(*) FROM token_probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
COMMIT;
SELECT qi.spike_arm_next();
SELECT qi.spike_disarm();
SELECT * FROM qi.test_token_state();
SELECT count(*) FROM token_probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SHOW duckdb.force_execution;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;
SQL

    test "$(grep -cx '100' "$run_dir/pg$major/wp6.sql.log")" -eq 5
    test "$(grep -cx 'on' "$run_dir/pg$major/wp6.sql.log")" -eq 1
    test "$(grep -cx 'off' "$run_dir/pg$major/wp6.sql.log")" -ge 4
    grep -qx '1|1|f' "$run_dir/pg$major/wp6.sql.log"
    grep -qx '2|2|f' "$run_dir/pg$major/wp6.sql.log"
    grep -qx '3|3|f' "$run_dir/pg$major/wp6.sql.log"
    grep -qx '4|4|f' "$run_dir/pg$major/wp6.sql.log"
    test "$(grep -c 'ERROR:  division by zero' "$run_dir/pg$major/wp6.sql.log")" -eq 2
    grep -qx '0' "$run_dir/pg$major/wp6.sql.log"

    cleanup
    trap - EXIT
    echo "PostgreSQL $major: WP6 one-shot and rollback persistence PASS"
done

echo "$run_dir"
echo "WP6 matrix PASS: backend-local one-shot token on PostgreSQL 16, 17, and 18"
