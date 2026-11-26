# PGQI-5 corpus qualification and typed oracle

P5-4A supplies a bounded correctness oracle for a controlled pilot. It does
not authorise workload acquisition, site execution or customer processing.

## Corpus boundary

The version 1 `corpus_qualification` object has three distinct collections:

- `natural_inventory` is the full authorised denominator. `UNSUPPORTED`,
  `NOT_EXECUTABLE` and `UNKNOWN` entries stay in it;
- `execution_corpus` identifies the exact SQL and dataset lineage that may be
  compared. A derived reproduction can link to a natural item but cannot
  become a new natural item;
- `synthetic_controls` test mechanisms and never increase natural coverage.

Inventory, executed SQL and datasets each require their own authority record
with prior authorisation. Storage paths, mode `0700`, retention authority and
retention class must likewise be approved before acquisition. Concurrent
writes are outside v1 and are rejected.

The qualification freezes the comparator limits for spool bytes, rows,
columns, field bytes, in-memory rows and external-merge fan-in. Private
validation reuses those limits rather than validator defaults.

Coverage reports counts and confirmed/upper ratios over the complete natural
denominator. Frequency coverage is returned only when all weights are known
and their total is non-zero. An empty denominator produces `null` ratios, not
100 percent coverage.

## Provider-free baseline

The baseline is a separate static native PostgreSQL execution, not routing
with a provider disabled. Its manifest binds exact server build,
configuration, schema and data identities and asserts:

- provider identity `POSTGRESQL_NATIVE_NO_DELEGATING_PROVIDER`;
- verified absence of `pg_qi`, `pg_duckdb` and any delegating provider;
- static data and no concurrent writes;
- one exact typed spool identity for every execution-corpus case.

The same build, configuration, schema, data and provider identities must have
been authorised in the scope-frozen corpus qualification before acquisition.
The clean-instance attestation and its authority reference are mandatory; the
driver's runtime checks supplement rather than replace that attestation.

The lab driver mode `native_baseline` performs fixed provider-absence checks
before executing the reviewed SQL. It must be used only on a disposable
baseline instance. A failed or missing check cannot produce baseline proof.

## Typed spool `PGQSP001`

All integers are big-endian. The format is:

1. eight bytes `PGQSP001`;
2. unsigned 32-bit column count;
3. per column: unsigned 32-bit UTF-8 name length, name bytes and unsigned
   32-bit PostgreSQL type OID;
4. repeated rows: unsigned 32-bit field count followed by, for each column,
   unsigned 32-bit field length and field bytes. Length `0xffffffff` is NULL.

Version 1 accepts only OIDs 20 (`int8`), 21 (`int2`) and 23 (`int4`). Values
must use canonical decimal syntax and fit their PostgreSQL type. String casts,
unknown types, duplicate column names, malformed framing and trailing bytes
are rejected. An empty result still carries and compares its column metadata.

## Comparison and bounds

The oracle compares column names, order and types, then the complete multiset
of canonical typed rows. Order may differ; duplicate multiplicity may not.
Rows are sorted into bounded chunks below a private caller-owned `0700`
scratch root and merged externally. Source spools must be regular,
non-symlink, caller-owned files with no group or other permissions. Configured
limits bound spool bytes, rows, columns, field bytes and in-memory rows.

SHA-256 values protect artefact identity but are never the sole correctness
proof. The comparator reopens and parses both spools and verifies that their
file identities remain stable. PASS means the complete supported typed
multisets match; a difference is FAIL; malformed or unsupported evidence is
invalid rather than a measured FAIL.

Within a sealed private bundle, each observed spool must also be the exact
artefact bound by the corresponding successful request contract. A missing
request or acquisition binding makes the result incomplete and cannot yield
aggregate PASS. Oracle scratch files are created only below the scope-frozen,
caller-owned `0700` scratch root.

The controlled v1 bundle has exactly one executed request per corpus query.
Repeated attempts require distinct versioned corpus case identifiers; they
cannot overwrite or ambiguously share a query identity. Baseline and observed
artefacts must have distinct paths.

## Non-go scope

This v1 oracle does not cover text, floating point, numeric, temporal, JSON,
arrays or provider-specific types; concurrent mutation; nondeterministic
queries; lossy casts; production connections; or confidential SQL stored in
the repository. Adding a type requires a versioned canonical representation
and adversarial tests before it can support a PASS claim.

## Verification map

| Requirement | Automated evidence |
|---|---|
| Natural denominator and lineage separation | `QualificationTests` |
| Authority, storage and concurrent-write refusal | `QualificationTests` |
| Provider-free static baseline identity | `QualificationTests` and protocol lab |
| NULL, integer bounds, types and empty results | `OracleTests` |
| Reordering and exact duplicate multiplicity | `OracleTests` |
| Truncation, malformed values and unsupported types | `OracleTests` |
| No hash-only or forged PASS | `PrivateValidationTests` |
| Bounded external sort and private scratch | `OracleTests` and protocol lab |

Run `python3 -m unittest tests.phase5.test_corpus_oracle
tests.phase5.test_evidence_tool` and the PostgreSQL 16 protocol harness. The
CI workflow runs these checks from committed source.
