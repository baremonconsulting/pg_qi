# Spike 1 — WP8: diagnostics and failpoints

Date: 2026-10-16

## Result

WP8 is **PASS** on PostgreSQL 16, 17, and 18.

## Read-only diagnostics

Primary assertions can now use `SHOW` without invoking the planner:

- `pg_qi.spike_stack_depth`
- `pg_qi.spike_cleanup_broken`
- `pg_qi.spike_frames_pushed`
- `pg_qi.spike_frames_popped`
- `pg_qi.spike_frames_unwound`
- `pg_qi.spike_force_transitions`
- `pg_qi.spike_nested_masks`
- `pg_qi.spike_nested_pg_plans`
- `pg_qi.spike_nested_duckdb_plans`
- `pg_qi.spike_armed_generation`
- `pg_qi.spike_consumed_generation`
- `pg_qi.spike_token_armed`
- `pg_qi.spike_reentry_active`
- `pg_qi.spike_failpoint_hits`
- `pg_qi.spike_failpoint_fired`

`qi.spike_state()` remains a TAP convenience function. Because its own SELECT
passes through the planner, returned pushed/popped values subtract that one
balanced frame. Raw counters still record the call; therefore correctness
assertions use `SHOW`.

`qi.spike_reset_state()` resets frame counters, token generations, IDs, and
the cleanup sentinel, and rejects use while a planner frame or reentry guard
is active.

## Failpoint contract

The `QiFailpoint` enum exposes only these locations:

1. `before_push`;
2. `inside_try_before_policy`;
3. `inside_try_after_policy`;
4. `inside_try_before_downstream`;
5. `inner_planner`;
6. `subtransaction_body`.

Configuration uses:

- `pg_qi.spike_failpoint`;
- `pg_qi.spike_failpoint_nth`.

Each matching hit increments a backend-local counter. On the configured Nth
hit, the failpoint first auto-disarms to `off`, records that it fired, and only
then raises `ERROR`.

`before_push` is the sole failpoint outside `PG_TRY`, where no frame exists.
Every location after frame creation is inside the owner frame's protected
region. No unsafe after-push/after-restore failpoint exists.

## Verification

For PostgreSQL 16, 17, and 18, the matrix proved:

- consecutive `SHOW` diagnostics do not alter counters;
- a normal SELECT increments pushed/popped once;
- `qi.spike_state()` reports its compensated snapshot while raw counters
  retain its real planner entry;
- `before_push` fires deterministically on the second hit and leaves no frame;
- before-policy and before-downstream errors unwind exactly one frame;
- after-policy error restores force, consumes the token, and unwinds once;
- inner-planner error unwinds inner and outer frames and resets the guard;
- subtransaction-body error is caught locally and the outer DuckDB plan
  completes;
- every failpoint auto-disarms;
- every scenario ends with depth zero and `cleanup_broken=false`.

The complete existing regression suite remains green. Server logs contain no
resource-owner warning, assertion failure, fatal error, or panic.

## Scope boundary

WP8 provides the deterministic harness. WP9 uses it to assemble and assert the
complete S0–S11 scenario matrix.
