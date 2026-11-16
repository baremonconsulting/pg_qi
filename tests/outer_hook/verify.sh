#!/usr/bin/env bash

set -euo pipefail

major=${1:?PostgreSQL major version is required}
pg_install_root=${PG_INSTALL_ROOT:-/usr/lib/postgresql}
pgbin=$pg_install_root/$major/bin
output=$(
    "$pgbin/psql" -X -qAt -v ON_ERROR_STOP=1 postgres <<'SQL'
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
SET pg_qi.routing_mode = allowlist;
SET pg_qi_outer_test.reenter_once = on;
SELECT id FROM outer_test_plain WHERE id = 1;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_verification;
SHOW pg_qi_outer_test.inner_force_seen;
SHOW duckdb.force_execution;
SET pg_qi_outer_test.reenter_once = off;
SELECT pg_qi_outer_test_become_outer();
SELECT id FROM outer_test_plain WHERE id = 2;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_request_applied;
SHOW duckdb.force_execution;
SELECT pg_qi_outer_test_restore_order();
DROP TABLE outer_test_plain;
DROP FUNCTION pg_qi_outer_test_become_outer();
DROP FUNCTION pg_qi_outer_test_restore_order();
DROP EXTENSION pg_qi;
DROP EXTENSION pg_duckdb;
SQL
)

expected=$'1\ndelegation_verified\nverified\noff\noff\n\n2\ncontext_unproved\noff\noff'
if [ "$output" != "$expected" ]; then
    printf 'unexpected outer-hook verification output:\n%s\n' "$output" >&2
    exit 1
fi
