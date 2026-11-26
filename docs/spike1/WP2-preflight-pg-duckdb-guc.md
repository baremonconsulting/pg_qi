# WP2 preflight for duckdb.force_execution

14 October 2026

The preflight verified a real, resettable `duckdb.force_execution` setting on
PostgreSQL 16–18. The test also checked that `GUC_ACTION_SAVE` sets and
restores the value in both directions.

## Implementation

- `src/include/pg_qi/qi_delegation.h` declares the internal interface.
  `src/qi_delegation.c` implements the preflight and setting update.
- PostgreSQL 17 and 18 use `get_config_handle()` to verify the setting and
  `set_config_with_handle()` to write it.
- PostgreSQL 16 checks `GetConfigOptionFlags(name, false)` and
  `GUC_CUSTOM_PLACEHOLDER`, then writes through `set_config_option()` by name.
- All three versions reject `GUC_NO_RESET`.
- The preflight runs only through `shared_preload_libraries`. A subsequent
  `CREATE EXTENSION` uses the installed hook.
- `qi.test_force_meta()` supplies preflight metadata for S0.
  `qi.test_force_save_roundtrip()` checks both directions at a new GUC nesting
  level.

## Verification

| PostgreSQL | Preflight | Handle | Placeholder | `GUC_NO_RESET` | SAVE round trip | Regression |
|---|---:|---:|---:|---:|---:|---:|
| 16.15 | PASS | N/A | false | false | PASS | 1/1 PASS |
| 17.11 | PASS | Present | false | false | PASS | 1/1 PASS |
| 18.6 | PASS | Present | false | false | PASS | 1/1 PASS |

All versions reported flags of `64`. The flag value is diagnostic. The
preflight uses the setting lookup to establish existence.

## Rejected configurations

All three versions rejected startup with pg_duckdb missing and with the
preload order `pg_qi,pg_duckdb`. PostgreSQL 17 and 18 returned a pg_qi error
with a hint about the preload order. On PostgreSQL 16,
`GetConfigOptionFlags(name, false)` raised the core error
`unrecognized configuration parameter` before the placeholder check.

## Reproduce the build

Build pg_duckdb at `ee38d3b540ecea1d93683ba99bdcec5632a21eaf` separately for
PostgreSQL 16, 17 and 18. Its DuckDB submodule is v1.5.4 at `08e34c447b`. The
host had 3.8 GB of RAM, so the run used the official DuckDB v1.5.4 binary.
The archive SHA-256 is
`838d98a85e697bab9935010c88a8c67d3312ccedcab4cb4a0ba01da65113bb70`.

## Build findings

1. Declare SQL entry points in separate C modules with `PGDLLEXPORT`. PGXS
   builds with hidden symbol visibility.
2. Treat each `set_config_*` result `<= 0` as an error. The function can return
   `-1` when it leaves the value unapplied.
3. Run `clean`, `build` and `install` in that order when switching PGXS
   versions. A stale build can otherwise install a library for a different
   PostgreSQL major.
