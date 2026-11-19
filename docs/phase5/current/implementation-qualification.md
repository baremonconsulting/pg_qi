# PGQI-5 current implementation qualification

Status: IMPLEMENTATION AUTHORISED; NATURAL QUALIFICATION INCONCLUSIVE; RUNTIME NOT AUTHORISED

## Scope

The controlled decision is
[`PQ-2`](../decisions/PQ-2-implementation-authorisation.md). It permits the
bounded offline and disposable synthetic-lab implementation tranche. It does
not permit acquisition of an unspecified workload, site or customer activity.

## Current roles

| Role | Current authority |
|---|---|
| Repository owner / product sponsor | Granted the bounded implementation tranche |
| Implementer | May implement and run disposable synthetic-lab checks |
| Independent reviewers | Five reviews are required for each task |
| Workload owner | UNASSIGNED; natural inventory is not authorised |
| Data owner | UNASSIGNED; non-synthetic data use is not authorised |
| Technical owner for a site | UNASSIGNED; site technical GO is not granted |
| Operation owner, operator and deputy | UNASSIGNED; runtime operation is not authorised |
| Evidence owner | UNASSIGNED; external evidence storage or sharing is not authorised |
| Stop authority for a site | UNASSIGNED; no site window may begin |

The implementer cannot sign off their own critical evidence. The sponsor
cannot override a technical, operation or data veto. Anyone may invoke STOP
for a safety or evidence-boundary breach. Restart requires a closed cause,
revalidated current scope and preflight, and new technical and operation
approval.

## Unknown-input register

| Unknown input | Owner role required | Blocked gate | Closure evidence |
|---|---|---|---|
| Natural inventory source and permitted fields | Workload owner | `QUALIFICATION_AUTHORIZED` | Approved source, fields and frozen inventory digest |
| Natural relevance thresholds | Sponsor and workload owner | `INVESTMENT_APPROVED` based on relevance | Versioned, precommitted metric and thresholds |
| Dataset and processing authority | Data owner | `DATA_EVIDENCE_AUTHORIZED` | Classification, locations, retention and approval |
| Site, build, session model and hooks | Technical owner | `SCOPE_AUTHORIZED` and `RELEASE_SITE_QUALIFIED` | Passed site preflight for exact identities |
| Resource feed, units and stop limits | Operation owner | `SCOPE_AUTHORIZED` and `OPERATION_GO` | Approved limits and resource probe |
| Evidence store, ACL and retention | Evidence owner and data owner | `DATA_EVIDENCE_AUTHORIZED` | Access test and retention/deletion rule |
| R1 session/cache retirement | Technical and operation owners | `RECOVERY_REHEARSED` | Tested R1 retirement and default-off reconnect evidence |
| R2 binary downgrade or environment restore | Technical and operation owners | `RECOVERY_REHEARSED` | Tested exact binary/config downgrade or verified environment restore evidence |
| Operation window, operator, deputy and stop authority | Operation owner | `OPERATION_GO` | Named acceptance for the exact window |

These owners are intentionally role names, not invented people. Each remains
unassigned until a separate controlled record identifies and records the
accepting person.

## Gate disposition

- `QUALIFICATION_AUTHORIZED`: NOT_GRANTED
- `INVESTMENT_APPROVED`: NOT_GRANTED; the separate bounded implementation
  mandate in `PQ-2` does not satisfy this natural-relevance gate
- `SCOPE_AUTHORIZED`: NOT_GRANTED
- `DATA_EVIDENCE_AUTHORIZED`: NOT_GRANTED outside disposable synthetic data
- `RELEASE_SITE_QUALIFIED`: NOT_GRANTED
- `TOOLKIT_ACCEPTED`: NOT_GRANTED
- `RECOVERY_REHEARSED`: NOT_GRANTED
- `OPERATION_GO`: NOT_GRANTED
- `INTERNAL_CLOSEOUT`: NOT_GRANTED
- `CUSTOMER_PILOT_MANDATE`: NOT_GRANTED
- `ORACLE_QUALIFIED`: NOT_GRANTED for any customer scope
- `RESOURCE_ACCEPTED`: NOT_GRANTED for any customer scope
- `CUSTOMER_OPERATION_GO`: NOT_GRANTED
- `SHARE_RELEASE`: NOT_GRANTED for customer material
- `PUBLICATION_RELEASE`: NOT_GRANTED

Natural workload relevance and customer value remain `INCONCLUSIVE`.
P5-8 customer execution is explicitly `NOT_RUN`; no customer, mandate, sponsor
decision or customer environment has been supplied or touched.
