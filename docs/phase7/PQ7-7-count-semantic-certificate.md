# PQ7-7 — Scoped `COUNT(*)` semantic certificate

The certificate allows the V2 `safe` gate to report `pass` for one narrowly
qualified case. It is not a delegation rule. It does not change the plan or
the engine that runs a query, open the recommendation barrier, or enable
shadow mode. With no approved certificate, the behavior is unchanged.

## Qualified scope

- One built-in `COUNT(*)` over one ordinary, permanent PostgreSQL heap with
  the existing P0 catalog, permissions and row-security checks. `FROM ONLY`
  is excluded: the pinned provider declines its generated DuckDB SQL.
- One exact PostgreSQL release/build, `pg_qi`, `pg_duckdb` and DuckDB binary
  tuple and deployment site. The PostgreSQL cluster identifier and a unique
  `pg_qi.site_build_id` both have to match.
- A fresh, exact native plan with a nonparallel `Aggregate`/`SeqScan` spine,
  observed in a `REPEATABLE READ READ ONLY` transaction with the active MVCC
  snapshot attached to execution. Native parallel workers and provider scan
  workers must be disabled. Unsupported transaction, role, snapshot or plan state
  abstains. The lifetime of the original plan is bound to its allocation
  context, and the executing aggregate, relation and exact approved record are
  checked again. Replacing the record does not recertify a pending plan. Cache
  reuse without a fresh observation yields no new decision.
- No implicit certification of `COUNT(expression)`, `MIN`, `MAX`, queries
  with filters, or other table classes and plan shapes.

## Acquire and approve evidence

Run `tests/phase7/verify_count_certificate.sh MAJOR NEW_PRIVATE_OUTPUT` on a
disposable Linux installation with the pinned provider already built. Supply
`PG_CONFIG`, `PG_QI_BUILD_ID`, and `PG_QI_POSTGRES_BUILD_ID` for the exact
production variant. The latter two are build labels; the mapped binary
SHA-256s independently bind the actual executables. For a source capsule
without Git metadata, supply `PG_QI_SOURCE_ARCHIVE` for the verified archive
and use its SHA-1 as the declared source label. The runner creates
a private Unix-socket cluster; do not run the
acquisition against a live site. It reuses the PQ7-4 evidence driver and typed
oracle with native, externally forced and shadow arms. The matrix verifies
empty/null/mixed tables, dead and vacuumed tuples, HOT updates, aborted
inserts and a concurrent commit. An arm without the imported snapshot must
observe the changed cardinality, and its own receipt proves the same subject
on a separate backend and a different non-imported snapshot. DuckDB refusal is a negative control,
never a passing equivalence result. A given site/build is judged from its
own matrix; coverage on another PostgreSQL major is not transferable.
Every evidence connection verifies the same cluster, site, server version and
postmaster start time before executing its subject. The witness subject token
also binds the case's measured native baseline and fixture checks to its run.

The test installs the certificate only in its isolated cluster. To inspect
an evidence bundle separately, run:

```text
python3 tools/phase7/count_certificate.py /private/count-matrix-pg19.RUN_ID \
    --major 19 --revision PG_QI_BUILD_ID
```

The tool replays the raw per-arm receipts and spools and binds their hashes to
the exact runtime identity. It does not install anything or accept a summary
`PASS` as a substitute. To prepare an inactive record after the evidence has
been reviewed, rerun it with `--expected-digest REVIEWED_SHA256`,
`--issued-at UNIX_SECONDS` and `--expires-at UNIX_SECONDS`. The dates define
a half-open window. Evidence cannot be reissued past 90 days from its
recorded acquisition. Record both the reviewed evidence digest and the
separate approval hash; the runtime does not read or reverify the offline
bundle. Do not commit raw receipts, deployable
certificate records, or site IDs.

## Runtime acceptance and withdrawal

Only a deployment administrator may apply an approved record. The startup
configuration opts in with `pg_qi.count_certificate_attestation`, preloading
`pg_duckdb` before `pg_qi`, and assigns a deployment-unique
`pg_qi.site_build_id`. A copied cluster must be assigned a fresh site ID;
lab evidence cannot approve a different site. Do not run the synthetic
fixture acquisition on a production system. The attestation reads the binaries mapped by the
Linux postmaster once, binding the checked file to its mapped device and
inode. Keep mapped artifacts immutable and restart the site to replace them.
The qualified layout uses `pg_duckdb.so` and a separate `libduckdb.so`.
Unrecognized, ambiguous or unreadable mappings, recovery and test builds do
not certify. No file or
network access is performed in a planner or executor callback for a
certificate check.

The accepted record and its independently reviewed hash are separate
reloadable settings: `pg_qi.count_certificate` and
`pg_qi.count_certificate_approved_sha256`. An empty, malformed, unapproved,
different-build, different-site or expired record cannot publish a semantic
pass. Removing either setting, then successfully reloading, revokes new
observations. Confirm the effective settings after a reload; an unrelated
configuration error can prevent the reload from being applied. A previously captured decision remains historical and is not
an authorization for another query. The approval is a trust decision by the
deployment operator, not a cryptographic signature by the test harness.

Publication uses the existing authorized V2 diagnostics. A topology
fingerprint alone does not qualify copied plans. The certificate and snapshot
are checked again on the first execution callback rather than inferred from
planning; this does not claim that execution subsequently completed.
