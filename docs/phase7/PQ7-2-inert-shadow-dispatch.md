# PQ7-2 — Exhaustive routing dispatch and inert shadow skeleton

## Scope

PQ7-2 introduces the `shadow_p0` routing mode without enabling a new routing
path. The production planner hook now dispatches every defined routing mode
explicitly:

| Mode | Policy intent | May create a delegating frame | May change force |
|---|---|---:|---:|
| `off` | pass through | no | no |
| `allowlist` | request delegation for an eligible query | yes | yes |
| `shadow_p0` | pass through | no | no |

Any value not represented by the closed enum fails through the same inert path
as a missing policy. The PostgreSQL GUC parser rejects unknown textual values
before planning.

## Invariants

- Only `allowlist` can set `request_applied` or create a delegating frame.
- `shadow_p0` calls the downstream planner exactly once and returns the same
  `PlannedStmt *` it receives.
- Planner arguments, including PostgreSQL 19 `ExplainState *`, are forwarded
  unchanged by the compatibility wrapper.
- Existing nested force masking and poisoned-state abort behavior are shared by
  all modes and remain unchanged.
- An external force is observed but is never attributed to pg_qi.
- `shadow_p0` publishes the V1 audit reason `decision_only`; it does not claim
  that any V2 gate has passed.
- The skeleton collects only bounded relation-table facts across the complete
  query tree before downstream planning. A missing catalog row or more than 16
  combined range-table entries, or more than 256 visited query-tree nodes,
  produces an
  internal `UNKNOWN` fact state and never requests delegation. PQ7-5 extends
  this skeleton into the full positive capability walker.
- Fact collection in `shadow_p0` precedes and is independent of the historical
  V1 allow-list decision. Facts are published only with a successfully
  completed outer decision. A completed decision that did not observe P0 facts
  publishes `unknown/no_query`; a failed planner invocation leaves the prior
  completed snapshot and its facts together unchanged.
- Only coarse state and reason are exposed as read-only diagnostics; relation
  names, OIDs and counts are not exposed.

## Evidence

The production outer-hook fixture verifies:

- force is off at the predecessor boundary in `shadow_p0`;
- one predecessor call for a simple query;
- pass-through policy and `request_applied = off`;
- the returned plan pointer in the direct `shadow_p0` path;
- identical planner arguments on the shared pass-through wrapper, including
  PostgreSQL 19's `ExplainState *` at compile time; argument identity uses the
  dynamic-outer fixture topology so that the test hook can record both sides;
- the downstream error path is preserved and force is restored;
- the existing nested masking path remains operational.

The normal production regression additionally verifies that a query which is
eligible for the V1 allowlist remains native in `shadow_p0`.

## Non-goals

PQ7-2 does not evaluate complete P0 capability, any V2 gate, or publish the V2
snapshot. It does not activate routing, merge to `main`, or create a release.
