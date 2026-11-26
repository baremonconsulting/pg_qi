# Spike 1 preparation

14 October 2026

Start on PostgreSQL 17 and verify the spike on PostgreSQL 16, 17 and 18.
In `https://github.com/baremonconsulting/pg_qi.git`, use `spike/1-delegation`
from `main` at `d3529d4e6a7cd71657bbf8279c533a3c5a81e2df`. Pin pg_duckdb to
`ee38d3b540ecea1d93683ba99bdcec5632a21eaf`.

## Which source to follow

If the documents disagree, use the first applicable source in this order.

1. The pinned pg_duckdb code and the matching PostgreSQL branch.
2. *Rigorous Review of the Spike 1 Delegation Mechanism Implementation*.
3. The consolidated v10 specification with the review corrections.
4. The Czech v8 implementation guide and earlier technical reviews.

## Decisions before WP0 and WP1

### R1 — Planner hook signature

The PostgreSQL 16–18 hook has four arguments. At the recorded PostgreSQL
`master` revision, it takes an additional argument of type `ExplainState *`.
Use PostgreSQL's public type in place of the proposed
`typedef void ExplainState;` placeholder.

### R2 — Operations that can raise an error

Once the delegation frame is on the stack, put every operation that can raise
`ERROR`, including failpoints, inside the corresponding `PG_TRY`. This
addresses the blocking issue in v10. The spike uses the existing frame
ownership model. Ownership by `SubTransactionId` and transaction and
subtransaction callbacks are outside its scope.

### R3 — Recovery variables

Declare a local variable `volatile` if it changes in `PG_TRY` and recovery
reads it in `PG_CATCH`. In particular, a future `qi_try_local_subxact()` will
use `volatile bool ok`. Keep backend-local static state under its existing
declaration rules.

### R4 — Publishing changes

Work on the local branch. A GitHub push or pull request requires a separate
instruction.

### R5 — Compatibility evidence

Read the source to establish where the planner API changes. Verify that
boundary by building against the PostgreSQL 16–18 headers in CI.

## Questions for later work packages

- S4 needs a reproducible downstream planner error or a clearly identified
  test shim. Choose one before implementing S4.
- S6b-2 needs a known delegatable query. The test must prove delegation or
  record the specific reason it was declined.
- S8 uses the benchmark report to decide whether to accept planner-time
  decisions for generic plan reuse or move to option B.
- Keep the first upstream API for option B small. Consider it if option A
  fails.
- Prepare the PostgreSQL 19 compatibility shim. The required runtime matrix
  for this spike covers PostgreSQL 16–18.

## Build prerequisites

The initial host had GCC and GNU Make. Install `pg_config` and the
PostgreSQL 16, 17 and 18 server development headers, then run the compile matrix
before closing WP1.

## WP0 readiness criteria

- The pinned commits are available and the source can be inspected.
- R1–R5 have recorded decisions.
- Each open question has a later point at which it must be resolved.
