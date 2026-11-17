# PQ-4 protocol and memory spike report

Date: 2026-11-17

Profile: disposable synthetic lab

Decision: accept the ADR 001 architecture for subsequent toolkit work

## Tested implementation

The spike uses the Python standard-library supervisor and the standalone
C/libpq driver described by [ADR 001](adr/001-pilot-runtime-and-protocol.md).
It was built with the private PostgreSQL 16 toolchain and run against a fresh
PostgreSQL 16.15 temporary cluster preloading `pg_duckdb` and `pg_qi`. The
cluster, table and data were synthetic and were removed by the harness.

The primary extended-protocol literal returned and independently decoded the
complete 1,000-row, two-column multiset. The same connection then recorded a
fresh planning tuple whose decision ID advanced from 1 to 2. It contained
`shadow_context=proven_client`, `request_delegation`, `delegation_verified`,
`qi_stack`, classifier status/reason `candidate`, engine `duckdb`, verification
`verified`, downstream DuckDB-scan observation and request applied `on`, while
ambient `duckdb.force_execution` was `off`. No diagnostic statement ran between
subject completion and those bounded `SHOW` reads.

## Results

| Case | Result | Observed evidence |
|---|---|---|
| Extended protocol, zero parameters | PASS | complete multiset and fresh snapshot |
| Named prepared wire lifecycle | PASS | `PQsendPrepare` + `PQsendQueryPrepared`, complete multiset |
| Simple protocol qualification | PASS in this lab only | separately labelled; not selected as default |
| Custom and generic parameter controls | PASS | psql extended `bind`; `unsupported_parameter`, request off |
| 1,200,000-row stream | PASS | 84,088,896 payload bytes; complete streaming oracle |
| Memory envelope | PASS | result exceeded 64 MiB address-space limit; measured peak RSS 8,816 KiB |
| Row, payload-byte, column and message limits | PASS | each ended `LIMIT_EXCEEDED` |
| Maximum field and spool limits | PASS | each ended `LIMIT_EXCEEDED` |
| Error after partial stream | PASS | 99 rows then SQLSTATE `22012`, terminal `FAILED` |
| Query deadline and PostgreSQL 16 cancellation | PASS | bounded cancel succeeded; matching active sleep became absent |
| Stalled connect envelope | PASS | non-responsive Unix endpoint timed out; no retry or fallback |
| Unresponsive child fault | PASS | supervisor escalated TERM to KILL within its envelope |

Limit outcomes retain partial spool evidence but never emit `COMPLETE` and are
not accepted as a truncated success. The deadline case attempts cancellation
only for its owned connection. The supervisor process boundary bounds the
potentially blocking PostgreSQL 16 cancellation path; a forced client exit
would still not prove backend retirement. This lab separately checked that the
matching sleeping query was no longer active; the later recovery gate still
owns general session-retirement proof.

## Query-integrity findings

The selected driver sends the immutable SQL bytes without adding parameters,
`LIMIT`, pagination, cursor declarations or SQL `PREPARE`/`EXECUTE`. It has no
auto-prepare, retry or fallback path. Named preparation is an explicit wire
lifecycle. Parameter controls use extended protocol and remain conservative;
they do not create a routed request.

## Scope and limitations

This spike qualifies an implementation mechanism on one synthetic PostgreSQL
16 build. It does not qualify a site, customer workload, natural relevance,
production use, a shared pool, a proxy, other PostgreSQL majors or a specific
resource/cancellation policy. Raw generated results remain local and ignored by
Git. Reproducible commands are in `tests/phase5/run-protocol-spike.sh`.
