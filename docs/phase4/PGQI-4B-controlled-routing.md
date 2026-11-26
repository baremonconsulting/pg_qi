# PGQI-4B: controlled allow-list routing

## Scope

PGQI-4B enables an explicit, superuser-controlled routing mode for the narrow
production allow-list introduced by PGQI-4A. The default remains `off`.
Classification alone never authorises delegation: context, provenance, local
state and policy gates must all pass first.

The implementation performs no network access, does not retry planning and
does not fall back by replanning after a downstream error.

## Proven planning context

A routing request may originate only when all of these conditions hold:

- the process is a regular client backend and not a parallel worker;
- no executor or utility command is active in the backend;
- the invocation is the outermost active pg_qi planner invocation; and
- `pg_qi` is the outermost installed planner hook at the decision boundary.

The executor and utility exclusions conservatively reject SPI planning,
prepared-command utility paths, `EXPLAIN` and other internally initiated work.
The outer-hook check prevents a later hook from transforming a plan after
pg_qi's verification boundary. Any unproved case passes through without a
pg_qi force request.

## Local policy

`pg_qi.routing_mode` is bounded backend-local policy with two values:

- `off`: observe only and never request delegation;
- `allowlist`: request delegation only after every context, provenance,
  availability and classifier gate passes.

The setting is `SUSET`; ordinary sessions cannot enable routing. External
`duckdb.force_execution` remains external and is never attributed to policy.

## Owned-force lifecycle

An approved request creates a backend-local frame and a GUC nest before
setting `duckdb.force_execution`. Nested planning masks a parent-owned request
in its own frame even when normal policy evaluation is disabled. Success and
error paths restore the GUC and remove the frame in LIFO order.

An invariant failure poisons routing state for the backend. No later request
is allowed, and an unsafe active state raises an error rather than entering a
downstream planner with ambiguous force ownership.

## Verification and fallback

The downstream planner is called exactly once. A returned `DuckDBScan` at the
proven outermost boundary verifies DuckDB execution for the planned statement.
A requested delegation without that positive match is a declined request; the
returned downstream plan remains authoritative and is not replanned.

PostgreSQL execution is not inferred from a negative match. It remains
`unknown` until the provider-completeness requirement is separately proven.

## Acceptance boundary

The production acceptance suite must cover PostgreSQL 16, 17, 18 and the
pinned PostgreSQL 19 build, including:

- default-off and explicit allow-list policy;
- positive allow-list delegation and authoritative DuckDB verification;
- every classifier rejection family without a force request;
- external-force provenance without policy attribution;
- executor, utility, nested and later-outer-hook context rejection;
- nested masking and restoration of parent-owned force;
- success, propagated-error and poisoned-state cleanup;
- custom, generic, reused and invalidated plan behaviour; and
- absence of test controls and network operations from production hook paths.

PGQI-4C defines the bounded pilot evidence and operator gate. Completion does
not constitute unrestricted production certification; Human Review remains
mandatory.
