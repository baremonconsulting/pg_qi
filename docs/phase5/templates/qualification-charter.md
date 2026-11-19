# PGQI-5 qualification charter

Status: DRAFT — not an operation approval

## Decision scope

- Charter ID:
- Revision:
- Stage: qualification / implementation-lab / site-preparation
- Purpose:
- Explicit exclusions:
- Start condition:
- Stop condition:
- Maximum authorised implementation tranche:

## Workload and value

- Workload owner:
- Authorised inventory source:
- Natural denominator definition:
- Frequency source and missing-data rule:
- Baseline-time source and missing-data rule:
- Eligibility hypothesis:
- Frozen metric and threshold revision:
- Minimum relevance needed for the next tranche:
- Value question and decision rule:

## Evidence origins and authorities

- Inventory origin, controlled source reference and authority:
- Executed SQL origin, controlled reference and execution authority:
- Dataset origin, controlled reference and data authority:
- Derivation method and parent inventory ID:
- Unknown or not-applicable fields and reasons:

## Data

- Data owner:
- Dataset profile: synthetic / sanitised / internal / customer
- Data location:
- Allowed temporary processing and spool:
- Prohibited content:
- Retention and verified deletion condition:

## Environment

- Site owner:
- PostgreSQL major/minor and build:
- OS and architecture:
- `pg_qi`, `pg_duckdb` and DuckDB identities:
- Session/pool model:
- Hook inventory:
- Resource feed and units:
- Resource and stop limits:
- R1 recovery: session/cache retirement and default-off reconnect:
- R2 recovery: exact binary/config downgrade or verified environment restore:

## Roles and availability

- Sponsor:
- Technical owner:
- Operation owner:
- Operator:
- Deputy:
- Evidence owner:
- Independent reviewer:
- Stop authority:

The implementer cannot approve their own critical evidence. The sponsor cannot
override a technical, operation or data veto. Anyone observing a safety or
evidence-boundary breach may invoke STOP. Restart requires a closed stop cause,
revalidation of the current scope and preflight, and new technical and
operation approval.

## Evidence governance

- Private evidence location and ACL:
- Shareable evidence recipients:
- Retention periods or end events:
- Incident channel:
- Sharing approval owner:
- Publication approval owner:

## Gate disposition

- `QUALIFICATION_AUTHORIZED`: NOT_GRANTED
- `INVESTMENT_APPROVED`: NOT_GRANTED
- `SCOPE_AUTHORIZED`: NOT_GRANTED
- Blocking unknowns (one row per item: owner role, blocked gate and closure evidence):
- Decision references:

This charter does not authorise `pg_qi.routing_mode=allowlist`, installation,
restart, customer execution, evidence sharing or publication.
