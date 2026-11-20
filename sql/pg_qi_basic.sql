CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;

SELECT qi.version();
SELECT qi.is_preloaded() IS NOT NULL AS reports_preload_state;
SHOW pg_qi.enabled;

\pset format unaligned
\pset tuples_only on

SELECT preflight_passed
       AND CASE
               WHEN current_setting('server_version_num')::integer >= 170000
                   THEN handle_present IS TRUE
               ELSE handle_present IS NULL
           END
       AND NOT is_placeholder
       AND NOT no_reset
       AND provider_name = 'DuckDBScan' AS force_meta_valid
FROM qi.test_force_meta();

SELECT qi.test_force_save_roundtrip(true);
SET duckdb.force_execution = on;
SELECT qi.test_force_save_roundtrip(false);
SET duckdb.force_execution = off;

SELECT * FROM qi.test_detector_contract();
SELECT * FROM qi.test_frame_contract();
SELECT * FROM qi.test_classifier_contract();
SELECT * FROM qi.test_decision_v2_contract();
SELECT qi.test_shadow_p0_observer_contract();
SELECT qi.test_semantic_contract();
SELECT qi.test_v2_snapshot_contract();
SELECT depth, cleanup_broken, accounting_valid FROM qi.test_frame_state();
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;

CREATE TABLE semantic_probe(a integer, b integer);
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 7) = 7
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 8) = 0;
SELECT (qi.test_semantic_probe('semantic_probe', 1, 2, 0) & 8) = 0
       AND (qi.test_semantic_probe('semantic_probe', 1, 3, 0) & 8) = 0
       AND (qi.test_semantic_probe('semantic_probe', 1, 4, 0) & 8) = 0;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 1) & 16) = 16
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 2) & 32) = 32
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 4) & 16) = 16
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 8) & 256) = 256
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 16) & 512) = 512
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 32) & 8) = 8
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 64) & 256) = 256;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 128) & 8) = 8
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 256) & 8) = 8
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 512) & 8) = 8
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 1024) & 8) = 8
       AND (qi.test_semantic_probe('semantic_probe', 1, 2, 2048) & 8) = 8
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 4096) & 8) = 8;
ALTER TABLE semantic_probe ENABLE ROW LEVEL SECURITY;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 512) = 512;
ALTER TABLE semantic_probe DISABLE ROW LEVEL SECURITY;
ALTER TABLE semantic_probe FORCE ROW LEVEL SECURITY;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 512) = 512;
ALTER TABLE semantic_probe NO FORCE ROW LEVEL SECURITY;
CREATE ROLE semantic_reader;
GRANT USAGE ON SCHEMA qi TO semantic_reader;
GRANT SELECT (b) ON semantic_probe TO semantic_reader;
REVOKE SELECT (b) ON semantic_probe FROM semantic_reader;
SET ROLE semantic_reader;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 128) = 128;
RESET ROLE;
GRANT SELECT (b) ON semantic_probe TO semantic_reader;
SET ROLE semantic_reader;
SELECT (qi.test_semantic_probe('semantic_probe', 1, 2, 0) & 64) = 64
       AND (qi.test_semantic_probe('semantic_probe', 1, 2, 0) & 128) = 128
       AND (qi.test_semantic_probe('semantic_probe', 2, 2, 0) & 128) = 0
       AND (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 128) = 0;
RESET ROLE;
ALTER TABLE semantic_probe ENABLE ROW LEVEL SECURITY;
SET ROLE semantic_reader;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 512) = 512;
RESET ROLE;
ALTER ROLE semantic_reader BYPASSRLS;
SET ROLE semantic_reader;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 512) = 512;
RESET ROLE;
ALTER ROLE semantic_reader NOBYPASSRLS;
ALTER TABLE semantic_probe DISABLE ROW LEVEL SECURITY;
INSERT INTO semantic_probe VALUES (0, 0);
BEGIN;
SELECT txid_current_if_assigned() IS NULL;
SELECT qi.test_semantic_probe('semantic_probe', 0, 1, 0) IS NOT NULL;
SELECT txid_current_if_assigned() IS NULL;
ROLLBACK;
BEGIN;
INSERT INTO semantic_probe VALUES (1, 1);
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 2048) = 2048;
ROLLBACK;
BEGIN;
UPDATE semantic_probe SET b = b;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 2048) = 2048;
ROLLBACK;
BEGIN;
DELETE FROM semantic_probe;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 2048) = 2048;
ROLLBACK;
BEGIN;
SAVEPOINT semantic_write;
INSERT INTO semantic_probe VALUES (1, 1);
ROLLBACK TO semantic_write;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 2048) = 2048;
ROLLBACK;
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT (qi.test_semantic_probe('semantic_probe', 0, 1, 0) & 1024) = 1024;
COMMIT;
DROP TABLE semantic_probe;
REVOKE USAGE ON SCHEMA qi FROM semantic_reader;
DROP ROLE semantic_reader;

CREATE TABLE spike_small(id integer PRIMARY KEY, v integer);
INSERT INTO spike_small
SELECT i, i * 10 FROM generate_series(1, 100) AS g(i);
SET duckdb.force_execution = on;
SELECT count(*) FROM spike_small;
SHOW pg_qi.spike_last_plan_has_duckdb_scan;
SET duckdb.force_execution = off;
DROP TABLE spike_small;

DROP EXTENSION pg_qi_spike_test;
DROP EXTENSION pg_qi;
DROP EXTENSION pg_duckdb;
