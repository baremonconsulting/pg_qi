#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
artifact_dir=${2:?artifact directory is required}
topology=${3:-dynamic}
case "$topology" in dynamic|preloaded) ;; *) echo "invalid hook topology: $topology" >&2; exit 2;; esac
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=$pg_install_root/$major/bin
repo=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$artifact_dir"
output=$artifact_dir/runtime.txt
errors=$artifact_dir/runtime-errors.txt

"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres >"$output" 2>"$errors" <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
DROP TABLE IF EXISTS pq42_runtime;
CREATE TABLE pq42_runtime(id integer PRIMARY KEY, divisor integer);
INSERT INTO pq42_runtime VALUES (1, 1), (2, 0);
SET pg_qi.enabled = on;
SET pg_qi.routing_mode = shadow_p0;
\echo BASELINE_CONTROL
SELECT id FROM pq42_runtime WHERE id=1;
CREATE FUNCTION pg_qi_outer_test_reset_metrics() RETURNS void
AS '$libdir/pg_qi_outer_test', 'pg_qi_outer_test_reset_metrics'
LANGUAGE C;
CREATE FUNCTION pg_qi_outer_test_restore_all_hooks() RETURNS void
AS '$libdir/pg_qi_outer_test', 'pg_qi_outer_test_restore_all_hooks'
LANGUAGE C;
SELECT pg_qi_outer_test_reset_metrics();
\echo BASELINE_V2
SHOW pg_qi.decision_v2;
\echo BASELINE_METRICS
SHOW pg_qi_outer_test.executor_start_calls;
SHOW pg_qi_outer_test.executor_run_calls;
SHOW pg_qi_outer_test.executor_finish_calls;
SHOW pg_qi_outer_test.executor_end_calls;
SHOW pg_qi_outer_test.executor_spi_calls;
SHOW pg_qi_outer_test.executor_spi_finish_calls;
SHOW pg_qi_outer_test.executor_run_active;
SET pg_qi_outer_test.spi_during_executor_run = on;
SELECT id FROM pq42_runtime WHERE id=1;
\echo PROBE_RESULT :ROW_COUNT :SQLSTATE
\echo PROBE_V2
SHOW pg_qi.decision_v2;
\echo PROBE_METRICS
SHOW pg_qi_outer_test.executor_start_calls;
SHOW pg_qi_outer_test.executor_run_calls;
SHOW pg_qi_outer_test.executor_finish_calls;
SHOW pg_qi_outer_test.executor_end_calls;
SHOW pg_qi_outer_test.executor_spi_calls;
SHOW pg_qi_outer_test.executor_spi_finish_calls;
SHOW pg_qi_outer_test.executor_run_active;
SHOW pg_qi_outer_test.process_utility_calls;
\echo UTILITY_BEFORE
SHOW pg_qi_outer_test.process_utility_calls;
SET application_name = 'pq42_utility_probe';
\echo UTILITY_AFTER
SHOW pg_qi_outer_test.process_utility_calls;
SET pg_qi_outer_test.spi_error = on;
\set ON_ERROR_STOP off
SELECT id FROM pq42_runtime WHERE id=1;
\echo SPI_DIVISION :SQLSTATE
\set ON_ERROR_STOP on
SET pg_qi_outer_test.spi_error = off;
SELECT id FROM pq42_runtime WHERE id=1;
\echo SPI_DIVISION_RECOVERY :SQLSTATE
\echo SPI_DIVISION_RECOVERY_V2
SHOW pg_qi.decision_v2;
SHOW pg_qi_outer_test.executor_spi_calls;
SHOW pg_qi_outer_test.executor_spi_finish_calls;
SHOW pg_qi_outer_test.executor_run_active;
SET statement_timeout='25ms';
SET pg_qi_outer_test.spi_cancel = on;
\set ON_ERROR_STOP off
SELECT id FROM pq42_runtime WHERE id=1;
\echo SPI_TIMEOUT :SQLSTATE
\set ON_ERROR_STOP on
RESET statement_timeout;
SET pg_qi_outer_test.spi_cancel = off;
SELECT id FROM pq42_runtime WHERE id=1;
\echo SPI_TIMEOUT_RECOVERY :SQLSTATE
\echo SPI_TIMEOUT_RECOVERY_V2
SHOW pg_qi.decision_v2;
SHOW pg_qi_outer_test.executor_spi_calls;
SHOW pg_qi_outer_test.executor_spi_finish_calls;
SHOW pg_qi_outer_test.executor_run_active;
SET pg_qi_outer_test.spi_during_executor_run = off;
\set ON_ERROR_STOP off
SELECT 10/divisor FROM pq42_runtime WHERE id=2;
\echo DIVISION :SQLSTATE
SET statement_timeout='25ms';
SELECT pg_sleep(1);
\echo TIMEOUT :SQLSTATE
\set ON_ERROR_STOP on
RESET statement_timeout;
BEGIN;
SAVEPOINT pq42_save;
SET LOCAL pg_qi_outer_test.spi_during_executor_run = on;
SET LOCAL pg_qi_outer_test.spi_error = on;
\set ON_ERROR_STOP off
SELECT id FROM pq42_runtime WHERE id=1;
\echo SAVEPOINT :SQLSTATE
\set ON_ERROR_STOP on
ROLLBACK TO pq42_save;
SET LOCAL pg_qi_outer_test.spi_during_executor_run = on;
SET LOCAL pg_qi_outer_test.spi_error = off;
\echo SAVEPOINT_BEFORE_RECOVERY_V2
SHOW pg_qi.decision_v2;
SELECT 42;
\echo SAVEPOINT_RECOVERY_RESULT :ROW_COUNT :SQLSTATE
COMMIT;
\echo SAVEPOINT_RECOVERY :SQLSTATE
\echo SAVEPOINT_RECOVERY_V2
SHOW pg_qi.decision_v2;
SHOW pg_qi_outer_test.executor_spi_calls;
SHOW pg_qi_outer_test.executor_spi_finish_calls;
SHOW pg_qi_outer_test.executor_run_active;
SET pg_qi_outer_test.spi_during_executor_run = on;
SET pg_qi_outer_test.spi_error = on;
DO $$DECLARE caught boolean := false; BEGIN
  BEGIN
    PERFORM id FROM pq42_runtime WHERE id=1;
  EXCEPTION WHEN division_by_zero THEN caught := true;
  END;
  IF NOT caught THEN
    RAISE EXCEPTION 'expected nested SPI division_by_zero was not caught';
  END IF;
  EXECUTE 'SET LOCAL pg_qi_outer_test.spi_error = off';
  PERFORM 44;
