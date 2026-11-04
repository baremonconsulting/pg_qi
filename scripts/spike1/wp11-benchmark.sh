#!/usr/bin/env bash

set -euo pipefail
export PG_QI_TESTING=1

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
install_root=${PG_INSTALL_ROOT:-/home/adam/.local/pgsql}
artifact_root=${WP11_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp11"}
requests=${WP11_REQUESTS:-500}
warmup=${WP11_WARMUP:-25}
majors=${WP11_MAJORS:-"16 17 18"}
skip_build_install=${WP11_SKIP_BUILD_INSTALL:-0}
mkdir -p "$artifact_root"

results="$artifact_root/results.csv"
printf 'postgresql,mode,path,requests,failed,request_p50_us,request_p95_us,request_p99_us,planner_entries,planner_total_us,planner_us_per_entry,generic_plans,custom_plans,duckdb_plans\n' >"$results"

percentile() {
    local file=$1 pct=$2 count index
    count=$(wc -l <"$file")
    index=$(((count * pct + 99) / 100))
    sort -n "$file" | sed -n "${index}p"
}

field_value() {
    local line=$1 key=$2 token
    for token in $line; do
        case "$token" in "$key"=*) printf '%s\n' "${token#*=}"; return;; esac
    done
    return 1
}

major_count=0
for major in $majors; do
    major_count=$((major_count + 1))
    pgroot="$install_root/$major"
    configure=$($pgroot/bin/pg_config --configure)
    case "$configure" in *--enable-cassert*) echo "PG $major is not a performance build" >&2; exit 1;; esac

    version_dir="$artifact_root/pg$major"
    data_dir="$version_dir/data"
    socket_dir="$version_dir/socket"
    mkdir -p "$version_dir" "$socket_dir"
    case "$artifact_root" in ""|/) echo "Unsafe WP11 artifact root" >&2; exit 1;; esac
    case "$data_dir" in "$artifact_root"/pg*/data) ;; *) exit 1;; esac
    rm -rf "$data_dir"

    if [ "$skip_build_install" != 1 ]; then
        make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" clean >/dev/null
        make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" -j2 >"$version_dir/build.log" 2>&1
        make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" install >"$version_dir/install.log" 2>&1
    fi
    cc -O2 -Wall -Wextra -Werror \
       -I"$($pgroot/bin/pg_config --includedir)" \
       "$repo_dir/scripts/spike1/wp11-bench-driver.c" \
       -L"$($pgroot/bin/pg_config --libdir)" \
       -Wl,-rpath,"$($pgroot/bin/pg_config --libdir)" -lpq \
       -o "$version_dir/wp11-bench-driver"

    "$pgroot/bin/initdb" -D "$data_dir" --no-locale --encoding=UTF8 >"$version_dir/initdb.log" 2>&1
    {
        echo "unix_socket_directories = '$socket_dir'"
        echo "listen_addresses = ''"
        echo "shared_preload_libraries = 'pg_duckdb,pg_qi'"
        echo "max_parallel_workers_per_gather = 0"
        echo "jit = off"
        echo "track_io_timing = off"
        echo "log_line_prefix = '%m [%p] %a '"
    } >>"$data_dir/postgresql.conf"
    server_log="$version_dir/server.log"
    : >"$server_log"
    "$pgroot/bin/pg_ctl" -D "$data_dir" -l "$server_log" -w start >/dev/null
    cleanup() { "$pgroot/bin/pg_ctl" -D "$data_dir" -m fast stop >/dev/null 2>&1 || true; }
    trap cleanup EXIT

    "$pgroot/bin/psql" -X -v ON_ERROR_STOP=1 -h "$socket_dir" -d postgres >"$version_dir/setup.log" 2>&1 <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;
CREATE TABLE qi_bench_t AS SELECT i AS id, i % 100 AS v FROM generate_series(1,10000) AS g(i);
ANALYZE qi_bench_t;
SQL

    for mode in bypass frame_only shadow_save delegate; do
        for path in simple custom generic; do
            run_dir="$version_dir/$mode-$path"
            mkdir -p "$run_dir"
            app="wp11_${major}_${mode}_${path}"
            conninfo="host=$socket_dir dbname=postgres"

            PGAPPNAME="${app}_warmup" \
            PGOPTIONS="-c pg_qi.spike_benchmark_mode=$mode" \
                "$version_dir/wp11-bench-driver" "$conninfo" "$path" "$warmup" \
                "$run_dir/warmup-us.txt" >"$run_dir/warmup-meta.txt" 2>"$run_dir/warmup-error.log"

            PGAPPNAME="$app" \
            PGOPTIONS="-c pg_qi.spike_benchmark_mode=$mode" \
                "$version_dir/wp11-bench-driver" "$conninfo" "$path" "$requests" \
                "$run_dir/latency-us.txt" >"$run_dir/plan-cache.txt" 2>"$run_dir/error.log"

            final_line=$(grep "PG_QI_FINAL app=$app " "$server_log" | tail -1)
            entries=$(field_value "$final_line" planner_entries)
            planner_us=$(field_value "$final_line" planner_us)
            duckdb_plans=$(field_value "$final_line" duckdb_plans)
            test "$entries" -gt 0
            per_entry=$(awk -v total="$planner_us" -v n="$entries" 'BEGIN {printf "%.3f", total/n}')
            p50=$(percentile "$run_dir/latency-us.txt" 50)
            p95=$(percentile "$run_dir/latency-us.txt" 95)
            p99=$(percentile "$run_dir/latency-us.txt" 99)
            generic=$(sed -n 's/.*generic=\([0-9]*\).*/\1/p' "$run_dir/plan-cache.txt")
            custom=$(sed -n 's/.*custom=\([0-9]*\).*/\1/p' "$run_dir/plan-cache.txt")
            test -n "$generic"; test -n "$custom"

            case "$path" in
                simple) test "$generic" -eq 0; test "$custom" -eq 0;;
                custom) test "$generic" -eq 0; test "$custom" -eq "$requests";;
                generic) test "$generic" -eq "$requests"; test "$custom" -eq 0;;
            esac
            if [ "$mode" = delegate ]; then
                if [ "$path" = generic ]; then
                    test "$duckdb_plans" -eq 1
                else
                    test "$duckdb_plans" -eq "$requests"
                fi
            else
                test "$duckdb_plans" -eq 0
            fi

            printf '%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
                "$major" "$mode" "$path" "$requests" "$p50" "$p95" "$p99" \
                "$entries" "$planner_us" "$per_entry" "$generic" "$custom" \
                "$duckdb_plans" >>"$results"
            echo "PG$major $mode/$path PASS"
        done
    done

    cleanup
    trap - EXIT
done

expected_runs=$((12 * major_count))
test "$(($(wc -l <"$results") - 1))" -eq "$expected_runs"
echo "WP11 benchmark PASS: $expected_runs/$expected_runs runs"
echo "$results"
