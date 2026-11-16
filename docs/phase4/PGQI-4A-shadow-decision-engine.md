# PGQI-4A: production shadow decision engine

> This document records the completed PGQI-4A boundary. PGQI-4B subsequently
> adds a default-off controlled routing path; see
> [PGQI-4B](PGQI-4B-controlled-routing.md).

## Outcome

PGQI-4A adds an observational production path without enabling routing. For
every outermost invocation observed by the pg_qi planner hook, it:

1. records whether pg_qi is enabled and whether force was already external;
2. evaluates a conservative candidate classifier when the earlier gates allow;
3. calls the downstream planner exactly once with the original arguments;
4. records positive `DuckDBScan` evidence at the downstream-return boundary;
5. publishes a fixed-size backend-local snapshot; and
6. returns the downstream plan unchanged.

The production path does not create a GUC nest, consume a Spike token, call
`qi_set_force_execution()`, retry planning or replace the plan pointer. Its
policy intent is always `pass_through` and `shadow_request_applied` is always
false.

## Observation contract

The snapshot is exposed as `PGC_INTERNAL` settings so `SHOW` can inspect it
without causing another planning event:

| Setting | Meaning |
|---|---|
| `pg_qi.shadow_contract_version` | schema version, initially `1` |
| `pg_qi.shadow_decision_id` | saturating backend-local completed-plan sequence |
| `pg_qi.shadow_policy_intent` | always `pass_through` in PGQI-4A |
| `pg_qi.shadow_reason` | first applicable decision gate |
| `pg_qi.shadow_force_origin` | `off` or genuinely external force in production shadow mode |
| `pg_qi.shadow_context` | `unproved_outermost` or recursive `nested` |
| `pg_qi.shadow_classifier_status` | `not_evaluated`, `candidate` or `rejected` |
| `pg_qi.shadow_classifier_reason` | conservative eligibility detail |
| `pg_qi.shadow_observed_engine` | `duckdb` only for a positive local match; otherwise `unknown` |
| `pg_qi.shadow_verification` | always `unverified` until a final boundary is proved |
| `pg_qi.shadow_observation` | local returned-plan evidence |
| `pg_qi.shadow_request_applied` | always `off` in PGQI-4A |

Nested completions do not overwrite the outer snapshot. An error propagates
unchanged and does not publish a partially completed record. Records are
backend-local and are not a durable audit trail.

## Conservative candidate subset

The shadow classifier is deliberately narrower than the historical WP14
differential prototype. It can identify a candidate only for a plain SELECT
over at most one permanent ordinary relation with no active inheritance
children, RLS metadata or table sampling. The existing expression allow-list
still applies, with additional rejection of parameters, aggregates, functions,
casts, grouping, ordering, joins and unreviewed expression shapes.

This result is research evidence, not permission to route. An ordinary query
may be rejected without affecting its plan or result.

## Context and engine limits

Planner-hook recursion depth identifies a known nested pg_qi invocation, but
depth zero cannot prove that planning originated directly from a client rather
than SPI or another execution context. The public context value is therefore
`unproved_outermost`, not `top_level`.

The detector sees the plan returned by pg_qi's downstream planner. A
later-loaded outer hook may still transform it. A positive provider match is
recorded as local DuckDB evidence with `unverified`; a negative match is
`unknown/unverified`, not PostgreSQL.

## Verification

CI builds production and test variants separately on PostgreSQL 16, 17, 18
and pinned PostgreSQL 19 RC2. The production-shadow job installs the actual
default build and checks:

- a plain candidate and policy-missing pass-through;
- aggregate, ordering, parameter, inheritance, RLS and TABLESAMPLE rejection;
- disabled behavior;
- external-force provenance;
- `pass_through`, `unverified` and request-not-applied invariants; and
- absence of test-only symbols from the production library.

Existing Spike regression, S0–S11, differential, re-entry, parallel and
concurrency suites continue to exercise the explicit test build.

## Non-goals and next gate

PGQI-4A does not implement a policy source, automatic routing, final-engine
verification, executor retry, network telemetry, shared memory or durable
storage. PGQI-4B must not enable routing until context proof, owned-force
isolation, poisoned-state handling, complete provider verification and the
remaining acceptance evidence in the
[production decision contract](../production-decision-contract.md) are
implemented and independently reviewed.
