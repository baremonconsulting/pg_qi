# Initial architecture

## Status

This document describes the bootstrap architecture introduced in version `0.0.1`. It is not a complete product specification.

## Current control flow

```text
PostgreSQL planner
        │
        ▼
pg_qi planner hook
        │
        ├── future policy evaluation
        │
        ▼
previous planner hook, when present
        │
        ▼
standard_planner(), when no previous hook exists
```

Version `0.0.1` performs no classification and changes no planner decision. The hook exists only to establish and test the integration skeleton.

## Loading model

The library can be loaded in two ways:

1. During `shared_preload_libraries` processing. The planner hook is installed.
2. Through `CREATE EXTENSION` or a SQL function call. SQL functions and GUC registration are available, but the planner hook is not installed.

Production versions of `pg_qi` will require `shared_preload_libraries`.

## Initial invariants

- Always preserve the downstream planner-hook chain.
- Never assume that `pg_qi` is the only extension using planner hooks.
- Do not implement query routing before the pg_duckdb delegation mechanism is demonstrated by a dedicated prototype.
- Keep PostgreSQL-version-specific code isolated.
- Keep the commercial platform outside this repository and dependent only on deliberate, public and versioned interfaces.
- Do not perform network calls in planner or executor hooks.
- Fail towards normal PostgreSQL execution.

## Planned component boundaries

The intended open-source codebase will be split into focused modules as implementation progresses:

```text
src/
├── pg_qi.c                 module initialisation
├── hooks/                  PostgreSQL hook integration
├── compatibility/          capability and version checks
├── telemetry/              local event contract and transport
├── safety/                 resource and execution safeguards
└── adapters/               execution-engine integration boundaries
```

This layout should be introduced incrementally. Empty directories and speculative APIs are deliberately avoided in the bootstrap commit.
