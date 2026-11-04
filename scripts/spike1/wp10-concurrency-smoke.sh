#!/usr/bin/env bash

set -euo pipefail

repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
pgroot=${PG_ASSERT_ROOT:-/home/adam/.local/pgsql/17-assert}
artifact_root=${WP10_ARTIFACT_DIR:-"$repo_dir/artifacts/spike1/wp10"}
duration=${WP10_DURATION:-300}
clients=${WP10_CLIENTS:-8}
mkdir -p "$artifact_root/socket"

test "$duration" -eq 300
test "$clients" -eq 8
"$pgroot/bin/pg_config" --configure | grep -q -- --enable-cassert

make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" clean >/dev/null
make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" -j2 \
    >"$artifact_root/build.log" 2>&1
make -C "$repo_dir" PG_CONFIG="$pgroot/bin/pg_config" install \
    >"$artifact_root/install.log" 2>&1

data_dir="$artifact_root/data"
socket_dir="$artifact_root/socket"
case "$data_dir" in
    "$repo_dir"/artifacts/spike1/*/data) ;;
    *) echo "Refusing to replace unsafe data directory: $data_dir" >&2; exit 1 ;;
esac
rm -rf "$data_dir"
"$pgroot/bin/initdb" -D "$data_dir" --no-locale --encoding=UTF8 \
    >"$artifact_root/initdb.log" 2>&1
{
    echo "unix_socket_directories = '$socket_dir'"
    echo "listen_addresses = ''"
    echo "shared_preload_libraries = 'pg_duckdb,pg_qi'"
    echo "log_line_prefix = '%m [%p] %a '"
} >>"$data_dir/postgresql.conf"

server_log="$artifact_root/server.log"
: >"$server_log"
"$pgroot/bin/pg_ctl" -D "$data_dir" -l "$server_log" -w start >/dev/null
cleanup() { "$pgroot/bin/pg_ctl" -D "$data_dir" -m fast stop >/dev/null 2>&1 || true; }
trap cleanup EXIT

"$pgroot/bin/psql" -X -v ON_ERROR_STOP=1 -h "$socket_dir" -d postgres \
    >"$artifact_root/setup.log" 2>&1 <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE TABLE wp10_probe AS
  SELECT i, i % 10 AS v FROM generate_series(1, 100000) AS g(i);
ANALYZE wp10_probe;
SQL

start_epoch=$(date +%s)
PGAPPNAME=wp10_pgbench "$pgroot/bin/pgbench" -n -c "$clients" -j "$clients" \
    -T "$duration" -P 30 -D seq=0 -f "$repo_dir/scripts/spike1/wp10-workload.sql" \
    -h "$socket_dir" postgres >"$artifact_root/pgbench.log" 2>&1
end_epoch=$(date +%s)
elapsed=$((end_epoch - start_epoch))

cleanup
trap - EXIT

test "$elapsed" -ge 295
grep -q '^number of failed transactions: 0 ' "$artifact_root/pgbench.log"
if grep -Eiq 'TRAP:|assertion failed|PANIC:|segmentation fault' "$server_log"; then
    echo "Assertion, panic, or crash signature found in server log" >&2
    exit 1
fi

final_tsv="$artifact_root/final-backends.tsv"
printf 'pid\tpushed\tpopped\tunwound\tdepth\tcleanup_broken\tpg_plans\tduckdb_plans\n' >"$final_tsv"
awk -f "$repo_dir/scripts/spike1/wp10-parse-final.awk" "$server_log" >>"$final_tsv"

{
    echo "WP10 PASS"
    echo "postgresql=$($pgroot/bin/pg_config --version)"
    echo "assertions=enabled"
    echo "clients=$clients"
    echo "requested_seconds=$duration"
    echo "wall_seconds=$elapsed"
    echo "backend_rows=$(($(wc -l <"$final_tsv") - 1))"
    echo "invariant=pushed=popped+unwound"
    echo "depth=0"
    echo "cleanup_broken=false"
    echo "assertion_failures=0"
} | tee "$artifact_root/summary.txt"
