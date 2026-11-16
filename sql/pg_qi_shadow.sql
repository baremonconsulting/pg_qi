CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;

CREATE TABLE shadow_plain(id integer PRIMARY KEY, v integer);
INSERT INTO shadow_plain VALUES (1, 10), (2, 20);

\pset format unaligned
\pset tuples_only on

SHOW pg_qi.routing_mode;
CREATE ROLE shadow_unprivileged;
SET ROLE shadow_unprivileged;
SET pg_qi.routing_mode = allowlist;
RESET ROLE;
DROP ROLE shadow_unprivileged;
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

SET pg_qi.routing_mode = allowlist;
SELECT id FROM ONLY shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_policy_intent;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_force_origin;
SHOW pg_qi.shadow_observed_engine;
SHOW pg_qi.shadow_verification;
SHOW pg_qi.shadow_request_applied;
SHOW duckdb.force_execution;

SELECT id FROM ONLY shadow_plain WHERE id = 1 AND (1 / 0) = 0;
SHOW duckdb.force_execution;
SELECT id FROM ONLY shadow_plain WHERE id = 2;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_verification;
SET pg_qi.routing_mode = off;

CREATE FUNCTION shadow_spi() RETURNS integer LANGUAGE plpgsql AS $$
DECLARE result integer;
BEGIN
    EXECUTE 'SELECT 42' INTO result;
    RETURN result;
END
$$;
SELECT shadow_spi();
SHOW pg_qi.shadow_context;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_request_applied;
EXPLAIN SELECT id FROM ONLY shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_context;
SHOW pg_qi.shadow_reason;
DROP FUNCTION shadow_spi();

SELECT count(*) FROM ONLY shadow_plain;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_classifier_reason;

SELECT 1 HAVING true;
SHOW pg_qi.shadow_classifier_reason;

SELECT id FROM ONLY shadow_plain ORDER BY id;
SHOW pg_qi.shadow_classifier_reason;

SELECT EXISTS (SELECT 1 FROM ONLY shadow_plain WHERE id = 1);
SHOW pg_qi.shadow_classifier_reason;

SET pg_qi.routing_mode = allowlist;
PREPARE shadow_parameter(integer) AS
    SELECT id FROM ONLY shadow_plain WHERE id = $1;
EXECUTE shadow_parameter(1);
EXECUTE shadow_parameter(2);
EXECUTE shadow_parameter(1);
EXECUTE shadow_parameter(2);
EXECUTE shadow_parameter(1);
EXECUTE shadow_parameter(2);
ALTER TABLE shadow_plain ALTER COLUMN v SET DEFAULT 0;
EXECUTE shadow_parameter(1);
SHOW pg_qi.shadow_context;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_classifier_status;
SHOW pg_qi.shadow_request_applied;
SHOW duckdb.force_execution;
DEALLOCATE shadow_parameter;
SET pg_qi.routing_mode = off;

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

SET pg_qi.routing_mode = allowlist;
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
SET pg_qi.routing_mode = off;

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
