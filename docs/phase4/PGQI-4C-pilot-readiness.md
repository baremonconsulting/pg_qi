# PGQI-4C: bounded pilot readiness

## Purpose

PGQI-4C produces reproducible evidence for a controlled customer pilot of the
PGQI-4B allow-list. It does not certify pg_qi for unrestricted production use,
change the default-off policy or expand the accepted SQL subset.

The gate applies to PostgreSQL 16, 17, 18 and the pinned PostgreSQL 19 build.
The PostgreSQL 19 result describes that pin; it is not a claim about an
unexamined final build.

## Work packages and acceptance

| ID | Evidence | Hard acceptance condition |
|---|---|---|
| C1 | Per-run provenance | Exact source revision and dirty state, server build, installed pg_qi and pg_duckdb hashes, build mode, corpus hash and unique run ID are recorded. A new server starts after production installation. |
| C2 | Production smoke and benchmark | A supported literal query returns the expected rows and publishes `delegation_verified`, `request_applied=on`, `duckdb`, `verified`. Off, disabled, and `shadow_p0` controls never publish an owned request. Raw samples are retained; the production matrix applies only a broad p50/p95 anti-catastrophe threshold to `shadow_p0`, not a product-performance gate. |
| C3 | Security and lifecycle tests | Ordinary-role controls, conservative unsupported cases, owned-force restoration, nesting and error propagation pass using the production extension and the separate outer-hook fixture. |
| C4 | Same-session diagnostics | Each post-query snapshot is collected with individual `SHOW` commands on the subject connection. Diagnostics are outside measured intervals. Errors and cached reuse are documented as retaining the last completed snapshot. |
| C5 | Rollback drill | Only a newly created marker-owned cluster is touched. The drill proves future-planning disablement and a default-off reconnect. Cached plans, external force and binary rollback are not misrepresented. |
| C6 | PostgreSQL matrix | Deterministic production smoke and rollback pass for all four pinned majors. Evidence uploads even after failure; a missing terminal status is incomplete. |
| C7 | Closeout | Every claim maps to an artefact or is marked not run. Limitations and Human Review are explicit and documentation is consistent. |

## Evidence rules

- `PG_QI_TESTING=0` is explicit during clean, build and install.
- A production library containing a Spike/test-only symbol is rejected.
- Each run uses a new private Unix-socket cluster with TCP disabled.
- Expected dependency pins and observed installed identities are different
  facts. An expected value is never substituted for an unavailable observed
  value.
- A disk hash alone is not proof of a module mapped by an already-running
  server. The harness starts a fresh server after verifying the installed
  path and records this provenance boundary.
- Result correctness is a hard gate. The PQ-42 production matrix also applies
  deliberately broad p50/p95 anti-catastrophe bounds; absolute latency and p99
  remain evidence rather than product performance claims.
- Failed and partial runs remain evidence. Missing files or a missing `PASS`
  terminal state cannot be interpreted as success.

## Scenario boundary

The positive corpus is intentionally narrow: a single permanent table,
integer projection and literal integer filter already admitted by PGQI-4B.
The controls cover policy off, extension disabled, extended-protocol
parameters, delegated prepared-plan reuse/invalidation semantics, planning-error
snapshot retention and reconnect rollback. Existing production regressions
remain authoritative for the complete classifier rejection families.

The harness adds no production counters, failpoints, SQL functions, network
calls, query-text retention or automatic fleet actions.

The production corpus explicitly covers joins, casts, functions, temporary,
unlogged and partitioned relations, an ordinary-role RLS query and denial of
ordinary-role routing control. The foreign-table case is `NOT_RUN`, never
silently passed, when the PostgreSQL installation does not provide
`file_fdw`; acceptance evidence requires it to run in the CI matrix. Poisoned
state is not deliberately induced through a production failpoint: test-build
coverage establishes that invariant, while the pilot procedure treats any
poisoned backend as disposable and requires reconnect.

## Stop conditions

Stop the pilot or mark evidence incomplete on any wrong result, unexpected
owned request, force leakage, provenance mismatch, missing scenario, server
crash, incomplete cleanup, unsupported dependency identity or documentation
contradiction. A poisoned backend must be disconnected; it is not repaired in
place.

Completion of automated evidence leaves the branch **Awaiting Human Review**.
It does not authorise merge, deployment or persistent allow-list enablement.
