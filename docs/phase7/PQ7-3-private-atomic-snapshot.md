# PQ7-3 — Private atomic V2 decision snapshot

PQ7-3 adds a bounded backend-local V2 diagnostic record. It does not activate
routing, open the V2 activation barrier, merge to `main`, or create a release.

## Publication

V2 publication is limited to a proven outer client planner invocation. The
downstream planner is called exactly once. A candidate is prepared locally,
the owned force frame is restored and removed, and only then is the complete
candidate copied into an inactive fixed slot and made active with one slot
index update. Nested planning, SPI planning from executor hooks, utility
planning, parallel workers, unproved hook topology, planner errors,
cancellation, and cleanup failure do not publish a V2 event.
PQ7-12 adds one narrow utility exception for a top-level client SQL `EXECUTE`
whose prepared-statement source is proven exactly; it remains decision-only.
Only `SELECT` plans without modifying CTEs can publish lifecycle evidence.
The outer-client proof also requires the planner query source to be the active
backend command source. SQL planned by deferred triggers or deferred foreign
key checks during transaction completion is therefore not published as a
client event.

The three gates remain `not_evaluated`, the activation barrier remains closed,
and the recommendation is `insufficient_evidence`. The envelope can describe
the existing V1 policy intent and observed engine without turning either into
a V2 routing instruction.

## Identity and lifecycle

Each event is identified by an opaque random backend incarnation, a diagnostic
generation, and an event sequence that increases monotonically within that
generation. Counters never wrap. Exhaustion disables further V2 publication
until reconnect. The routing
mode and enabled state are captured at decision entry and cannot be relabelled
by a later `SET`.

Successful `DISCARD ALL`, `SET/RESET ROLE`, and session-authorization changes
invalidate the record and advance the generation. A pool must use `DISCARD
ALL` or reconnect before handing a physical connection to another client.
Two logical clients that reuse the same backend and role without a handoff
signal are intentionally outside the correlation guarantee. A client must pin
one physical connection across request and read-back.

## Read-back and privacy

`SHOW pg_qi.decision_v2` is the sole V2 read-back. `SHOW` does not invoke the
planner. The setting is hidden from `SHOW ALL`, is not reportable, and stores
only an inert placeholder; its show hook renders the private fixed-size record
on demand.

Every read checks authorization again. Superusers and members of a provisioned
`NOLOGIN` role named `pgqi_diagnostic` may read the
record. A role with that name is rejected if it has `LOGIN`. The producing
effective and session users must still match the current principal. Unauthorized
reads return a constant denied envelope without identity or payload.

The record contains no SQL text, literals, parameters, fingerprints, relation
or column names, catalog OIDs, MCVs, histograms, estimates, or arbitrary
strings. It contains only fixed enums, versions, bounded counters, coarse P0
state, and opaque correlation identity.

`SELECT current_setting('pg_qi.decision_v2')` is deliberately not a supported
read-back: planning that SELECT creates a newer event before the value is
read. Regression coverage preserves this warning.

A complete event proves that planning completed and the corresponding
executor run began; it does not prove that execution succeeded. The wire
field `lifecycle=fresh_execution` records that correlation. This addition is
the exact-field-set envelope schema `sv=2`; strict `sv=1` readers and records
are intentionally incompatible rather than silently reinterpreted.
Re-executing a
cached plan does not invoke the planner and therefore does not create a fresh
event. Consumers must require the
expected correlation identity and sequence instead of treating the most
recent backend record as evidence for an arbitrary execution.

## Readers

`tools/phase7/decision_snapshot.py` implements strict and independent V1 and
V2 readers. It rejects duplicate, missing, extra, oversized, non-ASCII, or
contradictory fields. Unknown envelope or contract versions are unavailable
evidence and never fall back to V1. Unknown V2 numeric reasons are accepted
only under the restrictions frozen by PQ7-1.
