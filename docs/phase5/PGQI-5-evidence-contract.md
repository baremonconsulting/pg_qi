# PGQI-5 evidence contract

This document defines the machine-enforced evidence boundary for controlled
pilot execution. `tools/phase5/contract_catalog.json` is the authoritative
field and enum catalogue; `contract_rules.py` is the normative parser and
semantic validator.

## Trust boundary

All documents are strict UTF-8 JSON objects. Duplicate keys, non-finite
numbers, Boolean values used as integers, unknown controlling fields,
oversized or excessively nested documents, path traversal and symbolic links
are rejected. Referenced artefacts carry a relative path, exact byte length
and SHA-256 of their exact bytes. Canonical JSON is used only for digests of
contract objects themselves.

`scope` closes the workload definition, release definition, target, limits,
evidence policy, recovery plan, export policy, toolkit, role bindings and
window inputs. These are immutable input artefacts, not the later workload,
release or recovery evidence contracts that refer back to the scope; this
separation prevents a digest cycle. An operation approval receipt
contains the canonical scope digest. It is deliberately not part of the scope,
so no digest cycle exists. Any byte change in a scope input invalidates its
artefact reference; updating that reference changes the scope digest and makes
the old approval inapplicable.

Approval receipts bind both the scope digest and a fresh preflight digest.
Approval receipts (`OPERATION_GO`, `CUSTOMER_OPERATION_GO`) and export receipts
(`SHARE_RELEASE`, `PUBLICATION_RELEASE`) have separate schemas and cannot be
substituted for one another.

## Gates and state

Gate identifiers are semantic constants from the catalogue. Lifecycle,
technical result, evidence completeness, exit state, value outcome and project
decision are independent axes. `CLOSED` is not synonymous with `PASS`.
Aggregate pass requires technical `PASS`, complete evidence, a clean exit and
a validated or closed lifecycle. A recorded failure remains a failure when
cleanup is incomplete.

Request execution status, planning evidence and snapshot relation are also
independent. Planning errors may retain a previous snapshot but cannot claim a
current one. Unavailable evidence has no snapshot. Prepared statements use the
tuple `(scope_sha256, session_id, prepared_object_id, prepared_generation)` as
a registry key; the original planning request identity is also preserved. In an
A, B, cached-A sequence, cached A refers to A's registered snapshot and not to
the most recently published B snapshot. Cached execution does not republish A:
a later planning error therefore retains B as the backend's previous snapshot.

## Seal order

The only valid order is:

1. write each payload completely and flush/fsync it;
2. construct the index from exact payload byte identities;
3. construct the terminal record over the canonical index digest;
4. create the external validation receipt over both canonical digests.

The index cannot contain itself, the terminal, validation receipt or export
receipt. Validation cannot promote a non-passing terminal. Export approval is
a later, separate gate.

## Verification

Private evidence that claims a workload result also includes an exact
`corpus_qualification` input, a `baseline_manifest` payload and both typed
spool artefacts for every oracle case. The frozen qualification bytes define
the workload inventory identity. Baseline case identifiers must equal the
execution corpus; unsupported and unexecuted natural items remain in the
coverage denominator.

Oracle PASS is recomputed from complete typed result multisets. Artefact
digests bind bytes but cannot substitute for parsing and comparison. Provider
off/disabled executions are controls, not provider-free baselines. The exact
format and limits are defined in [PGQI-5 corpus and oracle](PGQI-5-corpus-oracle.md).

Run `python3 tests/phase5/test_contracts.py`. The suite includes adversarial
parser inputs, scope mutation, approval mismatch, receipt separation,
planning-error and cached-plan attribution, dirty failure, false aggregate
pass, self-reference, symlink/traversal and post-index payload mutation.
