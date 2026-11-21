#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=${PG_BIN:-$pg_install_root/$major/bin}
repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
cleanup() {
    status=$?
    if test "$status" -ne 0; then
        find "$work" -maxdepth 1 -type f -print -exec sed -n '1,200p' {} \; >&2
    fi
    rm -rf "$work"
    exit "$status"
}
trap cleanup EXIT

"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 \
    -v work="$work" postgres >"$work/session.out" 2>"$work/session.err" <<'SQL'
CREATE ROLE pgqi_diagnostic NOLOGIN;
CREATE ROLE plan_features_plain;
CREATE ROLE plan_features_diagnostic;
GRANT pgqi_diagnostic TO plan_features_diagnostic;
CREATE TABLE plan_features_unanalyzed(id integer)
WITH (autovacuum_enabled = false);
INSERT INTO plan_features_unanalyzed SELECT generate_series(1, 1000);
CREATE TABLE plan_features_fixture(id integer);
INSERT INTO plan_features_fixture SELECT generate_series(1, 1000);
ANALYZE plan_features_fixture;
GRANT SELECT ON plan_features_fixture TO plan_features_plain;
GRANT SELECT ON plan_features_fixture TO plan_features_diagnostic;
SET max_parallel_workers_per_gather = 0;
SET enable_indexscan = off;
SET enable_indexonlyscan = off;
SET enable_bitmapscan = off;
SET pg_qi.routing_mode = shadow_p0;

\o /dev/null
SELECT count(*) FROM plan_features_unanalyzed;
\o :work/unanalyzed.features
SHOW pg_qi.plan_features;

SET enable_seqscan = off;
\o /dev/null
SELECT count(*) FROM plan_features_fixture;
\o :work/disabled.features
SHOW pg_qi.plan_features;
SET enable_seqscan = on;

\o :work/count_star_explain.json
EXPLAIN (FORMAT JSON) SELECT count(*) FROM plan_features_fixture;
\o /dev/null
SELECT count(*) FROM plan_features_fixture;
\o :work/count_star.features
SHOW pg_qi.plan_features;
\o :work/count_star.decision
SHOW pg_qi.decision_v2;

\o /dev/null
SELECT 1;
\o :work/non_p0.features
SHOW pg_qi.plan_features;

\o :work/count_int_explain.json
EXPLAIN (FORMAT JSON) SELECT count(id) FROM plan_features_fixture;
\o /dev/null
SELECT count(id) FROM plan_features_fixture;
\o :work/count_int.features
SHOW pg_qi.plan_features;

\o /dev/null
SELECT min(id) FROM plan_features_fixture;
\o :work/min_seq.features
SHOW pg_qi.plan_features;
\o /dev/null
SELECT max(id) FROM plan_features_fixture;
\o :work/max_seq.features
SHOW pg_qi.plan_features;

SET min_parallel_table_scan_size = 0;
SET parallel_setup_cost = 0;
SET parallel_tuple_cost = 0;
SET max_parallel_workers_per_gather = 2;
\o /dev/null
SELECT count(*) FROM plan_features_fixture;
\o :work/parallel.features
SHOW pg_qi.plan_features;
SET max_parallel_workers_per_gather = 0;

\o :work/catalog.out
SELECT c.oid, c.relpages, c.reltuples
FROM pg_class AS c
WHERE c.oid = 'plan_features_fixture'::regclass;

\o /dev/null
SELECT count(*) FROM plan_features_fixture;
\o :work/before_error.features
SHOW pg_qi.plan_features;
\set ON_ERROR_STOP off
\o /dev/null
SELECT 1 / 0;
\set ON_ERROR_STOP on
\o :work/after_error.features
SHOW pg_qi.plan_features;

\o /dev/null
CREATE INDEX plan_features_fixture_id_idx ON plan_features_fixture(id);
SET enable_indexscan = on;
SET enable_indexonlyscan = on;
SELECT min(id) FROM plan_features_fixture;
\o :work/min_index.features
SHOW pg_qi.plan_features;

\o /dev/null
SELECT count(*) FROM plan_features_fixture;
SELECT set_config('role', 'plan_features_diagnostic', false);
\o :work/withheld.features
SHOW pg_qi.plan_features;
RESET ROLE;

SET ROLE plan_features_plain;
\o /dev/null
SELECT count(*) FROM plan_features_fixture;
\o :work/denied.features
SHOW pg_qi.plan_features;
RESET ROLE;

