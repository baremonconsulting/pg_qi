# pg_qi Spike 1 — final evidence report

Date: 2026-10-16
Status: **COMPLETE — verdict A**
Scope: PostgreSQL 16, 17, and 18 with pinned pg_duckdb

## 1. Executive conclusion

Spike 1 demonstrates that pg_qi can safely and deterministically request
pg_duckdb planning for one eligible top-level planning event by temporarily
setting `duckdb.force_execution`. The implementation preserves the planner-hook
chain, restores GUC state on success and error, masks synthetic nested planner
reentry, and keeps all control state backend-local.

The evidence supports **verdict A: the temporary GUC-based adapter is usable
for the proven scope**. A new upstream pg_duckdb rendezvous API is not required
to proceed to the next product-design stage.

This is not approval to merge the spike implementation unchanged into a
production release. The spike intentionally contains invasive diagnostics,
failpoints, synthetic reentry, benchmark modes, and SQL test controls that must
be removed or redesigned behind an internal/test-only boundary first.

## 2. Reproducibility and component identity

| Component | Evidence identity |
|---|---|
| pg_qi repository baseline | `d3529d4e6a7cd71657bbf8279c533a3c5a81e2df` |
| pg_qi Spike 1 implementation | uncommitted cumulative worktree based on the baseline above; package checksum is the immutable identity for this report |
| pg_duckdb | `ee38d3b540ecea1d93683ba99bdcec5632a21eaf` |
| DuckDB source archive used by CI | `v1.5.4`, SHA-256 `838d98a85e697bab9935010c88a8c67d3312ccedcab4cb4a0ba01da65113bb70` |
| PostgreSQL tested binaries | 16.15, 17.11, 18.6 |
| PostgreSQL recon snapshots | REL_16_STABLE `333d566c4f51f1b8b002175032586423c581d3d6`; REL_17_STABLE `9e8f4ce286bf03306fc0ad29374095782b2a8136`; REL_18_STABLE `70ffc30e12b1de43d6160682fc44c78b8de00f24` |
| PostgreSQL forward recon only | master `2246c04e7b99519010cd47be636273a5700b0b0d`; not part of runtime acceptance |

The absence of an implementation commit is deliberate: no commit or push was
authorised during WP0–WP13. Therefore the baseline Git SHA alone must not be
misrepresented as identifying the implementation. The WP13 archive plus its
SHA-256 manifest identifies the reviewed cumulative state until it is committed.

## 3. Acceptance results: S0–S11

Every row passed on every supported PostgreSQL version: **48/48 results PASS**.

| Scenario | PG16 | PG17 | PG18 | Principal evidence |
|---|:---:|:---:|:---:|---|
| S0 preflight/hook order | PASS | PASS | PASS | real non-placeholder resettable GUC; downstream hook present |
| S1 baseline | PASS | PASS | PASS | PostgreSQL plan; clean balanced state |
| S2 one-shot delegation | PASS | PASS | PASS | first query DuckDB, second PostgreSQL; one token consumption |
| S3 protected error | PASS | PASS | PASS | one unwind; GUC restored; token not resurrected by rollback |
| S4 downstream planner error | PASS | PASS | PASS | real `division by zero`; clean unwind |
| S5 execution error | PASS | PASS | PASS | no planner-frame leak and no false planner unwind |
| S6a-1 masked reentry | PASS | PASS | PASS | outer DuckDB, inner PostgreSQL, one nested mask |
| S6a-2 propagated reentry error | PASS | PASS | PASS | both frames unwound; guard reset; original error retained |
| S6a-3 locally caught error | PASS | PASS | PASS | inner cleanup; outer DuckDB plan completes |
| S6b-1 function guard off | PASS | PASS | PASS | runtime SPI query remains PostgreSQL; state clean |
| S6b-2 unsafe override on | PASS | PASS | PASS | request made; pinned pg_duckdb deterministically falls back |
| S7 custom plans | PASS | PASS | PASS | `generic_plans=0`, `custom_plans=2` |
| S8 generic reuse/invalidation | PASS | PASS | PASS | cached delegation reused; invalidation replans without consumed token |
| S9 provenance | PASS | PASS | PASS | EXTERNAL, QI_STACK, and nested mask distinguished |
| S10 utility/protocol/guards | PASS | PASS | PASS | EXPLAIN, cursor, view, multi-statement and CTAS paths clean |
| S11 parallel isolation | PASS | PASS | PASS | leader plus four workers; worker state clean and unarmed |

