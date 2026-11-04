# Spike 1 — WP9: automated S0–S11 scenario matrix

Date: 2026-10-16

## Result

WP9 is **PASS**: all 16 scenario rows pass on PostgreSQL 16, 17, and 18
(48 PASS results total).

| Scenario | PG16 | PG17 | PG18 | Evidence |
|---|---|---|---|---|
| S0 | PASS | PASS | PASS | real GUC, no placeholder/no-reset, downstream hook present |
| S1 | PASS | PASS | PASS | baseline PostgreSQL plan, balanced clean state |
| S2 | PASS | PASS | PASS | first query DuckDB, second PostgreSQL, token consumed once |
| S3 | PASS | PASS | PASS | protected failpoint, savepoint rollback, token not resurrected |
| S4 | PASS | PASS | PASS | real downstream planning `division by zero`, clean unwind |
| S5 | PASS | PASS | PASS | executor-time error, no planner unwind |
| S6a-1 | PASS | PASS | PASS | outer DuckDB, inner PostgreSQL, one nested mask |
| S6a-2 | PASS | PASS | PASS | propagated inner error, two unwinds, guard reset |
| S6a-3 | PASS | PASS | PASS | locally caught error, outer DuckDB completes |
| S6b-1 | PASS | PASS | PASS | unsafe=false, runtime SPI query remains PostgreSQL |
| S6b-2 | PASS | PASS | PASS | deterministic downstream decline documented below |
| S7 | PASS | PASS | PASS | forced custom plans: `generic=0`, `custom=2` |
| S8 | PASS | PASS | PASS | generic reuse and DDL invalidation semantics proven |
| S9 | PASS | PASS | PASS | EXTERNAL, QI_STACK, and nested-mask provenance distinguished |
| S10 | PASS | PASS | PASS | utility/protocol paths and active-control guard clean |
| S11 | PASS | PASS | PASS | leader plus four real workers report isolated clean state |

## Important semantic findings

### S6b-2 — pinned pg_duckdb decline

With `duckdb.unsafe_allow_execution_inside_functions=true`, every tested
version produced the same deterministic result:

- pg_qi armed generation was consumed;
- one force transition occurred;
- the runtime dynamic SQL completed successfully;
- the authoritative detector counted zero DuckDB plans;
- the inner plan was PostgreSQL;
- final depth and sentinel were clean.

Thus pg_qi made the delegation request correctly, but pg_duckdb at pinned SHA
`ee38d3b540ecea1d93683ba99bdcec5632a21eaf` declined/fell back during its
nested executor planning path even with the unsafe override enabled. This is
the concrete S6b-2 result for the pinned dependency, not an indeterminate
pass.

### S8 — generic-plan product semantics

With `force_generic_plan`, the first armed execution creates a delegated
generic plan. The second execution reuses that plan without entering the
planner hook, so it remains delegated. `pg_prepared_statements` reports
`generic_plans=2, custom_plans=0`.

After DDL invalidation, execution enters planning again. Since the one-shot
token has already been consumed, the replacement plan is PostgreSQL;
`generic_plans` increases to 3. Delegation therefore belongs to plan creation,
not each execution of a cached generic plan.

### S11 — worker isolation

Each version planned four workers and actually returned observations from the
leader plus four distinct parallel worker PIDs. Every worker reported:

- `depth=0`;
- `cleanup_broken=false`;
- `token_armed=false`.

The preceding armed query was delegated, proving the planning-window state did
not leak into workers.

## Additional observability added for WP9

- total PostgreSQL and DuckDB completed-plan counters;
- last inherited/effective force provenance;
- last request and GUC-change ownership flags;
- downstream planner-hook presence diagnostic;
- parallel-safe worker observer;
- active-frame control-guard contract helper.

## Automation and quality gates

`scripts/spike1/wp9-matrix.sh` creates isolated servers, executes every
scenario with exact assertions, and writes `results.tsv`. Assertions use C
plan detection and read-only diagnostics; JSON EXPLAIN is not a PASS oracle.

All three builds compile with warnings as errors and pass `make installcheck`.
Server logs contain no resource-owner warning, assertion failure, fatal error,
or panic. Every scenario finishes with `depth=0` and
`cleanup_broken=false`.
