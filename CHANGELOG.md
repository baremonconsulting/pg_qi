# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project intends to use semantic versioning once its public interfaces stabilise.

## [Unreleased]

### Added

- Experimental routing for qualified generated analytical reads during
  extended-protocol Bind. It checks eligibility, reviewed result equivalence
  and measured savings for the current typed values, including NULL. Generic
  plans and missing or expired evidence pass through.
- Exact-site `COUNT(*)` semantic qualification for decision-only shadow,
  with typed differential evidence and an independently approved, expiring
  certificate. The routing and recommendation activation boundaries are unchanged.
- Fail-closed plan-lifecycle correlation for simple and prepared execution,
  including cached-plan abstention, DDL replan evidence and SQL/libpq tests.
  SQL `EXECUTE` is now identified as a proven lifecycle context, while its
  allow-list policy remains decision-only and cannot request delegation.
- A decision-only conservative benefit model with checked integer bounds,
  profile format v2 and private benefit evidence.
- A private, bounded, backend-local V2 decision snapshot with atomic
  publication, role-safe `SHOW` read-back, strict readers, and lifecycle tests.
- Private `SHOW pg_qi.stats_confidence` evidence with fail-closed freshness
  reasons and PostgreSQL 16–19 lifecycle coverage.
- Bounded, build-bound model/resource profiles with SIGHUP-time parsing,
  per-decision expiry, atomic backend-local replacement and private evidence.
- A stricter direct-client proof that prevents deferred trigger and referential
  integrity planning during transaction completion from entering V1 or V2.
- Reservation of the `pg_qi` GUC prefix so misspelled or unknown parameters
  fail instead of becoming placeholder settings.
- An inert `shadow_p0` routing mode with exhaustive fail-closed dispatch,
  bounded pre-planning facts and production non-interference regression tests.
- PGQI-4C production-only pilot evidence harness, same-session diagnostics,
  isolated rollback drill and operator cookbook.
- Production request benchmark for the manual workflow; WP11 remains
  historical Spike evidence.
- Outer-hook fixture coverage for restoration after an error raised after an
  owned force request, without production failpoints.

- Initial PGXS extension skeleton.
- Transparent planner-hook chaining.
- `pg_qi.enabled` configuration parameter.
- `qi.version()` and `qi.is_preloaded()` diagnostics.
- Regression-test and CI foundations for PostgreSQL 16–18.
- PostgreSQL 16–18 planner compatibility layer.
- Backend-local delegation frames, provenance tracking and error-safe scoped
  restoration of `duckdb.force_execution`.
- Authoritative plan-tree detection for `DuckDBScan`.
- A backend-local one-shot delegation mechanism for the Spike 1 test harness.
- S0–S11 integration coverage for errors, nested planner re-entry, prepared
  statements, plan invalidation, utility paths and parallel-worker isolation.
- Concurrency smoke testing and a layer-separated benchmark harness.
- A separate `pg_qi_spike_test` extension for test-only SQL interfaces.
- PostgreSQL 19 RC2 source-build coverage in the compile, regression and
  integration matrices.
- A fail-closed Query-tree classifier prototype and a frozen differential
  semantics corpus for the WP14.2 technical gate.
- Current architecture and safety-boundary documentation, plus the normative
  production decision contract for the next routing stage.
- A production-built, backend-local PGQI-4A shadow decision record exposed by
  read-only `pg_qi.shadow_*` settings.
- A conservative shadow eligibility classifier covering security context,
  sampling, inheritance, parameters, grouping, ordering, aggregates, casts
  and functions with deny-by-default outcomes.
- Production-artifact shadow regression jobs for PostgreSQL 16–19 RC2.
- An experimental `pg_qi.routing_mode = allowlist` policy, disabled by
  default and restricted to superusers.
- Production context tracking for executor, utility, nested and outer-hook
  isolation, with authoritative positive DuckDB verification.
- A CI-only planner-hook fixture that verifies nested force masking and later
  outer-hook rejection on every supported PostgreSQL major.

### Changed

- Hardened the CI PGDG repository bootstrap against transient TLS failures
  with bounded retries, exact signing-key fingerprint validation and atomic
  keyring installation.
- Validated the temporary GUC-based `pg_duckdb` adapter on PostgreSQL 16, 17
  and 18 with pinned dependency versions; Spike 1 concluded with verdict A.
- Made the default build production-only. Spike GUCs, failpoints, synthetic
  re-entry, benchmark modes, test SQL functions and backend-exit evidence are
  compiled only when `PG_QI_TESTING=1`.
- CI now compiles both production and test variants, rejects test-surface
  leakage from the production library and runs regression plus S0–S11 matrices
  across all supported PostgreSQL versions.
- The production planner hook can make a scoped delegation request only when
  explicit local policy and every fail-closed gate allow it. Default behaviour
  remains observational pass-through.
- Record estimated DuckDB spill risk for supported fixed-width grouping,
  DISTINCT and sort plans in decision-only shadow mode. Record why the benefit
  model abstains when a memory estimate or spill resources are uncertain.
  Expose each shape's warning threshold as a PostgreSQL setting.

### Fixed

- Preserve the client statement's `pg_qi.shadow_*` evidence when the DuckDB
  heap reader or nested SQL plans another query during execution.
- A DuckDB-executed query no longer crashes the backend when `auto_explain`
  logs its plan. pg_duckdb's EXPLAIN callback re-executed the query outside an
  `EXPLAIN` command, without an active snapshot for simple-protocol queries.
  Preloaded `pg_qi` now hides that callback for DuckDB scans not prepared for
  `EXPLAIN`; such plans still show the `DuckDBScan` node.

### Security

- Removed invasive Spike 1 controls and diagnostics from the default shared
  library and production SQL extension surface.

### Known limitations

- A plan-only `EXPLAIN` of a DuckDB plan can make pg_duckdb fall back to
  PostgreSQL for later queries in the same transaction. Inspect the plan in a
  separate transaction before running the query.
- Routing is pre-alpha, default-off and limited to a narrow static allow-list;
  external policy management and telemetry export are not implemented.
- Runtime preload currently requires `pg_duckdb` to provide a real, resettable
  `duckdb.force_execution` GUC before `pg_qi` is initialised.
