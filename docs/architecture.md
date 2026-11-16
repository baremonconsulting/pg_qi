# Architecture and safety boundaries

## Status

This document describes the current `pg_qi` codebase after Spike 1, the
production/test split and the PostgreSQL 19 RC2 compatibility replay. The
project remains pre-alpha. The production build installs a planner hook and
the delegation infrastructure, but it does not classify or automatically
route queries.

The [production decision contract](production-decision-contract.md) defines
the required behaviour for the next implementation stage. It is a design
contract, not a description of functionality available today.

## Components

```text
src/pg_qi.c
    preload, GUC registration, planner-hook chaining and scoped frames

src/qi_delegation.c
    backend-local frame stack, duckdb.force_execution adapter and provenance

src/qi_classify.c
    fail-closed classifier prototype; evaluated only by test builds

src/qi_detector.c
    DuckDBScan plan-tree detector; consumed only by test diagnostics today

src/qi_diag.c, src/qi_reenter.c
    test-only diagnostics, failpoints and synthetic planner re-entry

src/include/pg_qi/planner_compat.h
    PostgreSQL planner-signature compatibility
```

The classifier and detector are compiled into the production library, but the
production planner path does not invoke them. Compilation is not a supported
production interface.

## Loading and hook ownership

`pg_qi` must be loaded through `shared_preload_libraries` to install its
planner hook. Loading the library later through `CREATE EXTENSION` makes the
SQL functions and GUC registration available but cannot retroactively install
the hook.

At preload, `pg_qi` saves the existing `planner_hook` and installs its own.
For every planning event it calls the saved hook exactly once, or calls
`standard_planner()` when no predecessor exists. On unload, it restores the
saved hook only if it still owns the global hook slot. Load order therefore
defines hook order; `pg_qi` never assumes that it is the only planner-hook
extension.

```text
caller or later-loaded outer hook
                |
                v
          pg_qi planner hook
                |
                +-- pg_qi.enabled = off --> saved hook or standard planner
                |
                +-- enabled -------------> scoped frame
                                             |
                                             v
                                   saved hook or standard planner
                                             |
                                             v
                                      returned plan
```

The current production path always enters a frame when enabled, but never
originates a delegation request because no production decision source exists.

## Delegation frames and GUC scope

Delegation state is backend-local. A bounded LIFO stack records, for each
planning invocation:

- a monotonically increasing frame identifier;
- the PostgreSQL GUC nest level;
- inherited force provenance;
- whether `pg_qi` requested delegation;
- effective force provenance; and
- whether the frame changed `duckdb.force_execution`.

The adapter changes `duckdb.force_execution` only through PostgreSQL's GUC
machinery. On a successful planning return, `AtEOXact_GUC()` restores the
frame's scoped change before the frame is popped. On an error, the catch path
removes the frame without throwing and rethrows the original error; PostgreSQL
then restores the GUC nest level during transaction or subtransaction error
cleanup. The catch path does not itself call `AtEOXact_GUC()`.

Frame removal is order-checked. An ordering or underflow violation marks the
backend-local cleanup sentinel as broken. The present sentinel is diagnostic;
production fail-closed enforcement for a poisoned state is a prerequisite of
the next runtime stage.

Synthetic nested-planner masking is part of the explicit test build. The
production build currently passes no general re-entry signal into a frame and
must not be described as providing a complete production nested-planning
guard.

## Force provenance

The adapter distinguishes three origins:

| Origin | Meaning | Current behaviour |
|---|---|---|
| `off` | `duckdb.force_execution` was false on entry | Remains false unless an owning frame changes it |
| `external` | The setting was already true on entry | Preserved; `pg_qi` does not silently revoke another component's request |
| `qi_stack` | A `pg_qi` frame owns the force request | Scoped to the owning frame and restored on exit |

An externally forced DuckDB plan is not a `pg_qi` eligibility decision. This
distinction must remain visible in future decision records. Consequently,
"fallback to PostgreSQL" means that `pg_qi` does not initiate delegation; it
does not mean that `pg_qi` overrides an external force request.

