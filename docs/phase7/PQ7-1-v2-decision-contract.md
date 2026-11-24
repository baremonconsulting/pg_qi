# PGQI-7 V2 decision contract

Status: frozen contract for decision-only shadow development. This document does not activate routing.

## Decision axes

V2 records three independent gates. Each gate is one of `NOT_EVALUATED`, `PASS`, `VETO`, or `UNKNOWN`:

- `eligible`: the query is inside the bounded syntactic and catalogue capability;
- `semantically_safe`: exact PostgreSQL semantics are certified for the complete environment identity;
- `beneficial`: the conservative model has sufficient evidence that delegation would be useful.

`UNKNOWN` is preserved as audit evidence. It is never treated as `PASS`.

## Recommendation

The recommendation is not a routing command:

- `WOULD_PASS_THROUGH`: at least one gate returned `VETO`;
- `WOULD_DELEGATE`: all three gates returned `PASS`, all version evidence is present, and the explicit activation barrier is open;
- `INSUFFICIENT_EVIDENCE`: a gate was not evaluated or is unknown, version evidence is missing or unsupported, the barrier is closed, or a future unknown reason code was observed.

The activation barrier is `CLOSED` by default. PQ7-1 provides no production mechanism that can open it. Recommendation, policy intent, force provenance, and observed engine are separate fields; none implies another. In particular, even the contract test for `WOULD_DELEGATE` retains `policy_intent=pass_through`, `force_origin=off`, and `observed_engine=unknown`.

## Versions

| Field | Initial value | Meaning |
|---|---:|---|
| contract version | 2 | decision schema and invariants |
| rule version | 1 | eligibility rule set |
| model version | 1 | benefit model identity |
| certificate version | 1 | semantic certificate format |

A positive recommendation requires the exact supported rule, model, and certificate versions. Missing and unknown future versions fail closed. Later snapshots must also bind them to the exact build and site identities defined by their owning tickets.

## Truth table

The table uses `*` for any state not selected by an earlier row.

| Eligible | Semantically safe | Beneficial | Versions | Barrier | Recommendation |
|---|---|---|---|---|---|
| VETO | * | * | * | * | WOULD_PASS_THROUGH |
| * | VETO | * | * | * | WOULD_PASS_THROUGH |
| * | * | VETO | * | * | WOULD_PASS_THROUGH |
| no VETO, any UNKNOWN / NOT_EVALUATED | any | any | * | * | INSUFFICIENT_EVIDENCE |
| PASS | PASS | PASS | missing / unsupported | * | INSUFFICIENT_EVIDENCE |
| PASS | PASS | PASS | exact supported versions | CLOSED | INSUFFICIENT_EVIDENCE |
| PASS | PASS | PASS | exact supported versions | OPEN | WOULD_DELEGATE |

Reject records that disagree with the table. Check for a veto in eligibility
first, then semantic safety, then benefit. If there is no veto, choose the
first unknown state in the same order. Check any remaining `NOT_EVALUATED`
state before evaluating the versions and barrier.

Keep an unrecognised reason code only for an `INSUFFICIENT_EVIDENCE` record
that declares contract version 2, has no veto and contains insufficient known
evidence for `WOULD_DELEGATE`. A future producer needs a contract or registry
revision to attach a new reason to a veto or a positive recommendation.

## Stable reason-code registry

Existing numbers are append-only and must never be reassigned.

| Code | Name |
|---:|---|
| 0 | NOT_EVALUATED |
| 100 | ELIGIBILITY_VETO |
| 200 | SEMANTIC_VETO |
| 300 | BENEFIT_VETO |
| 400 | ELIGIBILITY_UNKNOWN |
| 500 | SEMANTIC_UNKNOWN |
| 600 | BENEFIT_UNKNOWN |
| 700 | ACTIVATION_BARRIER_CLOSED |
| 800 | VERSION_EVIDENCE_INVALID |
| 900 | ALL_GATES_PASS |

Unknown numeric codes are forward-compatible audit data, not positive evidence.

## Compatibility boundary

The V1 backend-local snapshot and all existing V1 SQL/GUC fixtures remain unchanged. V2 publication and runtime wiring are intentionally deferred to later PGQI-7 tickets.
