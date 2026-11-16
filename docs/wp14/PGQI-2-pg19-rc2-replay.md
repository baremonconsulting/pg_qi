# PGQI-2 — PostgreSQL 19 RC2 compatibility replay

Date opened: 2026-11-16  
Scope: PostgreSQL 19 pin update and full compatibility replay  
Status: **IN PROGRESS**

## Purpose

Replay the existing WP14.2 gate against PostgreSQL 19 RC2 without changing
the pg_qi product scope, classifier allow-list, routing behavior or public SQL
surface.

## Immutable identities

| Component | Identity |
|---|---|
| pg_qi baseline | `6560691628d194deebdac735d303149d1ca6e200` |
| PostgreSQL 19 RC2 | `dffcca2e3c81178c64d4c1f44543a3507d0b7508` (`REL_19_RC2`) |
| pg_duckdb target | `8756099e267c47f0b86c24de4cf995327f1f4279` |
| DuckDB target | `d54cbbe3df17e8e5ce68312f483b4155836112aa` |

## Required checks

- production and test builds with `-Werror`;
- regression suite;
- S0–S11 integration matrix;
- differential semantics corpus and known-issue exclusions;
- PostgreSQL 17 assertion-enabled concurrency smoke remains green;
- PostgreSQL 16–18 target and baseline jobs remain green;
- no source-code, SQL API, classifier or routing behavior change.

## Acceptance

The replay passes only when the exact branch commit has a complete successful
GitHub Actions run and every required job is successful. A cancelled, skipped
or non-terminal required job is not a pass. PostgreSQL 19 GA remains a separate
future replay.

## Result

Pending hosted evidence.
