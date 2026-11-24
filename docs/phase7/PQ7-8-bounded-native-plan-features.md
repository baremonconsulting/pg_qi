# PQ7-8 — bounded native plan features

PQ7-8 records a deliberately small, fail-closed set of PostgreSQL planner
features for later benefit-model work. It does not route a query, execute a
second planner, or change any V2 gate.

## Accepted plan

The first supported spine is exactly:

```text
Agg (Plain, Simple)
└── Seq Scan
```

The aggregate must match the capability established before planning:
`count(*)`, `count(int4)`, `min(int4)`, or `max(int4)`. Typed aggregate
arguments are resolved through the post-planner `OUTER_VAR` reference to the
sequential scan target list. The scan relation is resolved through the
`PlannedStmt` range table and must have the same OID as the pre-planner fact.

Grouping, partial/final aggregation, wrappers, alternate scans, custom scans,
subplans, initplans, parameters, row marks, malformed structures, relation
mismatches, and invalid estimates produce an explicit unknown result.

## Recorded facts

For a recognised spine, pg_qi records:

- aggregate and scan startup cost, total cost, row estimate, and width;
- relation OID and post-planning catalogue observations
  `catalog_relpages`/`catalog_reltuples` (not a claim about the exact values
  consumed by the planner);
- parallel-safe, parallel-aware, parallel-mode, worker, and limit facts;
- the recognised spine and bounded traversal counters.

A validity mask separates an unavailable value from a legitimate zero.
Unknown `reltuples` is not converted to zero.

Plans using a disabled path abstain. PostgreSQL 18 and later expose this with
`disabled_nodes`; older supported versions encode it using `disable_cost`.
Parallel-safe values are the flags emitted by that planner invocation, not an
environment-independent property of the SQL text.

## Private diagnostics

`SHOW pg_qi.plan_features` reads the last atomically completed outer
observation. It is available only to a superuser or to a member of the
non-login `pgqi_diagnostic` role. Failed and nested planning cannot publish a
partial replacement. The diagnostic record is separate from the stable V2
decision wire format.

The state `bounded` only means that the native plan shape and values were
recognised. It is not a benefit decision and cannot activate delegation.

### Diagnostic record

The semicolon-delimited record is versioned with `v=1`. A complete record
contains the atomic identity keys `cid`, `g`, and `e`, followed by `state`,
`reason`, `spine`, `known`, the recognised estimates and catalogue observations,
the planner's parallel flags, and `nodes`/`depth`. For the first recognised
spine, `parallel_aware=false`, `workers=0`, and `has_limit=false` are known
facts. Numeric fields absent from the validity mask are rendered as `unknown`.

Non-complete states are `denied`, `withheld`, `none`, `unavailable`,
`exhausted`, and `invalid`. `withheld` means that a completed record belongs to
a different effective or session identity. Consumers must match `cid/g/e`
against `pg_qi.decision_v2` before combining the two records.

## Verification

`tests/phase7/verify_plan_features.sh` compares the recorded values with
`EXPLAIN (FORMAT JSON)` and `pg_class`, verifies an index-optimised negative
case, checks authorisation, and proves that a failed statement preserves the
previous completed record. CI runs this check on PostgreSQL 16–19.
