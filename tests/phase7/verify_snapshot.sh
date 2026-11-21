#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=$pg_install_root/$major/bin
repo=$(cd "$(dirname "$0")/../.." && pwd)
output=$(mktemp)
errors=$(mktemp)
cleanup() {
    status=$?
    if test "$status" -ne 0; then
        sed -n '1,240p' "$output" >&2
        sed -n '1,240p' "$errors" >&2
    fi
    rm -f "$output" "$errors"
    exit "$status"
}
trap cleanup EXIT

"$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres >"$output" 2>"$errors" <<'SQL'
\echo INITIAL
SHOW pg_qi.decision_v2;
CREATE ROLE pgqi_diagnostic NOLOGIN;
CREATE ROLE snapshot_plain;
CREATE ROLE snapshot_diagnostic;
GRANT pgqi_diagnostic TO snapshot_diagnostic;
CREATE TABLE snapshot_plain(id integer PRIMARY KEY);
INSERT INTO snapshot_plain VALUES (1);
GRANT SELECT ON snapshot_plain TO snapshot_plain, snapshot_diagnostic;
SET pg_qi.routing_mode = shadow_p0;
\o /dev/null
SELECT id FROM snapshot_plain WHERE id = 1;
\o
\echo FIRST
SHOW pg_qi.decision_v2;
\echo FIRST_AGAIN
SHOW pg_qi.decision_v2;
\set ON_ERROR_STOP off
\o /dev/null
SELECT 1 / 0;
\o
\set ON_ERROR_STOP on
\echo AFTER_ERROR
SHOW pg_qi.decision_v2;
SET pg_qi_outer_test.cancel_unconditionally = on;
\set ON_ERROR_STOP off
\o /dev/null
SELECT id FROM snapshot_plain WHERE id = 1;
\o
\set ON_ERROR_STOP on
SET pg_qi_outer_test.cancel_unconditionally = off;
\echo AFTER_CANCEL
SHOW pg_qi.decision_v2;
\echo CURRENT_SETTING
SELECT current_setting('pg_qi.decision_v2');
SET pg_qi.routing_mode = off;
\echo MODE_OLD
SHOW pg_qi.decision_v2;
\o /dev/null
SELECT id FROM snapshot_plain WHERE id = 1;
\o
\echo MODE_NEW
SHOW pg_qi.decision_v2;
CREATE FUNCTION snapshot_spi() RETURNS integer LANGUAGE plpgsql AS $$
DECLARE result integer;
BEGIN
    EXECUTE 'SELECT 42' INTO result;
    RETURN result;
END
$$;
\o /dev/null
SELECT snapshot_spi();
\o
\echo AFTER_SPI
SHOW pg_qi.decision_v2;
CREATE TABLE snapshot_deferred(id integer PRIMARY KEY);
CREATE FUNCTION snapshot_deferred_fire() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE 'EXECUTE snapshot_trigger_prepared';
    PERFORM 42;
    RETURN NEW;
END
$$;
PREPARE snapshot_trigger_prepared AS
    SELECT id FROM snapshot_plain WHERE id = 1;
CREATE CONSTRAINT TRIGGER snapshot_deferred_trigger
AFTER INSERT ON snapshot_deferred DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION snapshot_deferred_fire();
\echo BEFORE_DEFERRED
SHOW pg_qi.decision_v2;
\o /dev/null
INSERT INTO snapshot_deferred VALUES (1);
\o
\echo AFTER_DEFERRED
SHOW pg_qi.decision_v2;
CREATE TABLE snapshot_parent(id integer PRIMARY KEY);
CREATE TABLE snapshot_child(
    id integer REFERENCES snapshot_parent(id) DEFERRABLE INITIALLY DEFERRED);