END$$;
SET pg_qi_outer_test.spi_error = off;
SELECT 43;
\echo PLPGSQL_RECOVERY :SQLSTATE
\echo PLPGSQL_RECOVERY_V2
SHOW pg_qi.decision_v2;
SHOW pg_qi_outer_test.executor_spi_calls;
SHOW pg_qi_outer_test.executor_spi_finish_calls;
SHOW pg_qi_outer_test.executor_run_active;
SET pg_qi_outer_test.spi_during_executor_run = off;
\echo FORCE_AFTER_RECOVERY
SHOW duckdb.force_execution;
SELECT pg_qi_outer_test_restore_all_hooks();
SELECT id FROM pq42_runtime WHERE id=1;
\echo FINAL_RECOVERY :SQLSTATE
\echo FINAL_RECOVERY_V2
SHOW pg_qi.decision_v2;
DROP TABLE pq42_runtime;
DROP FUNCTION pg_qi_outer_test_reset_metrics();
DROP FUNCTION pg_qi_outer_test_restore_all_hooks();
DROP EXTENSION pg_qi;
DROP EXTENSION pg_duckdb;
SQL

grep -qx 'DIVISION 22012' "$output"
grep -qx 'PROBE_RESULT 1 00000' "$output"
grep -qx 'SPI_DIVISION 22012' "$output"
grep -qx 'SPI_DIVISION_RECOVERY 00000' "$output"
grep -qx 'SPI_TIMEOUT 57014' "$output"
grep -qx 'SPI_TIMEOUT_RECOVERY 00000' "$output"
grep -qx 'TIMEOUT 57014' "$output"
grep -qx 'SAVEPOINT 22012' "$output"
grep -qx 'SAVEPOINT_RECOVERY_RESULT 1 00000' "$output"
grep -qx 'SAVEPOINT_RECOVERY 00000' "$output"
grep -qx 'PLPGSQL_RECOVERY 00000' "$output"
grep -qx 'FINAL_RECOVERY 00000' "$output"
python3 - "$repo" "$output" "$topology" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools/phase7"))
from decision_snapshot import parse_v2
lines = Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
topology = sys.argv[3]
baseline_pos = lines.index("BASELINE_V2")
probe_pos = lines.index("PROBE_V2")
baseline = parse_v2(lines[baseline_pos + 1])
snapshot = parse_v2(lines[probe_pos + 1])
assert baseline["s"] == "complete" and snapshot["s"] == "complete"
assert baseline["mode"] == snapshot["mode"] == "shadow_p0"
assert (snapshot["cid"], snapshot["g"]) == (baseline["cid"], baseline["g"])
if topology == "dynamic":
    # The dynamically loaded outer fixture makes the hook chain unproved.
    # pg_qi must fail closed until the fixture restores the original hooks.
    assert snapshot == baseline, (baseline, snapshot)
else:
    # When the fixture is preloaded before pg_qi, pg_qi remains outermost and
    # each completed client query must publish a fresh decision.
    assert int(snapshot["e"]) == int(baseline["e"]) + 1, (baseline, snapshot)
