#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=$pg_install_root/$major/bin
error_log=$(mktemp)
trap 'rm -f "$error_log"' EXIT
output=$(
    "$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres 2>"$error_log" <<'SQL'
CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE TABLE outer_test_plain(id integer PRIMARY KEY, v integer);
INSERT INTO outer_test_plain VALUES (1, 10), (2, 20);
CREATE FUNCTION pg_qi_outer_test_become_outer() RETURNS void
AS '$libdir/pg_qi_outer_test', 'pg_qi_outer_test_become_outer'
LANGUAGE C;
CREATE FUNCTION pg_qi_outer_test_restore_order() RETURNS void
AS '$libdir/pg_qi_outer_test', 'pg_qi_outer_test_restore_order'
LANGUAGE C;
CREATE FUNCTION pg_qi_outer_test_reset_metrics() RETURNS void
AS '$libdir/pg_qi_outer_test', 'pg_qi_outer_test_reset_metrics'
LANGUAGE C;
SET pg_qi.routing_mode = allowlist;
SET pg_qi_outer_test.reenter_once = on;
SELECT id FROM outer_test_plain WHERE id = 1;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_verification;
SHOW pg_qi_outer_test.inner_force_seen;
SHOW duckdb.force_execution;
SET pg_qi.routing_mode = shadow_p0;
SET pg_qi_outer_test.reenter_once = off;
SELECT pg_qi_outer_test_reset_metrics();
SELECT id FROM outer_test_plain WHERE id = 1;
SHOW pg_qi_outer_test.planner_calls;
SHOW pg_qi_outer_test.force_at_entry;
SHOW pg_qi_outer_test.pointer_identity;
SHOW pg_qi.shadow_policy_intent;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_request_applied;
SHOW duckdb.force_execution;
SET pg_qi_outer_test.error_unconditionally = on;
\set ON_ERROR_STOP off
SELECT id FROM outer_test_plain WHERE id = 1;
\set ON_ERROR_STOP on
\echo :LAST_ERROR_SQLSTATE
SHOW duckdb.force_execution;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SET pg_qi_outer_test.error_unconditionally = off;
SET pg_qi.routing_mode = allowlist;
SET pg_qi_outer_test.error_after_owned_force = on;
\set ON_ERROR_STOP off
SELECT id FROM outer_test_plain WHERE id = 1;
\set ON_ERROR_STOP on
\echo :LAST_ERROR_SQLSTATE
SHOW duckdb.force_execution;
SET pg_qi_outer_test.error_after_owned_force = off;
SELECT id FROM outer_test_plain WHERE id = 1;
SHOW pg_qi.shadow_request_applied;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SELECT pg_qi_outer_test_become_outer();
\o /dev/null
EXPLAIN SELECT id FROM outer_test_plain WHERE id = 2;
\o
SHOW pg_qi_outer_test.pointer_identity;
SHOW pg_qi_outer_test.argument_identity;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_request_applied;
SHOW duckdb.force_execution;
SELECT pg_qi_outer_test_restore_order();
DROP TABLE outer_test_plain;
DROP FUNCTION pg_qi_outer_test_become_outer();
DROP FUNCTION pg_qi_outer_test_restore_order();
DROP FUNCTION pg_qi_outer_test_reset_metrics();
DROP EXTENSION pg_qi;
DROP EXTENSION pg_duckdb;
SQL
)

test "$(grep -Ec '^ERROR:  pg_qi outer fixture error after owned force$' "$error_log")" -eq 1
test "$(grep -Ec '^ERROR:  pg_qi outer fixture downstream error$' "$error_log")" -eq 1

expected=$'1\ndelegation_verified\nverified\noff\noff\n\n1\n1\noff\non\npass_through\ndecision_only\noff\noff\n40001\noff\nbounded\nok\nXX000\noff\n1\non\nunknown\nno_query\n\non\non\ncontext_unproved\noff\noff'
if [ "$output" != "$expected" ]; then
    printf 'unexpected outer-hook verification output:\n%s\n' "$output" >&2
    exit 1
fi
