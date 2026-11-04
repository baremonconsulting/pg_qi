#!/usr/bin/env bash

set -euo pipefail
export PG_QI_TESTING=1

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}
artifact_root=${WP8_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp8"}

mkdir -p "$artifact_root"

for major in 16 17 18; do
    make -C "$repo_dir" PG_CONFIG="$install_root/$major/bin/pg_config" clean >/dev/null
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
    socket_dir="$run_dir/pg$major/wp8-socket"
    data_dir="$run_dir/pg$major/wp8-data"
    mkdir -p "$socket_dir"
    "$install_root/$major/bin/initdb" -D "$data_dir" --no-locale --encoding=UTF8 \
        >"$run_dir/pg$major/wp8-initdb.log" 2>&1
    {
        echo "unix_socket_directories = '$socket_dir'"
        echo "listen_addresses = ''"
        echo "shared_preload_libraries = 'pg_duckdb,pg_qi'"
    } >>"$data_dir/postgresql.conf"
    "$install_root/$major/bin/pg_ctl" -D "$data_dir" \
        -l "$run_dir/pg$major/wp8-server.log" -w start >/dev/null
    cleanup() {
        "$install_root/$major/bin/pg_ctl" -D "$data_dir" -m fast stop >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    "$install_root/$major/bin/psql" -X -A -t -F '|' -h "$socket_dir" \
        -d postgres -v ON_ERROR_STOP=1 >"$run_dir/pg$major/wp8-setup.sql.log" 2>&1 <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;
CREATE TABLE diag_probe(id integer PRIMARY KEY, v integer);
INSERT INTO diag_probe SELECT i, i * 10 FROM generate_series(1, 100) g(i);
SQL

    "$install_root/$major/bin/psql" -X -A -t -F '|' -h "$socket_dir" \
        -d postgres >"$run_dir/pg$major/diagnostics.sql.log" 2>&1 <<'SQL'
SELECT qi.spike_reset_state();
SHOW pg_qi.spike_frames_pushed;
SHOW pg_qi.spike_frames_popped;
SELECT 42;
SHOW pg_qi.spike_frames_pushed;
SHOW pg_qi.spike_frames_popped;
SELECT * FROM qi.spike_state();
SHOW pg_qi.spike_frames_pushed;
SHOW pg_qi.spike_frames_popped;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;
SHOW pg_qi.spike_armed_generation;
SHOW pg_qi.spike_consumed_generation;
SHOW pg_qi.spike_token_armed;
SQL
    grep -qx '0|f|1|1|0|0|0|0|0|0|0|f|f|t' "$run_dir/pg$major/diagnostics.sql.log"
    test "$(grep -cx '0' "$run_dir/pg$major/diagnostics.sql.log")" -ge 5
    test "$(grep -cx '1' "$run_dir/pg$major/diagnostics.sql.log")" -eq 2
    test "$(grep -cx '2' "$run_dir/pg$major/diagnostics.sql.log")" -eq 2

    "$install_root/$major/bin/psql" -X -A -t -F '|' -h "$socket_dir" \
        -d postgres >"$run_dir/pg$major/before-push.sql.log" 2>&1 <<'SQL'
SELECT qi.spike_reset_state();
SET pg_qi.spike_failpoint_nth = 2;
SET pg_qi.spike_failpoint = before_push;
SELECT 1;
SELECT 2;
SHOW pg_qi.spike_failpoint;
SHOW pg_qi.spike_failpoint_fired;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_frames_unwound;
SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -q 'ERROR:  pg_qi failpoint fired: before_push' "$run_dir/pg$major/before-push.sql.log"
    grep -qx 'off' "$run_dir/pg$major/before-push.sql.log"
    test "$(grep -cx '0' "$run_dir/pg$major/before-push.sql.log")" -ge 2

    for point in inside_try_before_policy inside_try_before_downstream; do
        "$install_root/$major/bin/psql" -X -A -t -F '|' -h "$socket_dir" \
            -d postgres >"$run_dir/pg$major/$point.sql.log" 2>&1 <<SQL
SELECT qi.spike_reset_state();
SET pg_qi.spike_failpoint_nth = 1;
SET pg_qi.spike_failpoint = $point;
SELECT 1;
SHOW pg_qi.spike_failpoint;
SHOW pg_qi.spike_frames_unwound;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;
SQL
        grep -q "ERROR:  pg_qi failpoint fired: $point" "$run_dir/pg$major/$point.sql.log"
        grep -qx 'off' "$run_dir/pg$major/$point.sql.log"
        grep -qx '1' "$run_dir/pg$major/$point.sql.log"
    done

    "$install_root/$major/bin/psql" -X -A -t -F '|' -h "$socket_dir" \
        -d postgres >"$run_dir/pg$major/after-policy.sql.log" 2>&1 <<'SQL'
SET duckdb.force_execution = off;
SELECT qi.spike_reset_state();
SELECT qi.spike_arm_next();
SET pg_qi.spike_failpoint = inside_try_after_policy;
SELECT count(*) FROM diag_probe;
SHOW pg_qi.spike_failpoint;
SHOW pg_qi.spike_frames_unwound;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;
SHOW duckdb.force_execution;
SHOW pg_qi.spike_token_armed;
SQL
    grep -q 'ERROR:  pg_qi failpoint fired: inside_try_after_policy' "$run_dir/pg$major/after-policy.sql.log"
    test "$(grep -cx 'off' "$run_dir/pg$major/after-policy.sql.log")" -ge 3
    grep -qx '1' "$run_dir/pg$major/after-policy.sql.log"

    "$install_root/$major/bin/psql" -X -A -t -F '|' -h "$socket_dir" \
        -d postgres >"$run_dir/pg$major/inner-planner.sql.log" 2>&1 <<'SQL'
SET duckdb.force_execution = off;
SELECT qi.spike_reset_state();
SELECT qi.spike_arm_next();
SET pg_qi.spike_reentry_mode = propagate;
SET pg_qi.spike_failpoint = inner_planner;
SELECT count(*) FROM diag_probe;
SHOW pg_qi.spike_failpoint;
SHOW pg_qi.spike_frames_unwound;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_reentry_active;
SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -q 'ERROR:  pg_qi failpoint fired: inner_planner' "$run_dir/pg$major/inner-planner.sql.log"
    grep -qx '2' "$run_dir/pg$major/inner-planner.sql.log"
    test "$(grep -cx 'off' "$run_dir/pg$major/inner-planner.sql.log")" -ge 3

    "$install_root/$major/bin/psql" -X -A -t -F '|' -h "$socket_dir" \
        -d postgres >"$run_dir/pg$major/subtransaction-body.sql.log" 2>&1 <<'SQL'
SET duckdb.force_execution = off;
SELECT qi.spike_reset_state();
SELECT qi.spike_arm_next();
SET pg_qi.spike_reentry_mode = subxact;
SET pg_qi.spike_failpoint = subtransaction_body;
SELECT count(*) FROM diag_probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SHOW pg_qi.spike_failpoint;
SHOW pg_qi.spike_failpoint_fired;
SHOW pg_qi.spike_frames_unwound;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_reentry_active;
SHOW pg_qi.spike_cleanup_broken;
SHOW duckdb.force_execution;
SQL
    grep -qx '100' "$run_dir/pg$major/subtransaction-body.sql.log"
    grep -qx 'on' "$run_dir/pg$major/subtransaction-body.sql.log"
    test "$(grep -cx 'off' "$run_dir/pg$major/subtransaction-body.sql.log")" -ge 4
    grep -qx '1' "$run_dir/pg$major/subtransaction-body.sql.log"

    cleanup
    trap - EXIT
    echo "PostgreSQL $major: WP8 diagnostics and six protected failpoints PASS"
done

echo "$run_dir"
echo "WP8 matrix PASS: diagnostics and failpoints on PostgreSQL 16, 17, and 18"
