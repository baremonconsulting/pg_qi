# pg_qi — PostgreSQL Query Intelligence

[![CI](https://github.com/baremonconsulting/pg_qi/actions/workflows/ci.yml/badge.svg)](https://github.com/baremonconsulting/pg_qi/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

`pg_qi` is a PostgreSQL execution-policy extension for safe and explainable query-engine selection, initially targeting [`pg_duckdb`](https://github.com/duckdb/pg_duckdb).

> [!IMPORTANT]
> The project is in **pre-alpha development**. The current code is a bootstrap extension skeleton. It does not route queries and must not be used in production.

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
- a transparent planner-hook skeleton;
- `pg_qi.enabled`, the first extension GUC;
- SQL functions for version and preload diagnostics;
- regression tests;
- CI builds for PostgreSQL 16, 17 and 18.

No query classification, telemetry, `pg_duckdb` delegation or automatic routing is implemented yet.

## Requirements

- PostgreSQL 16, 17 or 18 with server development headers;
- a C compiler and GNU Make;
- PGXS, normally installed with the PostgreSQL server development package.

`pg_duckdb` is not yet a build-time dependency. Its integration will be introduced through a dedicated adapter after the integration mechanism has been validated.

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

Add `pg_qi` to `shared_preload_libraries`:

```conf
shared_preload_libraries = 'pg_qi'
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

The current hook is transparent. It always calls the next planner hook, or PostgreSQL's standard planner when no downstream hook exists.

## Development

Build and run the regression test suite against a running PostgreSQL instance:

```bash
make
sudo make install
make installcheck
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines and [docs/architecture.md](docs/architecture.md) for the initial architecture.

## Editions

There is a single PostgreSQL extension, `pg_qi`, and it is fully open source under Apache License 2.0. The complete single-instance router — including every safety mechanism — is and will remain open source; safety features are never paywalled.

Commercial offerings (the Baremon Query Intelligence Platform for fleet-scale analytics, governance and support, plus certified builds and services) run outside the database and consume `pg_qi` telemetry through its public contract. Certified builds carry the same extension name and identical functionality. See [docs/editions.md](docs/editions.md).

## Security

Do not report security vulnerabilities through public issues. Follow [SECURITY.md](SECURITY.md).

## Licence

Copyright 2026 Baremon s.r.o.

Licensed under the [Apache License 2.0](LICENSE).

`pg_qi`, Baremon and associated marks may be subject to separate trademark rights. The Apache licence does not grant permission to use Baremon trademarks except as required for reasonable attribution.
