# ADR 001: pilot runtime and PostgreSQL protocol

Status: accepted for the external PGQI-5 toolkit

## Decision

Use a Python standard-library supervisor and a separate C/libpq connect-only
driver. The driver receives one immutable SQL file, opens one explicit
connection, sends the statement once and streams rows in libpq single-row mode
to a bounded length-prefixed spool. The supervisor owns the wall-clock and
address-space envelope, bounded stdout/stderr drains and terminates the driver
process group if it does not reach a terminal record in time.

The primary positive path is extended protocol with zero parameters through
`PQsendQueryParams`. Simple protocol is a separately labelled qualification
mode. A named prepared lifecycle uses `PQsendPrepare` and
`PQsendQueryPrepared`; SQL `PREPARE` or `EXECUTE` is not substituted for the
wire protocol.

## Safety and semantic boundary

- No SQL rewriting, parameter insertion, `LIMIT`, cursor, pagination,
  auto-prepare, retry or fallback is permitted.
- Truncation and every row, byte, field, column, message or spool limit are
  terminal non-PASS outcomes.
- Connection and query work are bounded by monotonic deadlines. The driver
  closes only its own connection. On PostgreSQL 16, where cancellation APIs may
  block, the separate supervisor provides a bounded process-level escape and
  records whether SIGTERM or SIGKILL was required. Client termination is not
  proof that server work retired; later recovery gates must verify that fact.
- A server error after streamed rows is a failure with partial evidence, never
  a complete result. The binary spool is accepted only with `COMPLETE`.
- The implementation does not modify the `pg_qi` routing core, extension SQL,
  hooks or production Makefile.

## Limits

Every invocation must provide positive finite maxima for rows, payload bytes,
columns, bytes per field, spool bytes, libpq result messages, driver virtual
address space, driver output, query deadline and supervisor termination grace.
Measured peak RSS is reported separately from the virtual-address-space limit.
The spool accounts
for framing as well as payload. It is flushed and synchronised only before a
successful terminal record.

## Rejected alternatives

- A Python database adapter was rejected because implicit transaction,
  preparation and protocol behaviour would add an avoidable qualification
  surface.
- A full C coordinator was rejected because policy, manifests, journalling and
  validation are safer and more maintainable in the Python standard library.
- `PQexec`, buffered result collection, SQL cursors and client-added `LIMIT`
  were rejected because they either defeat the memory bound or change the
  subject query.
- Threads around a blocking cancel call were rejected as the primary bound;
  process isolation gives the supervisor a deterministic last resort.
- Simple protocol was rejected as the default because multi-statement and
  statement-boundary semantics require separate qualification.

## Qualification status

The disposable synthetic lab report records which protocol, error, limit,
memory and deadline cases were actually run. Missing production identities or
site authority remain `NOT_RUN`; this ADR does not convert a lab result into a
site, customer or operation approval.
