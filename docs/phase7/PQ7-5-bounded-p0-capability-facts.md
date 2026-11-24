# PQ7-5 — bounded P0 capability facts

`shadow_p0` contains an observation-only capability recognizer. It does not
request delegation and does not open the V2 activation barrier.

## Recognised capabilities

The recognizer admits exactly one aggregate target over exactly one local,
permanent user heap:

| Diagnostic value | Accepted expression |
| --- | --- |
| `count_star` | `count(*)` |
| `count_int4` | `count(int4_column)` |
| `min_int4` | `min(int4_column)` |
| `max_int4` | `max(int4_column)` |

The argument in the typed forms must be a direct base-table column. The
aggregate and its signature are identified by PostgreSQL catalogue OIDs, not by
names.

The accepted query has no predicate, join, grouping, CTE, subquery, set
operation, window, distinct aggregate, aggregate filter or ordering, limit,
row mark, parameter, retained cast/coercion node or additional target.
Semantically redundant casts that PostgreSQL removes during analysis leave no
node for the observer to distinguish. Unsupported and unknown shapes
produce an `unknown` fact state and never become evidence for delegation.

## Relation boundary

The relation must be an ordinary permanent heap in a user namespace. System,
TOAST, shared, temporary, unlogged, custom-access-method, foreign, view,
materialized-view, partitioned, partition, inheritance and row-security shapes
are rejected.

## Bounds and diagnostics

Inspection is positive and bounded to 16 range-table entries, 256 charged
nodes, depth 32 and 16 emitted facts. The current grammar emits one fact. The
counter helpers accept the exact boundary and reject boundary plus one. The
observer additionally rejects more than 16 RTEs before inspecting their
contents. Because the supported grammar is intentionally shallow and emits a
single fact, ordinary accepted queries cannot reach the node, depth or fact
ceilings; structurally unsupported shapes may abstain before a resource limit.

The test-only contract also runs a deterministic corpus of 2,112
well-allocated NodeTag, RTE-kind and aggregate-argument mutations. Every
mutation must abstain without a generic walker dispatch or observer-origin
ERROR. Focused catalogue fixtures additionally cover missing relation and
attribute OIDs, RTE/catalogue relkind disagreement and non-heap/custom access
methods without requiring optional contrib extensions in the CI image.

The internal diagnostics are:

- `pg_qi.shadow_p0_facts_state`: `bounded` or `unknown`;
- `pg_qi.shadow_p0_facts_reason`: bounded reason code;
- `pg_qi.shadow_p0_capability`: recognised value or `unknown`.

The atomic V2 snapshot retains its existing `p0` and `p0_reason` wire fields.
Capability recognition leaves eligibility, semantic-safety and benefit gates
as `not_evaluated`, keeps the activation barrier closed and leaves policy
intent at pass-through.
