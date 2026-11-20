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
SELECT id FROM shadow_plain WHERE id = 1;
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
SELECT id FROM shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_policy_intent;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_force_origin;
SHOW pg_qi.shadow_observed_engine;
SHOW pg_qi.shadow_verification;
SHOW pg_qi.shadow_request_applied;
SHOW duckdb.force_execution;

SET pg_qi.routing_mode = shadow_p0;
SELECT id FROM shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_policy_intent;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_force_origin;
SHOW pg_qi.shadow_observed_engine;
SHOW pg_qi.shadow_verification;
SHOW pg_qi.shadow_request_applied;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SHOW duckdb.force_execution;

SELECT count(*) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(id) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT min(id) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT max(id) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;

SELECT count(*) FROM shadow_plain WHERE id > 0;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(*), min(id) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(*) > 0 FROM pg_catalog.pg_class;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;

CREATE UNLOGGED TABLE shadow_unlogged(id integer);
SELECT count(*) FROM shadow_unlogged;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
DROP TABLE shadow_unlogged;

CREATE TEMP TABLE shadow_temporary(id integer);
SELECT count(*) FROM shadow_temporary;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
DROP TABLE shadow_temporary;

SELECT count(DISTINCT id) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(*) FILTER (WHERE id > 0) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(id + 0) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(id::bigint) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(*) FROM shadow_plain GROUP BY v;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
WITH source AS (SELECT * FROM shadow_plain)
SELECT count(*) FROM source;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(*) FROM (SELECT * FROM shadow_plain) AS source;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW pg_qi.shadow_p0_capability;
SELECT count(*) FROM shadow_plain AS a JOIN shadow_plain AS b USING (id);
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) FROM shadow_plain
WHERE EXISTS (SELECT 1 FROM shadow_plain AS nested WHERE nested.id = 1);
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) OVER () FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
SELECT array_agg(id ORDER BY id) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
CREATE AGGREGATE public.count(integer) (
    SFUNC = pg_catalog.int4pl,
    STYPE = integer,
    INITCOND = '0'
);
SELECT public.count(id) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
DROP AGGREGATE public.count(integer);
SELECT DISTINCT count(*) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) FROM shadow_plain ORDER BY count(*);
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) FROM shadow_plain LIMIT 1;
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) FROM shadow_plain HAVING true;
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) FROM shadow_plain
UNION ALL
SELECT count(*) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) FROM shadow_plain TABLESAMPLE SYSTEM (100);
SHOW pg_qi.shadow_p0_facts_reason;

CREATE VIEW shadow_view AS SELECT * FROM shadow_plain;
SELECT count(*) FROM shadow_view;
SHOW pg_qi.shadow_p0_facts_reason;
DROP VIEW shadow_view;
CREATE MATERIALIZED VIEW shadow_matview AS SELECT * FROM shadow_plain;
SELECT count(*) FROM shadow_matview;
SHOW pg_qi.shadow_p0_facts_reason;
DROP MATERIALIZED VIEW shadow_matview;

CREATE TABLE shadow_parent(id integer);
CREATE TABLE shadow_child() INHERITS (shadow_parent);
SELECT count(*) FROM ONLY shadow_parent;
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) FROM ONLY shadow_child;
SHOW pg_qi.shadow_p0_facts_reason;
DROP TABLE shadow_child;
DROP TABLE shadow_parent;

CREATE TABLE shadow_partitioned(id integer) PARTITION BY RANGE (id);
CREATE TABLE shadow_partition PARTITION OF shadow_partitioned
    FOR VALUES FROM (0) TO (10);
SELECT count(*) FROM ONLY shadow_partitioned;
SHOW pg_qi.shadow_p0_facts_reason;
SELECT count(*) FROM shadow_partition;
SHOW pg_qi.shadow_p0_facts_reason;
DROP TABLE shadow_partitioned;

ALTER TABLE shadow_plain ENABLE ROW LEVEL SECURITY;
SELECT count(*) FROM shadow_plain;
SHOW pg_qi.shadow_p0_facts_reason;
ALTER TABLE shadow_plain DISABLE ROW LEVEL SECURITY;

SELECT a01.id
FROM shadow_plain a01, shadow_plain a02, shadow_plain a03,
     shadow_plain a04, shadow_plain a05, shadow_plain a06,
     shadow_plain a07, shadow_plain a08, shadow_plain a09,
     shadow_plain a10, shadow_plain a11, shadow_plain a12,
     shadow_plain a13, shadow_plain a14, shadow_plain a15,
     shadow_plain a16, shadow_plain a17
WHERE false;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_request_applied;
SHOW pg_qi.shadow_p0_facts_state;
SHOW pg_qi.shadow_p0_facts_reason;
SHOW duckdb.force_execution;
SET pg_qi.routing_mode = allowlist;

SELECT id FROM shadow_plain WHERE id = 1 AND (1 / 0) = 0;
SHOW duckdb.force_execution;
SELECT id FROM shadow_plain WHERE id = 2;
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
EXPLAIN SELECT id FROM shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_context;
SHOW pg_qi.shadow_reason;
DROP FUNCTION shadow_spi();

SELECT count(*) FROM shadow_plain;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_classifier_reason;

SELECT 1 HAVING true;
SHOW pg_qi.shadow_classifier_reason;

SELECT id FROM shadow_plain ORDER BY id;
SHOW pg_qi.shadow_classifier_reason;

SELECT EXISTS (SELECT 1 FROM shadow_plain WHERE id = 1);
SHOW pg_qi.shadow_classifier_reason;

SET pg_qi.routing_mode = allowlist;
PREPARE shadow_parameter(integer) AS
    SELECT id FROM shadow_plain WHERE id = $1;
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
SELECT id FROM shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_classifier_reason;
ALTER TABLE shadow_plain DISABLE ROW LEVEL SECURITY;

SELECT id FROM shadow_plain TABLESAMPLE SYSTEM (100);
SHOW pg_qi.shadow_classifier_reason;

SET pg_qi.enabled = off;
SELECT id FROM shadow_plain WHERE id = 1;
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

SELECT id FROM shadow_plain WHERE id = 1;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_decision_id;
SELECT 1 / 0;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_decision_id;
SELECT id FROM shadow_plain WHERE id = 2;
SHOW pg_qi.shadow_context;

SET pg_qi.shadow_reason = 'policy_missing';

DROP TABLE shadow_plain;
DROP EXTENSION pg_qi;
DROP EXTENSION pg_duckdb;