SET ROLE plan_features_diagnostic;
\o :work/after_role_change.features
SHOW pg_qi.plan_features;
\o /dev/null
SET enable_indexscan = off;
SET enable_indexonlyscan = off;
SELECT count(*) FROM plan_features_fixture;
\o :work/diagnostic.features
SHOW pg_qi.plan_features;
RESET ROLE;

\o /dev/null
DROP TABLE plan_features_fixture;
DROP TABLE plan_features_unanalyzed;
DROP OWNED BY plan_features_plain;
DROP OWNED BY plan_features_diagnostic;
DROP ROLE plan_features_plain;
DROP ROLE plan_features_diagnostic;
DROP ROLE pgqi_diagnostic;
SQL

grep -q '^ERROR:  division by zero$' "$work/session.err"

python3 - "$repo" "$work" <<'PY'
import json
import math
import sys
from pathlib import Path

work = Path(sys.argv[2])

def feature(name):
    text = (work / name).read_text().strip()
    result = {}
    for item in text.split(";"):
        key, value = item.split("=", 1)
        result[key] = value
    return result

def explain(name):
    document = json.loads((work / name).read_text())
    root = document[0]["Plan"]
    assert root["Node Type"] == "Aggregate"
    assert root["Strategy"] == "Plain"
    assert root["Partial Mode"] == "Simple"
    assert len(root["Plans"]) == 1
    scan = root["Plans"][0]
    assert scan["Node Type"] == "Seq Scan"
    return root, scan

def assert_close(actual, expected):
    assert math.isfinite(actual)
    assert abs(actual - expected) <= 0.0051, (actual, expected)

catalog_oid, relpages, reltuples = (work / "catalog.out").read_text().strip().split("|")
unanalyzed = feature("unanalyzed.features")
assert unanalyzed["state"] == "bounded"
assert int(unanalyzed["known"]) == 231
assert unanalyzed["catalog_relpages"] == "unknown"
assert unanalyzed["catalog_reltuples"] == "unknown"
for prefix in ("count_star", "count_int"):
    facts = feature(prefix + ".features")
    root, scan = explain(prefix + "_explain.json")
    assert facts["v"] == "1" and facts["s"] == "complete"
    assert facts["state"] == "bounded" and facts["reason"] == "ok"
    assert facts["spine"] == "plain_agg_seq_scan"
    assert int(facts["known"]) == 255
    assert facts["relation"] == catalog_oid
    assert int(facts["catalog_relpages"]) == int(relpages)
    assert_close(float(facts["catalog_reltuples"]), float(reltuples))
    for fact_key, plan, json_key in (
        ("agg_startup", root, "Startup Cost"),
        ("agg_total", root, "Total Cost"),
        ("agg_rows", root, "Plan Rows"),
        ("scan_startup", scan, "Startup Cost"),
        ("scan_total", scan, "Total Cost"),
        ("scan_rows", scan, "Plan Rows"),
    ):
        assert_close(float(facts[fact_key]), float(plan[json_key]))
    assert int(facts["agg_width"]) == int(root["Plan Width"])
    assert int(facts["scan_width"]) == int(scan["Plan Width"])
    assert facts["parallel_aware"] == "false"
    assert facts["workers"] == "0" and facts["has_limit"] == "false"

decision = feature("count_star.decision")
count_star = feature("count_star.features")
assert (count_star["cid"], count_star["g"], count_star["e"]) == \
       (decision["cid"], decision["g"], decision["e"])
non_p0 = feature("non_p0.features")
assert non_p0["state"] == "unknown" and non_p0["reason"] == "p0_unknown"
assert int(non_p0["e"]) > int(count_star["e"])
for name in ("min_seq.features", "max_seq.features"):
    positive = feature(name)
    assert positive["state"] == "bounded" and positive["reason"] == "ok"
parallel = feature("parallel.features")
assert parallel["state"] == "unknown"
assert parallel["reason"] in {"partial_aggregate", "final_aggregate", "gather"}
disabled = feature("disabled.features")
assert disabled["state"] == "unknown" and disabled["reason"] == "disabled_path"

assert (work / "before_error.features").read_text() == \
       (work / "after_error.features").read_text()
negative = feature("min_index.features")
assert negative["state"] == "unknown"
assert negative["reason"] in {
    "auxiliary_plan", "unsupported_root", "unsupported_scan"
}
assert feature("denied.features") == {"v": "1", "s": "denied"}
assert feature("withheld.features") == {"v": "1", "s": "withheld"}
after_role = feature("after_role_change.features")
assert after_role["s"] == "none"
diagnostic = feature("diagnostic.features")
assert diagnostic["s"] == "complete" and diagnostic["state"] == "bounded"
PY

echo "PQ-36 bounded native plan feature verification PASS (PG$major)"
