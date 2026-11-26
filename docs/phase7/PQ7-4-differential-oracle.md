# PQ7-4 — typed three-arm differential oracle

## Purpose

PQ7-4 provides a fail-closed laboratory and CI proof that one reviewed `SELECT`
has the same typed result under three execution policies:

1. PostgreSQL native execution (`pg_qi.routing_mode=off`, external force off);
2. externally forced pg_duckdb execution (`duckdb.force_execution=on`);
3. inert `pg_qi` `shadow_p0` evaluation with external force off.

The three-arm check keeps pg_qi in `off` or `shadow_p0`. The forced arm
invokes pg_duckdb directly to establish provider execution and equivalence
independently of the routing decision.

## Bound statements

Pass `--bindings-file` to `run_differential.py` for a query with libpq text
parameters. Supply a JSON array in placeholder order. Each entry has an
`oid` and a `value`. OID zero asks PostgreSQL to infer the parameter type.
Use a JSON string for a text parameter or JSON null for SQL NULL. For
example, a query with a numeric bound and an optional numeric filter can
use these parameters.

```json
[{"oid": 23, "value": "17"}, {"oid": 23, "value": null}]
```

Use a version 2 run manifest with the SHA-256 of the bindings file in
`bindings_sha256`. Each arm uses the same validated bindings through the
extended protocol. The arm receipts include the bindings digest and the
witness token binds it to that execution. Version 1 manifests cover queries
without bindings.

A passing trial establishes provider execution and typed parity for that
statement, its bindings and the recorded test environment. Repeated timings
on representative data establish the benefit of delegation.
The CTE pipeline in the integration suite combines a window with a grouped
aggregate. It also tests an optional filter with a bound NULL. A provider
that falls back for that variant yields `force_declined`. A provider that
executes it must pass the same typed comparison as the single-bound case.

### Measure the bound route

Use `--bound-pairs` with a version 2 manifest and bindings file to qualify one
binding in a disposable cluster that preloads the execution witness. Give
each binding a new private output directory. The command uses the same
manifest and parameter format as the three-arm check.

```sh
python3 tools/phase7/run_differential.py \
  --driver tools/phase7/differential-driver \
  --conninfo 'service=pgqi_lab' \
  --manifest "$run/manifest.json" \
  --sql-file "$run/subject.sql" \
  --bindings-file "$run/bindings.json" \
  --run-root "$run" --bound-pairs 3 --deadline-ms 300000
```

The runner exports one read-only repeatable-read snapshot for the whole run.
An untimed probe gets the key from a fresh `bound` decision with an empty
policy. It then alternates native and bound executions in separate sessions.
The bound session uses a short-lived trial rule and zero saving floors within
its transaction to exercise the route. The execution witness must show a
completed DuckDB scan, and the result must match PostgreSQL on the imported
snapshot. A change to the qualification key during acquisition ends the run
with an inconclusive result. This can happen when catalogue row estimates
change during the measurements.

The driver times each unnamed extended-protocol request from submission
through receipt of the result, including Bind and planning. `result.json`
records the timings and session saving floors used in the assessment. For the
benefit decision it rounds the fastest native sample down and the slowest
bound sample up, both to milliseconds. A pass requires actual DuckDB
execution, typed parity and a saving that clears both floors. The default
floors are five seconds and ten per cent.

