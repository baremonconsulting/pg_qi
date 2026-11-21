#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=${PG_BIN:-$pg_install_root/$major/bin}
work=$(mktemp -d)
cleanup() {
    status=$?
    if test "$status" -ne 0; then
        find "$work" -maxdepth 1 -type f -print \
            -exec sed -n '1,240p' {} \; >&2
    fi
    rm -rf "$work"
    exit "$status"
}
trap cleanup EXIT

"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 \
    -v work="$work" postgres >"$work/session.out" 2>"$work/session.err" <<'SQL'
CREATE ROLE pgqi_diagnostic NOLOGIN;
\o :work/none.stats
SHOW pg_qi.stats_confidence;
CREATE ROLE stats_plain;
CREATE ROLE stats_diagnostic;
GRANT pgqi_diagnostic TO stats_diagnostic;
CREATE TABLE stats_confidence_fixture(id integer, payload integer)
WITH (autovacuum_enabled = false);
INSERT INTO stats_confidence_fixture
SELECT i, i % 100 FROM generate_series(1, 10000) AS g(i);
SELECT pg_stat_force_next_flush();
GRANT SELECT ON stats_confidence_fixture TO stats_plain;
GRANT SELECT ON stats_confidence_fixture TO stats_diagnostic;
SET max_parallel_workers_per_gather = 0;
SET enable_indexscan = off;
SET enable_indexonlyscan = off;
SET enable_bitmapscan = off;
SET pg_qi.routing_mode = shadow_p0;

\o /dev/null
SELECT count(*) FROM stats_confidence_fixture;
\o :work/before_analyze.stats
SHOW pg_qi.stats_confidence;
\o :work/before_analyze.decision
SHOW pg_qi.decision_v2;
\o :work/before_analyze.capability
SHOW pg_qi.shadow_p0_capability;

\o /dev/null
ANALYZE stats_confidence_fixture;
SELECT pg_stat_clear_snapshot();
SELECT count(*) FROM stats_confidence_fixture;
\o :work/after_analyze.stats
SHOW pg_qi.stats_confidence;
\o :work/after_analyze.features
SHOW pg_qi.plan_features;
\o :work/after_analyze.decision
SHOW pg_qi.decision_v2;
\o :work/after_analyze.capability
SHOW pg_qi.shadow_p0_capability;

\o /dev/null
SELECT pg_stat_reset_single_table_counters(
    'stats_confidence_fixture'::regclass);
SELECT count(*) FROM stats_confidence_fixture;
\o :work/after_counter_reset.stats
SHOW pg_qi.stats_confidence;
\o /dev/null
ANALYZE stats_confidence_fixture;

\o /dev/null
BEGIN;
UPDATE stats_confidence_fixture SET payload = payload WHERE id = 1;
SELECT count(*) FROM stats_confidence_fixture;
\o :work/own_xid.stats
SHOW pg_qi.stats_confidence;
\o /dev/null
ROLLBACK;

SET track_counts = off;
SELECT count(*) FROM stats_confidence_fixture;
\o :work/counters_disabled.stats
SHOW pg_qi.stats_confidence;
\o /dev/null
SET track_counts = on;

\o /dev/null
UPDATE stats_confidence_fixture
SET payload = CASE WHEN payload = 0 THEN 1 ELSE 0 END;
SELECT pg_stat_force_next_flush();
SELECT pg_stat_clear_snapshot();
SELECT count(*) FROM stats_confidence_fixture;
\o :work/after_drift.stats
SHOW pg_qi.stats_confidence;
\o :work/after_drift.decision
SHOW pg_qi.decision_v2;
\o :work/after_drift.capability
SHOW pg_qi.shadow_p0_capability;

\o /dev/null
ANALYZE stats_confidence_fixture(payload);
SELECT count(*) FROM stats_confidence_fixture;
\o :work/after_partial_analyze.stats
SHOW pg_qi.stats_confidence;

\o /dev/null
ANALYZE stats_confidence_fixture;
SELECT pg_stat_clear_snapshot();
SELECT count(*) FROM stats_confidence_fixture;
\o :work/after_reanalyze.stats
SHOW pg_qi.stats_confidence;
\o :work/after_reanalyze.decision
SHOW pg_qi.decision_v2;
\o :work/after_reanalyze.capability
SHOW pg_qi.shadow_p0_capability;

\o /dev/null
BEGIN READ ONLY;
SET LOCAL stats_fetch_consistency = snapshot;
SELECT pg_stat_clear_snapshot();
\o :work/snapshot_before.out
SELECT pg_stat_get_snapshot_timestamp() IS NULL;
\o /dev/null
SELECT count(*) FROM stats_confidence_fixture;
\o :work/snapshot_observer.stats
SHOW pg_qi.stats_confidence;
\o :work/snapshot_after.out
SELECT pg_stat_get_snapshot_timestamp() IS NULL;
\o /dev/null
ROLLBACK;

