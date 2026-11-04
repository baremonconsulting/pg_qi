# Spike 1 — WP5: canonical planner wrapper

Date: 2026-10-16

## Result

WP5 is **PASS** on PostgreSQL 16, 17, and 18.

## Implemented contract

- When `pg_qi.enabled=on`, every planner invocation creates a new GUC nest
  level and pushes exactly one backend-local frame.
- The frame is followed immediately by `PG_TRY`.
- Policy application, the downstream planner-hook call, and authoritative
  plan detection are inside the protected region.
- The downstream hook chain is preserved; `standard_planner` remains the
  fallback.
- `PG_CATCH` performs only the no-allocation/no-throw unwind of the current
  frame and then uses `PG_RE_THROW`, preserving the original PostgreSQL error.
- The success path restores the GUC stack using
  `AtEOXact_GUC(true, save_nestlevel)` and performs a no-throw pop.
- Values crossing the PostgreSQL error boundary are declared `volatile`.
- When `pg_qi.enabled=off`, the wrapper transparently bypasses its frame and
  policy machinery while retaining the downstream hook chain.

WP5 intentionally uses a no-delegation baseline policy. The backend-local
one-shot request and its generation are WP6 scope.

## Verification

For each PostgreSQL major (16, 17, and 18), the automated matrix:

1. compiled the extension with warnings treated as errors;
2. installed it and ran the existing regression suite;
3. retained the WP2 preflight and WP3 detector checks;
4. verified the successful baseline path with zero final depth;
5. triggered a genuine downstream planning error using `SELECT 1 / 0`;
6. observed the original `division by zero` error;
7. continued in the same backend and proved:
   - `depth=0`;
   - `cleanup_broken=false`;
   - `unwound=1`;
   - frame accounting remains valid;
   - `duckdb.force_execution=off` is restored;
   - diagnostics report a clean stack.

## Scope boundary

- Token consumption and actual one-shot delegation belong to WP6.
- Synthetic reentry belongs to WP7.
- General failpoint infrastructure belongs to WP8.
- Full S0–S11 scenario coverage belongs to WP9.
