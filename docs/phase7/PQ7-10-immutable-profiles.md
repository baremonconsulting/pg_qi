# PQ7-10 — immutable local model and resource profiles

PQ7-10 adds a bounded, backend-local profile observation for later benefit
model work. It does not evaluate the benefit model, reserve resources, open
the activation barrier or request delegation.

## Loading and atomicity

`pg_qi.model_profile` is one inline, canonical `PGC_SIGHUP` value. PostgreSQL
loads configuration outside statement planning. The assign hook parses the
entire value into a fixed-size local candidate and replaces the installed
candidate by one struct assignment. A planner call copies that installed
struct once; it never opens a file, contacts a service, allocates, parses or
uses SPI. Different backends may process SIGHUP at different command
boundaries, including between statements in one transaction, so every
observation records the generation, installation sequence and fingerprint it
actually used.

The supported operator pattern is an `include_if_exists` file containing a
single `pg_qi.model_profile` assignment, written through a same-directory
temporary file and rename. `tools/phase7/profile_generator.py` prints the
canonical value to standard output, or atomically writes a complete
`pg_qi.model_profile = '...'` assignment when `--output` is used. The profile
contains no secret. The setting is superuser-only, but superusers and roles
with PostgreSQL's settings-reading privileges can inspect its raw value.

An invalid profile value that reaches the assign hook replaces prior evidence
with an explicit unknown reason. A syntactically broken PostgreSQL
configuration file is rejected by PostgreSQL before any extension hook runs;
PostgreSQL then retains the previous configuration. Per-observation expiry is
the bounded fail-closed backstop for that server-level behavior.

## Format and bounds

PQ7-10 introduced format v1 as fixed-order ASCII `key=value` fields separated by semicolons and
ending in a lowercase CRC32C. Unknown, missing, duplicate or reordered fields,
noncanonical integers, overflow, embedded NUL, uppercase hexadecimal,
trailing bytes, invalid intervals and values over 2,048 bytes are rejected.
All quantities are unsigned integers with explicit units in their key names or
format definition. Floating point, locale-dependent parsing and unbounded
allocation are absent.

PQ7-11 supersedes the accepted payload with format v2 to add calibrated
benefit-model terms; format v1 remains historical and is rejected rather than
interpreted under changed units.

The bundle binds one generation and validity interval to:

- the exact PostgreSQL `PG_VERSION_NUM` and a nonzero 64-hex PostgreSQL build
  identity supplied at build time;
- a nonzero 40-hex pg_qi build identity supplied at build time;
- the compiled pg_duckdb and DuckDB source identities;
- a 64-hex deployment identity supplied as the postmaster-only
  `pg_qi.site_build_id` setting;
- the V2 contract, rule, model and certificate versions;
- the model coefficients and hypothetical memory, temporary-storage, thread
  and concurrency envelope.

The provider digests are declared build bindings, not proof of which binary is
currently mapped. Runtime provider attestation remains a separate semantic
certification concern. Default all-zero PostgreSQL or pg_qi build identities
can never produce `profile_present=true`.

Validity intervals are half-open (`issued_at <= now < expires_at`) and limited
to 90 days. Generation is recorded but not treated as a cross-backend or
cross-restart monotonic security boundary.

## Decision semantics

`profile_present=true` means that the bundle parsed, matched every supported
binding and is currently within its validity interval.
`limits_verified=false` is hard-wired in PQ7-10. The envelope is hypothetical;
the extension does not change memory, temp, thread or concurrency settings and
does not reserve resources. Consequently PQ7-10 cannot make the benefit gate
pass. Existing V2 reason precedence and recommendation behavior are unchanged.

Missing, stale, not-yet-valid, wrong-build, oversized and malformed profiles
remain explicit profile `UNKNOWN` observations.

## Private diagnostic record

`SHOW pg_qi.profile_evidence` follows the existing diagnostic-role and
producer-identity protections. A complete version-1 record contains the
atomic `cid/g/e` event identity, state, reason, `present`,
`limits_verified`, envelope kind, profile generation, backend-local install
sequence, CRC fingerprint and expiry. It never emits profile coefficients or
build/site identifiers.

## Verification

- The production parser is exercised by a deterministic 100,000-iteration
  ASan/UBSan mutation harness, including every seed truncation and the exact
  size boundary.
- The test extension checks fail-closed classification and proves that a valid
  profile still cannot set `limits_verified`.
- Live PostgreSQL 16–19 tests cover missing, valid A/B reload, wrong
  PostgreSQL, pg_qi and site builds, expired, malformed and oversized values,
  authorization, event identity and monotonic backend-local install sequence.
- Production builds continue to use the existing outer-hook and regression
  suites; routing and the activation barrier remain unchanged.
