# Production decision contract

## Status and scope

This document is the normative design input for the next production-routing
stage. It is not implemented by the current production build and does not
authorise automatic routing.

The contract separates four facts that must never be collapsed into one:

1. whether `pg_qi` was allowed to make a decision;
2. whether policy requested delegation;
3. who, if anyone, forced the downstream engine; and
4. which engine was verified from the returned plan.

## Decision record

A production decision must contain at least:

| Field | Required values |
|---|---|
| contract version | version of this decision schema |
| decision time | plan creation or replanning |
| policy intent | `pass_through` or `request_delegation` |
| reason code | stable machine-readable identifier |
| force origin | `off`, `external` or `qi_stack` |
| observed engine | `postgresql`, `duckdb` or `unknown` |
| verification | `verified` or `unverified` |

An observed engine is not inferred from policy intent. `duckdb` is verified
only by an authoritative plan-tree match. `postgresql` is verified only when
the supported provider set is complete for the deployment and the plan is
observed after all permitted transformations. Otherwise the engine is
`unknown` and verification is `unverified`.

## Ordered decision procedure

For each new plan, the implementation must apply this order:

1. **Provenance.** Derive logical force provenance from the owning frame stack
   before interpreting the ambient GUC. An active GUC is external only when no
   `pg_qi` frame owns it.
2. **Planning-context isolation.** Before any downstream call, a nested,
   re-entrant or otherwise unproved invocation must not inherit a parent-owned
   `qi_stack` request. Temporarily mask that request in its own scoped frame
   and restore it on success or error. This isolation also applies when
   `pg_qi` is disabled or its state is poisoned. If safe masking is
   unavailable, abort without entering the unsafe downstream path.
3. **Availability.** If `pg_qi` is disabled, not preloaded or its local state
   is poisoned, make no new `pg_qi` force request. After the isolation above,
   call the downstream planner unchanged when it is safe to do so.
4. **Planning context.** If the invocation is nested, re-entrant or otherwise
   outside the proven top-level context, it must not inherit a parent-owned
   request and cannot originate a new one unless a separately tested rule
   explicitly allows it.
5. **External provenance.** Preserve a genuinely external force request and
   use an external-force reason. Do not run this path as a `pg_qi`-approved
   delegation.
6. **Classification.** Evaluate a complete positive allow-list. Any unknown
   query, expression, relation, security or parameter semantic is ineligible.
7. **Policy.** Apply only local, bounded policy state. Missing, invalid, stale
   or incompatible policy passes through.
8. **Scoped request.** When classification and policy both allow delegation,
   open an owning frame and scope the adapter change to that frame.
9. **Downstream planning.** Call the predecessor exactly once, or
   `standard_planner()` when none exists. Preserve the original error.
10. **Verification.** Inspect the authoritative plan at the agreed final
   verification boundary and record the observed engine independently of the
   requested engine.
11. **Cleanup.** Restore owned GUC state and remove the frame in LIFO order.
   Cleanup failure poisons further `pg_qi` delegation in that backend.

The procedure must not perform network I/O and must not retry planning through
another engine after an error.

## Minimum fail-closed eligibility

The production classifier must positively account for every semantic field
that can affect execution equivalence. At minimum this includes:

- command and query shape;
- relation kind, inheritance, partitioning and table sampling;
- row-security and security-barrier qualifications;
- joins and join qualifications;
- target, filter, grouping, ordering, distinct, limit and offset expressions;
- aggregates, windows, subqueries, CTEs and set operations;
- functions, operators, casts, collations and data types;
- volatile or side-effecting expressions; and
- bound, unbound and parameter-dependent values.

Absence of an explicit safe rule is an ineligible result. A catalog lookup
failure, unknown AST node or unsupported provider state is also ineligible.

## Prepared and generic plans

The decision is attached to plan creation. Reusing a cached generic plan does
not create a fresh policy decision. Replanning after invalidation does.

Until parameter-sensitive equivalence is demonstrated, a decision that
depends on values unavailable during generic planning must pass through.
Disabling `pg_qi` affects future planning; it does not revoke an already cached
plan. Any requirement for per-execution revocation belongs to a separately
designed executor control.

## Fallback contract

`pg_qi` fallback is conservative non-intervention:

- ineligible, unknown or unavailable policy means no `pg_qi` force request;
- a declined delegation request leaves the downstream plan authoritative;
- an external force request remains external and is not relabelled as a
  `pg_qi` decision;
- downstream errors propagate unchanged; and
- no automatic executor retry is performed.

Fallback must not be described as guaranteed PostgreSQL execution when an
external force or an unrecognised provider may still determine the engine.

## Reason-code rules

Reason codes are lower-case ASCII identifiers. Their meaning is immutable
within a contract version, precedence is deterministic, and new codes are
added without reusing old values. Consumers must accept and preserve unknown
codes.

The first production version must reserve distinct families for:

- disabled or unavailable control state;
- external force provenance;
- classifier rejection;
- policy rejection;
- delegation request and decline;
- plan verification failure; and
- poisoned cleanup state.

Prototype classifier names may be adopted only after their coverage and
precedence are reviewed as part of the production API.

## Required implementation evidence

Automatic routing remains disabled until the implementation proves:

- production classifier coverage and negative tests for every unsupported
  semantic family;
- planner-hook chaining with another hook on each supported PostgreSQL major;
- balanced frames and GUC restoration on success, subtransaction error and
  propagated error;
- production nested/re-entry handling;
- masking and restoration of parent-owned force for unsupported nested work;
- external-force provenance without false policy attribution;
- custom, generic, reused and invalidated plan semantics;
- authoritative engine verification and the unknown/unverifiable path;
- absence of test controls from the production artefact;
- absence of network operations on hook paths; and
- stable reason-code compatibility tests.

All supported PostgreSQL and pinned `pg_duckdb` combinations must pass the
same acceptance suite. A human review is required before enabling routing in
the default production build.
