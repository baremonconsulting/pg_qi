# PGQI-5 stop, resource and fault controls

This pilot control plane is external to the PostgreSQL routing hook. It owns
request admission, a mandatory local resource feed, the durable request
journal, bounded cancellation and exact backend retirement observations.

## Fail-closed dispatch

`StopController` is the single admission owner. A dispatch intent is appended
and fsynced while holding its admission gate. The first stop is irreversible,
is journalled with the admitted inflight intent sequences, and prevents every
later intent. There is no retry or fallback path.

The approved feed is `LocalProcResourceFeed`: a versioned `resource_policy` plus ordered
`resource_sample` documents. The sampler measures the controller and its
descendant driver/helper process tree and the evidence directory. Both documents
bind the scope digest, source identity,
metric names and units. Missing, stale, reordered, future-dated or
wrong-scope samples stop admission. A `ResourceWatchdog` checks feed age within
`min(interval_ms, stop_overshoot_ms)`. RSS, CPU, evidence size and inflight
request limits all produce a truthful hard stop.

Control files must be owned by the current user, regular, non-symlink files
with mode `0600`. JSON parsing rejects duplicate keys, non-finite numbers,
invalid UTF-8 and schema additions.

## Deadlines and cancellation

Connect, query and cancel have separate deadlines. Cancellation runs in a
dedicated helper process. When libpq cancellation blocks, the parent kills the
helper at `cancel_deadline_ms` and reports `cancel_timed_out=true`. Closing a
client socket is never treated as backend retirement.

The `protocol-driver-fault` Make target exists only for tests. The production
driver contains no faultpoint. Integration tests cover connect, query and
cancel blackholes.

## Ownership and retirement

The registry binds each owned session to cluster identity, postmaster start
identity, PID and `backend_start`. `application_name` is not ownership proof.
Only an exact match is eligible for a targeted operation; the registry exposes
no broad kill operation. A clean retirement result requires an authorised
observer, unchanged cluster/start identity and observed absence of every exact
owned backend. Otherwise the result remains `UNKNOWN` and
`backend_absence_verified=false`.

## Failure and recovery semantics

SIGINT and SIGTERM latch fixed, sanitised reasons. Journal failure latches an
in-memory `JOURNAL_FAILURE` even when that reason cannot itself be persisted.
After SIGKILL or a torn write, recovery verifies only the hash-linked durable
prefix and always returns `INCOMPLETE`; later retirement proof is still
required. Raw partial evidence is retained.

Console and shared outputs contain fixed reason codes rather than exception,
connection or SQL text. Private evidence remains the source for detailed
diagnostics. No error path may derive PASS from a partial journal, a client
timeout, socket close or an unverified backend observation.

## Operator sequence

1. Materialise the approved policy from
   `templates/resource-policy.example.json`, bind its scope digest and store it
   as a private `0600` file.
2. Start the local sampler and watchdog before admitting any request.
3. Register the exact backend identity when a session is created.
4. Dispatch only through `AuditedSession` with the controller supplied.
5. On stop, wait for or cancel admitted inflight requests within their bounds.
6. Obtain retirement evidence from an authorised observer. If it is missing,
   publish `UNKNOWN`, never CLEAN.
7. Validate/export the retained private evidence. A recovered journal prefix
   remains INCOMPLETE.

PQ-10 makes `--cancel-deadline-ms` mandatory for the protocol driver, audited
session driver and supervisor. Operator scripts created before PQ-10 must add
that argument explicitly; omission is refused rather than assigned an implicit
timeout.
