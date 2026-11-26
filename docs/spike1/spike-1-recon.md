# Spike 1 source review

14 October 2026

The source establishes the planner API boundary at PostgreSQL 19 and the
pg_duckdb hooks and settings that Spike 1 relies on. The runtime checks
from S0 onwards verify how these pieces work together.

## Source revisions

| Repository | Branch or revision | Commit |
|---|---|---|
| `baremonconsulting/pg_qi` | Starting revision | `d3529d4e6a7cd71657bbf8279c533a3c5a81e2df` |
| `duckdb/pg_duckdb` | Support INSERT ... INTO ... SELECT ... for Postgres table (#688) | `ee38d3b540ecea1d93683ba99bdcec5632a21eaf` |
| PostgreSQL | `REL_16_STABLE` | `333d566c4f51f1b8b002175032586423c581d3d6` |
| PostgreSQL | `REL_17_STABLE` | `9e8f4ce286bf03306fc0ad29374095782b2a8136` |
| PostgreSQL | `REL_18_STABLE` | `70ffc30e12b1de43d6160682fc44c78b8de00f24` |
| PostgreSQL | `master` | `2246c04e7b99519010cd47be636273a5700b0b0d` |

The commits identify the exact source reviewed. Record any different minor
release used in a later build matrix.

## G1 — Planner API

**G1.1.** PostgreSQL 16, 17 and 18 use four arguments for
`planner_hook_type` and `standard_planner`.

```text
Query *parse,
const char *query_string,
int cursorOptions,
ParamListInfo boundParams
```

See `src/include/optimizer/planner.h` in the three stable branches.

**G1.2.** At the recorded PostgreSQL `master` revision, the same header adds a
fifth argument, `ExplainState *es`.

**G1.3.** The pinned pg_duckdb source selects the new signature with this
version check.

```c
#if PG_VERSION_NUM >= 190000
```

See `src/pgduckdb_hooks.cpp:257-310`.

Use the same version check in pg_qi and use the four-argument signature on
PostgreSQL 16–18. The compile matrix verifies the boundary found in the source.

## G2 — pg_duckdb integration

### G2.1 — Force setting

`duckdb.force_execution` is a Boolean GUC registered as `PGC_USERSET` with
the `GUC_REPORT` flag. See `src/pgduckdb_guc.cpp:147-154`.

### G2.2 — Execution inside functions

`duckdb.unsafe_allow_execution_inside_functions` is a Boolean `PGC_SUSET`
setting. When it is false and `executor_nest_level > 0`,
`IsAllowedStatement()` declines DuckDB execution. See
`src/pgduckdb_guc.cpp:153-154` and `src/pgduckdb_hooks.cpp:237-240`.

### G2.3 — A FROM clause is required to force execution

The force branch returns true when `duckdb_force_execution` is true,
the statement is allowed and `ContainsFromClause(query)` is true. See
`src/pgduckdb_hooks.cpp:116-200`.

Use a query such as `SELECT count(*) FROM qi_bench_t` for the delegation test.
A bare `SELECT 1` lacks the required `FROM` clause.

### G2.4 — When pg_duckdb reads the force setting

The pinned source references `duckdb_force_execution` in the GUC declaration
and registration, the planner's `ShouldTryToUseDuckdbExecution()` check and
the `COPY` utility path. The executor hooks use `IsDuckdbPlan()` to identify
the completed plan. See `src/pgduckdb_hooks.cpp:178-200`,
`src/utility/copy.cpp:377` and `src/pgduckdb_hooks.cpp:313-337`.

### G2.5 — Fallback

The force branch calls `DuckdbPlanNode(..., false)` and calls the previous
planner hook if pg_duckdb produces no plan. A statement that requires DuckDB
calls `DuckdbPlanNode(..., true)`. See `src/pgduckdb_hooks.cpp:264-297`.

### G2.6 — Planner hook order

pg_duckdb installs its planner hook as follows.

```c
prev_planner_hook = planner_hook ? planner_hook : standard_planner;
planner_hook = DuckdbPlannerHook;
```

See `src/pgduckdb_hooks.cpp:506-510`. Set
`shared_preload_libraries = 'pg_duckdb,pg_qi'` so pg_qi loads after pg_duckdb
and saves its hook as the downstream planner. S0 tests this order at runtime.

### G2.7 — Provider name and Material node

Both the scan and executor methods use `DuckDBScan` as the provider name.
`IsDuckdbPlan()` looks below a root `Material` node before checking the
`CustomScan`. A walk that checks the root alone would miss a wrapped scan.
See `src/pgduckdb_node.cpp:437-446` and
`src/pgduckdb_hooks.cpp:313-337`.

### G2.8 — EXPLAIN property

The supplementary property is named `DuckDB Execution Plan`. The C plan
walker is the authority for the Spike 1 checks. See
`src/pgduckdb_node.cpp:411-414` and
`test/pycheck/explain_test.py:63-81`.

### G2.9 — Versions in the provider's build matrix

The pinned pg_duckdb Docker matrix covers PostgreSQL 14, 15, 16, 17 and 18.
See `.github/workflows/docker.yaml:23-24` and
`.github/workflows/docker.yaml:105-106`.

## PostgreSQL GUC API

**GUC.1.** `GetConfigOptionFlags(name, true)` returns zero for a missing
setting, so zero alone cannot establish that the setting exists. With
`missing_ok=false`, the function raises `ERROR`. See the comments and
implementation in `src/backend/utils/misc/guc.c` in PostgreSQL 16–18.

**GUC.2.** The public `guc.h` in PostgreSQL 17 and 18 declares
`get_config_handle()` and `set_config_with_handle()`. PostgreSQL 16 uses
the older name-based API. See `src/include/utils/guc.h` in these branches.

**GUC.3.** `get_config_handle()` returns a handle for an existing setting
that is not a placeholder and NULL otherwise. See
`src/backend/utils/misc/guc.c` in PostgreSQL 17 and 18.

**GUC.4.** `GUC_NO_RESET` prohibits RESET and SAVE. The
`GUC_CUSTOM_PLACEHOLDER` flag identifies a placeholder. The public
`src/include/utils/guc.h` declares both flags on PostgreSQL 16–18.

## PostgreSQL error handling

**ERR.1.** Declare a local variable `volatile` if it changes in `PG_TRY` and
recovery reads it in `PG_CATCH`. PostgreSQL also advises keeping recovery
small because a second `ERROR` can make it unsafe. See
`src/include/utils/elog.h:317-382` in `REL_17_STABLE`. The rule applies
to all supported branches.

Keep delegation frame cleanup free of allocations and errors. Place every
failpoint that can raise `ERROR` after a frame push inside `PG_TRY`. Use
`volatile bool ok` in the proposed local subtransaction helper.

## WP0 exit criteria

| Check | Status |
|---|---|
| pg_qi revision recorded | PASS |
| pg_duckdb revision recorded and available | PASS |
| PostgreSQL 16–18 planner signature verified | PASS |
| PostgreSQL 19 boundary checked against PostgreSQL and pg_duckdb source | PASS |
| GUC names, contexts and API differences verified | PASS |
| Provider name and `Material` wrapper verified | PASS |
| Force setting read locations verified | PASS |
| Exact `EXPLAIN` property identified | PASS |
| Decisions recorded for known P0 conflicts | PASS |
| Hook order at runtime | OPEN — assigned to S0 |
| `GUC_NO_RESET` on a running cluster | OPEN — assigned to S0 |

WP0 passed the source review and WP1 can begin. S0 will verify the open
runtime checks.