baseline_metrics_pos = lines.index("BASELINE_METRICS")
probe_metrics_pos = lines.index("PROBE_METRICS")
baseline_metrics = [int(value) for value in lines[baseline_metrics_pos + 1:baseline_metrics_pos + 7]]
metrics = lines[probe_metrics_pos + 1:probe_metrics_pos + 9]
start, run, finish, end, spi, spi_finish = map(int, metrics[:6])
active, utility = metrics[6], int(metrics[7])
assert [start - baseline_metrics[0], run - baseline_metrics[1],
        finish - baseline_metrics[2], end - baseline_metrics[3],
        spi - baseline_metrics[4], spi_finish - baseline_metrics[5]] == [2, 2, 2, 2, 1, 1], (baseline_metrics, metrics)
assert active == "off", metrics
assert utility >= 1, metrics
utility_before = int(lines[lines.index("UTILITY_BEFORE") + 1])
utility_after = int(lines[lines.index("UTILITY_AFTER") + 1])
assert utility_after - utility_before == 2, (utility_before, utility_after)
division_recovery = lines.index("SPI_DIVISION_RECOVERY 00000")
timeout_recovery = lines.index("SPI_TIMEOUT_RECOVERY 00000")
assert lines[division_recovery + 3:division_recovery + 6] == ["2", "3", "off"], lines[division_recovery:division_recovery+6]
assert lines[timeout_recovery + 3:timeout_recovery + 6] == ["3", "5", "off"], lines[timeout_recovery:timeout_recovery+6]
recovery_markers = [
    "SPI_DIVISION_RECOVERY_V2", "SPI_TIMEOUT_RECOVERY_V2",
    "SAVEPOINT_BEFORE_RECOVERY_V2", "SAVEPOINT_RECOVERY_V2",
    "PLPGSQL_RECOVERY_V2",
]
recovered_snapshots = [snapshot]
for marker in recovery_markers:
    recovered = parse_v2(lines[lines.index(marker) + 1])
    assert recovered["s"] == "complete" and recovered["mode"] == "shadow_p0", recovered
    assert (recovered["cid"], recovered["g"]) == (baseline["cid"], baseline["g"])
    recovered_snapshots.append(recovered)
events = [int(value["e"]) for value in recovered_snapshots]
if topology == "dynamic":
    assert all(value == baseline for value in recovered_snapshots), recovered_snapshots
else:
    expected_deltas = [2, 2, 3, 1, 1]
    assert [right - left for left, right in zip(events, events[1:])] == expected_deltas, events
final_recovery = parse_v2(lines[lines.index("FINAL_RECOVERY_V2") + 1])
assert final_recovery["s"] == "complete" and final_recovery["mode"] == "shadow_p0"
assert (final_recovery["cid"], final_recovery["g"]) == (baseline["cid"], baseline["g"])
if topology == "dynamic":
    assert int(final_recovery["e"]) == int(baseline["e"]) + 1, (
        baseline, final_recovery)
else:
    assert int(final_recovery["e"]) == events[-1] + 2, (events, final_recovery)
savepoint_before = parse_v2(lines[lines.index("SAVEPOINT_BEFORE_RECOVERY_V2") + 1])
savepoint_after = parse_v2(lines[lines.index("SAVEPOINT_RECOVERY_V2") + 1])
assert savepoint_before["s"] == savepoint_after["s"] == "complete"
assert savepoint_before["mode"] == savepoint_after["mode"] == "shadow_p0"
if topology == "dynamic":
    assert savepoint_before == savepoint_after == baseline, (
        baseline, savepoint_before, savepoint_after)
else:
    assert (savepoint_before["cid"], savepoint_before["g"]) == (baseline["cid"], baseline["g"])
    assert (savepoint_after["cid"], savepoint_after["g"]) == (baseline["cid"], baseline["g"])
    assert int(savepoint_after["e"]) == int(savepoint_before["e"]) + 1, (
        savepoint_before, savepoint_after)
savepoint_pos = lines.index("SAVEPOINT_RECOVERY_V2")
assert lines[savepoint_pos + 2:savepoint_pos + 5] == ["4", "7", "off"], lines[savepoint_pos:savepoint_pos+5]
plpgsql_pos = lines.index("PLPGSQL_RECOVERY_V2")
assert lines[plpgsql_pos + 2:plpgsql_pos + 5] == ["6", "10", "off"], lines[plpgsql_pos:plpgsql_pos+5]
assert lines[lines.index("FORCE_AFTER_RECOVERY") + 1] == "off", lines
PY
grep -Fq 'CONTEXT:  SQL statement "SELECT pg_sleep(1)"' "$errors"
printf 'PASS\n' >"$artifact_dir/STATUS"
echo "PQ-42 runtime hook and cleanup verification PASS"
