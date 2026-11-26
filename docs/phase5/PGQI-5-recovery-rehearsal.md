# PGQI-5 recovery rehearsal and site readiness

This component validates recovery evidence. It does not execute recovery.
`RECOVERY_REHEARSED` and `OPERATION_GO` remain human gates and are not granted
by a successful tool result.

## Authority and target

A rehearsal needs a typed `rehearsal_mandate` with a current validity window,
three recorded approvals, distinct primary operator, second operator and
implementer, and a verified recoverable-copy identity distinct from its source
environment. Its recovery plan, source target, candidate release identity and
approved baseline identity are frozen into the measurement scope. Candidate
and baseline artefacts use the same `PGQI_STACK_SHA256_V1` composite over the
PostgreSQL build, `pg_qi` and `pg_duckdb` digests; configuration has its own
digest. The mandate
fixes the recovery plan, runbook, allowed strategies
and exact lifecycle actions. Its lifecycle executor is always
`HUMAN_OPERATOR_ONLY`.

The validator has no interface to stop or start PostgreSQL, change libraries or
configuration, administer a pool, cancel a backend, deallocate a prepared
statement, or activate routing. Operators perform those steps through the
separately authorised site procedure and retain the observations as private
artefacts.

## R1 — session, cached-plan and pool retirement

The R1 terminal records this exact order:

1. latch stop so that no new request is admitted;
2. set routing off;
3. deallocate prepared objects or retire their owning sessions;
4. recycle the pool, or prove that the frozen direct-session profile has no
   pool;
5. use an authorised, complete server-side observation to prove every exact
   owned backend absent;
6. open a new, non-owned backend and prove default-off, force-off PostgreSQL
   execution.

Client loss, reconnect, timeout, routing-off alone and an incomplete
`pg_stat_activity` view are not exit proof. Cached-plan persistence observed
after routing is disabled is retained as a limitation, not silently erased.
R1 must finish before R2 restarts or restores the server.

## R2 — return of the environment

The recovery plan chooses one or both acceptable strategies:

- `EXACT_BINARY_CONFIG_DOWNGRADE` is the task's `binary_downgrade`. It keeps
  the same cluster/data continuity, uses a new server-start identity, restores
  the approved prior artefact and configuration identities and cannot carry a
  restore-source identity.
- `VERIFIED_ENVIRONMENT_RESTORE` is the task's `environment_restore`. It binds
  the exact approved restore source and is never reported as a downgrade.

Both strategies require R1 PASS first, an operator stop, application of the
approved recovery, recorded artefact and configuration identities before and
after recovery, a new server start, identity verification and a fresh
default-off/force-off PostgreSQL control. R1 and R2 have separate terminal
digests and cannot reuse one control as both proofs.

## Second operator and incident rehearsal

The second operator is distinct from both the primary operator and implementer.
The walkthrough binds the frozen runbook and the exact R1/R2 terminals. It
requires practical performance of session retirement and incident handoff.

The incident sequence is fixed: latch stop, preserve evidence, isolate only the
exact poisoned backend, prove its exit server-side and invalidate the current
GO. The exit observation also proves that a different backend on the same
server start remains present, so a whole-server restart cannot masquerade as
backend isolation. Continuing after the incident always needs a new nonce and a new GO. A
reconnect does not heal an incident or prove backend exit.

## Timing and claims

Start, finish and elapsed milliseconds are observations from this one
rehearsal. They are not an SLA, RTO or general availability claim. Contracts
reject additional controlling fields, including ad-hoc `sla` or `rto` fields,
and every validation carries:

- `NO_SLA_CLAIM`;
- `NO_RTO_CLAIM`;
- `NO_AUTOMATIC_GATE_GRANT`.

## Validation and incident flow

The private bundle contains `mandate.json`, `r1-terminal.json`,
`r2-terminal.json`, `walkthrough.json`, `incident.json` and every artefact they
reference. The offline validator rechecks exact bytes, graph digests, ordered
steps, scope and rehearsal identity, role separation, strategy semantics,
fresh controls and the mandate window. A successful result is only
`ELIGIBLE_FOR_HUMAN_GRANT`.

Any missing, changed, partial or contradictory input is refused. Partial
evidence is preserved. The affected backend stays poisoned until exact exit is
proved. A new attempt requires a closed cause, a new rehearsal mandate where
needed, revalidated scope/preflight and a new human GO.
