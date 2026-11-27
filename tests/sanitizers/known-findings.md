# Known sanitiser findings

## PostgreSQL indirect calls under UBSan

PostgreSQL 18.7 at `f5c41f6681c910b3f69951bf6fb97c6d74605d25` aborts
during `initdb` when built with Ubuntu Clang 18.1.3 and the UBSan function
check enabled. The bootstrap reaches `hash_search_with_hash_value` in
`src/backend/utils/hash/dynahash.c` through the time-zone setup. UBSan
reports that the call to `string_compare` uses an incompatible function
pointer type. The locally verified original binary had ELF Build ID
`51f59bf946b4c1712939ba44988b987a6a7c124e`.

To reproduce, build that PostgreSQL revision with the undefined-mode flags
from the CI source builder and omit `-fno-sanitize=function`. Run its
`initdb` against a new disposable data directory. The report occurs during
PostgreSQL bootstrap before either extension is loaded.

The buildfarm reported the same failure. The follow-up found more callback
type mismatches and verified the compiler option used here.
[PostgreSQL buildfarm report](https://www.postgresql.org/message-id/6m45hib2ne5yiuxqopaa7y72e2arnc3kvc2mb6wjqpgtfpdo7y%40fcjinyz7gnt3)
· [Upstream follow-up](https://www.postgresql.org/message-id/x2bbwu5hkqie4s7iyi5k7qiipwi2cpkhe6zquwmalt2ptukywf%40sbtmpbfo6zxg)

The undefined build uses `-fno-sanitize=function` as a temporary exception.
PGXS passes the flag to pg_duckdb and pg_qi in that job. The job treats all
other enabled checks as fatal. Remove the exception once a later PostgreSQL
pin passes bootstrap and the instrumented suite with function checking enabled.

## pg_duckdb scan query text under ASan

Finding ID: `pg-duckdb-scan-query-text`.

pg_duckdb 1.2.2 at `4e921ffbe4a16b3caec6cf24ca0151fa9d88032d` leaves the
query text of its nested PostgreSQL table scan dangling. In
`src/scan/postgres_scan.cpp`, the `PostgresScanGlobalState` constructor calls
`Init(scan_query.str().c_str(), count_tuples_only)`. The temporary string is
destroyed at the end of that statement, but `CreateQueryDesc` keeps the
pointer as the scan's `sourceText`. A hook that reads the text when the scan
ends reads freed memory. The line is unchanged on pg_duckdb `main` as of
27 November 2026.

AddressSanitizer reports a heap use after free when `auto_explain` logs that
scan. `EXPLAIN ANALYZE` runs the DuckDB query from `ExplainNode` after
`ExecutorRun` has returned, so `auto_explain` treats the nested scan as a
top-level statement. Its `ExecutorEnd` hook then passes the freed text to
`escape_json`. Ordinary queries run the scan inside `ExecutorRun`, where
`auto_explain` does not log nested statements by default. pg_qi reads the
text only in `ExecutorStart`, while the string still exists.

The CI run used PostgreSQL 18.7 at `f5c41f6681c910b3f69951bf6fb97c6d74605d25`,
DuckDB at `8d78114e90a53823803660bf821f6073ac87da8c` and pg_duckdb, all built
with AddressSanitizer by Ubuntu Clang 18.1.3 on runner image
`ubuntu24 20261122.23.1`.
[CI job](https://github.com/baremonconsulting/pg_qi/actions/runs/46044383352/job/140659844483)

It reproduces with only `pg_duckdb` preloaded. Run this in one session
against that build:

```sql
CREATE EXTENSION pg_duckdb;
CREATE TABLE t (grp int, buyer int);
INSERT INTO t VALUES (1, 1), (1, 1), (1, 2), (2, 3);
LOAD 'auto_explain';
SET auto_explain.log_min_duration = 0;
SET auto_explain.log_format = json;
SET duckdb.force_execution = true;
EXPLAIN (ANALYZE, FORMAT JSON) SELECT grp, count(DISTINCT buyer) FROM t GROUP BY grp;
```

The same `EXPLAIN ANALYZE` without `auto_explain` produces no report. In
pg_qi, the `explain_analyze` case of `tests/explain_guard/verify.sh` reaches
the same read.

Upstream issue: not reported yet.

The address job sets `PG_QI_KNOWN_FINDINGS=pg-duckdb-scan-query-text`. It skips
only the `explain_analyze` case of `tests/explain_guard/verify.sh`. The
undefined job and the ordinary CI still run that case. Remove the variable
once the pinned pg_duckdb keeps the scan query text alive for the whole scan.
