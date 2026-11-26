# WP1 planner compatibility

14 October 2026

PostgreSQL 16–18 passed the build with warnings treated as errors, a preload
smoke test and the existing regression test. The compatibility header also
covers the new planner signature in the PostgreSQL 19 development branch.

## Implementation

`src/include/pg_qi/planner_compat.h` selects the signature with
`PG_VERSION_NUM >= 190000`. The header defines four names.

- `qi_planner_fn_type`
- `QI_PLANNER_PROTO(fn)`
- `QI_CALL_PLANNER(fn, ...)`, which takes the function being called explicitly
- `QI_CURRENT_EXPLAIN_STATE`

`src/pg_qi.c` uses the same macro to call the saved downstream hook and
`standard_planner`. The affected declaration uses PostgreSQL's own
`ExplainState` on the development branch. The PostgreSQL 16–18 version omits
the local `es` argument. The explicit function parameter also supports a
future synthetic recursion fixture. The hook chain and `_PG_fini()` retain
their existing semantics.

## Verification

| PostgreSQL | `pg_config` | Build | `make installcheck` | Result |
|---|---|---|---|---|
| 16 | 16.15 | PASS (`-Werror`) | 1/1 PASS | PASS |
| 17 | 17.11 | PASS (`-Werror`) | 1/1 PASS | PASS |
| 18 | 18.6 | PASS (`-Werror`) | 1/1 PASS | PASS |

Each test used a fresh cluster with `shared_preload_libraries = 'pg_qi'`. The
smoke test confirmed `qi.is_preloaded() = true` and `pg_qi.enabled = on` before
the regression ran. The required pre-GA runtime matrix covered PostgreSQL
16–18.

## Reproduce the matrix

Run `scripts/spike1/wp1-matrix.sh`. The default PostgreSQL installation paths
are under `$HOME/.local/pgsql/{16,17,18}`. The script writes results to
`artifacts/spike1/wp1`.

The recorded installations were built from the official PostgreSQL release
sources. The host lacked system installations of `bison`, `flex` and `m4`.
The test used copies extracted under `~/.local/pg-build-tools` with ordinary
user permissions.
The CI definition adds library preload to the existing 16–18 matrix.
GitHub Actions was not run for this local-only result.

## Compiler warning policy

A global `-Wall -Wextra -Werror` build failed on an unused parameter in the
PostgreSQL 16 public inline header `storage/bufpage.h`. The final `Makefile`
adds `-Werror` to the PGXS warning set, which keeps the extension build strict
and avoids promoting that upstream warning to an error.
