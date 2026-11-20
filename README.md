# pg_qi — PostgreSQL Query Intelligence

[![CI](https://github.com/baremonconsulting/pg_qi/actions/workflows/ci.yml/badge.svg)](https://github.com/baremonconsulting/pg_qi/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

`pg_qi` is a PostgreSQL execution-policy extension for safe and explainable query-engine selection, initially targeting [`pg_duckdb`](https://github.com/duckdb/pg_duckdb).

> [!IMPORTANT]
> The project is in **pre-alpha development** and must not be used for
> production routing. PGQI-4B adds an experimental, superuser-controlled
> `allowlist` mode for a deliberately narrow query subset; it remains `off` by
> default and is intended only for controlled validation.

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
- planner-hook chaining compatible with PostgreSQL 16, 17, 18 and 19 RC2;
- backend-local delegation frames with error-safe GUC restoration;
- a validated temporary adapter for requesting `pg_duckdb` planning through
  `duckdb.force_execution`;
- plan-tree detection for `DuckDBScan`;
- a production-built, backend-local shadow decision record;
- a conservative shadow candidate classifier separate from the WP14
  prototype;
- conservative client-context proof and final planner-boundary verification;
- scoped production routing for the narrow allow-list, disabled by default;
- `pg_qi.enabled` and `pg_qi.routing_mode` operator controls;
- SQL functions for version and preload diagnostics;
- an explicit test-only build containing Spike 1 controls, diagnostics,
  failpoints, re-entry simulation and benchmarks;
- regression and S0–S11 integration tests;
- CI builds and tests for PostgreSQL 16, 17, 18 and 19 RC2.

Spike 1 demonstrated deterministic one-shot delegation in its test harness,
including nested planning, errors, cached plans, concurrency and parallel
workers. The default production build deliberately exposes none of those test
controls. The production hook evaluates candidacy and observes the plan
returned by its downstream planner. When a superuser explicitly selects
`allowlist`, it may request delegation only after context, provenance, state
and classifier gates pass. Telemetry export and external policy management are
not implemented.
The broader WP14.2 classifier remains a test-gate prototype. See the
[Spike 1 report](docs/spike1/spike-1-report.md) and
[WP14 production/test boundary](docs/phase2/WP14-production-test-boundary.md).

## Requirements

- PostgreSQL 16, 17, 18 or 19 RC2 with server development headers;
- a C compiler and GNU Make;
- PGXS, normally installed with the PostgreSQL server development package;
- `pg_duckdb` for preload and runtime operation of the planner integration.

`pg_duckdb` is not a compile-time dependency. The current runtime adapter
requires a real, resettable `duckdb.force_execution` GUC and therefore requires
`pg_duckdb` to be loaded before `pg_qi`. The current target CI matrix pins
`pg_duckdb` commit `8756099e267c47f0b86c24de4cf995327f1f4279`; a separate
PostgreSQL 16–18 baseline matrix retains the original Spike 1 pin
`ee38d3b540ecea1d93683ba99bdcec5632a21eaf` with DuckDB `v1.5.4`.
Dependency changes require the integration matrix to be replayed.

PostgreSQL 19 support is pinned to RC2 commit
`dffcca2e3c81178c64d4c1f44543a3507d0b7508`. The pin will be replayed before
claiming support for the final PostgreSQL 19 release.

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
SHOW pg_qi.routing_mode;
```

The current production hook preserves the downstream planner-hook chain and
calls the next planner hook (or PostgreSQL's standard planner) exactly once.
Routing is disabled by default. A superuser may enable the experimental narrow
allow-list for the current configuration scope:

```sql
SET pg_qi.routing_mode = 'allowlist';
```

The internal `shadow_p0` mode observes bounded decision facts but remains
strictly non-intervening: it neither creates a delegation request nor changes
`duckdb.force_execution`.

```sql
SET pg_qi.routing_mode = 'shadow_p0';
```

The private atomic V2 decision envelope is read with `SHOW
pg_qi.decision_v2`. It is available only to superusers or members of an
administrator-created `NOLOGIN` role named `pgqi_diagnostic`; ordinary roles
receive no diagnostic payload. See
[`docs/phase7/PQ7-3-private-atomic-snapshot.md`](docs/phase7/PQ7-3-private-atomic-snapshot.md)
for lifecycle, pooling, privacy, and correlation rules.

Do not enable this mode for production workloads in the pre-alpha release.

The completed backend-local snapshot is available through read-only
`pg_qi.shadow_*` settings. Use `SHOW`, which does not itself create a plan:

```sql
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_classifier_status;
SHOW pg_qi.shadow_classifier_reason;
SHOW pg_qi.shadow_observed_engine;
SHOW pg_qi.shadow_verification;
```

`proven_client` requires a regular client backend outside executor and utility
processing, an outermost pg_qi invocation and pg_qi ownership of the final
planner-hook boundary. Other contexts do not originate requests. A negative
plan-tree match remains `unknown/unverified`, never verified PostgreSQL.

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines,
[docs/architecture.md](docs/architecture.md) for the current architecture and
the [production decision contract](docs/production-decision-contract.md) and
[PGQI-4B routing boundary](docs/phase4/PGQI-4B-controlled-routing.md).
The bounded pilot gate and operator procedure are documented in
[PGQI-4C pilot readiness](docs/phase4/PGQI-4C-pilot-readiness.md) and the
[pilot operator cookbook](docs/phase4/PGQI-4C-operator-cookbook.md).

## Editions

There is a single production PostgreSQL extension, `pg_qi`, and it is fully open source under Apache License 2.0. When the complete single-instance router is implemented, it will be open source in this repository, including every safety mechanism; safety features will never be paywalled. Most of the router is still planned.

The planned Baremon Query Intelligence Platform will run outside the database and consume `pg_qi` telemetry through a public, versioned contract that is not yet defined. Baremon may also provide certified builds, assessments, support and professional services. Certified builds use the same extension name and provide identical functionality. See [docs/editions.md](docs/editions.md) for the status of each component.

## Security

Do not report security vulnerabilities through public issues. Follow [SECURITY.md](SECURITY.md).

## Licence

Copyright 2026 Baremon s.r.o.

Licensed under the [Apache License 2.0](LICENSE).

`pg_qi`, Baremon and associated marks may be subject to separate trademark rights. The Apache licence does not grant permission to use Baremon trademarks except as required for reasonable attribution.