The authoritative oracle is the C plan-tree detector. JSON EXPLAIN is only
corroborating evidence. Every scenario ends with `depth=0` and
`cleanup_broken=false`.

## 4. Concurrency result

The PostgreSQL 17.11 assert build ran eight persistent clients for 300 seconds:

- 40,620 successful transactions and zero failed transactions;
- 135.385595 transactions per second;
- all eight workload backends completed PostgreSQL and DuckDB plans;
- every backend satisfied `pushed = popped + unwound`;
- every backend ended with depth zero and `cleanup_broken=false`;
- no assertion, panic, trap, or segmentation-fault signature occurred.

This is a concurrency smoke proof of backend-local isolation, not a production
load or capacity benchmark.

## 5. Benchmark result

The optimized non-assert matrix completed all **36/36 combinations**, covering
PostgreSQL 16.15, 17.11, and 18.6; four modes; and simple, forced-custom, and
forced-generic paths. It executed 18,000 measured requests with zero failures.

Selected simple-protocol request latency (microseconds):

| PG | mode | p50 | p95 | p99 |
|---:|---|---:|---:|---:|
| 16 | bypass | 1,941 | 2,604 | 2,845 |
| 16 | frame_only | 1,364 | 2,352 | 2,869 |
| 16 | shadow_save | 1,898 | 2,567 | 2,940 |
| 16 | delegate | 15,491 | 19,205 | 22,101 |
| 17 | bypass | 1,722 | 2,415 | 3,453 |
| 17 | frame_only | 2,061 | 2,931 | 3,220 |
| 17 | shadow_save | 1,954 | 2,929 | 3,265 |
| 17 | delegate | 15,263 | 19,893 | 24,067 |
| 18 | bypass | 1,848 | 2,649 | 3,224 |
| 18 | frame_only | 1,616 | 2,268 | 2,605 |
| 18 | shadow_save | 1,574 | 2,230 | 2,460 |
| 18 | delegate | 16,310 | 20,220 | 22,463 |

The non-delegating layers occupy the same request-latency band. Small
differences vary in sign and must not be presented as statistically precise
overhead claims. Actual delegation is a different scale and these results for
a small test query are integration measurements, not a claim of analytical
speedup.

All custom/generic counters and DuckDB plan counts matched the required values.
In particular, forced-generic execution creates one delegated plan and reuses
it for 500 executions without 500 new policy decisions.

## 6. Generic-plan semantics decision

Spike 1 explicitly **accepts decision-at-plan-time semantics for the intended
0.2 design stage**:

1. policy is evaluated when PostgreSQL creates or recreates a plan;
2. a cached generic plan retains the chosen execution engine on reuse;
3. DDL invalidation causes a new planning event and therefore a new decision;
4. a one-shot token already consumed during original plan creation is not
   automatically re-armed for invalidation.

This behavior is consistent with PostgreSQL plan caching and with an Observe /
pre-plan routing architecture. It must be documented for operators. If a future
requirement demands a fresh policy decision for every execution, that is a
different architecture and should be evaluated as a separate escalation toward
verdict B, not silently added to this adapter.

## 7. Known limits and non-claims

- Runtime acceptance covers PostgreSQL 16–18 only; PG19 was recon/probe context,
  not a supported runtime target.
- The conclusion is pinned to pg_duckdb
  `ee38d3b540ecea1d93683ba99bdcec5632a21eaf`; dependency changes require replay.
