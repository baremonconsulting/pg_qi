# One extension, one commercial platform

## The single-extension rule

There is exactly one PostgreSQL extension: `pg_qi`. The same source code, the same SQL objects, the same safety features and the same extension name for every user, whether or not they are a commercial customer.

Certified builds distributed by Baremon (for example RPM or DEB packages, or container images published under a name such as `baremon-pg_qi`) may differ in packaging name, build provenance and support terms — never in functionality. Inside the database it is always:

```sql
CREATE EXTENSION pg_qi;
```

There are no binary variants of `pg_qi`. This keeps bug reports unambiguous, upgrades predictable and the loaded library identity transparent.

## Open-source scope

All source code committed to this repository is licensed under Apache License 2.0. Once published under that licence, the granted rights are not withdrawn from existing versions.

The complete single-instance router is open source, including every safety mechanism: query classification, routing rules, eligibility policy, admission control, the execution watchdog, quarantine, manual whitelist and blacklist, explain reason codes, local decision telemetry, and the full off switch. Safety features are never paywalled. Everything needed to operate one instance safely lives in this repository.

## Commercial scope

The commercial product is the **Baremon Query Intelligence Platform**, together with certified builds, assessments and support. It runs outside the database and provides fleet-scale analytics, cross-instance comparison, ROI evidence, recommendations, governance (RBAC, SSO, audit, compliance exports) and central policy authoring with signed distribution.

The platform consumes `pg_qi` telemetry exclusively through the public, versioned telemetry contract. Proprietary source code, licensing mechanisms, private policy logic, customer-specific rules and commercial build material must not be committed to this repository.

## Dependency direction

```text
commercial platform  ─────►  deliberate public pg_qi interface (SQL / telemetry contract)
open-source pg_qi    ──X──►  commercial implementation
```

The open-source build must never require proprietary headers, source files, generated artefacts or credentials.

## Future proprietary database code

Commercial logic is designed to stay outside the database. If a compelling technical reason ever requires proprietary code to run inside PostgreSQL, it will ship as a clearly separate companion extension (for example `pg_qi_enterprise`, depending on `pg_qi`) — never as a modified or extended build of `pg_qi` itself. No such companion extension is currently planned.
