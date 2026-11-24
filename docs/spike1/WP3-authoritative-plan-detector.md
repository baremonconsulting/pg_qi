# WP3 DuckDB plan detection

14 October 2026

The same C plan walker recognised `DuckDBScan` in the required synthetic
plans and in plans produced by pg_duckdb on PostgreSQL 16–18. The walker
matches the exact provider name through
`CustomScanMethods.CustomName == "DuckDBScan"`.

## Plan walker

`src/qi_detector.c` visits the top-level plan and follows its child links.

- `lefttree` and `righttree` on each plan node
- `Append.appendplans` and `MergeAppend.mergeplans`
- `BitmapAnd.bitmapplans` and `BitmapOr.bitmapplans`
- `SubqueryScan.subplan`
- `CustomScan.custom_plans`
- `PlannedStmt.subplans`

The normal `lefttree` traversal reaches children of `Material`. After the
downstream planner returns, the hook records the match in the read-only
setting `pg_qi.spike_last_plan_has_duckdb_scan`. The test uses this setting
and the plan nodes as evidence. JSON `EXPLAIN` is supplementary.

## Verification

| Fixture | PG16 | PG17 | PG18 |
|---|---:|---:|---:|
| Provider at the root | PASS | PASS | PASS |
| Provider below `Material` | PASS | PASS | PASS |
| Provider in `PlannedStmt.subplans` | PASS | PASS | PASS |
| Provider absent | PASS | PASS | PASS |

The fixtures construct PostgreSQL plan nodes in C and call the planner
hook's walker. To verify a real plan on each version, the test created a
100-row table and ran `SET duckdb.force_execution = on`.
`SELECT count(*) FROM spike_small` returned 100, and
`SHOW pg_qi.spike_last_plan_has_duckdb_scan` returned `on`. The extended
regression test also passed on all three versions.

The evidence covers the plan shapes listed for Spike 1. A new plan node
that owns additional child plans needs its own coverage.

## Build findings

`CustomScanMethods` needs the public header `nodes/extensible.h`.
`NodeSetTag` takes a `T_*` value. With these fixes, all three builds passed
with `-Werror`.