Use representative data and bindings for timing. The integration fixture
generates a CTE report with paired `COUNT(DISTINCT)`, grouping and a window,
and checks both a range and a NULL upper bound. The small fixture exercises
the route, typed parity and the expected saving veto. Review passing evidence
and capture the key for the intended environment before installing a rule
according to the [bound routing procedure](PQ7-12-plan-lifecycle.md#qualify-and-install-a-rule).

## Components and trust boundary

- `tools/phase5/protocol_driver.c` writes the typed spool. PGQSP001 remains the
  default and byte-compatible. PGQSP002 is explicit opt-in.
- `tools/phase5/corpus_oracle.py` remains the only value, descriptor, ordering
  and multiset comparator.
- `tests/phase7/witness/pg_qi_execution_witness.c` is a separate, test-only
  preload library. It records bounded scalar execution metadata, never SQL,
  names or values. It is absent from production `pg_qi.so`.
- `tools/phase7/differential-driver` owns one exporter connection or one arm
  connection. Python never opens a database connection.
- `tools/phase7/run_differential.py` coordinates processes and creates private
  receipts.
- `tools/phase7/differential_oracle.py` validates bindings and derives the
  verdict. It delegates all row comparison to the existing phase-5 oracle.

The preload order in the dedicated test cluster is:

```text
pg_duckdb, pg_qi_execution_witness, pg_qi
```

This leaves pg_qi as the outer production hook while its chained predecessor
is the witness. The existing pg_qi hook-identity proof therefore remains valid.

## PGQSP002 format

All integers are network byte order.

```text
8 bytes   magic: PGQSP002
u32       column count
repeat column count:
  u32     UTF-8 name length
  bytes   column name
  u32     actual PQftype
  i32     actual signed PQfmod
  u32     actual PQfformat
repeat rows:
  u32     field count
  repeat field count:
    u32   byte length, or 0xffffffff for NULL
    bytes text-protocol value
u32       trailer marker: 0xfffffffe
u32       status: 0 complete, 1 controlled server error
8 bytes   five-byte SQLSTATE plus three zero bytes; all zero on success
u64       row count
u32       CommandComplete count
32 bytes  SHA-256 of the complete descriptor prefix
```

The mandatory trailer detects truncation at a row boundary. Trailing bytes,
missing trailers, digest mismatches, row-count mismatches, invalid SQLSTATE and
multiple completions are not valid evidence. Error-only responses use an
actual zero-column descriptor; metadata is never fabricated.

V2 supports `int2`, `int4`, `int8`, `text`, `varchar`, `bpchar`, `bool` and
NULL. Integer text is range-checked and canonicalised for cross-engine
comparison. Other supported values are compared as length-prefixed raw UTF-8
text-protocol bytes. Whitespace and `bpchar` padding are not normalised.
Duplicate multiplicity is preserved.

Rows are a multiset by default. `ordered=true` is valid only when the reviewed
subject has a complete `ORDER BY` over a unique key; it makes comparison
position-sensitive.

## Execution witness

The witness is armed for exactly one reviewed client subject. Setup queries,
SHOW readback and nested provider activity cannot consume or overwrite the
record. A monotonically increasing sequence must advance exactly once.

The record contains:

- success or error terminal state;
- `postgres`, `duckdb`, `mixed` or `unknown` engine;
- actual result TupleDesc OID, typmod and collation by ordinal;
- bounded CustomScan presence and completed-execution counters.
- a manifest-derived subject token plus backend PID, database OID and user OID.

The token is SHA-256 over the run id, subject id and SQL digest with NUL
separators. Receipts also bind the exporter and every arm by PID, database,
user and their numeric OIDs. Arm PIDs must be distinct and must differ from the
exporter. This prevents a valid witness or receipt from being relabelled for a
different subject or session. The published record uses a bounded
non-transactional buffer, so readback after rollback is stable.

PostgreSQL is proven only when the initialised plan tree has no CustomScan.
DuckDB is proven by a completed root DuckDBScan execution or completed
instrumented DuckDBScan with no competing provider. An initialised but
unexecuted custom branch, incomplete attribution, a first-call exception or an
ExecutorStart failure is `unknown`. Empty successful DuckDB results still
prove DuckDB execution. A DuckDB exception before a completed loop remains
unknown and therefore cannot produce PASS or ERROR_PARITY.

## Snapshot protocol

The exporter starts `REPEATABLE READ READ ONLY`, exports a snapshot and remains
alive. Each arm starts a separate transaction at the same isolation level and
executes `SET TRANSACTION SNAPSHOT` before its first snapshot-taking statement.
The receipt binds:

- SHA-256 of the export token;
- exporter's `pg_current_snapshot()` value;
- arm `pg_current_snapshot()` value;
- import command completion;
- backend pid, database and user;
- database and user OIDs for both exporter and every arm;
- run, subject and SQL digests.

The integration test commits a change after export. All three imported arms
must retain the old view. A fourth negative-control arm intentionally omits
the import and must observe the committed change. A semantic difference is
FAIL only after matching authorised snapshot provenance is proved; otherwise
it is INCONCLUSIVE.

## Verdicts

Precedence is fail-closed:

1. `INVALID` — malformed schema, framing, digest or replay/binding evidence.
2. `FAIL` — independently proven native/shadow interference, or a proven
   value, multiplicity, NULL, descriptor, collation, success/error or SQLSTATE
   difference under matching snapshot and engine bindings.
3. `INCONCLUSIVE` — unknown engine, declined force, missing import, snapshot
   mismatch, a witness that did not advance, incomplete or partial acquisition,
   timeout, cancellation or unsupported type.
4. `ERROR_PARITY` — every arm has a complete execution binding and the same
   controlled SQLSTATE; it is not PASS.
5. `PASS` — all three successful arms have the expected engines, identical
   snapshot provenance, descriptors, collations and values.

Expected engines are native=`postgres`, forced=`duckdb`, shadow=`postgres`.
Forced=`postgres` is `INCONCLUSIVE/force_declined`; mixed or unknown is also
INCONCLUSIVE. Native or shadow DuckDB execution is FAIL independently of value
comparison.

## Running the dedicated integration

The PostgreSQL installation must already contain the pinned pg_duckdb build.
The script creates and destroys a separate cluster and a private evidence tree:

```sh
bash tests/phase7/run-differential-integration.sh \
  16 /private/new-pq31-result
```

CI runs the same test for PostgreSQL 16, 17, 18 and 19. Generated raw evidence
is private and must not be committed. The committed WP14 differential script
is historical reproducibility material and is not an alternative oracle.
The output path is canonicalised to an absolute path before any evidence is
created. Live cases cover empty and single-row results, NULL duplicates, the
core supported scalar types, controlled execution errors and concurrent
INSERT/UPDATE/DELETE snapshot isolation. The character-typmod case deliberately
expects a descriptor FAIL when pg_duckdb returns metadata different from native
PostgreSQL; detecting that difference is oracle success, not equivalence.

## Non-goals

- production routing or automatic policy activation;
- proving that every useful query should be delegated;
- supporting arbitrary SQL or untrusted SQL input;
- numeric, temporal, domain or binary-protocol canonicalisation;
- customer execution, publication or release approval;
- replacing PostgreSQL isolation or pg_duckdb correctness testing.
