#!/usr/bin/env python3

import copy
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.phase5.corpus_oracle import MAGIC_V2, TRAILER_MARKER
from tools.phase7.differential_oracle import (DifferentialError, evaluate, execution_subject_token,
                                              main as oracle_main,
                                              validate_no_import_control)
from tools.phase7.run_differential import measured_bounds


V2 = ("v=2;sv=2;s=complete;cid=0123456789abcdef0123456789abcdef;g=1;e=1;"
      "mode=shadow_p0;enabled=on;context=proven_client;eligible=not_evaluated;"
      "safe=not_evaluated;beneficial=not_evaluated;barrier=closed;"
      "recommendation=insufficient_evidence;reason=0;rule=1;model=1;"
      "certificate=1;policy=pass_through;force=off;engine=unknown;"
      "verification=unverified;observation=no_authoritative_match;"
      "p0=bounded;p0_reason=ok;lifecycle=fresh_execution")


def write_v2(path: Path, columns: list[tuple[str, int, int, int]], rows: list[tuple],
             *, error: str = "") -> None:
    payload = bytearray(MAGIC_V2) + struct.pack(">I", len(columns))
    for name, oid, typmod, result_format in columns:
        encoded = name.encode()
        payload += (struct.pack(">I", len(encoded)) + encoded + struct.pack(">I", oid) +
                    struct.pack(">iI", typmod, result_format))
    digest = hashlib.sha256(payload).digest()
    for row in rows:
        payload += struct.pack(">I", len(row))
        for value in row:
            if value is None:
                payload += struct.pack(">I", 0xFFFFFFFF)
            else:
                encoded = str(value).encode()
                payload += struct.pack(">I", len(encoded)) + encoded
    state = (error.encode() if error else b"\x00" * 5) + b"\x00" * 3
    payload += (struct.pack(">II", TRAILER_MARKER, bool(error)) + state +
                struct.pack(">QI", len(rows), 0 if error else 1) + digest)
    path.write_bytes(payload); path.chmod(0o600)


def witness(sequence: int, engine: str, columns: list[tuple[str, int, int, int]],
            *, status: str = "success", pid: int = 101) -> str:
    meta = ",".join(f"{oid}:{typmod}:100" for _, oid, typmod, _ in columns)
    custom = 0 if engine == "postgres" else 1
    completed = 1 if engine == "duckdb" else 0
    present = "t" if engine != "postgres" else "f"
    token = hashlib.sha256(("run-1\0subject-1\0" + "a" * 64).encode()).hexdigest()
    return (f"v=1;seq={sequence};status={status};engine={engine};token={token};"
            f"pid={pid};db=5;user=10;meta={meta};"
            f"columns={len(columns)};truncated=f;custom_nodes={custom};"
            f"custom_completed={completed};duckdb_present={present}")