INSERT INTO snapshot_parent VALUES (1);
\echo BEFORE_DEFERRED_FK
SHOW pg_qi.decision_v2;
BEGIN;
\o /dev/null
INSERT INTO snapshot_child VALUES (1);
\o
\echo AFTER_DEFERRED_FK_INSERT
SHOW pg_qi.decision_v2;
COMMIT;
DEALLOCATE snapshot_trigger_prepared;
\echo AFTER_DEFERRED_FK_COMMIT
SHOW pg_qi.decision_v2;
SET ROLE snapshot_plain;
\echo DENIED
SHOW pg_qi.decision_v2;
\echo DENIED_CURRENT_SETTING
SELECT current_setting('pg_qi.decision_v2');
\echo SETTINGS_COUNT
SELECT count(*) FROM pg_settings WHERE name = 'pg_qi.decision_v2';
RESET ROLE;
\echo AFTER_ROLE_RESET
SHOW pg_qi.decision_v2;
SET ROLE snapshot_diagnostic;
\o /dev/null
SELECT id FROM snapshot_plain WHERE id = 1;
\o
\echo DIAGNOSTIC_ROLE
SHOW pg_qi.decision_v2;
RESET ROLE;
ALTER ROLE pgqi_diagnostic LOGIN;
SET ROLE snapshot_diagnostic;
\echo LOGIN_DIAGNOSTIC_DENIED
SHOW pg_qi.decision_v2;
RESET ROLE;
ALTER ROLE pgqi_diagnostic NOLOGIN;
REVOKE pgqi_diagnostic FROM snapshot_diagnostic;
SET ROLE snapshot_diagnostic;
\o /dev/null
SELECT id FROM snapshot_plain WHERE id = 1;
\o
\echo REVOKED
SHOW pg_qi.decision_v2;
RESET ROLE;
SET SESSION AUTHORIZATION snapshot_plain;
\echo SESSION_DENIED
SHOW pg_qi.decision_v2;
RESET SESSION AUTHORIZATION;
\echo AFTER_SESSION_RESET
SHOW pg_qi.decision_v2;
SET pg_qi.routing_mode = shadow_p0;
\o /dev/null
SELECT count(*) FROM snapshot_plain;
\o
\echo SEMANTIC_COUNT
SHOW pg_qi.decision_v2;
\o /dev/null
SELECT count(*) FROM snapshot_plain TABLESAMPLE SYSTEM (100) REPEATABLE (42);
\o
\echo Q28_NOT_EVALUATED
SHOW pg_qi.decision_v2;
\echo Q30_NATIVE_TYPE_SCALE
SELECT pg_typeof(v), v::text FROM (
    SELECT round(count(*) FILTER (WHERE id > 0)::numeric / 3, 2) AS v
    FROM snapshot_plain
) AS q;
\echo Q30_NOT_EVALUATED
SHOW pg_qi.decision_v2;
DISCARD ALL;
\echo AFTER_DISCARD
SHOW pg_qi.decision_v2;
DROP FUNCTION snapshot_spi();
DROP TABLE snapshot_deferred;
DROP FUNCTION snapshot_deferred_fire();
DROP TABLE snapshot_child;
DROP TABLE snapshot_parent;
DROP TABLE snapshot_plain;
DROP OWNED BY snapshot_plain;
DROP OWNED BY snapshot_diagnostic;
DROP ROLE snapshot_plain;
DROP ROLE snapshot_diagnostic;
DROP ROLE pgqi_diagnostic;
SQL

test "$(grep -Ec '^ERROR:  division by zero$' "$errors")" -eq 1
test "$(grep -Ec '^ERROR:  pg_qi outer fixture planner cancellation$' "$errors")" -eq 1
grep -q '^numeric|0.33$' "$output"

python3 - "$repo" "$output" <<'PY'
import sys
from pathlib import Path

repo, output = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo / "tools/phase7"))
from decision_snapshot import parse_v2

lines = output.read_text().splitlines()
tags = {"INITIAL", "FIRST", "FIRST_AGAIN", "AFTER_ERROR", "AFTER_CANCEL",
        "CURRENT_SETTING",
        "MODE_OLD", "MODE_NEW", "AFTER_SPI", "BEFORE_DEFERRED",
        "AFTER_DEFERRED", "BEFORE_DEFERRED_FK", "AFTER_DEFERRED_FK_INSERT",
        "AFTER_DEFERRED_FK_COMMIT", "DENIED", "AFTER_ROLE_RESET",
        "DENIED_CURRENT_SETTING", "SETTINGS_COUNT", "DIAGNOSTIC_ROLE",
        "LOGIN_DIAGNOSTIC_DENIED", "REVOKED", "SESSION_DENIED",
        "AFTER_SESSION_RESET", "SEMANTIC_COUNT", "Q28_NOT_EVALUATED",
        "Q30_NOT_EVALUATED", "AFTER_DISCARD"}
