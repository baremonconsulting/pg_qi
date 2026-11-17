#!/usr/bin/env python3
"""Normative and adversarial tests for PGQI-5 evidence contracts."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "contract_rules", ROOT / "tools/phase5/contract_rules.py")
RULES = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RULES)
ZERO = "0" * 64


def snapshot_tuple() -> dict[str, str]:
    return {"contract_version": "1", "context": "proven_client",
            "policy_intent": "request_delegation", "reason": "delegation_verified",
            "force_origin": "qi_stack", "classifier_status": "candidate",
            "classifier_reason": "candidate", "observed_engine": "duckdb",
            "verification": "verified", "observation": "duckdb_scan_at_downstream_boundary",
            "request_applied": "on", "duckdb_force_execution": "off"}


def state(**changes: str) -> dict[str, str]:
    value = {
        "lifecycle": "VALIDATED", "technical_result": "PASS",
        "evidence_completeness": "COMPLETE", "exit_state": "CLEAN",
        "value_outcome": "POSITIVE", "project_decision": "CONTINUE",
    }
    value.update(changes)
    return value


def base(kind: str, identity: str = "x") -> dict[str, object]:
    return {"schema_version": 1, "contract_type": kind, "contract_id": identity}


def artifact(path: str, payload: bytes = b"x") -> dict[str, object]:
    return {"path": path, "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload)}


def scope() -> dict[str, object]:
    value = base("scope", "scope-1")
    value.update({
        "scope_inputs": {name: artifact(f"inputs/{name}.json") for name in
                         ("workload_definition", "release_definition", "target", "limits",
                          "evidence_policy", "recovery_plan", "export_policy", "toolkit",
                          "role_bindings", "window")},
        "gates_required": ["SCOPE_AUTHORIZED", "OPERATION_GO"],
    })
    return value


def request(identity: str, planning: str, relation: str, **extra: object) -> dict[str, object]:
    value = base("request", identity)
    value.update({"scope_sha256": ZERO, "run_id": "run-1", "session_id": "session-1",
                  "request_id": identity, "query_id": "q-1", "execution_status": "FAILED",
                  "planning_evidence": planning, "snapshot_relation": relation})
    value.update(extra)
    return value


class ParserTests(unittest.TestCase):
    def test_static_malformed_fixtures_are_rejected(self) -> None:
        for path in sorted((ROOT / "tests/phase5/contracts/invalid").glob("*.json")):
            with self.subTest(path=path.name), self.assertRaises(RULES.ContractError):
                RULES.strict_load_bytes(path.read_bytes())

    def test_size_depth_and_invalid_utf8_are_rejected(self) -> None:
        catalog = RULES.load_catalog()
        for payload in (b"x" * (catalog["maximum_bytes"] + 1), b"\xff"):
            with self.assertRaises(RULES.ContractError):
                RULES.strict_load_bytes(payload)
        value: object = "leaf"
        for _ in range(catalog["maximum_depth"] + 1):
            value = {"nested": value}
        document = base("snapshot", "deep")
        document.update({"scope_sha256": ZERO, "snapshot_id": "s", "session_id": "session",
                         "planning_request_id": "p", "prepared_object_id": "A",
                         "prepared_generation": 1, "decision_id": 1, "tuple": value})
        with self.assertRaisesRegex(RULES.ContractError, "maximum_depth"):
            RULES.strict_load_bytes(json.dumps(document).encode())

    def test_each_parser_rule_has_a_valid_baseline(self) -> None:
        valid = request("baseline", "UNAVAILABLE", "NO_SNAPSHOT")
        RULES.strict_load_bytes(json.dumps(valid).encode())
        unknown = {**valid, "unexpected": True}
        with self.assertRaisesRegex(RULES.ContractError, "unknown controlling"):
            RULES.strict_load_bytes(json.dumps(unknown).encode())
        snapshot = base("snapshot", "bool-int")
        snapshot.update({"scope_sha256": ZERO, "snapshot_id": "s", "session_id": "session",
                         "planning_request_id": "p", "prepared_object_id": "A",
                         "prepared_generation": True, "decision_id": 1, "tuple": {"x": 1}})
        with self.assertRaisesRegex(RULES.ContractError, "invalid integer"):
            RULES.strict_load_bytes(json.dumps(snapshot).encode())
        for raw in (b'{"x":1e999}', b'{"x":"\\ud800"}'):
            with self.assertRaises(RULES.ContractError): RULES.strict_load_bytes(raw)
        wrong_version = {**valid, "schema_version": 1.0}
        with self.assertRaisesRegex(RULES.ContractError, "schema_version"):
            RULES.strict_load_bytes(json.dumps(wrong_version).encode())

    def test_invalid_types_and_calendar_dates_use_contract_error(self) -> None:
        for document in ({"schema_version": 1, "contract_type": [], "contract_id": "x"},
                         request("sqlstate", "UNAVAILABLE", "NO_SNAPSHOT", sqlstate=12345)):
            with self.assertRaises(RULES.ContractError):
                RULES.strict_load_bytes(json.dumps(document).encode())
        approval = base("approval_receipt", "bad-date")
        approval.update({"receipt_kind": "OPERATION_GO", "scope_sha256": ZERO,
                         "preflight_sha256": ZERO,
                         "valid_from": "2026-99-01T00:00:00Z",
                         "valid_through": "2026-01-02T00:00:00Z",
                         "approvals": ["operator"], "nonce": "nonce"})
        with self.assertRaises(RULES.ContractError): RULES.validate_contract(approval)
        bad_scope = scope(); bad_scope["gates_required"] = [{}]
        with self.assertRaises(RULES.ContractError): RULES.validate_contract(bad_scope)

    def test_traversal_and_symlink_are_rejected(self) -> None:
        for path in ("../escape", "/absolute", "a/../../escape", r"a\b",
                     "./index.json", "a//b"):
            with self.assertRaises(RULES.ContractError):
                RULES.validate_relative_path(path)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root / "real").write_text("x")
            (root / "link").symlink_to(root / "real")
            with self.assertRaises(RULES.ContractError):
                RULES.resolve_bundle_path(root, Path("link"))


class ScopeAndReceiptTests(unittest.TestCase):
    def test_exact_byte_mutation_invalidates_scope_input_and_old_go(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root / "inputs").mkdir(); document = scope()
            for key, reference in document["scope_inputs"].items():
                path = root / reference["path"]; path.write_bytes(b"x")
            RULES.verify_scope_inputs(root, document)
            receipt = base("approval_receipt", "go-1")
            receipt.update({"receipt_kind": "OPERATION_GO",
                            "scope_sha256": RULES.canonical_sha256(document),
                            "preflight_sha256": ZERO,
                            "valid_from": "2026-01-01T00:00:00Z",
                            "valid_through": "2026-01-02T00:00:00Z",
                            "approvals": ["operator"], "nonce": "nonce-1"})
            self.assertTrue(RULES.receipt_matches_scope(receipt, document, "OPERATION_GO"))
            self.assertFalse(RULES.receipt_matches_scope(receipt, document, "CUSTOMER_OPERATION_GO"))
            (root / document["scope_inputs"]["target"]["path"]).write_bytes(b"y")
            with self.assertRaises(RULES.ContractError):
                RULES.verify_scope_inputs(root, document)
            changed = copy.deepcopy(document)
            changed["scope_inputs"]["target"] = artifact("inputs/target.json", b"y")
            self.assertFalse(RULES.receipt_matches_scope(receipt, changed, "OPERATION_GO"))

    def test_receipt_classes_are_not_interchangeable(self) -> None:
        approval = base("approval_receipt")
        approval.update({"receipt_kind": "SHARE_RELEASE", "scope_sha256": ZERO,
                         "preflight_sha256": ZERO,
                         "valid_from": "2026-01-01T00:00:00Z",
                         "valid_through": "2026-01-02T00:00:00Z",
                         "approvals": ["a"], "nonce": "n"})
        with self.assertRaises(RULES.ContractError): RULES.validate_contract(approval)
        export = base("export_receipt", "share")
        export.update({"receipt_kind": "SHARE_RELEASE", "scope_sha256": RULES.canonical_sha256(scope()),
                       "index_sha256": ZERO, "validation_sha256": ZERO,
                       "artifact_sha256": ZERO, "recipients": ["customer"],
                       "approved_at": "2026-01-01T00:00:00Z", "approval_reference": "approval-1"})
        RULES.validate_contract(export)
        self.assertFalse(RULES.receipt_matches_scope(export, scope(), "OPERATION_GO"))
        self.assertTrue(RULES.export_receipt_matches(
            export, "SHARE_RELEASE", export["scope_sha256"], ZERO, ZERO, ZERO))
        self.assertFalse(RULES.export_receipt_matches(
            export, "PUBLICATION_RELEASE", export["scope_sha256"], ZERO, ZERO, ZERO))


class PlanningTests(unittest.TestCase):
    def test_planning_states_do_not_claim_false_freshness(self) -> None:
        valid = [
            request("fresh", "FRESH", "CURRENT", snapshot_id="s-a", planning_request_id="p-a"),
            request("error", "ERROR", "PREVIOUS", snapshot_id="s-a", planning_request_id="p-a"),
            request("unavailable", "UNAVAILABLE", "NO_SNAPSHOT"),
        ]
        for value in valid: RULES.validate_contract(value)
        invalid = request("bad", "ERROR", "CURRENT", snapshot_id="s-b", planning_request_id="p-b")
        with self.assertRaises(RULES.ContractError): RULES.validate_contract(invalid)
        stale = request("stale", "UNAVAILABLE", "NO_SNAPSHOT", snapshot_id="s-a")
        with self.assertRaises(RULES.ContractError): RULES.validate_contract(stale)

    def test_a_b_cached_a_uses_registry_generation(self) -> None:
        snapshots = []
        for sid, prepared, generation in (("s-a", "A", 1), ("s-b", "B", 1)):
            value = base("snapshot", sid)
            value.update({"scope_sha256": ZERO, "snapshot_id": sid, "session_id": "session-1",
                          "planning_request_id": f"p-{prepared}", "prepared_object_id": prepared,
                          "prepared_generation": generation, "decision_id": 1,
                          "tuple": snapshot_tuple()})
            snapshots.append(value); RULES.validate_contract(value)
        requests = [
            request("a", "FRESH", "CURRENT", snapshot_id="s-a", planning_request_id="p-A"),
            request("b", "FRESH", "CURRENT", snapshot_id="s-b", planning_request_id="p-B"),
            request("a2", "REUSED", "CURRENT", snapshot_id="s-a", planning_request_id="p-A",
                    prepared_object_id="A", prepared_generation=1),
            request("error-after-a2", "ERROR", "PREVIOUS", snapshot_id="s-b",
                    planning_request_id="p-B"),
        ]
        RULES.validate_cached_sequence(requests, snapshots)
        requests[-2]["snapshot_id"] = "s-b"
        with self.assertRaises(RULES.ContractError):
            RULES.validate_cached_sequence(requests, snapshots)

    def test_registry_refuses_fabricated_and_cross_session_attribution(self) -> None:
        snapshot = base("snapshot", "s-a")
        snapshot.update({"scope_sha256": ZERO, "snapshot_id": "s-a", "session_id": "session-1",
                         "planning_request_id": "p-a", "prepared_object_id": "A",
                         "prepared_generation": 1, "decision_id": 1, "tuple": snapshot_tuple()})
        fresh = request("fresh", "FRESH", "CURRENT", snapshot_id="s-a",
                        planning_request_id="invented")
        with self.assertRaises(RULES.ContractError):
            RULES.validate_cached_sequence([fresh], [snapshot])
        cross = request("cross", "REUSED", "CURRENT", snapshot_id="s-a",
                        planning_request_id="p-a", prepared_object_id="A", prepared_generation=1)
        cross["session_id"] = "session-2"
        with self.assertRaises(RULES.ContractError):
            RULES.validate_cached_sequence([cross], [snapshot])


class SealTests(unittest.TestCase):
    def terminal(self, current_state: dict[str, str], claimed: bool) -> dict[str, object]:
        value = base("terminal", "terminal-1")
        value.update({"scope_sha256": ZERO, "index_sha256": ZERO,
                      "state": current_state, "aggregate_pass": claimed})
        return value

    def test_fail_dirty_and_closed_are_not_promoted(self) -> None:
        cases = [state(technical_result="FAIL", exit_state="DIRTY"),
                 state(lifecycle="CLOSED", technical_result="FAIL")]
        for current in cases:
            RULES.validate_contract(self.terminal(current, False))
            with self.assertRaises(RULES.ContractError):
                RULES.validate_contract(self.terminal(current, True))

    def test_index_rejects_self_hash_and_mutation_breaks_seal(self) -> None:
        index = base("index", "index-1")
        index.update({"scope_sha256": ZERO, "payloads": [artifact("index.json")],
                      "payload_flush_fsync_complete": True, "index_sequence": 1})
        with self.assertRaises(RULES.ContractError): RULES.validate_contract(index)

        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root / "payload.json").write_bytes(b"x")
            index["payloads"] = [artifact("payload.json")]
            terminal = self.terminal(state(), True)
            terminal["index_sha256"] = RULES.canonical_sha256(index)
            validation = base("validation", "validation-1")
            validation.update({"scope_sha256": ZERO, "index_sha256": terminal["index_sha256"],
                               "terminal_sha256": RULES.canonical_sha256(terminal),
                               "profile": "private_full", "result": "PASS", "checks": ["seal"]})
            RULES.verify_seal(root, index, terminal, validation)
            (root / "payload.json").write_bytes(b"y")
            with self.assertRaises(RULES.ContractError):
                RULES.verify_seal(root, index, terminal, validation)

    def test_validation_cannot_promote_failed_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root / "payload.json").write_bytes(b"x")
            index = base("index", "i")
            index.update({"scope_sha256": ZERO, "payloads": [artifact("payload.json")],
                          "payload_flush_fsync_complete": True, "index_sequence": 1})
            terminal = self.terminal(state(technical_result="FAIL"), False)
            terminal["index_sha256"] = RULES.canonical_sha256(index)
            validation = base("validation", "v")
            validation.update({"scope_sha256": ZERO, "index_sha256": terminal["index_sha256"],
                               "terminal_sha256": RULES.canonical_sha256(terminal),
                               "profile": "private_full", "result": "PASS", "checks": ["x"]})
            with self.assertRaises(RULES.ContractError):
                RULES.verify_seal(root, index, terminal, validation)

    def test_crash_missing_and_false_fsync_never_seal(self) -> None:
        index = base("index", "i")
        index.update({"scope_sha256": ZERO, "payloads": [artifact("missing.json")],
                      "payload_flush_fsync_complete": True, "index_sequence": 1})
        terminal = self.terminal(state(), True); terminal["index_sha256"] = RULES.canonical_sha256(index)
        validation = base("validation", "v")
        validation.update({"scope_sha256": ZERO, "index_sha256": terminal["index_sha256"],
                           "terminal_sha256": RULES.canonical_sha256(terminal),
                           "profile": "private_full", "result": "PASS", "checks": ["seal"]})
        with tempfile.TemporaryDirectory() as name, self.assertRaises(RULES.ContractError):
            RULES.verify_seal(Path(name), index, terminal, validation)
        index["payload_flush_fsync_complete"] = False
        with self.assertRaises(RULES.ContractError): RULES.validate_contract(index)
        with self.assertRaises(RULES.ContractError):
            RULES.strict_load_bytes(b'{"schema_version":1,"contract_type":"terminal"')


class ContractCoverageTests(unittest.TestCase):
    def test_remaining_contract_families_have_valid_examples(self) -> None:
        documents: list[dict[str, object]] = []
        preflight = base("preflight"); preflight.update({"scope_sha256": ZERO,
            "target_identity_sha256": ZERO, "checks": ["identity"], "state": state()}); documents.append(preflight)
        workload = base("workload"); workload.update({"scope_sha256": ZERO,
            "inventory_sha256": ZERO, "entries": [{"query_id": "q", "sql": artifact("q.sql"),
            "inventory_origin": "natural_internal", "inventory_authority": "AUTHORISED",
            "executed_sql_origin": "derived_reproduction", "executed_sql_authority": "AUTHORISED",
            "dataset_origin": "synthetic_control", "dataset_authority": "AUTHORISED",
            "execution_authority": "OPERATION_GO"}]}); documents.append(workload)
        release = base("release"); release.update({"scope_sha256": ZERO, "source_commit": "a" * 40,
            "source_dirty": False, "server_build_sha256": ZERO, "pg_qi_sha256": ZERO,
            "pg_duckdb_sha256": ZERO, "configuration_sha256": ZERO}); documents.append(release)
        resource = base("resource"); resource.update({"scope_sha256": ZERO, "run_id": "r",
            "sample_sequence": 0, "monotonic_ns": 0, "source_id": "proc", "values": {"rss": 1},
            "units": {"rss": "bytes"}, "source_age_ms": 0}); documents.append(resource)
        retirement = base("retirement"); retirement.update({"scope_sha256": ZERO, "run_id": "r",
            "owned_sessions": [], "backend_absence_verified": True, "cached_plans_retired": True,
            "default_off_reconnect": True, "state": state()}); documents.append(retirement)
        recovery = base("recovery"); recovery.update({"scope_sha256": ZERO,
            "strategy": "VERIFIED_ENVIRONMENT_RESTORE", "artifact_sha256": ZERO,
            "configuration_sha256": ZERO, "rehearsal_result": "PASS", "state": state()}); documents.append(recovery)
        for document in documents:
            with self.subTest(kind=document["contract_type"]): RULES.validate_contract(document)


if __name__ == "__main__":
    unittest.main()
