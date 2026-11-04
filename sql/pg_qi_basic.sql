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
SELECT depth, cleanup_broken, accounting_valid FROM qi.test_frame_state();
SHOW pg_qi.spike_stack_depth;
SHOW pg_qi.spike_cleanup_broken;

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
