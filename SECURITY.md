# Security Policy

## Supported versions

Until the first stable release, security fixes are provided only for the latest code on the `main` branch and the latest published pre-release, where applicable.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability.

Use GitHub Private Vulnerability Reporting through the repository's **Security** tab. If that channel is unavailable, contact Baremon through <https://www.baremon.eu/contact/> and clearly mark the message as a confidential security report.

Include, where possible:

- PostgreSQL version;
- `pg_qi` version or commit;
- `pg_duckdb` version, when relevant;
- operating system and architecture;
- minimal reproduction steps;
- expected impact;
- whether the issue can crash a backend, corrupt memory, bypass policy or expose data.

Do not include production credentials, customer SQL text or customer data.

## Current safety boundary

The production build keeps routing disabled by default. Its experimental
`allowlist` mode is `SUSET` and may request delegation only for the narrow
positive classifier subset in a proven client planning context. Owned force is
scoped by a backend-local frame and restored on success or error; nested work
masks parent-owned force. A later outer hook, executor/utility context,
poisoned state, external force or unknown semantic prevents policy routing.

Decision output contains finite status codes only: it retains no SQL text,
values, parameters, relation names or plan pointers. Only a positive
`DuckDBScan` at the proven final planner boundary is marked verified. A
negative match is never presented as verified PostgreSQL. See the
[architecture](docs/architecture.md) and
[production decision contract](docs/production-decision-contract.md) for the
implemented boundary and the controls required before routing can be enabled.

Reports about planner-hook ordering, GUC restoration, fail-closed
classification, plan-provider verification or leakage of test-only controls
are security-relevant and should use the private reporting channel above.

## File privileges for disk spill

Stock pg_duckdb 1.2.1 and 1.2.2 require the effective non-superuser
PostgreSQL role to belong to both `pg_read_server_files` and
`pg_write_server_files` to keep `LocalFileSystem` available. The provider's
filesystem settings and the operating system's file permissions still apply.
[pg_duckdb 1.2.2 role check](https://github.com/duckdb/pg_duckdb/blob/v1.2.2/src/pg/permissions.cpp#L11-L15)

These grants cover files that PostgreSQL can read or write through its
operating system account. PostgreSQL warns that the roles bypass
database-level checks for direct file access and can lead to
superuser-level access. Reserve them for a dedicated execution role you
already trust with server files.
[PostgreSQL predefined roles](https://www.postgresql.org/docs/current/predefined-roles.html)

An upstream fix or a reviewed local pg_duckdb patch should let DuckDB write
its own spill files while preserving the usual file access checks for
application roles. [Upstream issue #864](https://github.com/duckdb/pg_duckdb/issues/864)
