# Spike 1 — WP10 concurrency smoke report

Date: 2026-10-16
Result: **PASS**

## Acceptance run

- PostgreSQL 17.11 built with `--enable-cassert`
- pg_duckdb commit `ee38d3b540ecea1d93683ba99bdcec5632a21eaf`
- 8 persistent pgbench clients and 8 worker threads
- 300 seconds, alternating delegated and non-delegated workload
- 40,620 successful transactions; 0 failed
- 135.385595 transactions/s
- no assertion, panic, trap, or segmentation-fault signature

## Backend-local acceptance criteria

Each persistent pgbench backend emitted a machine-readable `PG_QI_FINAL` record
at exit. The parser selected the eight workload backends (those with completed
DuckDB plans) and evaluated them independently.

| PID | pushed | popped | unwound | depth | cleanup_broken | PG plans | DuckDB plans |
|---:|---:|---:|---:|---:|:---:|---:|---:|
| 1627896 | 7609 | 7609 | 0 | 0 | false | 5073 | 2536 |
| 1627898 | 7672 | 7672 | 0 | 0 | false | 5115 | 2557 |
| 1627895 | 7588 | 7588 | 0 | 0 | false | 5059 | 2529 |
| 1627900 | 7598 | 7598 | 0 | 0 | false | 5066 | 2532 |
| 1627894 | 7639 | 7639 | 0 | 0 | false | 5093 | 2546 |
| 1627897 | 7633 | 7633 | 0 | 0 | false | 5089 | 2544 |
| 1627893 | 7541 | 7541 | 0 | 0 | false | 5028 | 2513 |
| 1627899 | 7669 | 7669 | 0 | 0 | false | 5113 | 2556 |

For every row:

- `pushed = popped + unwound`
- `depth = 0`
- `cleanup_broken = false`
- both ordinary PostgreSQL and delegated DuckDB plans completed

## Implementation and evidence

- `src/qi_delegation.c`: read-only final backend report
- `src/pg_qi.c`: one exit callback registered per planning backend
- `scripts/spike1/wp10-workload.sql`: deterministic alternating workload
- `scripts/spike1/wp10-concurrency-smoke.sh`: reproducible assert-build run
- `scripts/spike1/wp10-parse-final.awk`: per-backend acceptance parser
- `artifacts/spike1/wp10/pgbench.log`: pgbench result
- `artifacts/spike1/wp10/server.log`: raw machine-readable final records
- `artifacts/spike1/wp10/final-backends.tsv`: accepted backend rows
- `artifacts/spike1/wp10/summary.txt`: compact verdict

pg_duckdb helper processes inherit the pgbench application name. They are not
persistent pgbench clients and complete no delegated plan, so the parser
excludes them before enforcing the exact count of eight workload backends.

The first instrumentation attempt is preserved in
`artifacts/spike1/wp10-attempt1/`; it completed the full workload with zero
transaction failures but did not emit client-backend final records. It is not
used as acceptance evidence.
