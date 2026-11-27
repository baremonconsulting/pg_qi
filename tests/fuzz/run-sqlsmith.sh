#!/usr/bin/env bash

set -euo pipefail
umask 077

artifacts=$(realpath -m "${1:?Evidence directory is required}")
pg_config=${PG_CONFIG:?PG_CONFIG is required}
pgbin=$("$pg_config" --bindir)
repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
source "$repo_dir/deps/pg_duckdb.env"
mkdir -p "$artifacts"
scratch=$(mktemp -d "${RUNNER_TEMP:-/tmp}/pgqi-sqlsmith.XXXXXXXX")
data=$scratch/data
socket=$scratch/socket
port=55418
pguser=$(id -un)
psql=("$pgbin/psql" -X -v ON_ERROR_STOP=1 -h "$socket" -p "$port" -U "$pguser")
target="host='$socket' port=$port dbname=qi_fuzz user='$pguser'"

stop_server() {
    if [ -f "$data/postmaster.pid" ]; then
        "$pgbin/pg_ctl" -D "$data" -m fast -w stop
    fi
}
trap stop_server EXIT
mkdir "$socket"
"$pgbin/initdb" -D "$data" --auth=trust --no-locale --encoding=UTF8 --no-sync >"$artifacts/initdb.log"
{
    printf "unix_socket_directories = '%s'\n" "$socket"
    printf "port = %s\n" "$port"
    printf "listen_addresses = ''\n"
    printf "shared_preload_libraries = 'pg_duckdb,pg_qi'\n"
} >>"$data/postgresql.conf"
"$pgbin/pg_ctl" -D "$data" -l "$artifacts/server.log" -w start

"${psql[@]}" -d postgres <<'SQL'
CREATE DATABASE qi_fuzz;
ALTER DATABASE qi_fuzz SET statement_timeout = '10s';
SQL

cat >"$artifacts/schema.sql" <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;

CREATE TABLE observations (
    bucket int4,
    sample_id int8,
    report_date date,
    sampled_at timestamp,
    active bool,
    label text
);
INSERT INTO observations
SELECT i % 5, i % 19, DATE '2026-01-01' + i % 13,
       TIMESTAMP '2026-01-01' + (i % 23) * INTERVAL '1 hour',
       i % 2 = 0, CASE WHEN i % 7 <> 0 THEN 'label-' || i % 3 END
FROM generate_series(1, 64) AS samples(i);
INSERT INTO observations
SELECT * FROM observations WHERE bucket = 0;

CREATE TABLE labels (bucket int4, description text);
INSERT INTO labels VALUES (0, 'first'), (1, NULL), (2, 'second'), (2, 'second');
ANALYZE;
SQL
"${psql[@]}" -d qi_fuzz -f "$artifacts/schema.sql"

{
    printf 'pg_qi=%s\npg_duckdb=%s\nduckdb=%s\n' \
        "${PG_QI_BUILD_ID:?PG_QI_BUILD_ID is required}" "$PG_DUCKDB_SHA" "$DUCKDB_SHA"
    printf '%s\n' 'database statement_timeout=10s' 'sqlsmith session statement_timeout=1s'
    "$pg_config" --version
    sqlsmith --version
    "${psql[@]}" -qAt -d qi_fuzz -c \
        "SELECT extname || '=' || extversion FROM pg_extension WHERE extname IN ('pg_qi','pg_duckdb') ORDER BY extname"
} >"$artifacts/build-identity.txt" 2>&1

summary=$artifacts/results.tsv
printf 'mode\tseed\tkind\tqueries\ttimeouts\tconnection_failures\tsqlsmith_exit\tserver_healthy\n' >"$summary"

for mode in shadow_p0 allowlist; do
    "${psql[@]}" -d postgres -c "ALTER DATABASE qi_fuzz SET pg_qi.routing_mode = '$mode'"
    test "$("${psql[@]}" -qAt -d qi_fuzz -c 'SHOW pg_qi.routing_mode')" = "$mode"

    random_seed=$((1000000 + RANDOM * 32768 + RANDOM))
    for seed in 104729 224737 350377 "$random_seed"; do
        kind=fixed
        if [ "$seed" = "$random_seed" ]; then
            kind=random
        fi
        printf 'sqlsmith mode=%s seed=%s kind=%s\n' "$mode" "$seed" "$kind"
        log=$artifacts/$mode-$seed.log
        result=0
        sqlsmith --target="$target" --seed="$seed" --max-queries=1500 --exclude-catalog --verbose \
            >"$log" 2>&1 || result=$?

        queries=$(awk '/^queries: / { total = $2 } END { print total + 0 }' "$log")
        read -r attempts timeouts connection_failures < <(
            awk '/^[.tSCe]+$/ {
                     attempts += length()
                     timeouts += gsub(/t/, "")
                     breaks += gsub(/C/, "")
                 }
                 END { print attempts + 0, timeouts + 0, breaks + 0 }' "$log"
        )
        healthy=$("${psql[@]}" -qAt -d qi_fuzz -c 'SELECT 1') || healthy=0
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$mode" "$seed" "$kind" "$queries" "$timeouts" "$connection_failures" "$result" "$healthy" \
            | tee -a "$summary"
        if [ "$result" -ne 0 ] || [ "$queries" -ne 1500 ] || [ "$attempts" -ne "$queries" ] || \
            [ "$connection_failures" -ne 0 ] || [ "$healthy" != 1 ]; then
            tail -30 "$log" >&2
            exit 1
        fi
    done
done
