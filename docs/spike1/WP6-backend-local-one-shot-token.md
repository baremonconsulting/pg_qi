# Spike 1 — WP6: backend-local one-shot token

Date: 2026-10-16

## Result

WP6 is **PASS** on PostgreSQL 16, 17, and 18.

## Implemented contract

- Token state consists of backend-local `armed_generation` and
  `consumed_generation`; it is not a transactional GUC.
- `qi.spike_arm_next()` increments and returns the armed generation.
- A token is active exactly when the two generations differ.
- The planner consumes an active token only at frame depth zero.
- Consumption occurs before `NewGUCNestLevel()`, ambient GUC inspection, frame
  push, or any other operation in the wrapper that can throw.
- Consumption copies the armed generation to the consumed generation before
  planning starts, so transaction and savepoint rollback cannot resurrect it.
- Nested planner calls cannot consume a token.
- `arm`, `disarm`, and reset reject calls while a frame or reentry guard is
  active.
- SQL mutators are `VOLATILE PARALLEL UNSAFE`.
- `qi.test_token_state()` exposes both generations and the derived armed state.

## SQL API

- `qi.spike_arm_next() -> bigint`
- `qi.spike_disarm() -> bigint`
- `qi.spike_reset_token() -> void`
- `qi.test_token_state() -> (armed_generation, consumed_generation, token_armed)`

## Verification

The automated matrix compiled with warnings as errors and ran the existing
regression suite on PostgreSQL 16, 17, and 18. For each version it then proved:

1. generation 1 delegates the first eligible query to `DuckDBScan`;
2. the immediately repeated query is not delegated;
3. token state becomes `1|1|false`;
4. a token consumed by a failing planner remains `2|2|false` after `ROLLBACK`;
5. a token consumed after a savepoint remains `3|3|false` after
   `ROLLBACK TO SAVEPOINT`;
6. neither rollback path delegates the following query accidentally;
7. explicit disarm yields `4|4|false` and the following query is not delegated;
8. final `duckdb.force_execution=off`, frame depth is zero, and
   `cleanup_broken=false`.

## Scope boundary

WP6 does not add synthetic nested planning. Runtime proof that nested planner
invocations cannot consume a token is part of WP7 reentrancy coverage; the
depth-zero guard is implemented here.
