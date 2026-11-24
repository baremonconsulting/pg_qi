#!/usr/bin/env bash
# With pg_qi preloaded, auto_explain must not make pg_duckdb execute a
# DuckDB query a second time.
#
# pg_duckdb's ExplainCustomScan callback executes the prepared DuckDB
# statement, which is an EXPLAIN of the query only inside an EXPLAIN command.
# auto_explain calls it for regular queries from ExecutorEnd: the query then
# runs again and, without an active snapshot, the backend crashes (SIGSEGV).
#
# Needs a running server with pg_duckdb and pg_qi preloaded, a superuser
# connection (for LOAD 'auto_explain') and the auto_explain module.
set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=${PGBIN:-$pg_install_root/$major/bin}
dbname=${PGDATABASE:-postgres}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

query='SELECT grp, count(DISTINCT buyer) AS buyers FROM qi_explain_guard_t GROUP BY grp ORDER BY grp'
failures=0

wait_ready() {
    local i
    for i in $(seq 1 50); do
        "$pgbin/pg_isready" -q && return 0
        sleep 0.2
    done
    return 1
}

# Runs the given statements in one session with auto_explain logging every
# plan as a NOTICE and DuckDB execution forced.
session() {
    local name=$1 sql args=()
    shift
    args=(-c "LOAD 'auto_explain'" -c 'SET auto_explain.log_min_duration = 0'
          -c 'SET auto_explain.log_level = notice' -c 'SET auto_explain.log_format = json'
          -c 'SET duckdb.force_execution = true')
    for sql in "$@"; do
        args+=(-c "$sql")
    done
    wait_ready
    "$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 -d "$dbname" "${args[@]}" >"$work/$name.out" 2>"$work/$name.err" \
        || echo "psql exit status $?" >>"$work/$name.err"
}

fail() {
    printf 'FAIL %s: %s\n' "$1" "$2"
    sed 's/^/    /' "$work/$1.err" | head -n 20
    failures=$((failures + 1))
}

expect_rows() {
    local name=$1 want=$2
    [ "$(cat "$work/$name.out")" = "$want" ] || fail "$name" "unexpected output: $(tr '\n' ' ' <"$work/$name.out")"
}

# The query must be visible as a DuckDB scan in the auto_explain plan, but the
# DuckDB plan itself must not be produced by executing the query again.
expect_logged_without_reexecution() {
    local name=$1
    grep -q '"Custom Plan Provider": "DuckDBScan"' "$work/$name.err" \
        || fail "$name" 'auto_explain did not log the DuckDBScan node'
    if grep -q '"DuckDB Execution Plan"' "$work/$name.err"; then
        fail "$name" 'auto_explain executed the DuckDB query again'
    fi
}

wait_ready
"$pgbin/psql" -X -q -v ON_ERROR_STOP=1 -d "$dbname" \
    -c 'CREATE EXTENSION IF NOT EXISTS pg_duckdb' \
    -c 'CREATE EXTENSION IF NOT EXISTS pg_qi' \
    -c 'DROP TABLE IF EXISTS qi_explain_guard_t' \
    -c 'CREATE TABLE qi_explain_guard_t(grp int, buyer int)' \
    -c 'INSERT INTO qi_explain_guard_t VALUES (1, 1), (1, 1), (1, 2), (2, 3)'

# Simple query protocol: ExecutorEnd runs from PortalDrop without a snapshot.
session regular "$query" "SELECT 'alive'"
expect_rows regular $'1|2\n2|1\nalive'
expect_logged_without_reexecution regular

# Scrollable cursor: pg_duckdb puts a Material node above the DuckDB scan, and
# CLOSE runs ExecutorEnd with an active snapshot.
session scroll_cursor 'BEGIN' "DECLARE qi_explain_guard_c SCROLL CURSOR FOR $query" 'FETCH ALL FROM qi_explain_guard_c' \
    'CLOSE qi_explain_guard_c' 'COMMIT' "SELECT 'alive'"
expect_rows scroll_cursor $'1|2\n2|1\nalive'
expect_logged_without_reexecution scroll_cursor

# An EXPLAIN command prepares an EXPLAIN in DuckDB; its plan must stay.
session explain "EXPLAIN (FORMAT JSON) $query"
grep -q '"DuckDB Execution Plan"' "$work/explain.out" || fail explain 'EXPLAIN lost the DuckDB plan'
session explain_analyze "EXPLAIN (ANALYZE, FORMAT JSON) $query"
grep -q '"DuckDB Execution Plan"' "$work/explain_analyze.out" || fail explain_analyze 'EXPLAIN ANALYZE lost the DuckDB plan'

wait_ready
"$pgbin/psql" -X -q -d "$dbname" -c 'DROP TABLE IF EXISTS qi_explain_guard_t' >/dev/null 2>&1 || true

if [ "$failures" -ne 0 ]; then
    echo "explain guard: $failures failure(s)"
    exit 1
fi
echo 'explain guard: ok'
