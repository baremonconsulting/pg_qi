# PGQI-2 — PostgreSQL 19 RC2 compatibility replay

Date opened: 2026-11-16  
Scope: PostgreSQL 19 pin update and full compatibility replay  
Status: **COMPLETE — PASS**

## Purpose

Replay the existing WP14.2 gate against PostgreSQL 19 RC2 without changing
the pg_qi product scope, classifier allow-list, routing behaviour or public SQL
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
- no source-code, SQL API, classifier or routing behaviour change.

## Acceptance

The replay passes only when the exact branch commit has a complete successful
GitHub Actions run and every required job is successful. A cancelled, skipped
or non-terminal required job is not a pass. PostgreSQL 19 GA remains a separate
future replay.

## Result

Hosted workflow run `43858550453` tested implementation commit
`56b066e8d4c6dbb9268e39dc598650e7c16ec9c2`.

- PostgreSQL 19 RC2 production and test compilation with `-Werror`: PASS;
- PostgreSQL 19 RC2 regression: PASS;
- PostgreSQL 19 RC2 S0–S11 integration: PASS;
- PostgreSQL 19 RC2 differential semantics and known-issue exclusions: PASS;
- PostgreSQL 16–18 target and baseline jobs: PASS;
- PostgreSQL 17 assertion-enabled concurrency smoke: PASS;
- final workflow result after isolated retry: 20/20 jobs PASS.

The initial attempt had one infrastructure failure in `S0-S11 integration
PG16`: downloading the PGDG signing key ended with an OpenSSL unexpected EOF
before build or test execution. The isolated retry completed installation,
build, S0–S11 and evidence upload successfully. This was not a PostgreSQL 19
RC2 or pg_qi failure.

## Verdict

**PASS.** PostgreSQL 19 RC2 introduces no observed compatibility, regression,
delegation, differential-semantics or concurrency blocker within the existing
gate. This verdict remains pinned to the component identities above and does
not claim support for PostgreSQL 19 GA.