values = {}
for index, line in enumerate(lines):
    if line in tags:
        if index + 1 >= len(lines):
            raise SystemExit(f"missing snapshot after {line}")
        values[line] = lines[index + 1]
if set(values) != tags:
    raise SystemExit(f"missing tags: {sorted(tags - set(values))}")

parsed = {name: parse_v2(value) for name, value in values.items()
          if name != "SETTINGS_COUNT"}
assert parsed["INITIAL"]["s"] == "none"
assert parsed["FIRST"]["s"] == "complete"
assert parsed["FIRST"] == parsed["FIRST_AGAIN"] == parsed["AFTER_ERROR"]
assert parsed["AFTER_CANCEL"] == parsed["FIRST"]
assert int(parsed["CURRENT_SETTING"]["e"]) == int(parsed["FIRST"]["e"]) + 1
assert parsed["CURRENT_SETTING"]["mode"] == "shadow_p0"
assert parsed["MODE_OLD"] == parsed["CURRENT_SETTING"]
assert parsed["MODE_NEW"]["mode"] == "off"
assert int(parsed["MODE_NEW"]["e"]) == int(parsed["MODE_OLD"]["e"]) + 1
assert int(parsed["AFTER_SPI"]["e"]) == int(parsed["MODE_NEW"]["e"]) + 1
assert parsed["AFTER_DEFERRED"] == parsed["BEFORE_DEFERRED"]
assert parsed["AFTER_DEFERRED_FK_INSERT"] == parsed["BEFORE_DEFERRED_FK"]
assert parsed["AFTER_DEFERRED_FK_COMMIT"] == parsed["AFTER_DEFERRED_FK_INSERT"]
assert parsed["DENIED"]["s"] == "denied"
assert parsed["DENIED_CURRENT_SETTING"]["s"] == "denied"
assert values["SETTINGS_COUNT"] == "0"
assert parsed["AFTER_ROLE_RESET"]["s"] == "none"
assert parsed["DIAGNOSTIC_ROLE"]["s"] == "complete"
assert parsed["LOGIN_DIAGNOSTIC_DENIED"]["s"] == "denied"
assert parsed["REVOKED"]["s"] == "denied"
assert parsed["SESSION_DENIED"]["s"] == "denied"
assert parsed["AFTER_SESSION_RESET"]["s"] == "none"
assert parsed["SEMANTIC_COUNT"]["s"] == "complete"
assert parsed["SEMANTIC_COUNT"]["safe"] == "unknown"
assert parsed["SEMANTIC_COUNT"]["barrier"] == "closed"
assert parsed["SEMANTIC_COUNT"]["reason"] == "500"
assert parsed["SEMANTIC_COUNT"]["p0"] == "bounded"
assert parsed["SEMANTIC_COUNT"]["recommendation"] == "insufficient_evidence"
assert parsed["SEMANTIC_COUNT"]["mode"] == "shadow_p0"
assert parsed["Q28_NOT_EVALUATED"]["safe"] == "not_evaluated"
assert parsed["Q28_NOT_EVALUATED"]["reason"] == "0"
assert parsed["Q30_NOT_EVALUATED"]["safe"] == "not_evaluated"
assert parsed["Q30_NOT_EVALUATED"]["reason"] == "0"
assert parsed["AFTER_DISCARD"]["s"] == "none"
assert int(parsed["AFTER_DISCARD"]["g"]) > int(parsed["INITIAL"]["g"])
PY

session_a=$("$pgbin/psql" -X -qAt postgres -c 'SHOW pg_qi.decision_v2')
session_b=$("$pgbin/psql" -X -qAt postgres -c 'SHOW pg_qi.decision_v2')
python3 - "$repo" "$session_a" "$session_b" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools/phase7"))
from decision_snapshot import parse_v2
a, b = parse_v2(sys.argv[2]), parse_v2(sys.argv[3])
assert a["s"] == b["s"] == "none"
assert a["cid"] != b["cid"]
PY

echo "PQ-33 private atomic V2 snapshot verification PASS"
