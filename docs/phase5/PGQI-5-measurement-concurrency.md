# PGQI-5 frozen measurement and bounded concurrency

This component provides a reproducible measurement boundary for the controlled
pilot. It is not a benchmark claim, an autonomous load generator, or authority
to execute a customer workload.

## Approval closure

Before `OPERATION_GO`, the scope contains the exact measurement-plan artifact.
The plan freezes the workload and qualification identities, baseline manifest,
resource policy, release, target, dataset and toolkit digests; exact scenarios;
protocol and cache states; deterministic seed and balanced block order; warm-up
count; allowed concurrency levels; statistical rules; and any value weights.
Changing any byte changes the plan artifact, scope digest, and therefore
invalidates an earlier operation approval.

The plan itself deliberately has no `scope_sha256`, avoiding a circular hash.
At validation time, its direct workload, qualification, resource-policy,
release, target and toolkit identities must match the frozen scope inputs.
The resource-policy digest is recomputed from every enforced policy field
(identity, feed definition, timing, limits and units). The redundant
`scope_sha256` link is excluded from that digest to avoid a scope/policy hash
cycle, then checked separately against the active scope. The stop controller
must hold the same parsed policy object.

## Execution order

1. Load the caller-owned mode-0600 plan without following links and validate
   its exact schema.
2. Validate a currently effective `OPERATION_GO` for the exact scope and the
   explicit approval reference for the requested level.
3. For levels above one, validate the immediately preceding sealed level:
   `private_full` PASS, clean aggregate terminal, complete indexed measurement
   report and raw stream, successful retirement, and the approved resource-feed
   identity.
4. Re-run the complete offline `private_full` validator and require PASS for
   every frozen oracle case before creating any measurement session. Coverage
   and value weights come from that scope-bound qualification; callers cannot
   substitute PASS flags or coverage numbers.
5. For each block position, create at most the approved number of independently
   owned sessions, all fixed to that position's mode. Run the applicable
   warm-ups and measured batch, then retire those sessions before changing mode.
   Mode changes are not workload SQL and level 1 therefore owns only one live
   database session at a time.
6. Execute the frozen repetitions as complete six-order Williams blocks. Every
   mode occurs equally in every position and every ordered carry-over is
   represented. Scenario-to-slot assignment rotates deterministically. An
   executor runs only the single level named by its approval and cannot escalate
   itself.
7. On failure, timeout, resource stop, evidence limit, or session error, stop
   admitting new work, preserve the observation, and retire every owned session.

The three modes are:

- `disabled`: `pg_qi.enabled=off` and routing off;
- `off`: `pg_qi.enabled=on` and routing off;
- `allowlist`: `pg_qi.enabled=on` with the approved allowlist path.

## Timing boundary

`request_interval` is measured in the C/libpq process with a monotonic clock.
It begins immediately before the protocol send and finishes after libpq has
consumed the complete result stream. A deadline is retained as a censored
timeout. Snapshot capture, correctness oracle, diagnostics and evidence fsync
are excluded; their post-processing duration is recorded separately. This
prevents Python scheduling or evidence persistence from being presented as
database request latency.

## Evidence and statistics

Every JSONL sample is a typed contract bound to scope, plan, run, level,
scenario, mode, phase, block, order and session slot. The report retains PASS,
FAIL, TIMEOUT and INCOMPLETE counts. It uses nearest-rank percentiles, publishes
p95 only at the frozen minimum N (at least 20), publishes p99 only at the frozen
minimum N (at least 100), and otherwise records an explicit suppression reason.
Per-block median minimum and maximum expose run variability without deleting
outliers. Post-processing p50 is reported separately, as are the descriptive
`disabled → off` extension overhead and `off → allowlist` routing contrast.

The sealed offline validator re-reads the indexed sample stream and recomputes
its structure, counts, percentiles, suppression states and PASS completeness.
Changing the raw stream or asserting a different statistic invalidates the
bundle.

Coverage is reported as four separate facts:

- structural eligibility;
- frequency coverage;
- baseline-time measured share;
- measured natural-query share.

Missing coverage remains unknown, never zero. Value outcome is
`INCONCLUSIVE` unless the plan contains approved complete weight identities,
all corresponding coverage is complete, every required measurement passes,
and at least the frozen minimum of 20 paired blocks exists. `POSITIVE` requires
the empirical fifth-percentile paired block improvement to meet the frozen
threshold; a small-N p50 cannot create a positive value conclusion. Any value
result applies only to the measured, correct natural subset.

The selected controlled path is extended protocol with zero parameters. The
adapter maps `first_use` to a fresh audited dispatch and `warm_cached` to the
audited named-prepare lifecycle. Simple protocol and an unproven replan cache
label are refused rather than silently mislabeled.

## Interpretation limits

The output does not establish an SLA, ROI, capacity, infrastructure-wide
effect, production readiness, or general performance improvement. It is valid
only for the exact frozen release, target, dataset, scenarios, plan, resource
policy and approval window represented by the evidence bundle.
