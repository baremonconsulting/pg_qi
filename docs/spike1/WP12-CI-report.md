# Spike 1 — WP12 CI report

Date: 2026-10-16
Result: **PASS for implementation and local validation**
Hosted GitHub Actions status: **not run (no commit/push authorised)**

## Implemented CI topology

| Job | Matrix / trigger | Purpose | PR blocking |
|---|---|---|---|
| `compile` | PG 16/17/18 | PGXS compile with repository `-Werror` | yes |
| `regression` | PG 16/17/18 | pinned pg_duckdb, preload, `make installcheck` | yes |
| `integration` | PG 16/17/18 | complete automated S0–S11 matrix | yes |
| `reentrancy-focus` | PG17, three repetitions | protected ERROR paths, failpoints and S6 variants | yes |
| `parallel-isolation` | PG17 | S11 plus explicit parallel-worker evidence assertion | yes |
| `benchmark` | PG 16/17/18, manual input only | WP11 performance matrix | no (`continue-on-error`) |

The workflow pins pg_duckdb to
`ee38d3b540ecea1d93683ba99bdcec5632a21eaf` and pins the downloaded DuckDB
archive by SHA-256. No job follows moving pg_duckdb `main`.

## Diagnostics and artifacts

Every job uploads bounded evidence with `if: always()`:

- compile, install, and cleanup logs;
- `regression.diffs`, `regression.out`, and PostgreSQL server log;
- S0–S11 result table and scenario logs;
- repeated reentrancy/failpoint logs;
- S11 log with explicit parallel worker rows;
- benchmark CSV, server logs, latency files, and plan-cache metadata.

Database clusters and WAL are intentionally excluded from uploaded artifacts.
This preserves the evidence required for diagnosis without uploading large,
reconstructible binary state.

## Supporting scripts

- `.github/scripts/ci-install-postgresql.sh`: PGDG setup and build dependencies;
- `.github/scripts/ci-build-pg-duckdb.sh`: pinned pg_duckdb/DuckDB build;
- `.github/scripts/ci-build-install-pg-qi.sh`: logged pg_qi build/install;
- `scripts/spike1/wp9-matrix.sh`: supports one CI-selected major and optional
  reuse of already installed binaries;
- `scripts/spike1/wp11-benchmark.sh`: supports one CI-selected major and
  optional reuse of installed binaries.

The defaults of WP9 and WP11 remain unchanged: local runs still cover PG
16/17/18 and still build/install unless explicitly told not to.

## Local validation evidence

- workflow YAML parsed successfully;
- GitHub Actions workflow passed actionlint 1.7.12;
- all new and modified shell scripts passed `bash -n`;
- pg_qi compiled with `-Werror` against local PG16, PG17, and PG18 headers;
- single-version WP9 CI mode on PG17: 16/16 scenario rows PASS;
- single-version WP11 manual-job mode on PG17: 12/12 runs PASS;
- `git diff --check`: PASS.

## Remaining external verification

The hosted jobs require the workflow to exist in a GitHub commit. They have not
been executed because this work package does not commit or push changes. After
review, the next authorised step is to commit the cumulative Spike 1 branch,
push it, and observe the first Actions run. A hosted failure is not to be hidden:
the uploaded logs are the required input for a corrective WP12 iteration.
