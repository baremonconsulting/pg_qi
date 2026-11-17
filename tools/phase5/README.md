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

## Release and site-preflight policy

`site_preflight.py` validates an exact source/build/install/start dossier,
single endpoint policy and per-cell candidate replay before a connector may be
called. It rejects ambient libpq/runtime influences and compiles a fixed
read-only online query manifest. Its optional execution mode invokes only the
typed `site-preflight-driver` without a shell; the driver has no activation or
workload interface. See `docs/phase5/PGQI-5-release-site-preflight.md`.
