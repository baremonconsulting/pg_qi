#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
artifact_dir=${2:?artifact directory is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=$pg_install_root/$major/bin
repo=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$artifact_dir"

"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
DROP TABLE IF EXISTS pq42_parity;
CREATE TABLE pq42_parity(id integer PRIMARY KEY, grp integer, value integer, note text);
INSERT INTO pq42_parity VALUES
    (1, 1, 10, 'alpha'), (2, 1, NULL, 'beta'),
    (3, 2, 10, NULL), (4, 2, 20, 'delta'), (5, 2, 20, 'delta');
ANALYZE pq42_parity;
SQL

run_results() {
    local mode=$1 output=$2
    "$pgbin/psql" -X -v ON_ERROR_STOP=1 -v mode="$mode" postgres >"$output" <<'SQL'
\pset format unaligned
\pset tuples_only off
\pset null '<NULL>'
\pset footer on
SET lc_messages = 'C';
SET max_parallel_workers_per_gather = 0;
SET jit = off;
SET pg_qi.enabled = on;
SET pg_qi.routing_mode = :'mode';
\echo ACTUAL_MODE
SHOW pg_qi.routing_mode;
SELECT id,grp,value,note FROM pq42_parity ORDER BY id \gdesc
\echo V2_BEFORE_CASE_ORDERED
SHOW pg_qi.decision_v2;
\echo CASE_ORDERED
SELECT id,grp,value,note FROM pq42_parity ORDER BY id;
\echo ROW_COUNT :ROW_COUNT SQLSTATE :SQLSTATE
\echo V2_AFTER_CASE_ORDERED
SHOW pg_qi.decision_v2;
SELECT grp,value,note FROM pq42_parity \gdesc
\echo V2_BEFORE_CASE_MULTISET
SHOW pg_qi.decision_v2;
\echo MULTISET_BEGIN
SELECT grp,value,note FROM pq42_parity;
\echo MULTISET_END
\echo V2_AFTER_CASE_MULTISET
SHOW pg_qi.decision_v2;
SELECT grp,count(*),count(value),sum(value) FROM pq42_parity GROUP BY grp ORDER BY grp \gdesc
\echo V2_BEFORE_CASE_AGGREGATE
SHOW pg_qi.decision_v2;
\echo CASE_AGGREGATE
SELECT grp,count(*),count(value),sum(value) FROM pq42_parity GROUP BY grp ORDER BY grp;
\echo ROW_COUNT :ROW_COUNT SQLSTATE :SQLSTATE
\echo V2_AFTER_CASE_AGGREGATE
SHOW pg_qi.decision_v2;
SELECT value,count(*) FROM pq42_parity GROUP BY value ORDER BY value NULLS FIRST \gdesc
\echo V2_BEFORE_CASE_DUPLICATES_NULLS
SHOW pg_qi.decision_v2;
\echo CASE_DUPLICATES_NULLS
SELECT value,count(*) FROM pq42_parity GROUP BY value ORDER BY value NULLS FIRST;
\echo ROW_COUNT :ROW_COUNT SQLSTATE :SQLSTATE
\echo V2_AFTER_CASE_DUPLICATES_NULLS
SHOW pg_qi.decision_v2;
SELECT count(*) FROM pq42_parity \gdesc
\echo V2_BEFORE_CASE_COUNT
SHOW pg_qi.decision_v2;
\echo CASE_COUNT
SELECT count(*) FROM pq42_parity;
\echo ROW_COUNT :ROW_COUNT SQLSTATE :SQLSTATE
\echo V2_AFTER_CASE_COUNT
SHOW pg_qi.decision_v2;
SET plan_cache_mode = force_custom_plan;
PREPARE pq42_custom(integer) AS SELECT id,note FROM pq42_parity WHERE grp=$1 ORDER BY id;
\echo CASE_PREPARED_CUSTOM
EXECUTE pq42_custom(2) \gdesc
\echo V2_BEFORE_CASE_PREPARED_CUSTOM
SHOW pg_qi.decision_v2;
EXECUTE pq42_custom(2);
\echo V2_AFTER_CASE_PREPARED_CUSTOM
SHOW pg_qi.decision_v2;
\echo V2_BEFORE_CASE_PREPARED_CUSTOM_REUSE
SHOW pg_qi.decision_v2;
EXECUTE pq42_custom(2);
\echo ROW_COUNT :ROW_COUNT SQLSTATE :SQLSTATE
\echo V2_AFTER_CASE_PREPARED_CUSTOM_REUSE
SHOW pg_qi.decision_v2;
DEALLOCATE pq42_custom;
SET plan_cache_mode = force_generic_plan;
PREPARE pq42_generic(integer) AS SELECT id,note FROM pq42_parity WHERE grp=$1 ORDER BY id;
\echo CASE_PREPARED_GENERIC
EXECUTE pq42_generic(1) \gdesc
\echo V2_BEFORE_CASE_PREPARED_GENERIC
SHOW pg_qi.decision_v2;
EXECUTE pq42_generic(1);
\echo V2_AFTER_CASE_PREPARED_GENERIC
SHOW pg_qi.decision_v2;
\echo V2_BEFORE_CASE_PREPARED_GENERIC_REUSE
SHOW pg_qi.decision_v2;
EXECUTE pq42_generic(1);
\echo ROW_COUNT :ROW_COUNT SQLSTATE :SQLSTATE
\echo V2_AFTER_CASE_PREPARED_GENERIC_REUSE
SHOW pg_qi.decision_v2;
DEALLOCATE pq42_generic;
\echo CASE_PLAN
EXPLAIN (COSTS OFF, SETTINGS OFF) SELECT id,value FROM pq42_parity WHERE grp=2 ORDER BY id;
\echo ROW_COUNT :ROW_COUNT SQLSTATE :SQLSTATE
CREATE TEMP TABLE pq42_command_status(v integer);
INSERT INTO pq42_command_status VALUES (1),(2);
UPDATE pq42_command_status SET v=v+1;
DELETE FROM pq42_command_status WHERE v=2;
DROP TABLE pq42_command_status;
\echo V2_BEFORE
SHOW pg_qi.decision_v2;
SELECT id,grp,value,note FROM pq42_parity WHERE false;
\echo V2_AFTER
SHOW pg_qi.decision_v2;
SQL
}

run_errors() {
    local mode=$1 output=$2
    "$pgbin/psql" -X -At -v ON_ERROR_STOP=1 -v mode="$mode" postgres >"$output" 2>/dev/null <<'SQL'
SET lc_messages = 'C';
SET pg_qi.enabled = on;
SET pg_qi.routing_mode = :'mode';
\echo ACTUAL_MODE
SHOW pg_qi.routing_mode;
CREATE TEMP TABLE pq42_zero(v integer);
INSERT INTO pq42_zero VALUES (0);
\set ON_ERROR_STOP off
SELECT 10/v FROM pq42_zero;
\echo runtime_division :SQLSTATE
SELECT 10/0 FROM pq42_parity;
\echo planning_constant :SQLSTATE
SET statement_timeout = '25ms';
SELECT pg_sleep(1);
\echo runtime_timeout :SQLSTATE
\set ON_ERROR_STOP on
RESET statement_timeout;
BEGIN;
SAVEPOINT pq42_save;
\set ON_ERROR_STOP off
SELECT 10/v FROM pq42_zero;
\echo savepoint_error :SQLSTATE
\set ON_ERROR_STOP on
ROLLBACK TO pq42_save;
SELECT 42;
COMMIT;
\echo recovery :SQLSTATE
DO $$DECLARE caught boolean := false; BEGIN
  BEGIN
    PERFORM 10/v FROM pq42_zero;
  EXCEPTION WHEN division_by_zero THEN caught := true;
  END;
  IF NOT caught THEN
    RAISE EXCEPTION 'expected division_by_zero was not caught';
  END IF;
  PERFORM 44;
END$$;
SELECT 43;
\echo plpgsql_recovery :SQLSTATE
SQL
}

run_results off "$artifact_dir/off-results.txt"
run_results shadow_p0 "$artifact_dir/shadow-results.txt"
for marker in CASE_ORDERED CASE_AGGREGATE CASE_DUPLICATES_NULLS CASE_COUNT \
              CASE_PREPARED_CUSTOM CASE_PREPARED_GENERIC CASE_PLAN \
              MULTISET_BEGIN MULTISET_END V2_BEFORE V2_AFTER; do
    test "$(grep -cx "$marker" "$artifact_dir/off-results.txt")" -eq 1
    test "$(grep -cx "$marker" "$artifact_dir/shadow-results.txt")" -eq 1
done
grep -A3 -x 'ACTUAL_MODE' "$artifact_dir/off-results.txt" | grep -qx 'off'
grep -A3 -x 'ACTUAL_MODE' "$artifact_dir/shadow-results.txt" | grep -qx 'shadow_p0'
normalize() {
    sed '/^ACTUAL_MODE$/,+3d; /^V2_BEFORE$/,+3d; /^V2_AFTER$/,+3d; /^V2_\(BEFORE\|AFTER\)_CASE_/,+3d' "$1" |
        sed '/^MULTISET_BEGIN$/,/^MULTISET_END$/d'
}
normalize "$artifact_dir/off-results.txt" >"$artifact_dir/off-results-normalized.txt"
normalize "$artifact_dir/shadow-results.txt" >"$artifact_dir/shadow-results-normalized.txt"
cmp "$artifact_dir/off-results-normalized.txt" "$artifact_dir/shadow-results-normalized.txt"
python3 "$repo/tests/phase7/verify_parity.py" results \
    "$artifact_dir/off-results.txt" "$artifact_dir/shadow-results.txt"
python3 - "$artifact_dir/off-results.txt" "$artifact_dir/shadow-results.txt" <<'PY'
from collections import Counter
import sys
def rows(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    begin, end = lines.index("MULTISET_BEGIN"), lines.index("MULTISET_END")
    return Counter(lines[begin + 1:end])
assert rows(sys.argv[1]) == rows(sys.argv[2])
PY
python3 - "$repo" "$artifact_dir/shadow-results.txt" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools/phase7"))
from decision_snapshot import parse_v2
sys.path.insert(0, str(Path(sys.argv[1]) / "tests/phase7"))
from verify_parity import verify_case_snapshots
lines = Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
def value_after(marker):
    start = lines.index(marker)
    for value in lines[start + 1:start + 6]:
        if value.startswith("v=2;") or value == "none":
            return value
    raise AssertionError(marker)
before_raw, after = value_after("V2_BEFORE"), parse_v2(value_after("V2_AFTER"))
before_event = 0 if before_raw == "none" else int(parse_v2(before_raw).get("e", 0))
before_snapshot = None if before_raw == "none" else parse_v2(before_raw)
assert after["mode"] == "shadow_p0" and after["s"] == "complete"
assert int(after["e"]) == before_event + 1
if before_snapshot is not None:
    assert (after["cid"], after["g"]) == (before_snapshot["cid"], before_snapshot["g"])
assert after["p0"] == "unknown" and after["p0_reason"] == "unsupported_shape"
verify_case_snapshots(lines)
PY
run_errors off "$artifact_dir/off-errors.txt"
run_errors shadow_p0 "$artifact_dir/shadow-errors.txt"
grep -A1 -x 'ACTUAL_MODE' "$artifact_dir/off-errors.txt" | grep -qx 'off'
grep -A1 -x 'ACTUAL_MODE' "$artifact_dir/shadow-errors.txt" | grep -qx 'shadow_p0'
sed '/^ACTUAL_MODE$/,+1d' "$artifact_dir/off-errors.txt" >"$artifact_dir/off-errors-normalized.txt"
sed '/^ACTUAL_MODE$/,+1d' "$artifact_dir/shadow-errors.txt" >"$artifact_dir/shadow-errors-normalized.txt"
cmp "$artifact_dir/off-errors-normalized.txt" "$artifact_dir/shadow-errors-normalized.txt"
python3 "$repo/tests/phase7/verify_parity.py" errors \
    "$artifact_dir/off-errors.txt" "$artifact_dir/shadow-errors.txt"
grep -qx 'runtime_division 22012' "$artifact_dir/off-errors.txt"
grep -qx 'planning_constant 22012' "$artifact_dir/off-errors.txt"
grep -qx 'runtime_timeout 57014' "$artifact_dir/off-errors.txt"
grep -qx 'savepoint_error 22012' "$artifact_dir/off-errors.txt"
grep -qx 'recovery 00000' "$artifact_dir/off-errors.txt"
grep -qx 'plpgsql_recovery 00000' "$artifact_dir/off-errors.txt"
grep -qx 'COMMIT' "$artifact_dir/off-errors.txt"

snapshot=$(
    "$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres \
        -c 'SET pg_qi.routing_mode=shadow_p0' \
        -c 'SHOW pg_qi.shadow_decision_id' \
        -c 'SELECT count(*) FROM pq42_parity' \
        -c 'SHOW pg_qi.shadow_decision_id' \
        -c 'SHOW pg_qi.decision_v2'
)
python3 - "$repo" "$snapshot" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools/phase7"))
from decision_snapshot import parse_v2
lines = sys.argv[2].splitlines()
assert len(lines) == 4, lines
before, result, after, encoded = lines
assert int(after) == int(before) + 1, lines
assert result == "5", lines
value = parse_v2(encoded)
assert value["s"] == "complete", value
assert value["mode"] == "shadow_p0", value
assert int(value["e"]) > 0, value
assert value["p0"] == "bounded" and value["p0_reason"] == "ok", value
PY
printf '%s\n' "$snapshot" >"$artifact_dir/shadow-v2.txt"
printf 'PASS\n' >"$artifact_dir/STATUS"
"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres \
    -c 'DROP TABLE pq42_parity' -c 'DROP EXTENSION pg_qi' \
    -c 'DROP EXTENSION pg_duckdb' >/dev/null
echo "PQ-42 production non-interference verification PASS"
