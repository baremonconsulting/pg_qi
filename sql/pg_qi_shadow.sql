CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;

CREATE TABLE shadow_plain(id integer PRIMARY KEY, v integer);
INSERT INTO shadow_plain VALUES (1, 10), (2, 20);

\pset format unaligned
\pset tuples_only on

SELECT id FROM ONLY shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_policy_intent;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_context;
SHOW pg_qi.shadow_force_origin;
SHOW pg_qi.shadow_classifier_status;
SHOW pg_qi.shadow_classifier_reason;
SHOW pg_qi.shadow_observed_engine;
SHOW pg_qi.shadow_verification;
SHOW pg_qi.shadow_request_applied;

SELECT count(*) FROM ONLY shadow_plain;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_classifier_reason;

SELECT 1 HAVING true;
SHOW pg_qi.shadow_classifier_reason;

SELECT id FROM ONLY shadow_plain ORDER BY id;
SHOW pg_qi.shadow_classifier_reason;

SELECT EXISTS (SELECT 1 FROM ONLY shadow_plain WHERE id = 1);
SHOW pg_qi.shadow_classifier_reason;

PREPARE shadow_parameter(integer) AS
    SELECT id FROM ONLY shadow_plain WHERE id = $1;
EXECUTE shadow_parameter(1);
SHOW pg_qi.shadow_classifier_reason;
DEALLOCATE shadow_parameter;

CREATE TABLE shadow_child() INHERITS (shadow_plain);
SELECT id FROM shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_classifier_reason;
DROP TABLE shadow_child;

ALTER TABLE shadow_plain ENABLE ROW LEVEL SECURITY;
SELECT id FROM ONLY shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_classifier_reason;
ALTER TABLE shadow_plain DISABLE ROW LEVEL SECURITY;

SELECT id FROM ONLY shadow_plain TABLESAMPLE SYSTEM (100);
SHOW pg_qi.shadow_classifier_reason;

SET pg_qi.enabled = off;
SELECT id FROM ONLY shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_classifier_status;
SET pg_qi.enabled = on;

SET duckdb.force_execution = on;
SELECT count(*) FROM shadow_plain;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_force_origin;
SHOW pg_qi.shadow_policy_intent;
SHOW pg_qi.shadow_classifier_status;
SHOW pg_qi.shadow_observed_engine;
SHOW pg_qi.shadow_observation;
SHOW pg_qi.shadow_verification;
SHOW pg_qi.shadow_request_applied;
SHOW duckdb.force_execution;
SET duckdb.force_execution = off;

SELECT id FROM ONLY shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_decision_id;
SELECT 1 / 0;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_decision_id;
SELECT id FROM ONLY shadow_plain WHERE id = 2;
SHOW pg_qi.shadow_context;

SET pg_qi.shadow_reason = 'policy_missing';

DROP TABLE shadow_plain;
DROP EXTENSION pg_qi;
DROP EXTENSION pg_duckdb;
