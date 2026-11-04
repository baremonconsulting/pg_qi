# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project intends to use semantic versioning once its public interfaces stabilise.

## [Unreleased]

### Added

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
- PostgreSQL 19 RC1 source-build coverage in the compile, regression and
  integration matrices.
- A fail-closed Query-tree classifier prototype and a frozen differential
  semantics corpus for the WP14.2 technical gate.

### Changed

- Validated the temporary GUC-based `pg_duckdb` adapter on PostgreSQL 16, 17
  and 18 with pinned dependency versions; Spike 1 concluded with verdict A.
- Made the default build production-only. Spike GUCs, failpoints, synthetic
  re-entry, benchmark modes, test SQL functions and backend-exit evidence are
  compiled only when `PG_QI_TESTING=1`.
- CI now compiles both production and test variants, rejects test-surface
  leakage from the production library and runs regression plus S0–S11 matrices
  across all supported PostgreSQL versions.

### Security

- Removed invasive Spike 1 controls and diagnostics from the default shared
  library and production SQL extension surface.

### Known limitations

- The production build does not yet implement classification, telemetry,
  policy management or automatic routing.
- Runtime preload currently requires `pg_duckdb` to provide a real, resettable
  `duckdb.force_execution` GUC before `pg_qi` is initialised.
