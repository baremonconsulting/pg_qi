# PGQI-5 release identity and site-preflight boundary

P5-2 qualifies an exact candidate and prepares a connection-bound, read-only
preflight. It does not install software, restart PostgreSQL, activate routing,
dispatch workload SQL or grant `OPERATION_GO`.

## Evidence chain

Qualification keeps four identities separate:

1. source commit, clean tree and tree digest;
2. production build, toolchain, PostgreSQL major and dependency digests;
3. resolved installed paths and byte identities;
4. an externally trusted install-to-new-start attestation that binds the
   postmaster start, cluster/boot identity, mapped pg_qi/pg_duckdb identities,
   configuration, preload order and hook inventory.

An on-disk digest is not accepted as proof of an already mapped library. A
missing or untrusted start attestation refuses qualification. Restart, build,
configuration, preload, hook, database or role changes require a new dossier,
preflight and approval.

The caller supplies separate trusted scope, complete-dossier and start-
attestation SHA-256 anchors. Changing an internally consistent endpoint,
candidate, replay or start record without renewing its external anchor is
therefore rejected before a connector is called.

## Connection policy

The dossier contains one typed endpoint, never a URI or conninfo string:

- TLS uses one approved certificate hostname, one literal address,
  `sslmode=verify-full`, an explicit approved CA path and digest, disabled GSS fallback and an
  anonymous credential FD supplied later by the connection adapter. Ambient
  client-certificate, key and CRL parameters are cleared, and libpq runs with
  a process-private empty home directory so home-directory defaults cannot be
  discovered or used.
- A private socket uses one absolute directory, restrictive approved mode and
  peer authentication. It is not a fallback for TLS.

All inherited `PG*` variables and selected OpenSSL/dynamic-loader variables
cause offline refusal. Service files, passfiles, options, URI expansion,
multi-host lists and implicit endpoint search are outside the schema. Generated
libpq parameters contain no password; the driver points libpq to a private,
mode-0600 empty passfile instead of a device or user-home default.
The credential descriptor must resolve to an anonymous pipe; regular files and
named FIFOs are rejected before any connection attempt.
The adapter hashes the bounded CA input into a sealed in-memory file and gives
libpq that immutable capture, so a path replacement or in-place write after
verification cannot change the trust root. The private-socket adapter verifies
the approved owner UID and rejects group/world-writable directories.

## Fixed online checks

The compiled manifest begins with routing and ambient-force `SHOW` checks and
then uses only fixed, catalog-qualified read-only identity queries. The
connection adapter must execute the exact ordered manifest with bounded
protocol limits. A fresh cryptographic nonce plus the observed backend PID and
backend start bind each result to one connection. The observed system
identifier and postmaster start are compared with the externally attested
start identity. Preload order and hook inventory remain in that privileged,
externally anchored start attestation; the ordinary application role is not
granted `pg_read_all_settings`. An observation is accepted only with zero activation attempts. No `SET`, workload
probe or retry belongs to this interface.

`site-preflight-driver` is the narrow C/libpq adapter for these checks. It
accepts typed endpoint fields rather than a conninfo string, reads a TLS
credential only from an inherited anonymous descriptor, disables passfile and
GSS fallback, uses `sslmode=verify-full`, and emits observations only after the
complete fixed query sequence succeeds. The P5-1B general laboratory
`--conninfo` interface is not a site interface.

The CI integration starts a disposable PostgreSQL server, exercises both a
private socket and certificate-verified TLS, proves hostname mismatch refusal,
checks the recorded SQL allowlist, and scans the evidence for a credential
canary. Production qualification still requires the externally anchored
dossier and start attestation; this disposable integration is not production
identity evidence.

## Candidate replay

Replay is exact-candidate evidence, not a green summary badge. It requires all
18 PGQI-4C cases and relevant core CI cells for PostgreSQL 16, 17, 18 and the
pinned PostgreSQL 19 source build. Every cell carries its own evidence digest.
Missing or `NOT_RUN` is `INCOMPLETE`; a measured failure remains `FAIL`; extra,
duplicate or wrong-candidate cells are invalid.

Run the policy tests without a database or network:

```sh
python3 tests/phase5/test_site_preflight.py
```

Run the adapter integration against a disposable local server:

```sh
make -C tools/phase5 clean all
tests/phase5/run-site-preflight-integration.sh /tmp/pgqi-site-preflight
```

