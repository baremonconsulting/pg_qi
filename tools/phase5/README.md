# PGQI-5 protocol spike tools

This directory contains the external protocol and memory spike selected by
ADR 001. It is not part of the PostgreSQL extension and does not alter routing
hooks or extension SQL.

Build the connect-only C driver against an explicit PostgreSQL installation:

```sh
make -C tools/phase5 PG_CONFIG=/path/to/pg_config clean all
```

Run the disposable PostgreSQL 16 synthetic lab:

```sh
PG_CONFIG=/path/to/postgresql/16/bin/pg_config \
  tests/phase5/run-protocol-spike.sh /private/new-result-directory
```

The harness refuses non-PostgreSQL-16 toolchains and existing result
directories. It builds the committed `pg_qi` source in a private temporary
copy, preloads that exact staged library into a marker-owned temporary cluster,
and removes the cluster on exit. It does not install or overwrite `pg_qi` in
the selected toolchain. Generated evidence is ignored by Git and must not be
interpreted as a site or customer approval.

The driver is a lab-only protocol mechanism, not a site runner or SQL safety
boundary. It can execute the one reviewed SQL file supplied by the harness and
must never receive untrusted SQL or a production connection. Every invocation
requires an explicit connection string, session/protocol mode, new spool and
snapshot paths, positive finite limits and deadlines. A binary spool is valid
only when the terminal state is `COMPLETE` and the offline verifier passes.

## Audited single-session path

`audited-session-driver` and `pilot_runtime.py` are the separate P5-3A path.
The coordinator synchronises a hash-chained dispatch intent before sending a
bounded versioned frame. The C/libpq process holds one connection and accepts
only execute, prepare, execute-prepared, deallocate, fixed BEGIN and close
opcodes. It resolves an immutable case identifier under a
private case registry, verifies its SHA-256 digest and never reconnects or
replays a request. Every request receives new exclusive spool and same-session
spool/snapshot files for subject executions and planning snapshots for prepare.
Session loss after intent is `INCOMPLETE`, never proof that the
request did not execute.

This controlled-pilot path does not install PostgreSQL, alter roles or
persistent settings, control cluster lifecycle, expose a general SQL CLI, or
imply `OPERATION_GO`.

## Offline evidence validation

`evidence_tool.py` validates sealed evidence without database, network or shell
access. `private_full` rechecks the complete local graph, oracle and individual
site cells. `shared_derived` checks only an allow-listed derived directory and
its link to an external private validation receipt; it never claims to have
revalidated hidden raw evidence.

```sh
python3 tools/phase5/evidence_tool.py validate \
  --profile private_full --bundle EVIDENCE \
  --trusted-scope-sha256 APPROVED_SCOPE_SHA256 \
  --receipt validation.json --receipt-id validation-1

python3 tools/phase5/evidence_tool.py export \
  --private-bundle EVIDENCE --private-receipt validation.json \
  --output SHARED-DERIVED
```

The input bundle and receipt are never modified. Validation receipts are
created exclusively, and an export destination must not already exist.

## Corpus qualification and typed oracle

`corpus_oracle.py` is the fail-closed P5-4A boundary. It keeps the natural
inventory, executable corpus and synthetic controls separate, validates the
provider-free static PostgreSQL baseline manifest, and compares complete typed
integer result multisets with an externally sorted, bounded-memory algorithm.
Raw SHA-256 equality is an integrity check, not an oracle result.

```sh
python3 tools/phase5/corpus_oracle.py qualify \
  --manifest qualification.json --output qualification-result.json
python3 tools/phase5/corpus_oracle.py validate-baseline \
  --manifest baseline.json --output baseline-result.json
python3 tools/phase5/corpus_oracle.py compare \
  --baseline raw/native.spool --observed raw/subject.spool \
  --scratch-root /private/0700/scratch --max-spool-bytes 1048576 \
  --max-rows 10000 --max-columns 32 --max-field-bytes 128 \
  --max-rows-in-memory 64 --output comparison.json
```

Only PostgreSQL `int2`, `int4`, `int8` and NULL fields are accepted in v1.
The baseline must come from the `native_baseline` session mode on a separate
static PostgreSQL instance with no `pg_qi`, `pg_duckdb` or delegating provider.
See `docs/phase5/PGQI-5-corpus-oracle.md` for the exact evidence contract and
limitations.

## Release and site-preflight policy

`site_preflight.py` validates an exact source/build/install/start dossier,
single endpoint policy and per-cell candidate replay before a connector may be
called. It rejects ambient libpq/runtime influences and compiles a fixed
read-only online query manifest. Its optional execution mode invokes only the
typed `site-preflight-driver` without a shell; the driver has no activation or
workload interface. See `docs/phase5/PGQI-5-release-site-preflight.md`.

## Frozen measurement and bounded concurrency

`measurement_runtime.py` executes one explicitly authorised concurrency level
from a measurement plan that is already part of the approved scope hash. It
uses three fixed session modes (`disabled`, `off`, and `allowlist`), verifies
all scenario correctness results before opening a measurement session, writes
every warm-up, failure, timeout and measured request to an exclusive bounded
JSONL stream, and retires every owned session. It never escalates concurrency
itself. A level above one requires the immediately preceding level's sealed
`private_full` PASS, clean terminal state, retirement evidence, and the exact
approved server-resource feed.

The raw interval starts immediately before protocol send and ends only after
the complete result stream is consumed. Snapshot, diagnostics, oracle and
evidence persistence are outside that interval and are reported separately.
The offline evidence validator recomputes counts, percentiles, suppression
status, variability and completeness from the indexed raw stream; a report is
not accepted as proof by assertion.

See `docs/phase5/PGQI-5-measurement-concurrency.md` and the frozen-plan example
in `docs/phase5/templates/measurement-plan.example.json`.

## Recovery rehearsal and site readiness

`recovery_rehearsal.py` is an offline validator for evidence produced by human
operators on an explicitly approved recoverable copy. It has no database,
process-control, pool-administration or restart interface. R1 session/cache
retirement, R2 binary/config downgrade or environment restore, the independent
operator walkthrough and incident handoff remain separate typed terminals.
The result can be only `ELIGIBLE_FOR_HUMAN_GRANT` or `REFUSED`; it never grants
`RECOVERY_REHEARSED` or `OPERATION_GO`.

Run it only after the five typed input documents and their referenced private
proofs have been collected:

```sh
python3 tools/phase5/recovery_rehearsal.py \
  --bundle /private/rehearsal-bundle \
  --output /private/recovery-validation.json
```

See `docs/phase5/PGQI-5-recovery-rehearsal.md` for the operator boundary,
ordered runbook and refusal conditions.
