#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=${PG_BIN:-$pg_install_root/$major/bin}
work=$(mktemp -d)
cleanup() {
    status=$?
    if test "$status" -ne 0; then
        find "$work" -maxdepth 1 -type f -print -exec sed -n '1,120p' {} \; >&2
    fi
    rm -r -- "$work"
    exit "$status"
}
trap cleanup EXIT

"$pgbin/psql" -XqAt -v ON_ERROR_STOP=1 -v work="$work" postgres \
    >"$work/session.out" 2>"$work/session.err" <<'SQL'
SHOW pg_qi.spill_evidence;
BEGIN;
SET LOCAL statement_timeout = '10s';
SET LOCAL pg_qi.routing_mode = shadow_p0;
SET LOCAL duckdb.force_execution = off;
SET LOCAL duckdb.threads = 1;
SET LOCAL max_parallel_workers_per_gather = 0;
CREATE TABLE spill_fixture(id integer, key integer, label text);
INSERT INTO spill_fixture
SELECT i, i % 14000, md5(i::text) FROM generate_series(1, 20000) AS series(i);
ANALYZE spill_fixture;

SET LOCAL duckdb.memory_limit = 32;
\o /dev/null
SELECT key, count(*) FROM spill_fixture GROUP BY key;
\o :work/group_small.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SELECT count(DISTINCT key), count(DISTINCT id) FROM spill_fixture;
\o :work/distinct_small.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SELECT key, id FROM spill_fixture ORDER BY key, id;
\o :work/sort_small.spill
SHOW pg_qi.spill_evidence;

SET LOCAL duckdb.memory_limit = 2;
\o /dev/null
SELECT key, count(*) FROM spill_fixture GROUP BY key;
\o :work/group_large.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SELECT count(DISTINCT key), count(DISTINCT id) FROM spill_fixture;
\o :work/distinct_large.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SELECT key, id FROM spill_fixture ORDER BY key, id;
\o :work/sort_large.spill
SHOW pg_qi.spill_evidence;
\o :work/sort_large.benefit
SHOW pg_qi.benefit_evidence;
\o :work/sort_large.decision
SHOW pg_qi.decision_v2;

SET LOCAL duckdb.memory_limit = 32;
\o /dev/null
SELECT label FROM spill_fixture ORDER BY label;
\o :work/variable_width.spill
SHOW pg_qi.spill_evidence;
\o :work/variable_width.benefit
SHOW pg_qi.benefit_evidence;

\o /dev/null
SELECT key, count(label) FROM spill_fixture GROUP BY key;
\o :work/variable_count.spill
SHOW pg_qi.spill_evidence;

SET LOCAL enable_hashagg = off;
\o /dev/null
SELECT DISTINCT key FROM spill_fixture;
\o :work/sorted_distinct.spill
SHOW pg_qi.spill_evidence;
SET LOCAL enable_hashagg = on;

\o /dev/null
SELECT count(*) FROM spill_fixture;
\o :work/scalar_control.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SELECT count(DISTINCT key) + 1 FROM spill_fixture;
\o :work/wrapped_distinct.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SET LOCAL duckdb.memory_limit = 0;
SELECT DISTINCT key FROM spill_fixture;
\o :work/default_budget.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SET LOCAL pg_qi.routing_mode = off;
SELECT id FROM spill_fixture ORDER BY id;
\o :work/off_control.spill
SHOW pg_qi.spill_evidence;
\o
ROLLBACK;
SQL

python3 -I - "$work" <<'PY'
from pathlib import Path
import sys

work = Path(sys.argv[1])


def read(name):
    return dict(part.split("=", 1) for part in (work / name).read_text().strip().split(";"))


def require(values, **expected):
    for field, wanted in expected.items():
        if values.get(field) != wanted:
            raise SystemExit(f"Expected {field}={wanted}, got {values}")


for case, shape in (
    ("group", "hash_aggregate"),
    ("distinct", "distinct"),
    ("sort", "sort"),
):
    fits = read(f"{case}_small.spill")
    pressure = read(f"{case}_large.spill")
    require(fits, s="complete", state="within_budget", shape=shape, budget_bytes="33554432")
    require(pressure, s="complete", state="likely_spill", shape=shape, budget_bytes="2097152")
    assert int(fits["estimated_bytes"]) == int(pressure["estimated_bytes"])
    assert int(pressure["budget_bytes"]) <= int(pressure["estimated_bytes"]) < int(fits["budget_bytes"])
    print(f"{case} expected bytes {fits['estimated_bytes']}, spill risk rises under the smaller budget")

pressure = read("sort_large.spill")
benefit = read("sort_large.benefit")
decision = read("sort_large.decision")
require(benefit, s="complete", state="unknown", reason="spill_resources_unverified")
require(decision, s="complete", beneficial="unknown", barrier="closed",
        recommendation="insufficient_evidence", policy="pass_through", force="off")
assert all(pressure[key] == benefit[key] == decision[key] for key in ("cid", "g", "e"))
require(read("variable_width.spill"), s="complete", state="unknown", reason="unbounded_width")
require(read("variable_width.benefit"), s="complete", state="unknown", reason="spill_estimate_unknown")
require(read("variable_count.spill"), s="complete", state="unknown", reason="unbounded_width")
require(read("sorted_distinct.spill"), s="complete", state="within_budget", shape="distinct")
require(read("wrapped_distinct.spill"), s="complete", state="unknown", reason="unsupported_plan")
require(read("default_budget.spill"), s="complete", state="unknown", reason="budget_unknown")
for case in ("scalar_control", "off_control"):
    require(read(f"{case}.spill"), s="complete", state="not_evaluated")
print("Spill evidence and abstention checks passed")
PY