Preload fails when `duckdb.force_execution` is missing, is a placeholder or is
not resettable. The pinned `pg_duckdb` dependency declares it as Boolean; the
current preflight does not independently validate the GUC type. The adapter is
validated only against the pinned dependency identities recorded in the test
reports.

## Classifier prototype

`qi_classify_query()` is a positive allow-list over an analysed PostgreSQL
`Query` tree. Unknown node types and unsupported relations, joins, operators,
aggregates and data types receive an ineligible result. Its reason names are
deterministic within the current prototype.

The prototype is evidence for a future policy stage, not a complete safety
proof. It is not called by the production hook and does not yet establish a
contract for every query property, including row-security qualifications,
table sampling, grouping sets and all sort/group semantics. Production
routing must remain disabled until the decision contract and its complete
fail-closed traversal are implemented and tested.

## Decision time and cached plans

Planner-hook policy is evaluated when PostgreSQL creates a plan, not on every
execution:

- a custom plan receives a decision when that custom plan is created;
- a generic plan retains its chosen engine while the cached plan is reused;
- invalidation causes a new planning event and therefore a new decision; and
- changing `pg_qi.enabled` does not rewrite a generic plan that has already
  been created.

Unknown or parameter-dependent semantics must be ineligible unless the future
classifier can prove them safe at planning time. Per-execution enforcement
would require a separate executor-stage design and is outside this
architecture.

## Fallback and error behaviour

| Situation | Required interpretation |
|---|---|
| `pg_qi` disabled or policy rejects a query | Call the downstream planner unchanged; do not request delegation |
| No saved planner hook | Call `standard_planner()` |
| Delegation requested but downstream returns no verified `DuckDBScan` | The returned downstream plan remains authoritative; record the engine as unknown unless it is independently verified |
| Downstream planning raises an error | Restore scoped state through PostgreSQL error cleanup and propagate the original error |
| External force is already active | Preserve it and record external provenance; do not claim PostgreSQL fallback |

There is no automatic retry, executor fallback or second planning attempt
after a planner or execution error.

## Observed execution engine

Policy intent and observed plan are separate facts. The detector recognises a
`CustomScan` whose provider name is exactly `DuckDBScan`, including supported
child and subplan locations. A positive match proves that the plan returned to
the `pg_qi` hook contains that provider. A negative match is not a universal
proof that every possible alternative engine is absent.

The detector is currently used only by the test build. A later-loaded outer
planner hook may also transform a plan after `pg_qi` returns, so a future
production claim about the final engine requires either a controlled hook
ordering contract or verification at a point after all plan transformations.
Unverifiable results must be reported as unknown, not as verified PostgreSQL.

## Production and test surfaces

The default build exposes only:

- the planner integration;
- `pg_qi.enabled`;
- `qi.version()`; and
- `qi.is_preloaded()`.

`PG_QI_TESTING=1` additionally builds the separate `pg_qi_spike_test`
extension and enables Spike controls, diagnostics, failpoints, synthetic
re-entry and benchmark instrumentation. These interfaces are not supported
production APIs and must not be present in a production artefact. See the
[production/test boundary](phase2/WP14-production-test-boundary.md).

## No synchronous external work in hooks

Planner and executor hooks must not perform DNS lookups, HTTP requests, socket
I/O, remote policy fetches or synchronous telemetry export. A future policy
snapshot must be local, bounded and available before hook entry. Telemetry
must use a bounded local hand-off and must never make query planning depend on
network availability.

## Stable decision reasons

Future public reason codes are machine-readable interface values. They must
be lower-case stable identifiers, have deterministic precedence and evolve
additively. Operators must preserve unknown codes rather than reinterpret
them. Human-readable messages may change without changing the code.

The current classifier names are prototype values. They become a public
contract only when the production decision contract is implemented and its
versioned interface is released.

## Next implementation boundary

PGQI-3 changes documentation and freezes the design boundary only. The next
runtime stage must implement and test the
[production decision contract](production-decision-contract.md) before any
automatic routing can be enabled.
