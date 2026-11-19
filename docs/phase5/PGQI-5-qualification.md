# PGQI-5 qualification and investment boundary

## Purpose

PGQI-5 evaluates a controlled pilot of the existing bounded router. The first
gate is deliberately cheaper than building or running the pilot toolkit: it
records the natural workload denominator, the authorised environment, the
people who may make each decision, and the evidence required to stop safely.

Qualification never enables routing. It can authorise further implementation,
but it cannot create an operation, customer, sharing or publication approval.

## Current qualification record

As of 2026-11-17, the controlled [`PQ-2` implementation decision](decisions/PQ-2-implementation-authorisation.md)
authorises the PGQI-5 toolkit in the `phase5/controlled-pilot` branch. The only
accepted execution profile at this gate is a disposable, synthetic lab owned
by the test harness. This mandate is separate from workload qualification.

The following facts remain unresolved and therefore block site activation:

- no natural internal or customer workload inventory has been supplied;
- no representative dataset or data authority has been approved;
- no concrete pilot site, PostgreSQL build or dependency set has passed the
  PGQI-5 site preflight;
- no resource feed, limits, evidence store, access list or retention period
  has been approved;
- no named operation owner, deputy, data owner, workload owner or stop
  authority has accepted an operation window;
- no recovery environment or prior binary/configuration set has been accepted.

Natural workload relevance and customer value are therefore `INCONCLUSIVE`.
Synthetic test cases must not be counted as natural workload coverage.

No external lab host has passed the qualification gate. Prior test-host state
is not assumed and operational endpoint details do not belong in this public
repository.

## Qualification gates

### `QUALIFICATION_AUTHORIZED`

Allows only authorised acquisition and classification of workload metadata.
It requires an owner of the workload, a permitted source and a defined set of
fields. It does not permit query execution, copying SQL into the repository or
collecting customer data speculatively.

### `INVESTMENT_APPROVED`

Allows a bounded next tranche only after workload relevance, the value
hypothesis, precommitted thresholds and the maximum tranche are approved and
recorded. PGQI-5 does not currently satisfy this gate. The separate `PQ-2`
mandate authorises bounded offline implementation and disposable synthetic-lab
tests without redefining or granting `INVESTMENT_APPROVED`.

### `SCOPE_AUTHORIZED`

Allows later site-specific preparation only after the charter has no safety
TBDs for the requested stage. It binds the exact site, build, workload, data,
limits, evidence policy, recovery strategy, roles and time window. It is not
present for the current planning record.

## Required workload inventory

The inventory is a denominator, not a list of selected successful examples.
Every authorised item receives a stable opaque ID and records:

- separate inventory, executed-SQL and dataset origins, controlled references
  and independent authorities;
- frequency weight and baseline-time weight, or `UNKNOWN` with a reason;
- SQL shape without placing confidential query text in this repository;
- protocol, parameterisation, prepared/pool behaviour and result type family;
- current eligibility result and reason family;
- whether execution is authorised, inventory-only or prohibited;
- derivation lineage for every rewritten or synthetic reproduction.

Unsupported and non-executable items remain in the natural denominator.
Derived and synthetic items never increase natural coverage.

The exact TSV vocabulary, formulas, missing-data treatment and synthetic
verification fixture are defined in [qualification inventory and metrics](qualification-metrics.md).
Criteria, inventory identity and thresholds are frozen before evaluation and
versioned rather than changed retrospectively.

## Decision outcomes

- `CONTINUE`: the stated implementation tranche is justified.
- `QUALIFICATION_STOPPED`: an authorised, non-empty natural inventory has no
  eligible cases after every classification is resolved, or conclusively fails
  a precommitted threshold; the affected value path stops before runner
  construction or use.
- `PAUSED`: a named required input is unavailable.
- `INCONCLUSIVE`: evidence is insufficient; this is not a PASS.

The separate current implementation decision is `CONTINUE` only for the
bounded `PQ-2` toolkit and disposable-lab tranche. `INVESTMENT_APPROVED` is
not granted, and natural qualification is `INCONCLUSIVE`.
Site execution, customer work, sharing and publication remain unauthorised.

## Role separation

The same person may hold compatible roles, but the authority remains distinct:

| Role | Decision owned |
|---|---|
| Sponsor | Investment tranche and final product decision |
| Workload owner | Natural denominator and business relevance |
| Data owner | Permitted data, processing and locations |
| Technical owner | Build, safety and correctness gates |
| Operation owner | Site window, resources and recovery |
| Operator and deputy | Authorised execution and practical handover |
| Evidence owner | Storage, access, retention and export |
| Independent reviewer | Critical evidence and false-PASS review |
| Stop authority | Immediate stop; restart still requires a new GO |

No sponsor or author may override a technical, operational or data veto.
An implementer cannot independently approve their own critical evidence. A
stopped activity requires new technical and operation approval before restart.

The [current implementation qualification](current/implementation-qualification.md)
assigns every unresolved input to a required owner role and blocked gate. It
does not invent people for roles that have not accepted them.

## Templates

- [Qualification charter](templates/qualification-charter.md)
- [Natural workload inventory](templates/natural-workload-inventory.tsv)
- [Qualification decision](templates/qualification-decision.md)
- [Qualification inventory and metrics](qualification-metrics.md)
- [Corpus qualification and typed oracle](PGQI-5-corpus-oracle.md)
- [Corpus qualification JSON example](templates/corpus-qualification.example.json)
- [Provider-free baseline manifest example](templates/baseline-manifest.example.json)
- [Current implementation qualification](current/implementation-qualification.md)
