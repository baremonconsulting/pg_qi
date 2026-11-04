#!/usr/bin/env bash

set -euo pipefail
export PG_QI_TESTING=1

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}
artifact_root=${WP9_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp9"}
majors=${WP9_MAJORS:-"16 17 18 19"}
skip_build_install=${WP9_SKIP_BUILD_INSTALL:-0}
mkdir -p "$artifact_root"
results="$artifact_root/results.tsv"
: >"$results"
printf 'postgresql\tscenario\tresult\n' >>"$results"

assert_clean() {
    test "$(tail -2 "$1" | head -1)" = "0"
    test "$(tail -1 "$1")" = "off"
}

major_count=0
for major in $majors; do
    major_count=$((major_count + 1))
    pgroot="$install_root/$major"
    if [ "$skip_build_install" != 1 ]; then
        make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" clean >/dev/null
        make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" -j2 >"$artifact_root/build-pg$major.log" 2>&1
        make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" install >"$artifact_root/install-pg$major.log" 2>&1
    fi

    version_dir="$artifact_root/pg$major"
    data_dir="$version_dir/data"
    socket_dir="$version_dir/socket"
    mkdir -p "$socket_dir"
    "$pgroot/bin/initdb" -D "$data_dir" --no-locale --encoding=UTF8 >"$version_dir/initdb.log" 2>&1
    {
        echo "unix_socket_directories = '$socket_dir'"
        echo "listen_addresses = ''"
        echo "shared_preload_libraries = 'pg_duckdb,pg_qi'"
    } >>"$data_dir/postgresql.conf"
    "$pgroot/bin/pg_ctl" -D "$data_dir" -l "$version_dir/server.log" -w start >/dev/null
    cleanup() { "$pgroot/bin/pg_ctl" -D "$data_dir" -m fast stop >/dev/null 2>&1 || true; }
    trap cleanup EXIT
    psql=("$pgroot/bin/psql" -X -A -t -F '|' -h "$socket_dir" -d postgres)

    "${psql[@]}" -v ON_ERROR_STOP=1 >"$version_dir/setup.log" 2>&1 <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;
CREATE TABLE probe AS SELECT i, i % 10 AS v FROM generate_series(1, 1000000) g(i);
CREATE TABLE runtime_zero(v integer);
INSERT INTO runtime_zero VALUES (2),(1),(0);
CREATE VIEW probe_view AS SELECT * FROM probe WHERE v=1;
CREATE OR REPLACE FUNCTION inner_probe() RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE x bigint;
BEGIN
  PERFORM qi.spike_arm_next();
  EXECUTE 'SELECT count(*) FROM probe' INTO x;
  RETURN x;
END$$;
SQL
    make -C "$repo_dir" installcheck PG_CONFIG="$pgroot/bin/pg_config" \
        PGHOST="$socket_dir" PGUSER="$(id -un)" >"$version_dir/installcheck.log" 2>&1
    grep -q 'All 1 tests passed' "$version_dir/installcheck.log"

    "${psql[@]}" >"$version_dir/S0.log" 2>&1 <<'SQL'
SELECT preflight_passed AND NOT is_placeholder AND NOT no_reset AND provider_name='DuckDBScan' FROM qi.test_force_meta();
SHOW pg_qi.spike_previous_planner_hook_present;
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -qx 't' "$version_dir/S0.log"; grep -qx 'on' "$version_dir/S0.log"; assert_clean "$version_dir/S0.log"
    printf '%s\tS0\tPASS\n' "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S1-S2.log" 2>&1 <<'SQL'
SET duckdb.force_execution=off;
SELECT qi.spike_reset_state();
SELECT count(*) FROM probe; SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT qi.spike_arm_next();
SELECT count(*) FROM probe; SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT count(*) FROM probe; SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SHOW pg_qi.spike_consumed_generation; SHOW pg_qi.spike_token_armed; SHOW pg_qi.spike_force_transitions;
SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    test "$(grep -cx 'on' "$version_dir/S1-S2.log")" -eq 1; test "$(grep -cx 'off' "$version_dir/S1-S2.log")" -ge 3; assert_clean "$version_dir/S1-S2.log"
    printf '%s\tS1\tPASS\n%s\tS2\tPASS\n' "$major" "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S3.log" 2>&1 <<'SQL'
SET duckdb.force_execution=off; SELECT qi.spike_reset_state(); BEGIN; SAVEPOINT s;
SELECT qi.spike_arm_next(); SET pg_qi.spike_failpoint=inside_try_after_policy;
SELECT count(*) FROM probe;
ROLLBACK TO SAVEPOINT s;
SHOW pg_qi.spike_frames_unwound; SHOW pg_qi.spike_token_armed; SHOW duckdb.force_execution;
COMMIT; SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -q 'failpoint fired: inside_try_after_policy' "$version_dir/S3.log"; grep -qx '1' "$version_dir/S3.log"; assert_clean "$version_dir/S3.log"
    printf '%s\tS3\tPASS\n' "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S4.log" 2>&1 <<'SQL'
SET duckdb.force_execution=off; SELECT qi.spike_reset_state(); SELECT qi.spike_arm_next();
SELECT 1/0;
SHOW pg_qi.spike_frames_unwound; SHOW duckdb.force_execution;
SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -q 'ERROR:  division by zero' "$version_dir/S4.log"; grep -qx '1' "$version_dir/S4.log"; assert_clean "$version_dir/S4.log"
    printf '%s\tS4\tPASS\n' "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S5.log" 2>&1 <<'SQL'
SELECT qi.spike_reset_state(); SELECT 10/v FROM runtime_zero ORDER BY 1;
SHOW pg_qi.spike_frames_unwound; SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -q 'ERROR:  division by zero' "$version_dir/S5.log"; test "$(grep -cx '0' "$version_dir/S5.log")" -ge 2; assert_clean "$version_dir/S5.log"
    printf '%s\tS5\tPASS\n' "$major" >>"$results"

    for variant in 1 2 3; do
        mode=propagate; inner=off
        if [ "$variant" = 2 ]; then inner=on; fi
        if [ "$variant" = 3 ]; then mode=subxact; inner=on; fi
        "${psql[@]}" >"$version_dir/S6a-$variant.log" 2>&1 <<SQL
SET duckdb.force_execution=off; SELECT qi.spike_reset_state(); SELECT qi.spike_arm_next();
SET pg_qi.spike_reentry_mode=$mode; SET pg_qi.spike_reentry_inner_error=$inner;
SELECT count(*) FROM probe;
SHOW pg_qi.spike_last_plan_has_duckdb_scan; SHOW pg_qi.spike_nested_masks;
SHOW pg_qi.spike_nested_pg_plans; SHOW pg_qi.spike_frames_unwound;
SHOW pg_qi.spike_reentry_active; SHOW duckdb.force_execution;
SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
        assert_clean "$version_dir/S6a-$variant.log"
    done
    grep -qx '1000000' "$version_dir/S6a-1.log"; grep -qx '1' "$version_dir/S6a-1.log"
    grep -q 'synthetic inner planner error' "$version_dir/S6a-2.log"; grep -qx '2' "$version_dir/S6a-2.log"
    grep -qx '1000000' "$version_dir/S6a-3.log"; grep -qx '1' "$version_dir/S6a-3.log"
    printf '%s\tS6a-1\tPASS\n%s\tS6a-2\tPASS\n%s\tS6a-3\tPASS\n' "$major" "$major" "$major" >>"$results"

    for unsafe in false true; do
        "${psql[@]}" >"$version_dir/S6b-$unsafe.log" 2>&1 <<SQL
SELECT qi.spike_reset_state(); SET duckdb.unsafe_allow_execution_inside_functions=$unsafe;
SELECT inner_probe(); SHOW pg_qi.spike_duckdb_plans; SHOW pg_qi.spike_consumed_generation;
SHOW pg_qi.spike_force_transitions; SHOW pg_qi.spike_last_plan_has_duckdb_scan; SHOW pg_qi.spike_frames_unwound;
SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
        grep -qx '1000000' "$version_dir/S6b-$unsafe.log"; assert_clean "$version_dir/S6b-$unsafe.log"
        grep -qx '1' "$version_dir/S6b-$unsafe.log"
        grep -qx 'off' "$version_dir/S6b-$unsafe.log"
    done
    test "$(awk '/^1000000$/{getline; print; exit}' "$version_dir/S6b-false.log")" = 0
    unsafe_result=$(awk '/^1000000$/{getline; print; exit}' "$version_dir/S6b-true.log")
    test "$unsafe_result" = 0 || test "$unsafe_result" = 1
    printf '%s\tS6b-1\tPASS\n%s\tS6b-2\tPASS\n' "$major" "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S7.log" 2>&1 <<'SQL'
SET duckdb.force_execution=off; SELECT qi.spike_reset_state(); SET plan_cache_mode=force_custom_plan;
PREPARE c(int) AS SELECT count(*) FROM probe WHERE v=$1;
SELECT qi.spike_arm_next(); EXECUTE c(1); SHOW pg_qi.spike_last_plan_has_duckdb_scan;
EXECUTE c(2); SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT name,generic_plans,custom_plans FROM pg_prepared_statements WHERE name='c';
DEALLOCATE c; SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -qx 'c|0|2' "$version_dir/S7.log"; test "$(grep -cx 'on' "$version_dir/S7.log")" -eq 1; assert_clean "$version_dir/S7.log"
    printf '%s\tS7\tPASS\n' "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S8.log" 2>&1 <<'SQL'
SET duckdb.force_execution=off; SELECT qi.spike_reset_state(); SET plan_cache_mode=force_generic_plan;
PREPARE g(int) AS SELECT count(*) FROM probe WHERE v=$1;
SELECT qi.spike_arm_next(); EXECUTE g(1); SHOW pg_qi.spike_last_plan_has_duckdb_scan;
EXECUTE g(2); SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT name,generic_plans,custom_plans FROM pg_prepared_statements WHERE name='g';
ALTER TABLE probe ADD COLUMN invalidator integer;
EXECUTE g(3); SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SELECT name,generic_plans,custom_plans FROM pg_prepared_statements WHERE name='g';
DEALLOCATE g; SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -qx 'g|2|0' "$version_dir/S8.log"; grep -qx 'g|3|0' "$version_dir/S8.log"; test "$(grep -cx 'on' "$version_dir/S8.log")" -eq 2; assert_clean "$version_dir/S8.log"
    printf '%s\tS8\tPASS\n' "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S9.log" 2>&1 <<'SQL'
SELECT qi.spike_reset_state(); SET duckdb.force_execution=on; SELECT count(*) FROM probe;
SHOW pg_qi.spike_last_force_origin; SHOW pg_qi.spike_last_effective_origin; SHOW pg_qi.spike_last_delegation_requested; SHOW pg_qi.spike_last_guc_changed;
SET duckdb.force_execution=off; SELECT qi.spike_arm_next(); SELECT count(*) FROM probe;
SHOW pg_qi.spike_last_force_origin; SHOW pg_qi.spike_last_effective_origin; SHOW pg_qi.spike_last_delegation_requested; SHOW pg_qi.spike_last_guc_changed;
SELECT qi.spike_arm_next(); SET pg_qi.spike_reentry_mode=propagate; SELECT count(*) FROM probe;
SHOW pg_qi.spike_last_force_origin; SHOW pg_qi.spike_last_effective_origin; SHOW pg_qi.spike_last_delegation_requested; SHOW pg_qi.spike_last_guc_changed;
SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -qx 'external' "$version_dir/S9.log"; grep -qx 'qi_stack' "$version_dir/S9.log"; assert_clean "$version_dir/S9.log"
    printf '%s\tS9\tPASS\n' "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S10.log" 2>&1 <<'SQL'
SELECT qi.spike_reset_state(); EXPLAIN SELECT count(*) FROM probe; EXPLAIN ANALYZE SELECT count(*) FROM probe;
BEGIN; DECLARE c CURSOR FOR SELECT * FROM probe LIMIT 2; FETCH ALL FROM c; CLOSE c; COMMIT;
SELECT count(*) FROM probe_view; SELECT 1; SELECT 2;
CREATE TABLE ctas_probe AS SELECT * FROM probe LIMIT 10; DROP TABLE ctas_probe;
SELECT qi.test_active_control_guard();
SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -qx 't' "$version_dir/S10.log"; assert_clean "$version_dir/S10.log"
    printf '%s\tS10\tPASS\n' "$major" >>"$results"

    "${psql[@]}" >"$version_dir/S11.log" 2>&1 <<'SQL'
SET duckdb.force_execution=off; SELECT qi.spike_reset_state(); SELECT qi.spike_arm_next(); SELECT count(*) FROM probe; SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SET max_parallel_workers_per_gather=4; SET min_parallel_table_scan_size=0; SET parallel_setup_cost=0; SET parallel_tuple_cost=0;
EXPLAIN (COSTS OFF) SELECT (o).pid,(o).parallel_worker,(o).depth,(o).cleanup_broken,(o).token_armed,count(*) FROM (SELECT qi.spike_observer() o FROM probe OFFSET 0) s GROUP BY 1,2,3,4,5;
SELECT (o).pid,(o).parallel_worker,(o).depth,(o).cleanup_broken,(o).token_armed,count(*) FROM (SELECT qi.spike_observer() o FROM probe OFFSET 0) s GROUP BY 1,2,3,4,5 ORDER BY 1;
SHOW pg_qi.spike_stack_depth; SHOW pg_qi.spike_cleanup_broken;
SQL
    grep -q 'Parallel Seq Scan' "$version_dir/S11.log"; grep -Eq '^[0-9]+\|t\|0\|f\|f\|' "$version_dir/S11.log"; grep -qx 'on' "$version_dir/S11.log"; assert_clean "$version_dir/S11.log"
    printf '%s\tS11\tPASS\n' "$major" >>"$results"

    cleanup; trap - EXIT
    echo "PostgreSQL $major: S0-S11 PASS"
done

test "$(grep -c $'\tPASS$' "$results")" -eq "$((16 * major_count))"
echo "$results"
echo "WP9 matrix PASS: all 16 scenario rows on PostgreSQL $majors"
