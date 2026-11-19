#!/usr/bin/env python3
"""Create disposable, externally anchored site-preflight dossiers for CI only."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/phase5"))
import site_preflight as policy  # noqa: E402

parser = argparse.ArgumentParser()
for name in ("commit", "tree_sha", "postgres", "pg_qi", "pg_duckdb", "config_sha",
             "major", "server_version", "database_oid", "role", "postmaster_start",
             "pg_qi_version", "pg_duckdb_version", "cluster_id", "boot_id", "socket",
             "port", "rootcert", "rootcert_sha", "socket_uid"):
    parser.add_argument("--" + name.replace("_", "-"))
parser.add_argument("--kind", choices=("socket", "tls"), required=True)
parser.add_argument("--duckdb-linkage", choices=tuple(sorted(policy.DUCKDB_LINKAGE_MODES)),
                    default="static_in_pg_duckdb")
parser.add_argument("--duckdb")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

if args.duckdb_linkage == "dynamic_separate_library" and not args.duckdb:
    parser.error("--duckdb is required for dynamic_separate_library")
if args.duckdb_linkage == "static_in_pg_duckdb" and args.duckdb:
    parser.error("--duckdb is forbidden for static_in_pg_duckdb")

sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
build = {"production": True, "pg_qi_testing": False, "postgresql_major": int(args.major),
         "postgresql_source_sha256": "a" * 64, "pg_duckdb_source_commit": "b" * 40,
         "duckdb_source_commit": "c" * 40, "duckdb_linkage": args.duckdb_linkage,
         "server_build_sha256": sha(args.postgres), "pg_qi_sha256": sha(args.pg_qi),
         "pg_duckdb_sha256": sha(args.pg_duckdb),
         "duckdb_sha256": sha(args.duckdb) if args.duckdb else "d" * 64,
         "configuration_sha256": args.config_sha, "toolchain_sha256": "e" * 64}
installed = {"postgres_path": args.postgres, "postgres_sha256": build["server_build_sha256"],
             "pg_qi_path": args.pg_qi, "pg_qi_sha256": build["pg_qi_sha256"],
             "pg_duckdb_path": args.pg_duckdb, "pg_duckdb_sha256": build["pg_duckdb_sha256"],
             "duckdb_sha256": build["duckdb_sha256"], "configuration_sha256": args.config_sha}
if args.duckdb:
    installed["duckdb_path"] = args.duckdb
start = {"install_event_id": "ci-install-1", "start_id": "placeholder",
         "install_sequence": 1, "start_sequence": 2, "cluster_id": args.cluster_id,
         "boot_id": args.boot_id, "postmaster_start_time": args.postmaster_start,
         "postgres_sha256": installed["postgres_sha256"],
         "pg_qi_mapped_sha256": installed["pg_qi_sha256"],
         "pg_duckdb_mapped_sha256": installed["pg_duckdb_sha256"],
         "duckdb_dependency_sha256": installed["duckdb_sha256"],
         "configuration_sha256": args.config_sha,
         "shared_preload_libraries": ["pg_duckdb", "pg_qi"],
         "hook_inventory": ["pg_duckdb", "pg_qi"]}
if args.duckdb:
    start["duckdb_mapped_path"] = args.duckdb
start["start_id"] = policy._start_id(start["cluster_id"], start["postmaster_start_time"])
start["attestation_sha256"] = hashlib.sha256(policy.canonical_bytes(start)).hexdigest()
if args.kind == "socket":
    endpoint = {"kind": "private_socket", "socket_directory": args.socket,
                "socket_owner": args.role, "socket_owner_uid": int(args.socket_uid),
                "socket_mode": 0o700, "port": int(args.port),
                "database": "postgres", "role": args.role, "credential_channel": "peer"}
else:
    endpoint = {"kind": "tls_tcp", "host": "localhost", "hostaddr": "127.0.0.1",
                "port": int(args.port), "database": "postgres", "role": args.role,
                "sslmode": "verify-full", "sslrootcert_path": args.rootcert,
                "sslrootcert_sha256": args.rootcert_sha, "gssencmode": "disable",
                "credential_channel": "anonymous_fd", "passfile_disabled": True}
expected = {"server_version_num": args.server_version, "database_name": "postgres",
            "database_oid": args.database_oid, "session_user": args.role,
            "current_user": args.role, "postmaster_start_time": args.postmaster_start,
            "start_id": start["start_id"], "cluster_id": start["cluster_id"],
            "pg_qi_version": args.pg_qi_version, "pg_duckdb_version": args.pg_duckdb_version,
            "routing_mode": "off",
            "duckdb_force_execution": "off", "pg_qi_enabled": "on"}
build_sha = hashlib.sha256(policy.canonical_bytes(build)).hexdigest()
cells = [{"cell_id": cell, "result": "PASS", "evidence_sha256": "f" * 64}
         for cell in sorted(policy.REQUIRED_REPLAY_CELLS)]
dossier = {"schema_version": 1, "document_type": "site_qualification",
           "qualification_id": "ci-site-preflight", "scope_sha256": "0" * 64,
           "source": {"commit": args.commit, "dirty": False, "tree_sha256": args.tree_sha},
           "build": build, "installed": installed, "start": start, "endpoint": endpoint,
           "expected_online": expected,
           "candidate_replay": {"source_commit": args.commit, "build_sha256": build_sha,
                                "cells": cells}}
args.output.write_bytes(policy.canonical_bytes(dossier))
print(json.dumps({"dossier_sha256": hashlib.sha256(policy.canonical_bytes(dossier)).hexdigest(),
                  "start_sha256": start["attestation_sha256"], "scope_sha256": "0" * 64},
                 sort_keys=True))
