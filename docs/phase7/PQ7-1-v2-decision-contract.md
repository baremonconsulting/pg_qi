# PGQI-7 V2 decision contract

Status: frozen contract for decision-only shadow development. This document does not activate routing.

## Decision axes

V2 records three independent gates. Each gate is one of `NOT_EVALUATED`, `PASS`, `VETO`, or `UNKNOWN`:

- `eligible`: the query is inside the bounded syntactic and catalog capability;
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

Any serialized combination that contradicts these rules is invalid and must be rejected. A veto has priority over unknown or not-evaluated states. If several gates veto, reason precedence is eligibility, semantic safety, then benefit. Without a veto, unknown reason precedence is eligibility, semantic safety, then benefit; any remaining not-evaluated state precedes version and barrier evaluation. An unrecognized reason code is retained only with `INSUFFICIENT_EVIDENCE`, only when no gate vetoes, and never when the known fields otherwise justify `WOULD_DELEGATE`.

Unknown reason-code preservation applies only to records that still declare contract version 2. A future producer must not attach an unknown reason to a known veto or positive recommendation and expect a V2 reader to reinterpret it; such a record is invalid and requires an explicit contract/registry revision.

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
