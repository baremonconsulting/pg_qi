# PGQI-4C pilot operator cookbook

## Before a pilot

Use the exact reviewed source revision and pinned dependencies. Install pg_qi
as a production build and place `pg_duckdb,pg_qi` in that order in
`shared_preload_libraries`. Restart PostgreSQL; a reload is insufficient for a
preloaded planner hook. Keep `pg_qi.routing_mode=off` as the persistent
default.

Record, without credentials or query text:

```sql
SHOW shared_preload_libraries;
SHOW pg_qi.enabled;
SHOW pg_qi.routing_mode;
SHOW duckdb.force_execution;
```

Collect these preflight facts before the subject query. A diagnostic `SELECT`
itself plans and can replace the backend-local decision snapshot.

## Controlled session activation

Only a superuser can select the allow-list:

```sql
SET pg_qi.routing_mode = allowlist;
```

Run one approved literal-query probe. Immediately on the same connection use
individual `SHOW` commands:

```sql
SHOW pg_qi.shadow_decision_id;
SHOW pg_qi.shadow_contract_version;
SHOW pg_qi.shadow_context;
SHOW pg_qi.shadow_reason;
SHOW pg_qi.shadow_policy_intent;
SHOW pg_qi.shadow_force_origin;
SHOW pg_qi.shadow_classifier_status;
SHOW pg_qi.shadow_classifier_reason;
SHOW pg_qi.shadow_observed_engine;
SHOW pg_qi.shadow_verification;
SHOW pg_qi.shadow_request_applied;
SHOW duckdb.force_execution;
```

An accepted pilot probe requires `shadow_context=proven_client`,
`shadow_policy_intent=request_delegation`, `shadow_reason=delegation_verified`,
`shadow_force_origin=qi_stack`, `shadow_classifier_status=candidate`,
`shadow_classifier_reason=candidate`, `shadow_observed_engine=duckdb`,
`shadow_verification=verified`, `shadow_request_applied=on`, followed by
ambient `duckdb.force_execution=off`. A failed planning attempt does not publish a new
snapshot; the displayed record is the last completed planning decision.
Likewise, execution of a cached plan may leave the decision ID unchanged.
Negative engine observation is not proof of PostgreSQL execution.

## Stop new delegation

On every affected session:

```sql
SET pg_qi.routing_mode = off;
```

This controls future planning only. It does not revoke an already cached plan,
cancel running work, clear an external `duckdb.force_execution=on`, or override
another session's explicit setting. Deallocate prepared statements or close
the session. Recycle the relevant connection-pool sessions and confirm a new
connection reports `routing_mode=off`.

Disconnect any backend that reports an unsafe/poisoned routing invariant. Do
not attempt to repair it in place.

## Binary/configuration rollback

1. Set the persistent default to `off` and retire pilot pool sessions.
2. Stop PostgreSQL using the site's normal controlled procedure.
3. Restore the reviewed prior libraries and prior
   `shared_preload_libraries` configuration.
4. Start PostgreSQL and verify preload order and hashes.
5. Open a new session and repeat the default-off preflight and a safe control.

`DROP EXTENSION` does not unload a preloaded hook. The executable PGQI-4C
drill deliberately proves session disable/reconnect behavior only; it never
targets an existing customer cluster and does not claim to rehearse a binary
downgrade.

## Reproduce the evidence

The commands below create only marker-owned temporary clusters. They do not
install extension files: first install the reviewed production build into a
dedicated development/CI PostgreSQL tree. The runner fails if that installed
binary differs from its clean `PG_QI_TESTING=0` build.

```sh
scripts/phase4c/run-production.sh 17
scripts/phase4c/run-rollback-drill.sh 17
scripts/phase4c/run-benchmark.sh 17 25 250
```

The runner refuses arbitrary data directories, inherited connection targets,
reused run roots and test-only production artifacts. Preserve the whole run
directory for review, especially on failure.