- Delegation requires a qualifying statement with a FROM clause.
- Inside PL/pgSQL/runtime SPI, pinned pg_duckdb fell back to PostgreSQL even with
  `duckdb.unsafe_allow_execution_inside_functions=true`. This does not leak
  state, but the override does not guarantee delegation.
- The detector is proven for the plan shapes exercised by Spike 1; it is not a
  claim of completeness for every future PostgreSQL plan-node type.
- The concurrency test is a five-minute smoke test on one host, not soak,
  failover, crash-recovery, or multi-host validation.
- Benchmark values are environment-specific and do not establish workload ROI.
- Hosted GitHub Actions were not executed because no commit/push was authorised;
  WP12 validates the workflow locally, including actionlint and representative
  matrix jobs.
- Spike 1 proves an integration mechanism. It does not implement production
  classification, admission control, telemetry, or policy management.

## 8. Upstream API assessment

No upstream pg_duckdb change is required for verdict A. The existing GUC is
adequate for the demonstrated temporary adapter when it is scoped by the
backend-local frame and PostgreSQL GUC nest level.

If later requirements invalidate plan-time semantics or require guaranteed
delegation in currently declined nested-executor paths, the minimal verdict-B
proposal remains:

- a stable provider-name discovery function; and
- a backend-local one-shot `request_execution(origin, request_id)` rendezvous
  operation.

Broader policy, telemetry, or `would_delegate()` APIs should not be proposed
without a separate demonstrated requirement.

## 9. Test-only code that must not ship unchanged

The following surfaces exist to create evidence and must be removed, compiled
only in a test build, or redesigned before production:

- all `pg_qi.spike_*` control and diagnostic GUCs;
- SQL functions `qi.spike_arm_next`, `qi.spike_disarm`,
  `qi.spike_reset_token`, `qi.spike_reset_state`, `qi.spike_state`, and
  `qi.spike_observer`;
- SQL/C helpers prefixed `test_`, including detector, frame, token, preflight,
  save/restore, and active-control-guard contracts;
- all six injectable failpoints and their hit/fired counters;
- synthetic planner reentry modes and deliberate inner-error injection;
- benchmark modes (`bypass`, `frame_only`, `shadow_save`, `delegate`) and
  planner timing collection;
- the `PG_QI_FINAL` backend-exit evidence line;
- the WP0–WP12 shell harnesses, generated fixture data, and acceptance artifacts.

The planner compatibility shim, centralized detector, scoped GUC restore
pattern, no-throw frame cleanup, and provenance model are reusable design input,
but still require normal product review and a narrowed public surface.

## 10. CI and evidence map

| Evidence | Location |
|---|---|
| pinned source recon | `docs/spike1/spike-1-recon.md` |
| S0–S11 result analysis | `docs/spike1/WP9-S0-S11-scenario-matrix.md` |
| complete scenario matrix | `artifacts/spike1/wp9/results.tsv` |
| concurrency report | `docs/spike1/WP10-concurrency-smoke-report.md` |
| concurrency raw summary | `artifacts/spike1/wp10/summary.txt` |
| benchmark report | `docs/spike1/WP11-benchmark-report.md` |
| complete benchmark matrix | `artifacts/spike1/wp11/results.csv` |
| CI report | `docs/spike1/WP12-CI-report.md` |
| workflow | `.github/workflows/ci.yml` |

## 11. Final gate and next action

All Spike 1 definition-of-done gates supported without a remote push are met:

- pinned and reproducible dependencies;
- automatic S0–S11 results on PostgreSQL 16/17/18;
- required detector and error/reentry cases green;
- concurrency invariants green;
- layer-separated benchmark complete;
- generic-plan semantics explicitly accepted;
- evidence-backed A/B/C verdict recorded;
- test-only code identified;
- claims bounded to the evidence.

**Final verdict: A. Spike 1 ends here.**

The next controlled step is human review of this report and cumulative diff,
followed—only with explicit authorisation—by a commit/push and observation of
the first hosted GitHub Actions run. Productisation should start from a cleanup
plan, not by merging the experimental surface unchanged.
