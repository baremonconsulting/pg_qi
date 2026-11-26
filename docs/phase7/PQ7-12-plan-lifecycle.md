# PQ7-12 — plan lifecycle abstention and cache semantics

PQ7-12 prevents a decision produced while planning one statement from being
presented as evidence for an unrelated or reused execution. The same
lifecycle binds decisions for the experimental `bound` routing mode to
their executions.

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

### Delegation with bound values

During extended-protocol `Bind`, PostgreSQL converts the parameters into
typed values with NULL flags, then chooses a plan. A custom planning call
receives those values in `ParamListInfo` before PostgreSQL starts the portal.
In `pg_qi.routing_mode = 'bound'`, pg_qi assesses eligibility, semantic
safety and expected savings for the current values at that point. This
superuser-controlled mode starts with an empty policy.
[PostgreSQL Bind](https://github.com/postgres/postgres/blob/5c839a2a08b9b87a349be97371fac6c5a2873b10/src/backend/tcop/postgres.c#L1849-L1938)

Generic planning receives a null parameter list. Reusing a cached generic
plan can bypass the planner hook entirely. pg_qi leaves those executions to
PostgreSQL's normal planning policy. The server keeps control of when to
use generic plans.
[PostgreSQL plan cache](https://github.com/postgres/postgres/blob/5c839a2a08b9b87a349be97371fac6c5a2873b10/src/backend/utils/cache/plancache.c#L1234-L1296)

Bound routing checks the full analysed statement, including its subqueries
and CTEs. It checks table, column, function and provider-role permissions
and permits read-only access to ordinary permanent heap tables.
Statements with row security, locks, mutable functions, native function
access hooks or unsupported transaction state follow the normal path.
Routing is available for standalone statements and supported read-only
transactions. Read-write transaction blocks and savepoints follow the
normal path.
Bound values must have built-in base types and a bounded, self-contained
representation.

An exact rule with reviewed parity and sufficient measured savings lets
pg_qi copy the statement, substitute the typed constants and make a scoped
pg_duckdb planning request. Eligibility starts as unknown. It passes when
the registered DuckDB scan covers the whole plan and preserves the result
column names, types, modifiers, collations and origins. On a clean decline,
pg_qi restores the routing setting and plans the original statement with its
original parameters. pg_duckdb may emit a warning on refusal. Planning
errors and cancellation reach the client through PostgreSQL's normal
error handling. In an explicit transaction,
pg_duckdb can open its own transaction during preparation. A later
`SAVEPOINT` can fail even when pg_qi falls back to a PostgreSQL plan.

#### Qualify and install a rule

1. In a disposable, representative environment, run the statement through
   libpq with its real types and values. Capture the qualification key
   using step 2 and use a provisional rule in that environment to exercise
   the DuckDB arm. For each binding, verify actual engine execution and
   compare types, rows, NULLs and required ordering against PostgreSQL on
   the same snapshot. Measure paired end-to-end times including Bind,
   planning and execution. The
   [differential runner](PQ7-4-differential-oracle.md#measure-the-bound-route)
   collects this evidence for an exact binding.
2. In the target environment, leave `pg_qi.bound_policy` empty, select
   `bound` and execute the same binding on the intended connection
   identity. Read `SHOW pg_qi.bound_key` immediately after the execution.
   Correlate it with the fresh custom-plan event in `SHOW pg_qi.decision_v2`.
   Use a custom plan for this capture. The key is local to the postmaster
   lifetime and covers the resolved query, typed values, database and
   role identities, transaction mode, relation estimates and key execution
   settings, including non-default planner options. Keep the key private
   with the qualification evidence. Capture and qualify bindings again
   when upgrading from the earlier key format.
3. Install the reviewed rules in the superuser setting `pg_qi.bound_policy`.
   Each entry has the form below. Separate multiple entries with a comma.
   Expiry is a Unix timestamp in seconds.

   ```text
   key:postgres_lower_ms:duckdb_upper_ms:expires_at
   ```

A rule records typed parity for the exact query and bindings on the test
snapshot. Use conservative timings for the intended environment and an
expiry suitable for the workload. Qualify again if the data changes in ways
that matter to the query. Keep changes to DuckDB's session state through
`duckdb.raw_query` outside the connections that use reviewed rules.
The rule must clear both thresholds,
`pg_qi.bound_min_saving_ms` and `pg_qi.bound_min_saving_percent`, which
default to 5,000 ms and 10%. Settings changes covered by the key and
unqualified bindings pass through. Clear `pg_qi.bound_policy` to withdraw
rules for future plans.

## Verification

`tests/phase7/verify_lifecycle.sh` exercises simple execution, SQL generic and
custom plans, repeated cached execution, a routing-mode change, DDL
invalidation, an open cursor, independent backends and libpq prepared
execution. The strict V2 reader requires
`lifecycle=fresh_execution` on every complete record. CI runs this suite on
every supported PostgreSQL major.

The differential integration runs a prepared CTE pipeline with a left join,
aggregation and a window function. It checks text and binary Bind formats,
SQL NULL, actual DuckDB execution, typed parity, column origins, clean
declines, a revoked provider role, settings changes, volatile expressions,
a benefit veto, rule expiry and generic-plan reuse. Its fixed timing inputs
test the policy decision. Performance qualification uses measurements from
the intended workload.
