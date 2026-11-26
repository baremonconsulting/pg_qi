# Production compatibility CI

The production compatibility job is the required verification gate for the
observer-only `shadow_p0` path
against PostgreSQL 16.16, 17.12, 18.7, and 19.0. Each matrix leg builds the
corresponding PostgreSQL release from a pinned upstream commit and records the
provider, pg_qi, pg_duckdb, DuckDB source, and resolved `libduckdb.so`
identities in a validated manifest.
The installed `postgres`, `pg_duckdb.so`, and `libduckdb.so` hashes must equal
their corresponding pinned-source build outputs; recording unrelated source
and binary identities is rejected.

The job builds pg_qi with `PG_QI_TESTING=0`. It rejects a mismatch between the
built and installed libraries, test-only strings or exported symbols, and
test-only pg_qi files in the PostgreSQL installation. The external hook fixture
is test infrastructure and is not part of the pg_qi production artefact. CI
removes it after the probes and repeats the production-boundary check before
retaining final evidence.

## Non-interference gates

Identical, isolated `off` and `shadow_p0` sessions exercise ordered rows,
aggregates, NULL and duplicate values, custom and generic prepared statements,
plan output, runtime and planning errors, statement cancellation, savepoint
recovery, and PL/pgSQL exception recovery. Ordered results, row counts, plans,
command tags and SQLSTATEs must be byte-identical; the unordered duplicate-row
case is compared as a multiset. A V2 decision snapshot is checked separately
to prove that the shadow observer actually evaluated a statement.

The hook-chain fixture also runs guarded SPI from a downstream `ExecutorRun`
hook. It verifies hook chaining, nested execution, balanced cleanup after
errors and cancellation, and the usability of the next statement.

## Inspect the plan in a separate transaction

On the tested pg_duckdb builds, a plan-only `EXPLAIN` of a DuckDB plan calls
`ExecutorStart` and skips `ExecutorFinish`. The provider's executor nesting
level stays raised until the transaction ends, so later eligible queries fall
back to PostgreSQL even with `duckdb.force_execution` enabled.

Run the plan inspection between `BEGIN` and `ROLLBACK`, then execute the
measured query in a new transaction. For a query that reads a PostgreSQL table
through DuckDB, set `duckdb.log_pg_explain = on` with `SET LOCAL` and capture the
`(PGDuckDB/PostgresTableReader)` notice from that execution. In the same
session, `SHOW pg_qi.decision_v2` with `force=external` and `engine=unknown`
flags an externally forced query with no verified DuckDB scan. Use the
execution evidence to confirm the route.

## Performance gate

The production benchmark records `disabled`, `off`, `shadow_p0`, and
`allowlist` modes after warmup. CI retains 500 raw samples per mode and gates
the `shadow_p0` p50 and p95 against deliberately broad anti-catastrophe bounds
relative to `off`. The p99 value is retained as evidence but is not an
acceptance threshold on a shared runner. These measurements are engineering
guards, not product performance claims. The default bound is
`shadow_p0 <= 4 × off + 200 microseconds` for both gated percentiles. Raw
samples are parsed independently and must reproduce the recorded summary.
The current probe is intentionally an `unsupported_shape` query, so this is a
planner-path anti-catastrophe guard; it is not a bound for the full bounded-P0
catalogue/statistics evaluation path.

The production matrix rejects any production scenario recorded as `NOT_RUN`; a local exploratory
run may retain that state only when the caller has not enabled the strict
`PG_QI_REJECT_NOT_RUN=1` gate.

Run the local checks only against a disposable PostgreSQL test installation.
The probes create and drop database objects and some install a test fixture:

```sh
# The root must contain versioned trees such as
# /path/to/disposable/postgresql/17/bin/{postgres,pg_config,psql}.
export PG_INSTALL_ROOT=/path/to/disposable/postgresql
export PG_QI_BUILD_ID=$(git rev-parse HEAD)
export PG_QI_POSTGRES_BUILD_ID=$(
  PG_CONFIG="$PG_INSTALL_ROOT/17/bin/pg_config" \
  bash .github/scripts/ci-postgres-build-id.sh 17
)
make PG_CONFIG="$PG_INSTALL_ROOT/17/bin/pg_config" PG_QI_TESTING=0 install
make -C tests/outer_hook \
  PG_CONFIG="$PG_INSTALL_ROOT/17/bin/pg_config" clean all install
export PGHOST=/path/to/private/socket
export PGUSER=$(id -un)
# Start that cluster with shared_preload_libraries='pg_duckdb,pg_qi_outer_test,pg_qi'.
bash tests/phase7/verify_noninterference.sh 17 /tmp/pgqi-noninterference
bash tests/outer_hook/verify_runtime.sh 17 /tmp/pgqi-runtime preloaded
bash scripts/phase4c/run-benchmark.sh 17 50 500 /tmp/pgqi-benchmark
python3 -m unittest tests.phase7.test_pq42_ci
```

Keep both build-identity variables exported: the benchmark deliberately
rebuilds `pg_qi` and refuses to continue if that output differs from the
installed production library. The exact source-output/provider manifest is a
hosted CI artefact; a local run without the retained PostgreSQL and pg_duckdb
source build trees is exploratory evidence, not a substitute for that gate.