\o /dev/null
SELECT count(*) FROM stats_confidence_fixture;
SELECT set_config('role', 'stats_diagnostic', false);
\o :work/withheld.stats
SHOW pg_qi.stats_confidence;
RESET ROLE;

SET ROLE stats_plain;
\o /dev/null
SELECT count(*) FROM stats_confidence_fixture;
\o :work/denied.stats
SHOW pg_qi.stats_confidence;
RESET ROLE;

SET ROLE stats_diagnostic;
\o /dev/null
SELECT count(*) FROM stats_confidence_fixture;
\o :work/diagnostic.stats
SHOW pg_qi.stats_confidence;
RESET ROLE;

\o /dev/null
CREATE TABLE stats_ndistinct(a integer, b integer);
INSERT INTO stats_ndistinct
SELECT i % 100, i % 100 FROM generate_series(1, 10000) AS g(i);
ANALYZE stats_ndistinct;
\o :work/ndistinct_before.json
EXPLAIN (FORMAT JSON) SELECT a, b, count(*) FROM stats_ndistinct GROUP BY a, b;
\o /dev/null
CREATE STATISTICS stats_ndistinct_s (ndistinct) ON a, b
FROM stats_ndistinct;
ANALYZE stats_ndistinct;
\o :work/ndistinct_after.json
EXPLAIN (FORMAT JSON) SELECT a, b, count(*) FROM stats_ndistinct GROUP BY a, b;

\o /dev/null
CREATE TABLE stats_dependencies(a integer, b integer);
INSERT INTO stats_dependencies
SELECT i % 100, i % 100 FROM generate_series(1, 10000) AS g(i);
ANALYZE stats_dependencies;
\o :work/dependencies_before.json
EXPLAIN (FORMAT JSON) SELECT * FROM stats_dependencies WHERE a = 42 AND b = 42;
\o /dev/null
CREATE STATISTICS stats_dependencies_s (dependencies) ON a, b
FROM stats_dependencies;
ANALYZE stats_dependencies;
\o :work/dependencies_after.json
EXPLAIN (FORMAT JSON) SELECT * FROM stats_dependencies WHERE a = 42 AND b = 42;

\o /dev/null
CREATE TABLE stats_mcv(a integer, b integer);
INSERT INTO stats_mcv
SELECT CASE WHEN i <= 5000 THEN 1 ELSE i END,
       CASE WHEN i <= 5000 THEN 1 ELSE i + 10000 END
FROM generate_series(1, 10000) AS g(i);
ANALYZE stats_mcv;
\o :work/mcv_before.json
EXPLAIN (FORMAT JSON) SELECT * FROM stats_mcv WHERE a = 1 AND b = 1;
\o /dev/null
CREATE STATISTICS stats_mcv_s (mcv) ON a, b FROM stats_mcv;
ANALYZE stats_mcv;
\o :work/mcv_after.json
EXPLAIN (FORMAT JSON) SELECT * FROM stats_mcv WHERE a = 1 AND b = 1;

\o /dev/null
CREATE TABLE stats_expression(t text);
INSERT INTO stats_expression
SELECT CASE WHEN i <= 5000 THEN 'HOT' ELSE 'v-' || i::text END
FROM generate_series(1, 10000) AS g(i);
ANALYZE stats_expression;
\o :work/expression_before.json
EXPLAIN (FORMAT JSON) SELECT * FROM stats_expression WHERE lower(t) = 'hot';
\o /dev/null
CREATE STATISTICS stats_expression_s ON (lower(t)) FROM stats_expression;
ANALYZE stats_expression;
\o :work/expression_after.json
EXPLAIN (FORMAT JSON) SELECT * FROM stats_expression WHERE lower(t) = 'hot';

\o /dev/null
DROP TABLE stats_expression;
DROP TABLE stats_mcv;
DROP TABLE stats_dependencies;
DROP TABLE stats_ndistinct;
DROP TABLE stats_confidence_fixture;
DROP OWNED BY stats_plain;
DROP OWNED BY stats_diagnostic;
DROP ROLE stats_plain;
DROP ROLE stats_diagnostic;
DROP ROLE pgqi_diagnostic;
SQL

python3 - "$work" <<'PY'
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])

def record(name):
    text = (work / name).read_text().strip()
    return dict(item.split("=", 1) for item in text.split(";"))

def plan_rows(name):
    return float(json.loads((work / name).read_text())[0]["Plan"]["Plan Rows"])

