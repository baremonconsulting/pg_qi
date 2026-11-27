# pg_qi documentation

## Install and operate

- [Installation](../README.md#installation) covers preload order, extension
  setup and routing controls.
- [Known issues](../README.md#known-issues) describes current provider
  limitations, including disk spill.
- [Operator cookbook](phase4/PGQI-4C-operator-cookbook.md) explains how to run
  a controlled pilot, read diagnostics and rehearse rollback.
- [Production compatibility CI](production-compatibility-ci.md) describes
  the tested build matrix and the checks for each supported version.
- [Security policy](../SECURITY.md) explains the current safety boundary and
  how to report a vulnerability privately.

## Understand the design

- [Architecture](architecture.md) covers PostgreSQL hooks, plan selection
  and error handling.
- [Production decision contract](production-decision-contract.md) defines
  the decisions and guarantees available to clients.
- [V2 decision contract](phase7/PQ7-1-v2-decision-contract.md) describes the
  decision record and its reason codes.
- [Editions](editions.md) explains the scope of the extension and the planned
  Baremon platform.

## Review the evidence

- [Spike 1 report](spike1/spike-1-report.md) and [its supporting records](spike1/)
  cover the initial planner integration, scenario matrix and benchmarks.
- [WP14 records](wp14/) cover the classifier prototype and PostgreSQL
  compatibility replays.
- [Shadow decision engine](phase4/PGQI-4A-shadow-decision-engine.md),
  [controlled routing](phase4/PGQI-4B-controlled-routing.md) and
  [pilot closeout](phase4/PGQI-4C-closeout.md) trace the evidence for each stage
  of the production build.
- [Phase 7 records](phase7/) cover recent decisions and qualification,
  including the [differential oracle](phase7/PQ7-4-differential-oracle.md)
  and [prepared-plan lifecycle](phase7/PQ7-12-plan-lifecycle.md).

## Pilot administration

[Phase 5](phase5/README.md) records Baremon's internal process for
qualifying controlled pilots, gathering evidence and reviewing approvals.
