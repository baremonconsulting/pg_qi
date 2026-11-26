# PGQI-5 offline validator and exporter

The P5-5B tool is an offline trust boundary. It does not import database,
network or process-execution clients, does not dereference URLs and never
modifies its input bundle. Its results are evidence classifications, not an
operation, sharing or publication approval.

## Private-full profile

The private bundle contains exactly `scope.json`, `index.json`,
`terminal.json` and every file named by the index. Every scope input and
indexed payload is opened beneath the bundle root without following symbolic
links, captured once, and checked by exact byte length and SHA-256 from that
immutable in-memory snapshot. Unindexed or unexpected
files are invalid.

The graph requires one approval receipt, preflight, workload, release,
retirement and recovery record, plus request, snapshot and resource evidence,
the complete local oracle and the canonical site-cell set. `OPERATION_GO`
binds the scope and exact preflight digest. Each contract is linked to the same
scope. Requests and snapshots use the scope/session/prepared-plan registry.
Workload SQL and retained oracle bytes must be indexed.

The result classes are deliberately distinct:

- `PASS`: the complete graph, oracle, mandatory site cells, cleanup and
  terminal state agree;
- `FAIL`: a measured technical, oracle, site or cleanup failure is preserved;
- `INCOMPLETE`: required evidence, a mandatory execution, raw retention or a
  conclusive state is absent;
- `INVALID`: structure, schema, linkage, hash, allow-list or credential rules
  are violated.

The validation receipt is created outside the bundle with exclusive-create
semantics. Missing raw retention may produce a truthful local `INCOMPLETE`
receipt; it never becomes a reproducibility claim.

Private validation requires the approved scope SHA-256 as an external trust
anchor. Recomputing a modified baseline, scope, index and terminal cannot move
that trust anchor and is therefore rejected. The exporter obtains the same
anchor from the separately stored private validation receipt.

## Shared-derived profile

The exporter creates a new directory and writes only:

- `manifest.json`;
- `results.json`;
- `site-summary.json`;
- `index.json`;
- `terminal.json`.

It constructs these files from typed identifiers and result enums. It does not
copy the private tree, SQL, result bytes, logs, diagnostics or paths. The
manifest binds the source scope and the exact external private validation
receipt and states `raw_reproducibility=NOT_CLAIMED`.

Shared validation checks only this allow-list, its seal and its private-receipt
link. It cannot and does not claim to have inspected hidden raw evidence.

## Refusal and durability rules

- Duplicate keys, invalid UTF-8, unknown fields, unsafe names, symlinks,
  special files, traversal, oversized/deep values and invalid enums are
  rejected.
- Credential-like content is rejected even in private raw/debug artefacts.
  Recognised ZIP, tar, gzip, bzip2, xz, 7z, RAR and zstd containers are
  rejected rather than inspected incompletely; evidence inputs must be plain.
- A destination that already exists is never reused.
- Export payloads are written with exclusive creation and fsynced before the
  index and terminal record; the directory is fsynced last.
- Partial receipts and exporter output created by the current invocation are
  removed after an error. Files and containing directories are fsynced before
  success is reported. Inputs and pre-existing paths are never removed.

Run all offline tests with:

```sh
python3 tests/phase5/test_contracts.py
python3 tests/phase5/test_evidence_tool.py
python3 tests/phase5/test_offline.py
```
