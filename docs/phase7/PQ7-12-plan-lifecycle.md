# PQ7-12 — plan lifecycle abstention and cache semantics

PQ7-12 prevents a decision produced while planning one statement from being
presented as evidence for an unrelated or merely reused execution. It remains
decision-only: it does not open the activation barrier or alter routing.

## Publication boundary

The planner prepares an unpublished candidate. The lifecycle layer records a
bounded SHA-256 digest of plan topology, planner estimates and statement
metadata. It visits at most 8192 plan nodes and never serialises expression
values, SQL text or parameter contents. The fixed eight-entry backend-local
registry retains only the digest. Each newly planned candidate retires older
unbound candidates, preventing
a rejected cached alternative from publishing during a later execution.
Only `SELECT` plans without modifying CTEs are armed. PostgreSQL
starts these portals during Bind, so no unbound DML candidate can survive a
protocol-message boundary.
`ExecutorStart` binds the candidate to the exact plan object produced by the
immediately preceding planner call, or to the same bounded structure when
PostgreSQL has copied that object. If multiple pending candidates have the same digest, the match is
ambiguous and all of them abstain. The first outer executor start also retires
all unbound alternatives, including a generic plan built for cost comparison
but rejected in favour of a custom plan. The first
`ExecutorRun` publishes the candidate and immediately removes the pending
entry. Execution-only metadata is not part of the digest.

Failure to fingerprint, registry exhaustion, an oversized plan, a mismatched
plan, or a missing execution hook leaves the snapshot unchanged. A direct
snapshot commit also rejects candidates without the proven
`fresh_execution` lifecycle marker.

Pending candidates are cleared after a bound executor ends without running,
on transaction abort or completion, on subtransaction abort, `DISCARD ALL`,
`RESET ALL`, role changes and session-authorisation changes. The registry contains no SQL
text or parameter values and is never exported.

## Prepared statements and cached plans

- A fresh simple, custom or replanned generic plan may publish once its
  corresponding executor run starts.
- Reuse of a generic cached plan performs no planner invocation, has no pending
  candidate and publishes no new event.
- A policy or routing GUC change alone does not turn old plan evidence into a
  new event.
- DDL invalidation followed by execution creates a fresh plan and may publish
  a new event.
- SQL `EXECUTE` is the only utility context admitted for lifecycle proof, and
  only when the planner source is the exact prepared statement source captured
  from PostgreSQL's prepared-statement registry. It remains ineligible to
  request allow-list routing. Parameter expressions, SPI, other utility,
  cursor and nested execution paths abstain unless they satisfy the ordinary
  direct-client proof independently.
- SQL and libpq prepared execution use the same lifecycle rules.

An unchanged event sequence after execution therefore means that no fresh
plan/execution correlation was established. It must not be interpreted as a
decision about that execution. Consumers must compare the opaque backend
incarnation, generation and event sequence with their expected correlation.

## Verification

`tests/phase7/verify_lifecycle.sh` exercises simple execution, SQL generic and
custom plans, repeated cached execution, a routing-mode change, DDL
invalidation, an open cursor, independent backends and libpq prepared
execution. The strict V2 reader requires
`lifecycle=fresh_execution` on every complete record. CI runs this suite on
every supported PostgreSQL major.
