CREATE EXTENSION pg_duckdb;
CREATE EXTENSION pg_qi;

CREATE TABLE qi_pilot_t(id integer PRIMARY KEY, v integer, nullable integer);
INSERT INTO qi_pilot_t
SELECT i, i % 100, CASE WHEN i % 11 = 0 THEN NULL ELSE i * 2 END
FROM generate_series(1, 10000) AS g(i);
ANALYZE qi_pilot_t;

CREATE UNLOGGED TABLE qi_pilot_unlogged(id integer PRIMARY KEY);
INSERT INTO qi_pilot_unlogged VALUES (1);
CREATE TABLE qi_pilot_partitioned(id integer) PARTITION BY RANGE (id);
CREATE TABLE qi_pilot_partitioned_p1 PARTITION OF qi_pilot_partitioned FOR VALUES FROM (0) TO (10);
INSERT INTO qi_pilot_partitioned VALUES (1);
CREATE TABLE qi_pilot_rls(id integer PRIMARY KEY, owner_name name);
INSERT INTO qi_pilot_rls VALUES (1, 'qi_pilot_role');
CREATE ROLE qi_pilot_role;
GRANT SELECT ON qi_pilot_rls TO qi_pilot_role;
ALTER TABLE qi_pilot_rls ENABLE ROW LEVEL SECURITY;
CREATE POLICY qi_pilot_rls_policy ON qi_pilot_rls USING (owner_name = current_user);
SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'file_fdw') AS file_fdw_available \gset
\if :file_fdw_available
CREATE EXTENSION file_fdw;
CREATE SERVER qi_pilot_file FOREIGN DATA WRAPPER file_fdw;
CREATE FOREIGN TABLE qi_pilot_foreign(id integer)
SERVER qi_pilot_file OPTIONS (filename '/dev/null', format 'csv');
\endif
