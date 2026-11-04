# WP14: Production/test build boundary

## Outcome

The default build is production-only. Spike 1 diagnostics, failpoints,
synthetic planner re-entry, benchmark controls, test SQL functions and the
backend final report are absent from the production shared library and SQL
extension.

The complete Spike 1 verification surface remains available in an explicit
test build. It is packaged as the dependent `pg_qi_spike_test` extension so
production and test SQL interfaces cannot be confused.

## Build contracts

Production build:

```sh
make clean
make
make install
```

Test build:

```sh
make PG_QI_TESTING=1 clean
make PG_QI_TESTING=1
make PG_QI_TESTING=1 install
```

Test databases must install both extensions:

```sql
CREATE EXTENSION pg_qi;
CREATE EXTENSION pg_qi_spike_test;
```

## Boundary inventory

Production exports only the supported SQL surface from `pg_qi`: `version()`
and `is_preloaded()`. The supported `pg_qi.enabled` setting and planner hook
remain available.

Only the test build compiles `qi_diag.c` and `qi_reenter.c`, registers
`pg_qi.spike_*` settings, exposes `qi_test_*` and `qi_spike_*` C entry points,
enables benchmark modes, or emits `PG_QI_FINAL` evidence.

## Verification

CI compiles both variants with `-Werror` on PostgreSQL 16, 17 and 18. It also
scans the production library for test-only names and runs the existing
regression and S0-S11 suites against the explicit test build.
