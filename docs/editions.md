# One extension, one commercial platform

> [!NOTE]
> This document defines the intended editions model. Items marked **planned**
> do not exist yet. The production build does not classify or route queries;
> see [Current state](../README.md#current-state) in the README.

## The single-extension rule

There is exactly one production PostgreSQL extension: `pg_qi`. The same source code, the same SQL objects, the same safety features and the same extension name are used for every user, whether or not they are a commercial customer.

The repository also contains `pg_qi_spike_test`, a dependent extension built only in explicit test builds (`PG_QI_TESTING=1`) for the Spike 1 verification surface. It is not an edition and is not part of the default production installation; see [WP14](phase2/WP14-production-test-boundary.md).

Certified builds distributed by Baremon (for example RPM or DEB packages, or container images published under a name such as `baremon-pg_qi`) may differ in packaging name, build provenance and support terms — never in functionality. Inside the database it is always:

```sql
CREATE EXTENSION pg_qi;
```

There are no proprietary, feature-gated or functionally different binary variants of `pg_qi`. This keeps bug reports unambiguous, upgrades predictable and the loaded library identity transparent.

## Open-source scope

All source code committed to this repository is licensed under Apache License 2.0. Once published under that licence, the granted rights are not withdrawn from existing versions.

The complete single-instance router will be open source in this repository, including every safety mechanism. Safety features will never be paywalled. Most of the router is planned and not yet implemented:

| Component | Status |
|---|---|
| Planner-hook integration and `pg_duckdb` delegation adapter | Implemented and validated by Spike 1; the production build does not initiate delegation yet |
| Off switch (`pg_qi.enabled`) | Implemented; when disabled, the planner hook passes straight through |
| Query classification | Planned |
| Routing rules and eligibility policy | Planned |
| Admission control | Planned |
| Execution watchdog | Planned |
| Quarantine | Planned |
| Manual whitelist and blacklist | Planned |
| Explain reason codes | Planned |
| Local decision telemetry | Planned |

Once these components exist, everything needed to operate one instance safely will live in this repository.

## Commercial scope

The commercial product, the **Baremon Query Intelligence Platform**, is planned and not yet available. It is designed to run outside the database and to provide fleet-scale analytics, cross-instance comparison, ROI evidence, recommendations, governance (RBAC, SSO, audit, compliance exports) and central policy authoring with signed distribution.

The platform will integrate with `pg_qi` exclusively through deliberate, public and versioned telemetry and policy interfaces. These interfaces are planned and not yet defined. Baremon may also provide certified builds, assessments, support and professional services under commercial terms. Proprietary source code, licensing mechanisms, private policy logic, customer-specific rules and commercial build material must not be committed to this repository.

## Dependency direction

```text
commercial platform  ─────►  deliberate public pg_qi interfaces (SQL / telemetry / policy contracts)
open-source pg_qi    ──X──►  commercial implementation
```

The open-source build must never require proprietary headers, source files, generated artefacts or credentials.

## Future proprietary database code

Commercial logic is designed to stay outside the database. If a compelling technical reason ever requires proprietary code to run inside PostgreSQL, it will ship as a clearly separate companion extension (for example `pg_qi_enterprise`, depending on `pg_qi`) — never as a modified or extended build of `pg_qi` itself. No such companion extension is currently planned.
