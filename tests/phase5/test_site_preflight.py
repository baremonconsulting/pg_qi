#!/usr/bin/env python3
"""Adversarial tests for the P5-2 release and site-preflight boundary."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/phase5"))
SPEC = importlib.util.spec_from_file_location("site_preflight", ROOT / "tools/phase5/site_preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(preflight)

ZERO = "0" * 64
COMMIT = "a" * 40


def dossier(endpoint_kind: str = "tls_tcp") -> dict:
    build = {"production": True, "pg_qi_testing": False, "postgresql_major": 19,
             "postgresql_source_sha256": "b" * 64,
             "pg_duckdb_source_commit": "c" * 40, "duckdb_source_commit": "d" * 40,
             "duckdb_linkage": "static_in_pg_duckdb",
             "server_build_sha256": "9" * 64, "pg_qi_sha256": "1" * 64,
             "pg_duckdb_sha256": "2" * 64, "duckdb_sha256": "3" * 64,
             "configuration_sha256": "4" * 64, "toolchain_sha256": "5" * 64}
    build_sha = hashlib.sha256(preflight.canonical_bytes(build)).hexdigest()
    endpoint = ({"kind": "tls_tcp", "host": "db.internal.example", "hostaddr": "192.0.2.10",
                 "port": 5432, "database": "pilot", "role": "pilot_reader",
                 "sslmode": "verify-full", "sslrootcert_path": "/etc/pgqi/ca.pem",
                 "sslrootcert_sha256": "6" * 64,
                 "gssencmode": "disable", "credential_channel": "anonymous_fd",
                 "passfile_disabled": True} if endpoint_kind == "tls_tcp" else
                {"kind": "private_socket", "socket_directory": "/run/postgresql-private",
                 "socket_owner": "postgres", "socket_owner_uid": 1000,
                 "socket_mode": 0o700, "port": 5432,
                 "database": "pilot", "role": "pilot_reader", "credential_channel": "peer"})
    cells = [{"cell_id": identity, "result": "PASS", "evidence_sha256": "7" * 64}
             for identity in sorted(preflight.REQUIRED_REPLAY_CELLS)]
    start = {"install_event_id": "install-1", "start_id": "placeholder",
             "install_sequence": 10, "start_sequence": 11, "cluster_id": "cluster-1",
             "boot_id": "boot-1", "postmaster_start_time": "2026-11-17T10:00:00Z",
             "postgres_sha256": "9" * 64, "pg_qi_mapped_sha256": "1" * 64,
             "pg_duckdb_mapped_sha256": "2" * 64, "duckdb_dependency_sha256": "3" * 64,
             "configuration_sha256": "4" * 64,
             "shared_preload_libraries": ["pg_duckdb", "pg_qi"],
             "hook_inventory": ["pg_duckdb", "pg_qi"]}
    start["start_id"] = preflight._start_id(start["cluster_id"], start["postmaster_start_time"])
    start_digest = hashlib.sha256(preflight.canonical_bytes(start)).hexdigest()
    start["attestation_sha256"] = start_digest
    return {"schema_version": 1, "document_type": "site_qualification",
            "qualification_id": "qualification-1", "scope_sha256": ZERO,
            "source": {"commit": COMMIT, "dirty": False, "tree_sha256": "8" * 64},
            "build": build,
            "installed": {"postgres_path": "/opt/postgresql/bin/postgres", "postgres_sha256": "9" * 64,
                          "pg_qi_path": "/opt/postgresql/lib/pg_qi.so", "pg_qi_sha256": "1" * 64,
                          "pg_duckdb_path": "/opt/postgresql/lib/pg_duckdb.so", "pg_duckdb_sha256": "2" * 64,
                          "duckdb_sha256": "3" * 64,
                          "configuration_sha256": "4" * 64},
            "start": start,
            "endpoint": endpoint,
            "expected_online": {"server_version_num": "190000", "database_name": "pilot",
                                "database_oid": "16384", "session_user": "pilot_reader",
                                "current_user": "pilot_reader", "postmaster_start_time": "2026-11-17T10:00:00Z",
                                "start_id": start["start_id"], "cluster_id": "cluster-1", "pg_qi_version": "0.1.0",
                                "pg_duckdb_version": "1.0.0", "shared_preload_libraries": "pg_duckdb,pg_qi",
                                "routing_mode": "off", "duckdb_force_execution": "off", "pg_qi_enabled": "on"},
            "candidate_replay": {"source_commit": COMMIT, "build_sha256": build_sha, "cells": cells}}


def anchors(value: dict) -> tuple[str, str]:
    return (hashlib.sha256(preflight.canonical_bytes(value)).hexdigest(),
            value["start"]["attestation_sha256"])


def reseal_start(value: dict) -> None:
    start = dict(value["start"]); del start["attestation_sha256"]
    value["start"]["attestation_sha256"] = hashlib.sha256(
        preflight.canonical_bytes(start)).hexdigest()


def intent(value: dict | None = None, trusted_from: dict | None = None) -> dict:
    selected = value or dossier()
    trusted = trusted_from or selected
    dossier_sha, start_sha = anchors(trusted)
    return preflight.validate_dossier(selected, ZERO, dossier_sha, start_sha)


def observation(compiled: dict) -> dict:
    result = dict(compiled["dossier"]["expected_online"])
    result.update({"connection_id": "connection-1",
                   "queries_executed": [key for key, _ in preflight.FIXED_PREFLIGHT_QUERIES],
                   "activation_attempts": 0})
    return result


class SpyConnector:
    def __init__(self, mutate: tuple[str, object] | None = None) -> None:
        self.connect_attempts = 0; self.mutate = mutate

    def connect_and_observe(self, compiled: dict) -> tuple[str, dict]:
        self.connect_attempts += 1
        observed = observation(compiled)
        if self.mutate:
            observed[self.mutate[0]] = self.mutate[1]
        return "connection-1", observed


class OfflinePolicyTests(unittest.TestCase):
    def test_golden_tls_and_socket_profiles_compile_without_secret(self) -> None:
        for kind in ("tls_tcp", "private_socket"):
            with self.subTest(kind=kind):
                compiled = intent(dossier(kind)); rendered = json.dumps(compiled)
                self.assertNotIn("password", rendered.lower())
                self.assertNotIn("service", rendered.lower())
                self.assertEqual(compiled["libpq_parameters"]["passfile"], "/dev/null")

    def test_source_build_install_and_start_mismatches_refuse(self) -> None:
        mutations = [
            ("dirty", lambda value: value["source"].__setitem__("dirty", True)),
            ("testing", lambda value: value["build"].__setitem__("pg_qi_testing", True)),
            ("installed", lambda value: value["installed"].__setitem__("pg_qi_sha256", "f" * 64)),
            ("mapped", lambda value: value["start"].__setitem__("pg_qi_mapped_sha256", "f" * 64)),
            ("sequence", lambda value: value["start"].__setitem__("start_sequence", 10)),
            ("preload", lambda value: value["start"].__setitem__("shared_preload_libraries", ["pg_qi", "pg_duckdb"])),
            ("hook", lambda value: value["start"].__setitem__("hook_inventory", ["pg_duckdb", "other", "pg_qi"])),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name):
                value = dossier(); mutate(value)
                if name in {"mapped", "sequence", "preload", "hook"}:
                    reseal_start(value)
                with self.assertRaises(preflight.QualificationError): intent(value)

    def test_external_dossier_anchor_rejects_consistent_endpoint_and_source_rewrite(self) -> None:
        trusted = dossier()
        for mutation in ("endpoint", "source"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(trusted)
                if mutation == "endpoint":
                    changed["endpoint"]["host"] = "attacker.example"
                    changed["endpoint"]["hostaddr"] = "203.0.113.99"
                else:
                    changed["source"]["commit"] = "e" * 40
                    changed["candidate_replay"]["source_commit"] = "e" * 40
                spy = SpyConnector(); dossier_sha, start_sha = anchors(trusted)
                with self.assertRaises(preflight.QualificationError):
                    preflight.qualify_connection(changed, ZERO, dossier_sha, start_sha, {}, spy)
                self.assertEqual(spy.connect_attempts, 0)

    def test_endpoint_service_multihost_tls_and_socket_refusals(self) -> None:
        mutations = [
            lambda value: value["endpoint"].__setitem__("host", "a.example,b.example"),
            lambda value: value["endpoint"].__setitem__("hostaddr", "192.0.2.10,192.0.2.11"),
            lambda value: value["endpoint"].__setitem__("sslmode", "require"),
            lambda value: value["endpoint"].__setitem__("gssencmode", "prefer"),
            lambda value: value["endpoint"].__setitem__("service", "evil"),
        ]
        for mutate in mutations:
            value = dossier(); mutate(value)
            with self.assertRaises(preflight.QualificationError): intent(value)
        value = dossier("private_socket"); value["endpoint"]["socket_mode"] = 0o777
        with self.assertRaises(preflight.QualificationError): intent(value)
        value = dossier("private_socket"); value["endpoint"]["socket_directory"] = "/run/a,/run/b"
        with self.assertRaises(preflight.QualificationError): intent(value)

    def test_all_ambient_pg_and_runtime_influences_refuse_before_connect(self) -> None:
        names = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD",
                 "PGPASSFILE", "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR", "PGOPTIONS",
                 "PGAPPNAME", "PGSSLMODE", "PGGSSENCMODE", "OPENSSL_CONF", "LD_PRELOAD")
        for name in names:
            spy = SpyConnector()
            with self.subTest(name=name), self.assertRaises(preflight.QualificationError):
                value = dossier(); dossier_sha, start_sha = anchors(value)
                preflight.qualify_connection(value, ZERO, dossier_sha, start_sha,
                                               {name: "CANARY_SECRET"}, spy)
            self.assertEqual(spy.connect_attempts, 0)

    def test_replay_missing_not_run_fail_duplicate_unknown_and_wrong_candidate(self) -> None:
        for mutation, error in (
            ("missing", preflight.QualificationIncomplete), ("not-run", preflight.QualificationIncomplete),
            ("fail", preflight.QualificationFailed), ("duplicate", preflight.QualificationError),
            ("unknown", preflight.QualificationError), ("candidate", preflight.QualificationError)):
            with self.subTest(mutation=mutation):
                value = dossier(); cells = value["candidate_replay"]["cells"]
                if mutation == "missing": cells.pop()
                elif mutation == "not-run": cells[0]["result"] = "NOT_RUN"
                elif mutation == "fail": cells[0]["result"] = "FAIL"
                elif mutation == "duplicate": cells.append(copy.deepcopy(cells[0]))
                elif mutation == "unknown": cells[0]["cell_id"] = "pg19:4c:unknown"
                else: value["candidate_replay"]["source_commit"] = "b" * 40
                with self.assertRaises(error): intent(value)

    def test_replay_fail_dominates_not_run(self) -> None:
        value = dossier(); value["candidate_replay"]["cells"][0]["result"] = "FAIL"
        value["candidate_replay"]["cells"][1]["result"] = "NOT_RUN"
        with self.assertRaises(preflight.QualificationFailed): intent(value)


class OnlineBoundaryTests(unittest.TestCase):
    def test_success_uses_exact_queries_and_never_activates(self) -> None:
        value = dossier(); spy = SpyConnector()
        dossier_sha, start_sha = anchors(value)
        receipt = preflight.qualify_connection(value, ZERO, dossier_sha, start_sha, {}, spy)
        self.assertEqual(spy.connect_attempts, 1); self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(receipt["activation_attempts"], 0)
        source = (ROOT / "tools/phase5/site_preflight.py").read_text()
        for forbidden in ("SET pg_qi.routing_mode", "SET duckdb.force_execution", "CREATE EXTENSION",
                          "ALTER SYSTEM", "shell=True", "import socket", "psycopg"):
            self.assertNotIn(forbidden, source)

    def test_driver_rows_are_strictly_bound_to_online_evaluation(self) -> None:
        compiled = intent(); expected = compiled["dossier"]["expected_online"]
        raw = {"result": "PASS", "connection_nonce": "1" * 32, "checks": {
            "routing_mode": {"rows": [[expected["routing_mode"]]]},
            "duckdb_force": {"rows": [[expected["duckdb_force_execution"]]]},
            "server_version_num": {"rows": [[expected["server_version_num"]]]},
            "database_identity": {"rows": [[expected["database_name"], expected["database_oid"]]]},
            "role_identity": {"rows": [[expected["session_user"], expected["current_user"]]]},
            "postmaster_start": {"rows": [[expected["postmaster_start_time"]]]},
            "cluster_identity": {"rows": [[expected["cluster_id"]]]},
            "backend_identity": {"rows": [["1234", "2026-11-17T10:00:01.000000Z"]]},
            "extensions": {"rows": [["pg_duckdb", expected["pg_duckdb_version"]],
                                       ["pg_qi", expected["pg_qi_version"]]]},
            "preload": {"rows": [[expected["shared_preload_libraries"]]]},
            "pg_qi_enabled": {"rows": [[expected["pg_qi_enabled"]]]},
        }}
        connection_id, observed = preflight._driver_observation(compiled, raw)
        self.assertEqual(preflight.evaluate_observation(compiled, observed, connection_id)["result"], "PASS")
        for mutation in (lambda value: value["checks"]["extensions"]["rows"].pop(),
                         lambda value: value["checks"]["routing_mode"].update(rows=[["allowlist"]]),
                         lambda value: value["checks"].update(extra={"rows": []})):
            changed = copy.deepcopy(raw); mutation(changed)
            with self.assertRaises((preflight.QualificationError, preflight.QualificationFailed)):
                cid, result = preflight._driver_observation(compiled, changed)
                preflight.evaluate_observation(compiled, result, cid)

    def test_each_online_mismatch_has_zero_activation(self) -> None:
        compiled = intent()
        for field in compiled["dossier"]["expected_online"]:
            spy = SpyConnector((field, "mismatch"))
            with self.subTest(field=field), self.assertRaises(preflight.QualificationFailed):
                value = dossier(); dossier_sha, start_sha = anchors(value)
                preflight.qualify_connection(value, ZERO, dossier_sha, start_sha, {}, spy)
            self.assertEqual(spy.connect_attempts, 1)

    def test_query_manifest_connection_and_activation_tamper_refuse(self) -> None:
        compiled = intent()
        for field, value in (("queries_executed", []), ("activation_attempts", 1),
                             ("connection_id", "other-connection")):
            observed = observation(compiled); observed[field] = value
            with self.subTest(field=field), self.assertRaises(preflight.QualificationError):
                preflight.evaluate_observation(compiled, observed, "connection-1")

    def test_restart_and_new_connection_require_new_matching_observation(self) -> None:
        compiled = intent(); first = observation(compiled)
        self.assertEqual(preflight.evaluate_observation(compiled, first, "connection-1")["result"], "PASS")
        restarted = observation(compiled); restarted["connection_id"] = "connection-2"
        restarted["start_id"] = "start-2"
        with self.assertRaises(preflight.QualificationFailed):
            preflight.evaluate_observation(compiled, restarted, "connection-2")

    def test_connector_error_is_sanitised(self) -> None:
        class FailingConnector:
            def connect_and_observe(self, compiled: dict) -> tuple[str, dict]:
                raise RuntimeError("password=CANARY_SECRET host=untrusted")
        value = dossier()
        with self.assertRaises(preflight.QualificationFailed) as caught:
            dossier_sha, start_sha = anchors(value)
            preflight.qualify_connection(value, ZERO, dossier_sha, start_sha, {},
                                           FailingConnector())
        self.assertNotIn("CANARY_SECRET", str(caught.exception))


class CliTests(unittest.TestCase):
    def test_cli_pass_and_sanitised_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "dossier.json"; path.write_text(json.dumps(dossier()))
            dossier_sha, start_sha = anchors(dossier())
            stdout, stderr = StringIO(), StringIO()
            clean = {key: value for key, value in os.environ.items()
                     if not key.upper().startswith("PG") and key not in {"OPENSSL_CONF", "OPENSSL_MODULES", "LD_PRELOAD", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
            with mock.patch.dict(os.environ, clean, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
                code = preflight.main(["--dossier", str(path), "--trusted-scope-sha256", ZERO,
                                       "--trusted-dossier-sha256", dossier_sha,
                                       "--trusted-start-sha256", start_sha])
            self.assertEqual(code, 0); self.assertIn('"PASS"', stdout.getvalue())
            stdout, stderr = StringIO(), StringIO()
            with mock.patch.dict(os.environ, {"PGPASSWORD": "CANARY_SECRET"}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
                code = preflight.main(["--dossier", str(path), "--trusted-scope-sha256", ZERO,
                                       "--trusted-dossier-sha256", dossier_sha,
                                       "--trusted-start-sha256", start_sha])
            self.assertEqual(code, 2); self.assertNotIn("CANARY_SECRET", stderr.getvalue())

    def test_loader_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            fifo = Path(name) / "dossier.fifo"; os.mkfifo(fifo)
            with self.assertRaises(preflight.QualificationError):
                preflight._load(fifo)

    def test_driver_rejects_regular_file_credential_before_connect(self) -> None:
        driver = ROOT / "tools/phase5/site-preflight-driver"
        if not driver.exists():
            self.skipTest("site-preflight-driver is not built")
        with tempfile.TemporaryDirectory() as name:
            ca = Path(name) / "ca.pem"; ca.write_text("bounded-test-ca")
            credential = Path(name) / "credential"; credential.write_text("not-an-anonymous-channel\n")
            descriptor = os.open(credential, os.O_RDONLY)
            try:
                result = subprocess.run([
                    str(driver), "--transport", "tls", "--host", "localhost",
                    "--hostaddr", "127.0.0.1", "--port", "1", "--dbname", "postgres",
                    "--user", "pilot_reader", "--sslrootcert", str(ca),
                    "--sslrootcert-sha256", hashlib.sha256(ca.read_bytes()).hexdigest(),
                    "--credential-fd", str(descriptor), "--deadline-ms", "100"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(descriptor,),
                    env={"PATH": "/usr/bin:/bin", "LANG": "C"}, timeout=1, check=False)
            finally:
                os.close(descriptor)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout),
                             {"reason": "credential_channel", "result": "FAIL"})


if __name__ == "__main__":
    unittest.main()
