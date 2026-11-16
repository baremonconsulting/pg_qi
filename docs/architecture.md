# Architecture and safety boundaries

## Status

This document describes the current `pg_qi` codebase after Spike 1, the
production/test split and the PostgreSQL 19 RC2 compatibility replay. The
project remains pre-alpha. The production build installs a planner hook,
evaluates a conservative PGQI-4A shadow candidate and records local plan
evidence, but it does not automatically route queries.

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
    broader WP14 classifier prototype used by test evidence

src/qi_eligibility.c
    conservative production shadow candidate classifier

src/qi_decision.c
    versioned backend-local shadow decision record and read-only GUCs

src/qi_detector.c
    DuckDBScan plan-tree detector used by shadow evidence and test diagnostics

src/qi_diag.c, src/qi_reenter.c
    test-only diagnostics, failpoints and synthetic planner re-entry

src/include/pg_qi/planner_compat.h
    PostgreSQL planner-signature compatibility
```

The production shadow classifier is deliberately narrower than the WP14
prototype. Neither classifier result is permission to route.

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
                +-- observe availability, context, provenance and candidacy
                |
                +-- saved hook or standard planner (exactly once)
                                      |
                                      v
                              observe returned plan
                                      |
                                      v
                          publish pass-through snapshot
```

The PGQI-4A production path creates no delegation frame or GUC nest and never
originates a delegation request. The explicit test build retains the Spike
delegation path and its frames.

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

Synthetic nested-planner masking is part of the explicit test build. PGQI-4A
tracks recursion depth for observation, but depth zero is only an unproved
outermost hook invocation; it is not a complete production top-level-context
proof. Production shadow mode never owns force, so no inherited `qi_stack`
request can originate from its call path.

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

## Classifiers

`qi_classify_query()` is a positive allow-list over an analysed PostgreSQL
`Query` tree. Unknown node types and unsupported relations, joins, operators,
aggregates and data types receive an ineligible result. Its reason names are
deterministic within the current prototype.

The prototype is evidence for a future policy stage, not a complete safety
proof. PGQI-4A therefore wraps it with a narrower production shadow classifier
that explicitly rejects active RLS metadata, security qualifications,
TABLESAMPLE, active inheritance, partitioned or non-permanent relations,
joins, aggregates, grouping, ordering, parameters, functions and casts.

The result is candidate research data, not a routing certificate. Production
routing remains disabled until the complete routing contract is implemented
and tested.

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

PGQI-4A uses the detector only as evidence at the downstream-planner-return
boundary. A later-loaded outer planner hook may transform the plan after
`pg_qi` returns, so every shadow result remains `unverified`. A negative match
is recorded as `unknown`, not as PostgreSQL.

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

PGQI-4A shadow reasons and eligibility details are versioned machine-readable
values. Reserved routing reasons remain unimplemented.

## Next implementation boundary

PGQI-4A implements only the observational subset of the
[production decision contract](production-decision-contract.md). The remaining
routing gates—policy source, proven context, owned-force isolation, poison
handling and final-engine verification—must be implemented and reviewed before
automatic routing can be enabled.
