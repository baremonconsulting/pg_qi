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
\o :work/default_thresholds
SHOW pg_qi.spill_group_threshold;
SHOW pg_qi.spill_distinct_threshold;
SHOW pg_qi.spill_sort_threshold;
\o
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

SET LOCAL duckdb.memory_limit = 2;
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

SET LOCAL pg_qi.spill_group_threshold = 1.75;
\o /dev/null
SELECT key, count(*) FROM spill_fixture GROUP BY key;
\o :work/group_override.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SELECT count(DISTINCT key), count(DISTINCT id) FROM spill_fixture;
\o :work/distinct_independent.spill
SHOW pg_qi.spill_evidence;

SET LOCAL pg_qi.spill_distinct_threshold = 3.25;
\o /dev/null
SELECT count(DISTINCT key), count(DISTINCT id) FROM spill_fixture;
\o :work/distinct_override.spill
SHOW pg_qi.spill_evidence;

\o /dev/null
SELECT key, id FROM spill_fixture ORDER BY key, id;
\o :work/sort_independent.spill
SHOW pg_qi.spill_evidence;

SET LOCAL pg_qi.spill_sort_threshold = 1.25;
\o /dev/null
SELECT key, id FROM spill_fixture ORDER BY key, id;
\o :work/sort_override.spill
SHOW pg_qi.spill_evidence;
SET LOCAL pg_qi.spill_sort_threshold = DEFAULT;
\o :work/sort_saved.spill
SHOW pg_qi.spill_evidence;

SET LOCAL pg_qi.spill_sort_threshold = '1e-100';
\o /dev/null
SELECT key, id FROM spill_fixture ORDER BY key, id;
\o :work/sort_saturated.spill
SHOW pg_qi.spill_evidence;

SET LOCAL pg_qi.spill_group_threshold = DEFAULT;
SET LOCAL pg_qi.spill_distinct_threshold = DEFAULT;
SET LOCAL pg_qi.spill_sort_threshold = DEFAULT;

SAVEPOINT invalid_threshold;
\o :work/invalid_thresholds
\set ON_ERROR_STOP off
SET LOCAL pg_qi.spill_sort_threshold = 0;
\qecho :SQLSTATE
ROLLBACK TO SAVEPOINT invalid_threshold;
SET LOCAL pg_qi.spill_sort_threshold = 'Infinity';
\qecho :SQLSTATE
ROLLBACK TO SAVEPOINT invalid_threshold;
SET LOCAL pg_qi.spill_sort_threshold = 'NaN';
\qecho :SQLSTATE
ROLLBACK TO SAVEPOINT invalid_threshold;
\set ON_ERROR_STOP on
\o

SET LOCAL duckdb.memory_limit = 1;
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

SET LOCAL duckdb.memory_limit = 8;
CREATE TABLE wide_sort_fixture AS
SELECT i::bigint * 104857603 AS id, i % 4093 AS k,
       i::bigint * 734003221 AS p0, i::bigint * 1363148839 AS p1,
       i::bigint * 2411724869 AS p2, i::bigint * 3250585693 AS p3,
       i::bigint * 4508876929 AS p4, i::bigint * 6396313783 AS p5
FROM generate_series(1, 96000) AS series(i);
ANALYZE wide_sort_fixture;
\o /dev/null
SELECT id, k, p0, p1, p2, p3, p4, p5 FROM wide_sort_fixture ORDER BY k, id;
\o :work/wide_sort.spill
SHOW pg_qi.spill_evidence;

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
\o :work/restored_thresholds
SHOW pg_qi.spill_group_threshold;
SHOW pg_qi.spill_distinct_threshold;
SHOW pg_qi.spill_sort_threshold;
SQL

python3 -I - "$work" <<'PY'
from fractions import Fraction
from pathlib import Path
import sys

work = Path(sys.argv[1])


def read(name):
    return dict(part.split("=", 1) for part in (work / name).read_text().strip().split(";"))


def require(values, **expected):
    for field, wanted in expected.items():
        if values.get(field) != wanted:
            raise SystemExit(f"Expected {field}={wanted}, got {values}")


assert (work / "default_thresholds").read_text().splitlines() == ["4", "5.5", "2.5"]
assert (work / "restored_thresholds").read_text().splitlines() == ["4", "5.5", "2.5"]
assert (work / "invalid_thresholds").read_text().splitlines() == ["22023"] * 3

for case, shape, threshold, override in (
    ("group", "hash_aggregate", "4", "1.75"),
    ("distinct", "distinct", "5.5", "3.25"),
    ("sort", "sort", "2.5", "1.25"),
):
    fits = read(f"{case}_small.spill")
    pressure = read(f"{case}_large.spill")
    require(fits, s="complete", state="within_budget", shape=shape, budget_bytes="2097152",
            threshold=threshold)
    require(pressure, s="complete", state="likely_spill", shape=shape, budget_bytes="1048576",
            threshold=threshold)
    unadjusted = int(fits["unadjusted_bytes"])
    estimated = int(fits["estimated_bytes"])
    assert unadjusted == int(pressure["unadjusted_bytes"])
    assert estimated == int(pressure["estimated_bytes"])
    assert estimated == unadjusted // Fraction(threshold)
    assert int(pressure["budget_bytes"]) <= estimated < int(fits["budget_bytes"]) <= unadjusted
    require(read(f"{case}_override.spill"), state="likely_spill", shape=shape,
            budget_bytes="2097152", threshold=override, unadjusted_bytes=str(unadjusted),
            estimated_bytes=str(unadjusted // Fraction(override)))
    print(f"{case} estimated bytes {estimated}, spill risk rises under the smaller budget")

pressure = read("sort_large.spill")
benefit = read("sort_large.benefit")
decision = read("sort_large.decision")
for case in ("distinct", "sort"):
    default = read(f"{case}_small.spill")
    assert all(read(f"{case}_independent.spill")[key] == default[key]
               for key in ("shape", "state", "threshold", "estimated_bytes"))
assert read("sort_saved.spill") == read("sort_override.spill")
require(read("sort_saturated.spill"), state="likely_spill", estimated_bytes=str(2**64 - 1))
require(benefit, s="complete", state="unknown", reason="spill_resources_unverified")
require(decision, s="complete", beneficial="unknown", barrier="closed",
        recommendation="insufficient_evidence", policy="pass_through", force="off")
assert all(pressure[key] == benefit[key] == decision[key] for key in ("cid", "g", "e"))
require(read("wide_sort.spill"), s="complete", state="likely_spill", shape="sort",
        budget_bytes="8388608")
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
