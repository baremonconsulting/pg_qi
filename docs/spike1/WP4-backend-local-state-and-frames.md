# Spike 1 — WP4: backend-local state and frames

Date: 2026-10-16

## Result

WP4 is **PASS** on PostgreSQL 16, 17, and 18.

## Implemented contract

- A fixed 64-entry backend-static frame array; the frame stack never uses
  `palloc`.
- `QiForceOrigin` distinguishes `OFF`, `EXTERNAL`, and `QI_STACK`.
- Each frame records GUC nest level, inherited and effective provenance,
  delegation request, GUC-change ownership, reentry, generation, and frame ID.
- Capacity is checked before depth or counters change.
- `pop` and `unwind_on_error` allocate nothing and throw no errors. A LIFO
  violation only sets `cleanup_broken`, preserving an original PostgreSQL
  error during cleanup.
- A nested frame derives force provenance from its immediate parent rather
  than rereading the ambient GUC.
- Counters implement the invariant
  `depth = pushed - popped - unwound`.
- Read-only diagnostics expose stack depth and the cleanup sentinel; SQL test
  helpers expose all counters and the frame contract.

## Verification

The matrix compiled with `-Werror`, installed, started a correctly ordered
`pg_duckdb,pg_qi` server, rejected missing/wrong preload order, and ran the
regression suite for each supported PostgreSQL major.

The isolated frame tests prove:

1. correct LIFO removal;
2. rejection of frame 65 without changing depth/counters;
3. detection of corrupted removal order without throwing;
4. zero depth after the unwind path;
5. child provenance inherited from the immediate parent;
6. valid accounting invariant;
7. live backend state remains `depth=0`, `cleanup_broken=false`.

## Scope boundary

WP4 provides and verifies the state machinery only. Integrating frames around
the planner-hook call with `PG_TRY/PG_CATCH` belongs to WP5.
