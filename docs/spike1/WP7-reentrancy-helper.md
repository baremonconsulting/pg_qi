# Spike 1 — WP7: reentrancy helper

Date: 2026-10-16

## Result

WP7 is **PASS** on PostgreSQL 16, 17, and 18.

## Implemented contract

- Synthetic reentry accepts only a top-level delegated `CMD_SELECT` without a
  utility statement.
- The helper creates the inner query with `copyObject(parse)` and calls the
  public PostgreSQL `planner(...)`; it does not use SPI or the plan cache.
- A helper-owned backend-local guard prevents recursive reentry and is always
  restored in `PG_FINALLY`.
- The nested pg_qi frame derives provenance from its immediate parent.
- When the parent origin is `QI_STACK`, the nested frame temporarily masks
  `duckdb.force_execution` to `off`. User-owned `EXTERNAL` force is not
  reclassified or masked.
- The `propagate` mode returns inner errors through both planner frames.
- The `subxact` mode catches the inner error locally and lets the outer planner
  continue.
- `qi_try_local_subxact()` is the single internal-subtransaction pattern in
  pg_qi. It preserves and restores both `MemoryContext` and `ResourceOwner`,
  and uses `volatile bool ok` across the PostgreSQL error boundary.
- Successful inner plans are inspected by the authoritative C detector and
  increment exactly one PostgreSQL/DuckDB result counter.

## Spike controls

- `pg_qi.spike_reentry_mode = off | propagate | subxact`
- `pg_qi.spike_reentry_inner_error = on | off`
- `pg_qi.spike_reentry_active` is a read-only guard diagnostic.

These controls are test infrastructure for Spike 1, not a production policy
API.

## Verification

The automated matrix compiles with warnings as errors and executes the three
required S6a paths on PostgreSQL 16, 17, and 18.

### S6a-1 — masked recursion

- outer query: DuckDB plan;
- inner query: PostgreSQL plan detected by the C visitor;
- `nested_masks=1`, `nested_pg_plans=1`, `nested_duckdb_plans=0`;
- final `depth=0`, guard off, force off, clean accounting.

### S6a-2 — propagated inner error

- original synthetic inner error reaches the client;
- inner and outer frames both unwind (`unwound=2`);
- final `depth=0`, guard off, force off, `cleanup_broken=false`.

### S6a-3 — locally caught inner error

- inner frame unwinds inside the local subtransaction (`unwound=1`);
- outer query continues and produces a DuckDB plan;
- parent force state survives the local rollback and is restored afterward;
- final `depth=0`, guard off, force off, clean accounting.

Existing WP2–WP6 regression and integration checks remain green. Server logs
contain no resource-owner warning, assertion failure, fatal error, or panic.

## Scope boundary

WP7 covers deterministic synthetic planner recursion (S6a). PL/pgSQL runtime
SPI isolation (S6b), broader failpoints, and consolidated read-only diagnostics
remain WP8/WP9 work.
