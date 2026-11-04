# pg_qi — PostgreSQL Query Intelligence

[![CI](https://github.com/baremonconsulting/pg_qi/actions/workflows/ci.yml/badge.svg)](https://github.com/baremonconsulting/pg_qi/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

`pg_qi` is a PostgreSQL execution-policy extension for safe and explainable query-engine selection, initially targeting [`pg_duckdb`](https://github.com/duckdb/pg_duckdb).

> [!IMPORTANT]
> The project is in **pre-alpha development**. The production build does not
> yet classify or automatically route queries and must not be used in
> production. Spike 1 has validated the low-level `pg_duckdb` delegation
> mechanism; that result is an integration proof, not a production router.

## Project goals

`pg_qi` is intended to provide a controlled policy layer between PostgreSQL workloads and alternative execution engines. The design prioritises:

- safe and conservative decisions;
- explicit reason codes and operator control;
- measurable overhead;
- predictable fallback behaviour;
- clean integration boundaries;
- no application changes.

## Current state

The repository currently contains:

- a PGXS buildable PostgreSQL extension;
- planner-hook chaining compatible with PostgreSQL 16, 17 and 18;
- backend-local delegation frames with error-safe GUC restoration;
- a validated temporary adapter for requesting `pg_duckdb` planning through
  `duckdb.force_execution`;
- plan-tree detection for `DuckDBScan`;
- `pg_qi.enabled`, the first extension GUC;
- SQL functions for version and preload diagnostics;
- an explicit test-only build containing Spike 1 controls, diagnostics,
  failpoints, re-entry simulation and benchmarks;
- regression and S0–S11 integration tests;
- CI builds and tests for PostgreSQL 16, 17 and 18.

Spike 1 demonstrated deterministic one-shot delegation in its test harness,
including nested planning, errors, cached plans, concurrency and parallel
workers. The default production build deliberately exposes none of those test
controls. Query classification, telemetry, policy management and automatic
routing are not implemented yet. See the
[Spike 1 report](docs/spike1/spike-1-report.md) and
[WP14 production/test boundary](docs/phase2/WP14-production-test-boundary.md).

## Requirements

- PostgreSQL 16, 17 or 18 with server development headers;
- a C compiler and GNU Make;
- PGXS, normally installed with the PostgreSQL server development package;
- `pg_duckdb` for preload and runtime operation of the planner integration.

`pg_duckdb` is not a compile-time dependency. The current runtime adapter
requires a real, resettable `duckdb.force_execution` GUC and therefore requires
`pg_duckdb` to be loaded before `pg_qi`. Spike 1 and CI are pinned to
`pg_duckdb` commit `ee38d3b540ecea1d93683ba99bdcec5632a21eaf` and DuckDB
`v1.5.4`; changes to that dependency require the integration matrix to be
replayed.

## Build

```bash
make
sudo make install
```

To use a specific PostgreSQL installation:

```bash
make PG_CONFIG=/path/to/pg_config
sudo make PG_CONFIG=/path/to/pg_config install
```

## Installation

Add `pg_duckdb` and `pg_qi`, in that order, to
`shared_preload_libraries`:

```conf
shared_preload_libraries = 'pg_duckdb,pg_qi'
```

Restart PostgreSQL, then install the extension in each database where it is required:

```sql
CREATE EXTENSION pg_qi;
```

Verify the installation:

```sql
SELECT qi.version();
SELECT qi.is_preloaded();
SHOW pg_qi.enabled;
```

The current production hook preserves the downstream planner-hook chain and
does not initiate delegation because no production policy source exists yet.
It calls the next planner hook, or PostgreSQL's standard planner when no
downstream hook exists.

## Development

Build and run the regression suite against a PostgreSQL instance that preloads
`pg_duckdb,pg_qi`:

```bash
make PG_QI_TESTING=1 clean
make PG_QI_TESTING=1
sudo make PG_QI_TESTING=1 install
make PG_QI_TESTING=1 installcheck
```

The test build additionally installs `pg_qi_spike_test`. Test databases create
`pg_qi` first and then `pg_qi_spike_test`; this secondary extension is not part
of the default production installation.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines and [docs/architecture.md](docs/architecture.md) for the initial architecture.

## Editions

There is a single production PostgreSQL extension, `pg_qi`, and it is fully open source under Apache License 2.0. When the complete single-instance router is implemented, it will be open source in this repository, including every safety mechanism; safety features will never be paywalled. Most of the router is still planned.

The planned Baremon Query Intelligence Platform will run outside the database and consume `pg_qi` telemetry through a public, versioned contract that is not yet defined. Baremon may also provide certified builds, assessments, support and professional services. Certified builds use the same extension name and provide identical functionality. See [docs/editions.md](docs/editions.md) for the status of each component.

## Security

Do not report security vulnerabilities through public issues. Follow [SECURITY.md](SECURITY.md).

## Licence

Copyright 2026 Baremon s.r.o.

Licensed under the [Apache License 2.0](LICENSE).

`pg_qi`, Baremon and associated marks may be subject to separate trademark rights. The Apache licence does not grant permission to use Baremon trademarks except as required for reasonable attribution.