def closer(after, before, actual):
    assert abs(after - actual) < abs(before - actual), (before, after, actual)

before = record("before_analyze.stats")
analyzed = record("after_analyze.stats")
drift = record("after_drift.stats")
reanalyzed = record("after_reanalyze.stats")
assert before["state"] == "unknown"
assert before["reason"] == "catalog_estimate_unknown"
assert int(before["known"]) == 11
assert int(before["n_mod_since_analyze"]) > 0
assert analyzed["state"] == "unknown"
assert analyzed["reason"] == "freshness_unproven"
assert analyzed["source"] == "manual"
assert int(analyzed["n_mod_since_analyze"]) == 0
assert int(analyzed["analyze_age_ms"]) >= 0
assert drift["state"] == "unknown"
assert drift["reason"] == "modifications_reported"
assert int(drift["n_mod_since_analyze"]) > 0
assert reanalyzed["state"] == "unknown"
assert reanalyzed["reason"] == "freshness_unproven"
assert int(reanalyzed["n_mod_since_analyze"]) == 0
assert record("own_xid.stats")["reason"] == "own_xid"
assert record("counters_disabled.stats")["reason"] == "counters_disabled"
assert record("after_counter_reset.stats")["reason"] == "no_recorded_analyze"
assert record("after_partial_analyze.stats")["reason"] == \
       "modifications_reported"
partial = record("after_partial_analyze.stats")
assert int(partial["e"]) > int(drift["e"])
assert partial["n_mod_since_analyze"] == drift["n_mod_since_analyze"]
assert partial["source"] == "manual"
assert (work / "snapshot_before.out").read_text().strip() == "t"
assert (work / "snapshot_after.out").read_text().strip() == "t"
snapshot_observer = record("snapshot_observer.stats")
assert snapshot_observer["reason"] == "freshness_unproven"
assert int(snapshot_observer["e"]) > int(reanalyzed["e"])
assert record("none.stats")["s"] == "none"
assert record("denied.stats") == {"v": "1", "s": "denied"}
assert record("withheld.stats") == {"v": "1", "s": "withheld"}
assert record("diagnostic.stats")["reason"] == "freshness_unproven"

expected_keys = {
    "v", "s", "cid", "g", "e", "state", "reason", "known",
    "relation", "source", "catalog_relpages", "catalog_reltuples",
    "scan_rows", "scan_width", "n_mod_since_analyze", "analyze_age_ms",
}
for name in (
    "before_analyze.stats", "after_analyze.stats", "after_drift.stats",
    "after_reanalyze.stats", "after_counter_reset.stats",
    "after_partial_analyze.stats", "own_xid.stats",
    "counters_disabled.stats", "diagnostic.stats",
):
    assert set(record(name)) == expected_keys, name

features = record("after_analyze.features")
decision = record("after_analyze.decision")
assert (analyzed["cid"], analyzed["g"], analyzed["e"]) == \
       (features["cid"], features["g"], features["e"]) == \
       (decision["cid"], decision["g"], decision["e"])
assert int(analyzed["known"]) == 63
assert analyzed["relation"] == features["relation"]
assert analyzed["catalog_relpages"] == features["catalog_relpages"]
assert analyzed["catalog_reltuples"] == features["catalog_reltuples"]
assert analyzed["scan_rows"] == features["scan_rows"]
assert analyzed["scan_width"] == features["scan_width"]

def decision_contract(name):
    value = record(name)
    return (
        value["eligible"], value["safe"], value["beneficial"],
        value["barrier"], value["recommendation"], value["reason"],
        value["rule"],
    )

baseline = decision_contract("before_analyze.decision")
assert decision_contract("after_analyze.decision") == baseline
assert decision_contract("after_drift.decision") == baseline
assert decision_contract("after_reanalyze.decision") == baseline
for name in (
    "before_analyze.capability", "after_analyze.capability",
    "after_drift.capability", "after_reanalyze.capability",
):
    assert (work / name).read_text().strip() == "count_star", name
for name in (
    "before_analyze.decision", "after_analyze.decision",
    "after_drift.decision", "after_reanalyze.decision",
):
    value = record(name)
    assert value["p0"] == "bounded" and value["p0_reason"] == "ok"
    assert value["safe"] == "unknown"

closer(plan_rows("ndistinct_after.json"),
       plan_rows("ndistinct_before.json"), 100.0)
closer(plan_rows("dependencies_after.json"),
       plan_rows("dependencies_before.json"), 100.0)
closer(plan_rows("mcv_after.json"), plan_rows("mcv_before.json"), 5000.0)
closer(plan_rows("expression_after.json"),
       plan_rows("expression_before.json"), 5000.0)
PY

echo "PQ-37 statistics confidence verification PASS (PG$major)"
