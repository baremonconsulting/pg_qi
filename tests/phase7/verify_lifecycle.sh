#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=$pg_install_root/$major/bin
repo=$(cd "$(dirname "$0")/../.." && pwd)
output=$(mktemp)
driver=$(mktemp)
cleanup() {
    status=$?
    if test "$status" -ne 0; then
        sed -n '1,240p' "$output" >&2
    fi
    rm -f "$output" "$driver"
    exit "$status"
}
trap cleanup EXIT

"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres >"$output" <<'SQL'
DROP TABLE IF EXISTS lifecycle_probe CASCADE;
DROP TABLE IF EXISTS lifecycle_skew CASCADE;
DROP FUNCTION IF EXISTS lifecycle_parameter();
CREATE TABLE lifecycle_probe(id integer PRIMARY KEY);
INSERT INTO lifecycle_probe VALUES (1), (2);
SET pg_qi.routing_mode = shadow_p0;
SELECT id FROM lifecycle_probe WHERE id = 1;
SHOW pg_qi.decision_v2;
SET plan_cache_mode = force_generic_plan;
PREPARE lifecycle_generic(integer) AS
    SELECT id FROM lifecycle_probe WHERE id = $1;
EXECUTE lifecycle_generic(1);
SHOW pg_qi.decision_v2;
EXECUTE lifecycle_generic(2);
SHOW pg_qi.decision_v2;
SET pg_qi.routing_mode = off;
EXECUTE lifecycle_generic(1);
SHOW pg_qi.decision_v2;
ALTER TABLE lifecycle_probe ADD COLUMN sql_marker integer;
EXECUTE lifecycle_generic(1);
SHOW pg_qi.decision_v2;
SET pg_qi.routing_mode = shadow_p0;
SET plan_cache_mode = force_custom_plan;
PREPARE lifecycle_custom(integer) AS
    SELECT id FROM lifecycle_probe WHERE id = $1;
EXECUTE lifecycle_custom(1);
SHOW pg_qi.decision_v2;
EXECUTE lifecycle_custom(2);
SHOW pg_qi.decision_v2;
SET pg_qi.routing_mode = allowlist;
CREATE FUNCTION lifecycle_parameter() RETURNS integer LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 42;
    RETURN 1;
END
$$;
PREPARE lifecycle_nested(integer) AS
    SELECT id FROM lifecycle_probe WHERE id = $1;
SHOW pg_qi.decision_v2;
EXECUTE lifecycle_nested(lifecycle_parameter());
SHOW pg_qi.decision_v2;
BEGIN;
DECLARE lifecycle_cursor CURSOR FOR SELECT id FROM lifecycle_probe ORDER BY id;
SHOW pg_qi.decision_v2;
FETCH 1 FROM lifecycle_cursor;
SHOW pg_qi.decision_v2;
CLOSE lifecycle_cursor;
COMMIT;
CREATE TABLE lifecycle_skew(v integer);
INSERT INTO lifecycle_skew SELECT 1 UNION ALL
    SELECT 2 FROM generate_series(1, 100000);
CREATE INDEX ON lifecycle_skew(v);
ANALYZE lifecycle_skew;
BEGIN;
SET plan_cache_mode = auto;
PREPARE lifecycle_auto(integer) AS
    SELECT count(*) FROM lifecycle_skew WHERE v = $1;
EXECUTE lifecycle_auto(1);
EXECUTE lifecycle_auto(1);
EXECUTE lifecycle_auto(1);
EXECUTE lifecycle_auto(1);
EXECUTE lifecycle_auto(1);
EXECUTE lifecycle_auto(1);
SHOW pg_qi.decision_v2;
SET plan_cache_mode = force_generic_plan;
EXECUTE lifecycle_auto(1);
SHOW pg_qi.decision_v2;
COMMIT;
SELECT 'CACHE_COUNTS', generic_plans, custom_plans
FROM pg_prepared_statements WHERE name = 'lifecycle_auto';
SQL

python3 - "$repo" "$output" <<'PY'
import sys
from pathlib import Path

repo, output = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo / "tools/phase7"))
from decision_snapshot import parse_v2

lines = output.read_text().splitlines()
snapshots = [parse_v2(line) for line in lines if line.startswith("v=2;")]
assert len(snapshots) == 13, (len(snapshots), lines)
assert all(value["s"] == "complete" for value in snapshots)
assert all(value["lifecycle"] == "fresh_execution" for value in snapshots)
events = [int(value["e"]) for value in snapshots]
assert events[1] > events[0]                 # first generic execution
assert events[2] == events[1]                # cached generic reuse abstains
assert events[3] == events[2]                # GUC change does not imply replan
assert events[4] > events[3]                 # DDL invalidation replans
assert events[5] > events[4]                 # first custom plan
assert events[6] > events[5]                 # second custom plan
assert events[7] == events[6]                # pre-execution control read
assert events[8] == events[7] + 1            # nested SPI is not published
assert snapshots[8]["mode"] == "allowlist"
assert snapshots[8]["policy"] == "pass_through"
assert snapshots[8]["force"] == "off"
assert events[9] == events[8]                # DECLARE remains unproved
assert events[10] == events[9]               # FETCH remains unproved
assert events[11] > events[10]               # auto custom plan publishes
assert events[12] == events[11]              # rejected generic cannot publish later
counts = next((line for line in lines if line.startswith("CACHE_COUNTS|")), None)
assert counts is not None, lines
_, generic, custom = counts.split("|")
assert int(generic) >= 1 and int(custom) >= 6, counts
PY

client_pg_config=${PG_CLIENT_CONFIG:-"$pgbin/pg_config"}
cc -std=c99 -Wall -Wextra -Werror \
    -I"$("$client_pg_config" --includedir)" \
    "$repo/tests/phase7/lifecycle_driver.c" \
    -L"$("$client_pg_config" --libdir)" -Wl,-rpath,"$("$client_pg_config" --libdir)" \
    -lpq -o "$driver"
"$driver"
session_a=$("$pgbin/psql" -X -qAt postgres \
    -c 'SET pg_qi.routing_mode=shadow_p0' \
    -c 'SELECT id FROM lifecycle_probe WHERE id=1' \
    -c 'SHOW pg_qi.decision_v2' | tail -n 1)
session_b=$("$pgbin/psql" -X -qAt postgres \
    -c 'SET pg_qi.routing_mode=shadow_p0' \
    -c 'SELECT id FROM lifecycle_probe WHERE id=2' \
    -c 'SHOW pg_qi.decision_v2' | tail -n 1)
python3 - "$repo" "$session_a" "$session_b" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools/phase7"))
from decision_snapshot import parse_v2
a, b = parse_v2(sys.argv[2]), parse_v2(sys.argv[3])
assert a["s"] == b["s"] == "complete"
assert int(a["e"]) > 0 and int(b["e"]) > 0
assert a["cid"] != b["cid"]
PY

"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres <<'SQL' >/dev/null
DROP TABLE lifecycle_probe CASCADE;
DROP TABLE lifecycle_skew CASCADE;
DROP FUNCTION lifecycle_parameter();
SQL

echo "PQ-39 plan lifecycle verification PASS"
