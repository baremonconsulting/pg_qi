# PQ-58 — pg_duckdb 1.2.2 compatibility replay

## Purpose

Qualify pg_duckdb 1.2.2 and its embedded DuckDB 2.0.1 on PostgreSQL 16–19.
The replay covers delegation, result equivalence, production routing and
build-bound Phase 7 evidence for [PQ-58](https://baremon.atlassian.net/browse/PQ-58).

## Immutable identities

| Component | Source commit | Version |
| --- | --- | --- |
| PostgreSQL 16 | `c0534eb02de050ac1035ad7b3ad7c3f02980f153` | 16.16 |
| PostgreSQL 17 | `702b3dc86d35bcc08c8e23cffa3c4d9fe9d32c85` | 17.12 |
| PostgreSQL 18 | `f5c41f6681c910b3f69951bf6fb97c6d74605d25` | 18.7 |
| PostgreSQL 19 | `5c839a2a08b9b87a349be97371fac6c5a2873b10` | 19.0 |
| Target pg_duckdb | `4e921ffbe4a16b3caec6cf24ca0151fa9d88032d` | [v1.2.2](https://github.com/duckdb/pg_duckdb/releases/tag/v1.2.2) |
| Target DuckDB submodule | `8d78114e90a53823803660bf821f6073ac87da8c` | [v2.0.1](https://github.com/duckdb/duckdb/releases/tag/v2.0.1) |
| Previous pg_duckdb | `8756099e267c47f0b86c24de4cf995327f1f4279` | v1.2.0 |
| Previous DuckDB submodule | `d54cbbe3df17e8e5ce68312f483b4155836112aa` | v2.0.0 |
| Spike 1 baseline pg_duckdb | `ee38d3b540ecea1d93683ba99bdcec5632a21eaf` | Historical pin |
| Spike 1 baseline DuckDB | `08e34c447bae34eaee3723cac61f2878b6bdf787` | v1.5.4 |

The Spike 1 jobs use the DuckDB v1.5.4 Linux amd64 archive with SHA-256
`838d98a85e697bab9935010c88a8c67d3312ccedcab4cb4a0ba01da65113bb70`.
The target provider is built from source. Its DuckDB gitlink resolves to
the v2.0.1 commit in the table.

## Required checks

The 24 required checks cover four production and test compilations with
`-Werror`, four regressions, four target integration jobs, three historical
integration jobs, two frozen differential and known-issue jobs, four production
routing jobs, and the PostgreSQL 17 re-entrancy, concurrency and parallel-worker
checks. The full workflow also runs the Phase 5 protocol and memory spike.
Each production routing job includes PGQI-4C smoke, rollback and overhead
checks. The manual benchmark is a separate workflow.

## Acceptance

- Pass every required check and complete the manual benchmark.
- Pass all 64 target S0–S11 rows and all 48 Spike 1 rows.
- Report zero differential divergences and keep all five upstream patterns
  ineligible. Record each reproduction result from the frozen probe.
- Validate the provider manifests against the target source commits and
  confirm the production replay on all four PostgreSQL versions.
- Check that records carrying the previous provider identities fail closed.
  Issue replacement evidence for the new provider and record its approval
  scope.
- Record the disk-spill result and update the operational documentation.

## Results

### Provider integration

pg_duckdb 1.2.2 can call the planner hook again to prepare a PostgreSQL
table scan while a DuckDB query is executing. The first PostgreSQL 16.16
replay showed `DuckDBScan` and `DuckDB Execution Plan` in `EXPLAIN`,
then a `PGDuckDB/PostgresTableReader` execution notice and the correct
count. The internal scan overwrote the last-plan diagnostics in the test
build. The correction keeps its client-plan diagnostics through
executor-internal planning and continues to count each planner call.
The existing regression and all 16 S0–S11 rows passed in the subsequent
local replay, as did 115 frozen differential cases.

Changing both entries in `deps/pg_duckdb.env` in a disposable source
checkout rebuilt the profile, count-certificate and semantic objects on
PostgreSQL 16.16. The three rebuilt objects contained both replacement
pins. A further incremental build restored both target pins in all three.

### Hosted replay

The hosted workflow tested pull request head
`a090a287d5fc6cccf2a6818b87f8db232103f074` through merge ref
`20098ee414744aec31175a3e2a0aeff06595eff4`. All 25 jobs passed, including
the 24 required checks. [CI replay](https://github.com/baremonconsulting/pg_qi/actions/runs/45983539402)

- Target S0–S11 passed 64/64 rows across PostgreSQL 16.16, 17.12, 18.7
  and 19.0. The independent Spike 1 jobs passed 48/48 on PostgreSQL 16–18.
- The frozen differential corpus passed all 115 cases on PostgreSQL
  16.16 and all 115 on 19.0, with zero divergences.
- Production routing, the typed PQ-31 integration and the PGQI-4C smoke,
  rollback and overhead checks passed on all four versions. The provider
  manifests validated the exact source commits and matched the binary
  hashes observed in the running postmasters.
- The PostgreSQL 17 assertion-enabled concurrency run, re-entrancy and
  failpoint checks, and parallel-worker isolation all passed.

The frozen known-issue probe compares native and armed output, including
errors. All five patterns were ineligible on both versions.

| Upstream issue | Classifier | PostgreSQL 16.16 | PostgreSQL 19.0 |
| --- | --- | --- | --- |
| #1077 | Ineligible | `NOT_REPRODUCED` | `NOT_REPRODUCED` |
| #1078 | Ineligible | `NOT_REPRODUCED` | `NOT_REPRODUCED` |
| #1069 | Ineligible | `REPRODUCED` | `REPRODUCED` |
| #1098 | Ineligible | `REPRODUCED` | `REPRODUCED` |
| #1096 | Ineligible | `REPRODUCED` | `REPRODUCED` |

### Manual benchmark

The manual PGQI-4C benchmark passed four out of four jobs on the exact
PostgreSQL versions and target provider commits above. It checked 250
samples per mode after 25 warm-up iterations and passed the p50 and p95
overhead checks on each version. The `benchmark-pg16` to `benchmark-pg19`
artefacts record the raw samples, results and provider identities.
[Manual benchmark](https://github.com/baremonconsulting/pg_qi/actions/runs/45981854924)

The benchmark used pg_qi commit
`f55aac97fbd426772b94d81092e052f72a4e2ed2`. The following compatibility
change applies to the test build's diagnostics.

### Disk spill

The PQ-53 failure reproduced on PostgreSQL 16.16 using the stock
pg_duckdb 1.2.2 Linux x86-64 release archive. Its SHA-256 was
`df3cf472de130985a4ce6dce9399f94c27c159e6bf4b93ab583e0cb9a49badc8`.
The synthetic PostgreSQL heap held 180,000 rows with an integer key and
256-byte text. A window query ordered by the text and key and counted
the non-null `lag` values. Each case ran in a separate repeatable-read,
read-only transaction. The DuckDB cases used one thread, a 256 MB
temporary-directory limit and `duckdb.force_execution = on`.

| Server file privileges | Engine | DuckDB memory | Result | Observed temporary bytes |
| --- | --- | ---: | --- | ---: |
| Neither role | PostgreSQL | 64 MiB | 179,999 | 0 |
| Neither role | DuckDB | 64 MiB | LocalFileSystem denied | 0 |
| Read role only | DuckDB | 64 MiB | LocalFileSystem denied | 0 |
| Write role only | DuckDB | 64 MiB | LocalFileSystem denied | 0 |
| Both roles | DuckDB | 64 MiB | 179,999 | 43,417,600 |
| Neither role | DuckDB | 256 MiB | 179,999 | 0 |

The forced cases reached `PGDuckDB/PostgresTableReader`. The error was
`File system LocalFileSystem has been disabled by configuration`. The
source still checks membership of both server file roles. The release
default for `duckdb.allow_unredacted_secrets` was `off` throughout the
trial. [PQ-53](https://baremon.atlassian.net/browse/PQ-53),
[upstream issue #864](https://github.com/duckdb/pg_duckdb/issues/864),
[pg_duckdb 1.2.2 permissions](https://github.com/duckdb/pg_duckdb/blob/v1.2.2/src/pg/permissions.cpp#L11-L15)

### Phase 7 evidence and approvals

In the local PostgreSQL 16.16 replay, source-built pg_duckdb 1.2.2 loaded
the separate DuckDB 2.0.1 shared library. The COUNT matrix passed nine
equivalence cases and the forced-refusal control. The issuer prepared an
isolated `count_star_serial_rr_ro` record for the exact local binaries.
The runtime accepted the matching record in that cluster. It rejected
records carrying the previous provider pins and returned to unknown on
withdrawal. The raw local receipts and prepared record are held in the
private replay directory.

The local immutable-profile check accepted the generated fixture profiles
for the new pins. A profile with the previous pg_duckdb and DuckDB source
commits returned `wrong_provider_build` with `present=false`. The separate
source-built provider replay also loaded fresh fixture generations 11 and
12. The private replay directory holds both generated profiles and their
runtime read-back.

Each hosted production job generated fixture profiles and prepared a
COUNT(*) certificate for its own build and disposable cluster. The
runtime checks accepted matching records and refused the previous
provider pins on all four versions. The `production-shadow-pg*`
artefacts hold each COUNT matrix, runtime results and evidence index.
[Hosted Phase 7 evidence](https://github.com/baremonconsulting/pg_qi/actions/runs/45983539402)

| PostgreSQL | COUNT evidence SHA-256 for merge ref `20098ee` |
| --- | --- |
| 16.16 | `aa4b5e3c0e2379e3af857211243e1b5aec3c534d213d3072d30a83207a3f4e53` |
| 17.12 | `800115d20454e4361aaa52f0c4dc390bd4bc456c6be3085bdd7af7b39693d921` |
| 18.7 | `1aae6d0f64c2694409867906c825ed9058f436b5da887d5787e52c0de9fb20f3` |
| 19.0 | `bf86792e13fc0829253a39277b9ba0b785e23565c412ea6087e2d659347f3962` |

The deployment owner approved the replacement COUNT(*) certificates and
model profiles for the four isolated builds in the final CI replay of
PR revision `399f0fffdee041b6bfee31b2b6e2c744e7fae436`. The approval
covers the cluster and build identities attested by CI merge ref
`eb3d36da21502bd14c274195e4276ffafe5e88ba`.
[Approval in PQ-58](https://baremon.atlassian.net/browse/PQ-58?focusedCommentId=10728),
[approved replay](https://github.com/baremonconsulting/pg_qi/actions/runs/45988165722)

| PostgreSQL | Approved COUNT evidence SHA-256 |
| --- | --- |
| 16.16 | `d1ce73946d6471d923fe2647ea5476ca1c94093bb29a677c127d092104579f13` |
| 17.12 | `f87fc332e010e6a08205717822c1abd7a6dfa9514697ba62815bcd6fd420d5e9` |
| 18.7 | `8b301da19888c07dd30ebeb10d148f87d834ff65ce35c37cc4472051cc49f668` |
| 19.0 | `cfe8f5ff875a73997f9e1ab9ba4d1988db7089faa671128b8f7fd7f3e97c3c2f` |

Each deployment needs fresh evidence and approval for its exact site
and build under the Phase 7 procedures.
[COUNT certificate procedure](../phase7/PQ7-7-count-semantic-certificate.md),
[immutable profiles](../phase7/PQ7-10-immutable-profiles.md)

## Verdict

**Compatibility replay passed.**

If reversal is needed, revert the pull request and redeploy the preceding
build with the evidence for its exact identity. The provider upgrade leaves
the extension catalogue intact.
