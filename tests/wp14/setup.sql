CREATE TABLE qi_gate_data (
    id int4 PRIMARY KEY,
    group_id int4,
    small_value int2,
    big_value int8,
    event_date date,
    event_time timestamp,
    nullable_value int4
);
INSERT INTO qi_gate_data
SELECT i,
       i % 5,
       (i % 17)::int2,
       (i::int8 * 1000000000),
       DATE '2000-01-01' + (i % 31),
       TIMESTAMP '2000-01-01 00:00:00' + (i || ' minutes')::interval,
       CASE WHEN i % 7 = 0 THEN NULL ELSE i % 11 END
FROM generate_series(1, 200) AS g(i);
INSERT INTO qi_gate_data VALUES
    (-2147483648, -1, -32768, -9223372036854775807,
     DATE '0001-01-01', TIMESTAMP '0001-01-01 00:00:00', NULL),
    (2147483647, 1, 32767, 9223372036854775807,
     DATE '9999-12-31', TIMESTAMP '9999-12-31 23:59:59', 10);
CREATE TABLE qi_gate_join (id int4 PRIMARY KEY, payload int8);
INSERT INTO qi_gate_join SELECT id, big_value FROM qi_gate_data;
CREATE TABLE qi_gate_empty (id int4, event_date date, event_time timestamp);

CREATE TABLE qi_issue_text (s text COLLATE "C");
INSERT INTO qi_issue_text VALUES ('App\\Models\\Product'), ('App\\Models\\Order');
CREATE TABLE qi_issue_numeric (id int4, dec numeric(10,2), unbound numeric);
INSERT INTO qi_issue_numeric VALUES
    (1, 'NaN', 'NaN'), (2, 10.00, 10),
    (3, NULL, 'Infinity'), (4, NULL, '-Infinity');
