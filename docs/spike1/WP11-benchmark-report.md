# Spike 1 — WP11 benchmark report

Date: 2026-10-16
Result: **PASS (36/36 benchmark combinations)**

## Scope and method

The benchmark used optimized, non-assert PostgreSQL builds independently for
16.15, 17.11, and 18.6. pg_duckdb was pinned at
`ee38d3b540ecea1d93683ba99bdcec5632a21eaf`.

Each combination used one stable libpq session, 25 warm-up requests, and 500
measured requests against a 10,000-row table. The environment fixed
`max_parallel_workers_per_gather=0`, `jit=off`, and `track_io_timing=off`.

Paths:

- `simple`: fresh planning through the simple query protocol;
- `custom`: a parameterized prepared statement with `force_custom_plan`;
- `generic`: the same prepared statement with `force_generic_plan`.

Modes:

- `bypass`: hook dispatch and downstream planner only;
- `frame_only`: frame push/pop, GUC nest level, and protected wrapper;
- `shadow_save`: frame work plus SAVE/restore of the force GUC;
- `delegate`: actual pg_duckdb planning and execution.

Latency percentiles come from client-side monotonic-clock samples. Planner
entries and planner elapsed microseconds come from backend-local instrumentation
and are emitted at backend exit. Plan-cache counters come from
`pg_prepared_statements` in the same session; the metadata query is excluded
from collection.

## Selected request-level results

All values are microseconds.

| PG | mode/path | p50 | p95 | p99 | failed |
|---:|---|---:|---:|---:|---:|
| 16 | bypass/simple | 1,941 | 2,604 | 2,845 | 0 |
| 16 | frame_only/simple | 1,364 | 2,352 | 2,869 | 0 |
| 16 | shadow_save/simple | 1,898 | 2,567 | 2,940 | 0 |
| 16 | delegate/simple | 15,491 | 19,205 | 22,101 | 0 |
| 17 | bypass/simple | 1,722 | 2,415 | 3,453 | 0 |
| 17 | frame_only/simple | 2,061 | 2,931 | 3,220 | 0 |
| 17 | shadow_save/simple | 1,954 | 2,929 | 3,265 | 0 |
| 17 | delegate/simple | 15,263 | 19,893 | 24,067 | 0 |
| 18 | bypass/simple | 1,848 | 2,649 | 3,224 | 0 |
| 18 | frame_only/simple | 1,616 | 2,268 | 2,605 | 0 |
| 18 | shadow_save/simple | 1,574 | 2,230 | 2,460 | 0 |
| 18 | delegate/simple | 16,310 | 20,220 | 22,463 | 0 |

The complete 36-row matrix, including custom and generic paths and planner
timing, is in `artifacts/spike1/wp11/results.csv`.

## Plan-cache proof

For every version and every mode:

- custom path: `custom_plans=500`, `generic_plans=0`;
- generic path: `generic_plans=500`, `custom_plans=0`;
- simple path: neither prepared-plan counter applies.

Actual delegation was also asserted, not inferred:

- delegate/simple: 500 completed DuckDB plans;
- delegate/custom: 500 completed DuckDB plans;
- delegate/generic: 1 completed DuckDB plan followed by cached reuse for 500
  executions.

This confirms decision-at-plan-time behavior: a generic cached plan does not
re-enter the policy decision for each execution.

## Interpretation

The non-delegating modes remain in the same request-latency band. Internal
planner measurements put wrapper and shadow-save work in the tens-of-microseconds
range for fresh/custom planning. Differences of only a few microseconds vary in
sign between runs and versions, so this single-host smoke benchmark must not be
used as a statistically precise claim about the marginal cost of name-based
(PG16) versus handle-based (PG17/18) GUC setting.

Actual delegation is intentionally a different scale: its simple-path request
median was 15.3 ms on PG16, 15.3 ms on PG17, and 16.3 ms on PG18 for this small
test query. These figures describe integration overhead on this host, not the
expected benefit for analytical workloads.

## Acceptance evidence

- 36/36 combinations completed;
- 18,000 measured requests total, zero failures;
- required custom/generic counters matched exactly;
- required DuckDB plan counts matched exactly;
- no assertion, panic, trap, segmentation-fault, ERROR, or FATAL signature;
- PG17 default `normal` mode passed the existing `make installcheck` suite.

The first benchmark attempt used an unparameterized prepared statement and was
rejected because it could not distinguish forced custom from generic planning.
It is preserved under `artifacts/spike1/wp11-attempt1/` and is not acceptance
evidence.
