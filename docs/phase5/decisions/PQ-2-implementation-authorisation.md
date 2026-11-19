# PGQI-5 implementation authorisation

Status: GRANTED FOR THE BOUNDED SCOPE BELOW

## Controlled decision

- Decision reference: `PQ-2`, repository-owner instruction dated 2026-11-17
- Decision authority: repository owner and product sponsor
- Branch: `phase5/controlled-pilot`
- Authorised tranche: implement the complete PGQI-5 epic, its documentation,
  offline tooling and disposable synthetic-lab verification
- Required quality gate: five independent reviews for every implementation task
- Completion boundary: all epic deliverables are implemented and reviewed, then
  presented for human review before any merge or activation decision

This is an implementation mandate. It is not evidence of natural workload
relevance and does not grant `QUALIFICATION_AUTHORIZED`, `SCOPE_AUTHORIZED`,
site access, customer execution, installation, restart, routing, evidence
sharing, publication or production use.

Any task that requires one of those authorities must produce the conditional
mechanism and stop before the unauthorised action. Missing site inputs cannot
be replaced with assumed values.

The implementation tranche stops on a safety or data-authority blocker, a
failed task review that cannot be corrected within scope, or completion of the
epic for human review. A later runtime activity requires a separate controlled
decision for its exact environment and window.