class DifferentialOracleTest(unittest.TestCase):
    columns = [("id", 23, -1, 0), ("label", 25, -1, 0)]

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scratch = self.root / "scratch"; self.scratch.mkdir(mode=0o700)
        self.manifest = {"schema_version": 1, "document_type": "pq31_differential_run",
            "run_id": "run-1", "subject_id": "subject-1", "sql_sha256": "a" * 64,
            "ordered": False, "authorization_reference": "auth-1",
            "arms": ["native", "forced", "shadow"]}
        self.receipts = {}
        rows = [(1, "a"), (2, None), (2, None)]
        for arm, engine in (("native", "postgres"), ("forced", "duckdb"),
                            ("shadow", "postgres")):
            path = self.root / f"{arm}.spool"; write_v2(path, self.columns, rows)
            self.receipts[arm] = self.receipt(arm, engine, path, len(rows))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bound_receipts_must_describe_the_same_bindings(self) -> None:
        unbound_token = execution_subject_token(self.manifest)
        self.manifest.update(schema_version=2, bindings_sha256="c" * 64)
        bound_token = execution_subject_token(self.manifest)
        for receipt in self.receipts.values():
            receipt.update(schema_version=2, bindings_sha256="c" * 64)
            receipt["witness_after"] = receipt["witness_after"].replace(unbound_token, bound_token)
        self.assertEqual(evaluate(self.manifest, self.receipts, self.scratch)["result"], "PASS")
        self.receipts["forced"]["bindings_sha256"] = "d" * 64
        with self.assertRaisesRegex(DifferentialError, "bindings_sha256 binding mismatch"):
            evaluate(self.manifest, self.receipts, self.scratch)

    def receipt(self, arm: str, engine: str, path: Path, rows: int, *,
                sqlstate: str = "") -> dict:
        pid = {"native": 101, "forced": 102, "shadow": 103}[arm]
        v2_after = V2.replace(";e=1;", ";e=2;")
        if arm in {"native", "forced"}:
            v2_after = v2_after.replace("mode=shadow_p0", "mode=off")
        if arm == "forced":
            v2_after = v2_after.replace("force=off", "force=external")
        return {"schema_version": 1, "document_type": "pq31_arm_receipt",
            "run_id": "run-1", "subject_id": "subject-1", "arm": arm,
            "sql_sha256": "a" * 64,
            "exporter": {"pid": 100, "database": "postgres", "user": "postgres",
                "database_oid": 5, "user_oid": 10},
            "snapshot": {"token_sha256": "b" * 64, "imported": True,
                "import_command_complete": True, "exporter_snapshot": "1:1:",
                "arm_snapshot": "1:1:"},
            "backend": {"pid": pid, "database": "postgres", "user": "postgres",
                "database_oid": 5, "user_oid": 10},
            "v2_before": V2, "v2_after": v2_after,
            "witness_before": witness(10, "postgres", self.columns, pid=pid),
            "witness_after": witness(11, engine, self.columns,
                                      status="error" if sqlstate else "success", pid=pid),
            "spool": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size, "rows": rows,
                "terminal_status": "error" if sqlstate else "complete",
                "sqlstate": sqlstate},
            "acquisition": {"complete": True, "reason": "ok"}}

    def test_pass_and_value_multiplicity_order_mutations(self) -> None:
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["result"], "PASS")
        forced = self.root / "forced-change"; write_v2(
            forced, self.columns, [(1, "a"), (2, None)])
        self.receipts["forced"] = self.receipt("forced", "duckdb", forced, 2)
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["result"], "FAIL")

        reordered = self.root / "forced-reordered"; write_v2(
            reordered, self.columns, [(2, None), (1, "a"), (2, None)])
        self.receipts["forced"] = self.receipt("forced", "duckdb", reordered, 3)
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["result"], "PASS")
        self.manifest["ordered"] = True
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["reason"], "ordered_mismatch")

    def test_snapshot_precedes_semantic_mismatch_but_not_engine_interference(self) -> None:
        forced = self.root / "forced-extra"; write_v2(
            forced, self.columns, [(1, "a"), (2, None), (2, None), (3, "late")])
        self.receipts["forced"] = self.receipt("forced", "duckdb", forced, 4)
        self.receipts["forced"]["snapshot"]["imported"] = False
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["reason"], "snapshot_import_unproved")
        self.receipts["native"]["witness_after"] = witness(11, "duckdb", self.columns)
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["reason"], "native_or_shadow_interference")

    def test_no_import_control_binds_run_backend_snapshot_and_spool(self) -> None:
        path = self.root / "missing-import.spool"
        write_v2(path, self.columns, [(3, "new commit")])
        control = self.receipt("native", "postgres", path, 1)
        control["backend"]["pid"] = 104
        control["witness_before"] = witness(10, "postgres", self.columns, pid=104)
        control["witness_after"] = witness(11, "postgres", self.columns, pid=104)
        control["snapshot"].update(imported=False, import_command_complete=False,
                                   arm_snapshot="1:2:")
        validate_no_import_control(self.manifest, self.receipts, control)

        variants = {
            "other run": {"run_id": "run-2"},
            "reused backend": {"backend": self.receipts["native"]["backend"]},
            "old snapshot": {"snapshot": {**control["snapshot"], "arm_snapshot": "1:1:"}},
            "imported snapshot": {"snapshot": {**control["snapshot"], "imported": True}},
            "different exporter token": {"snapshot": {**control["snapshot"], "token_sha256": "c" * 64}},
            "no native execution": {"witness_after": witness(11, "unknown", self.columns, pid=104)},
            "changed spool": {"spool": {**control["spool"], "sha256": "9" * 64}},
        }
        for name, change in variants.items():
            with self.subTest(name=name), self.assertRaises(DifferentialError):
                validate_no_import_control(self.manifest, self.receipts, {**control, **change})

    def test_force_decline_and_unknown_are_inconclusive(self) -> None:
        for engine, reason in (("postgres", "force_declined"),
                               ("unknown", "forced_engine_unproved")):
            with self.subTest(engine=engine):
                receipts = copy.deepcopy(self.receipts)
                receipts["forced"]["witness_after"] = witness(11, engine, self.columns, pid=102)
                self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"], reason)

    def test_forced_uncertainty_does_not_mask_native_shadow_difference(self) -> None:
        changed = self.root / "shadow-changed"
        write_v2(changed, self.columns, [(99, "different")])
        receipts = copy.deepcopy(self.receipts)
        receipts["shadow"] = self.receipt("shadow", "postgres", changed, 1)
        receipts["forced"]["witness_after"] = witness(
            11, "unknown", self.columns, pid=102)
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["result"], "FAIL")
        receipts = copy.deepcopy(self.receipts)
        receipts["forced"]["witness_after"] = witness(11, "unknown", self.columns, pid=102)
        receipts["shadow"]["witness_after"] = receipts["shadow"][
            "witness_after"].replace(":100", ":101")
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "collation_mismatch")
        receipts = copy.deepcopy(self.receipts)
        receipts["forced"]["witness_after"] = witness(11, "unknown", self.columns, pid=102)
        path = self.root / "shadow-error-under-forced-unknown"
        write_v2(path, self.columns, [], error="22012")
        receipts["shadow"] = self.receipt("shadow", "postgres", path, 0, sqlstate="22012")
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "success_error_mismatch")
        receipts = copy.deepcopy(self.receipts)
        forced = self.root / "forced-different-under-shadow-unknown"
        write_v2(forced, self.columns, [(77, "forced-different")])
        receipts["forced"] = self.receipt("forced", "duckdb", forced, 1)
        receipts["shadow"]["witness_after"] = witness(
            11, "unknown", self.columns, pid=103)
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["result"], "FAIL")

    def test_error_parity_and_sqlstate_difference(self) -> None:
        for arm, engine in (("native", "postgres"), ("forced", "duckdb"),
                            ("shadow", "postgres")):
            path = self.root / f"{arm}-error"; write_v2(path, self.columns, [], error="22012")
            self.receipts[arm] = self.receipt(arm, engine, path, 0, sqlstate="22012")
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["result"], "ERROR_PARITY")
        path = self.root / "forced-error-2"; write_v2(path, self.columns, [], error="22P02")
        self.receipts["forced"] = self.receipt("forced", "duckdb", path, 0, sqlstate="22P02")
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["reason"], "sqlstate_mismatch")

    def test_descriptor_and_collation_mutations(self) -> None:
        baseline_receipts = copy.deepcopy(self.receipts)
        changed = [("id", 20, -1, 0), ("label", 25, -1, 0)]
        path = self.root / "forced-oid"; write_v2(path, changed, [(1, "a")])
        receipt = self.receipt("forced", "duckdb", path, 1)
        receipt["witness_before"] = witness(10, "postgres", changed, pid=102)
        receipt["witness_after"] = witness(11, "duckdb", changed, pid=102)
        self.receipts["forced"] = receipt
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["reason"], "descriptor_mismatch")

        self.receipts = baseline_receipts
        self.receipts["forced"]["witness_after"] = self.receipts["forced"][
            "witness_after"].replace(":100", ":101")
        self.assertEqual(evaluate(self.manifest, copy.deepcopy(self.receipts),
                                  self.scratch)["reason"], "collation_mismatch")

    def test_receipt_replay_and_spool_binding_are_invalid(self) -> None:
        replay = copy.deepcopy(self.receipts)
        replay["forced"]["run_id"] = "old-run"
        with self.assertRaises(DifferentialError):
            evaluate(self.manifest, replay, self.scratch)
        bad = copy.deepcopy(self.receipts)
        bad["forced"]["spool"]["sha256"] = "0" * 64
        with self.assertRaises(DifferentialError):
            evaluate(self.manifest, bad, self.scratch)
        for invalid in (True, 1.0, "1"):
            with self.subTest(schema_version=invalid):
                manifest = copy.deepcopy(self.manifest)
                manifest["schema_version"] = invalid
                with self.assertRaises(DifferentialError):
                    evaluate(manifest, copy.deepcopy(self.receipts), self.scratch)
                receipts = copy.deepcopy(self.receipts)
                receipts["forced"]["schema_version"] = invalid
                with self.assertRaises(DifferentialError):
                    evaluate(self.manifest, receipts, self.scratch)

    def test_well_formed_missing_witness_and_shadow_interference(self) -> None:
        receipts = copy.deepcopy(self.receipts)
        receipts["forced"]["witness_after"] = receipts["forced"]["witness_before"]
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "witness_not_advanced")
        receipts = copy.deepcopy(self.receipts)
        receipts["shadow"]["v2_after"] = receipts["shadow"]["v2_after"].replace(
            "barrier=closed", "barrier=open")
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch),
                         {"result": "FAIL", "reason": "shadow_policy_interference"})
        receipts = copy.deepcopy(self.receipts)
        receipts["forced"]["witness_after"] = receipts["forced"]["witness_before"]
        receipts["native"]["witness_after"] = witness(
            11, "duckdb", self.columns, pid=101)
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "native_or_shadow_interference")

    def test_planner_error_without_execution_witness_is_inconclusive(self) -> None:
        receipts = copy.deepcopy(self.receipts)
        for arm, engine in (("native", "postgres"), ("forced", "duckdb"),
                            ("shadow", "postgres")):
            path = self.root / f"{arm}-planner-error"
            write_v2(path, [], [], error="42601")
            receipts[arm] = self.receipt(arm, engine, path, 0, sqlstate="42601")
            receipts[arm]["witness_after"] = receipts[arm]["witness_before"]
            receipts[arm]["v2_after"] = receipts[arm]["v2_before"]
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "decision_not_advanced")

    def test_empty_v2_envelope_is_inconclusive(self) -> None:
        none = "v=2;sv=2;s=none;cid=0123456789abcdef0123456789abcdef;g=1;e=0"
        receipts = copy.deepcopy(self.receipts)
        for receipt in receipts.values():
            receipt["v2_before"] = none; receipt["v2_after"] = none
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "decision_not_advanced")
        withheld = "v=2;sv=2;s=withheld"
        receipts = copy.deepcopy(self.receipts)
        receipts["forced"]["v2_before"] = withheld
        receipts["forced"]["v2_after"] = withheld
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "decision_not_advanced")
        receipts["native"]["witness_after"] = witness(
            11, "duckdb", self.columns, pid=101)
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "native_or_shadow_interference")

    def test_disabled_arm_is_invalid(self) -> None:
        receipts = copy.deepcopy(self.receipts)
        receipts["shadow"]["v2_after"] = receipts["shadow"]["v2_after"].replace(
            "enabled=on", "enabled=off")
        with self.assertRaisesRegex(DifferentialError, "not enabled"):
            evaluate(self.manifest, receipts, self.scratch)

    def test_terminal_and_identity_contradictions_are_invalid(self) -> None:
        receipts = copy.deepcopy(self.receipts)
        receipts["forced"]["witness_after"] = receipts["forced"]["witness_after"].replace(
            "status=success", "status=error")
        with self.assertRaisesRegex(DifferentialError, "terminal status"):
            evaluate(self.manifest, receipts, self.scratch)
        receipts = copy.deepcopy(self.receipts)
        receipts["forced"]["backend"]["pid"] = 999
        with self.assertRaisesRegex(DifferentialError, "binding mismatch"):
            evaluate(self.manifest, receipts, self.scratch)

    def test_malformed_terminal_enum_produces_cli_invalid_report(self) -> None:
        receipts = copy.deepcopy(self.receipts)
        receipts["forced"]["spool"]["terminal_status"] = []
        paths = {}
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        for arm, receipt in receipts.items():
            paths[arm] = self.root / f"{arm}.json"
            paths[arm].write_text(json.dumps(receipt), encoding="utf-8")
        output = self.root / "result.json"
        result = oracle_main([
            "--manifest", str(manifest_path),
            "--native", str(paths["native"]),
            "--forced", str(paths["forced"]),
            "--shadow", str(paths["shadow"]),
            "--scratch-root", str(self.scratch),
            "--output", str(output),
        ])
        self.assertEqual(result, 1)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["result"], "INVALID")
        self.assertEqual(report["reason"], "invalid spool terminal status")

    def test_partial_error_and_cancellation_are_inconclusive(self) -> None:
        for state, expected in (("22012", "partial_output"), ("57014", "cancelled")):
            receipts = copy.deepcopy(self.receipts)
            for arm, engine in (("native", "postgres"), ("forced", "duckdb"),
                                ("shadow", "postgres")):
                path = self.root / f"{arm}-{state}"
                write_v2(path, self.columns, [(1, "partial")], error=state)
                receipts[arm] = self.receipt(arm, engine, path, 1, sqlstate=state)
            self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                             expected)

    def test_error_parity_does_not_mask_collation_difference(self) -> None:
        receipts = {}
        for arm, engine in (("native", "postgres"), ("forced", "duckdb"),
                            ("shadow", "postgres")):
            path = self.root / f"{arm}-metadata-error"
            write_v2(path, self.columns, [], error="22012")
            receipts[arm] = self.receipt(arm, engine, path, 0, sqlstate="22012")
        receipts["forced"]["witness_after"] = receipts["forced"][
            "witness_after"].replace(":100", ":101")
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "collation_mismatch")

    def test_unsupported_type_preserves_invalid_and_engine_fail_precedence(self) -> None:
        unsupported = [("amount", 1700, -1, 0)]
        path = self.root / "forced-numeric"
        write_v2(path, unsupported, [("1.25",)])
        receipt = self.receipt("forced", "duckdb", path, 1)
        receipt["witness_before"] = witness(10, "postgres", unsupported, pid=102)
        receipt["witness_after"] = witness(11, "duckdb", unsupported, pid=102)
        unsupported_receipt = copy.deepcopy(receipt)
        receipts = copy.deepcopy(self.receipts); receipts["forced"] = copy.deepcopy(unsupported_receipt)
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "unsupported_result_type")
        receipts = copy.deepcopy(self.receipts); receipts["forced"] = copy.deepcopy(unsupported_receipt)
        receipts["native"]["witness_after"] = witness(11, "duckdb", self.columns, pid=101)
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "native_or_shadow_interference")
        receipts = copy.deepcopy(self.receipts); receipts["forced"] = copy.deepcopy(unsupported_receipt)
        receipts["shadow"]["spool"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(DifferentialError, "binding mismatch"):
            evaluate(self.manifest, receipts, self.scratch)

    def test_error_witness_unsupported_type_is_inconclusive(self) -> None:
        unsupported = [("amount", 1700, -1, 0)]
        receipts = {}
        for arm, engine, pid in (("native", "postgres", 101),
                                 ("forced", "duckdb", 102),
                                 ("shadow", "postgres", 103)):
            path = self.root / f"{arm}-unsupported-error"
            write_v2(path, [], [], error="22012")
            receipt = self.receipt(arm, engine, path, 0, sqlstate="22012")
            receipt["witness_after"] = witness(11, engine, unsupported,
                                                status="error", pid=pid)
            receipts[arm] = receipt
        self.assertEqual(evaluate(self.manifest, receipts, self.scratch)["reason"],
                         "unsupported_result_type")


class BoundTimingTests(unittest.TestCase):
    def test_observed_bounds_clear_both_saving_floors(self) -> None:
        samples = [{"native_ns": 50_000_999_999, "bound_ns": 44_999_999_999},
                   {"native_ns": 51_000_000_000, "bound_ns": 45_000_000_000}]
        bounds = measured_bounds(samples, 5000, 10)
        self.assertEqual(bounds["postgres_lower_ms"], 50_000)
        self.assertEqual(bounds["duckdb_upper_ms"], 45_000)
        self.assertEqual(bounds["beneficial"], "pass")
        self.assertEqual(measured_bounds(samples, 5001, 10)["beneficial"], "veto")
        self.assertEqual(measured_bounds(samples, 5000, 11)["beneficial"], "veto")

        samples[1]["bound_ns"] += 1
        bounds = measured_bounds(samples, 5000, 10)
        self.assertEqual(bounds["duckdb_upper_ms"], 45_001)
        self.assertEqual(bounds["beneficial"], "veto")


if __name__ == "__main__":
    unittest.main()
