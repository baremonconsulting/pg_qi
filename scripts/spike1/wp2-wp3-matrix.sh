#!/usr/bin/env bash

set -euo pipefail

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
artifact_root=${WP23_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp2-wp3"}
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}

mkdir -p "$artifact_root"
run_root=$(mktemp -d "$artifact_root/run.XXXXXX")

for major in 16 17 18; do
    pgroot="$install_root/$major"
    version_dir="$run_root/pg$major"
    mkdir -p "$version_dir"

    for case_name in missing wrong good; do
        data_dir="$version_dir/$case_name-data"
        socket_dir="$version_dir/$case_name-socket"
        mkdir -p "$socket_dir"
        "$pgroot/bin/initdb" -D "$data_dir" --no-locale --encoding=UTF8 \
            >"$version_dir/$case_name-initdb.log" 2>&1
        {
            echo "unix_socket_directories = '$socket_dir'"
            echo "listen_addresses = ''"
            if [ "$case_name" = missing ]; then
                echo "shared_preload_libraries = 'pg_qi'"
            elif [ "$case_name" = wrong ]; then
                echo "shared_preload_libraries = 'pg_qi,pg_duckdb'"
            else
                echo "shared_preload_libraries = 'pg_duckdb,pg_qi'"
            fi
        } >>"$data_dir/postgresql.conf"
    done

    if "$pgroot/bin/pg_ctl" -D "$version_dir/missing-data" \
        -l "$version_dir/missing-server.log" -w start \
        >"$version_dir/missing-start.log" 2>&1; then
        "$pgroot/bin/pg_ctl" -D "$version_dir/missing-data" -m fast stop >/dev/null
        echo "PG$major unexpectedly started without pg_duckdb" >&2
        exit 1
    fi

    if "$pgroot/bin/pg_ctl" -D "$version_dir/wrong-data" \
        -l "$version_dir/wrong-server.log" -w start \
        >"$version_dir/wrong-start.log" 2>&1; then
        "$pgroot/bin/pg_ctl" -D "$version_dir/wrong-data" -m fast stop >/dev/null
        echo "PG$major unexpectedly accepted the wrong preload order" >&2
        exit 1
    fi

    "$pgroot/bin/pg_ctl" -D "$version_dir/good-data" \
        -l "$version_dir/good-server.log" -w start \
        >"$version_dir/good-start.log" 2>&1
    cleanup() {
        "$pgroot/bin/pg_ctl" -D "$version_dir/good-data" -m fast stop \
            >/dev/null 2>&1 || true
    }
    trap cleanup EXIT

    "$pgroot/bin/psql" -X -A -t -F '|' \
        -h "$version_dir/good-socket" -d postgres -v ON_ERROR_STOP=1 \
        >"$version_dir/positive.sql.log" 2>&1 <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
SELECT preflight_passed,
       handle_present IS NULL,
       COALESCE(handle_present, false),
       is_placeholder,
       no_reset,
       provider_name
FROM qi.test_force_meta();
SELECT * FROM qi.test_detector_contract();
SELECT * FROM qi.test_frame_contract();
SELECT depth, cleanup_broken, accounting_valid FROM qi.test_frame_state();
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;
CREATE TABLE spike_small(id integer PRIMARY KEY, v integer);
INSERT INTO spike_small
SELECT i, i * 10 FROM generate_series(1, 100) AS g(i);
SET duckdb.force_execution = on;
SELECT count(*) FROM spike_small;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SQL

    if [ "$major" = 16 ]; then
        grep -qx 't|t|f|f|f|DuckDBScan' "$version_dir/positive.sql.log"
    else
        grep -qx 't|f|t|f|f|DuckDBScan' "$version_dir/positive.sql.log"
    fi
    grep -qx 't|t|t|t' "$version_dir/positive.sql.log"
    grep -qx 't|t|t|t|t|t|t' "$version_dir/positive.sql.log"
    grep -qx '0|f|t' "$version_dir/positive.sql.log"
    grep -qx '0' "$version_dir/positive.sql.log"
    grep -qx 'off' "$version_dir/positive.sql.log"
    grep -qx '100' "$version_dir/positive.sql.log"
    grep -qx 'on' "$version_dir/positive.sql.log"

    make -C "$repo_dir" installcheck \
        PG_CONFIG="$pgroot/bin/pg_config" \
        PGHOST="$version_dir/good-socket" PGUSER="$(id -un)" \
        >"$version_dir/installcheck.log" 2>&1
    grep -q 'All 1 tests passed' "$version_dir/installcheck.log"

    cleanup
    trap - EXIT
    echo "PostgreSQL $major: WP2/WP3 PASS"
done

echo "$run_root"
echo "WP2/WP3 matrix PASS: PostgreSQL 16, 17, and 18"
