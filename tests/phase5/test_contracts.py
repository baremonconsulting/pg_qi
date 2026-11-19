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
                         ("workload_definition", "measurement_plan", "baseline_manifest", "dataset",
                          "release_definition", "target", "limits",
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
    def test_internal_workload_refuses_customer_inventory_even_if_derived(self) -> None:
        value = base("workload", "workload-customer-inventory")
        value.update({"scope_sha256": ZERO, "inventory_sha256": ZERO,
            "entries": [{"query_id": "q-1", "sql": artifact("q.sql"),
                "inventory_origin": "natural_customer",
                "inventory_authority": "INVENTORY_ONLY",
                "executed_sql_origin": "derived_reproduction",
                "executed_sql_authority": "AUTHORISED",
                "dataset_origin": "derived_reproduction",
                "dataset_authority": "AUTHORISED",
                "execution_authority": "OPERATION_GO"}]})
        with self.assertRaisesRegex(RULES.ContractError, "customer-origin execution"):
            RULES.validate_contract(value)

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
                         "recovery_validation_sha256": ZERO,
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
    def test_customer_go_requires_customer_bindings_only(self) -> None:
        receipt = base("approval_receipt", "customer-go")
        receipt.update({"receipt_kind": "CUSTOMER_OPERATION_GO",
            "scope_sha256": ZERO, "preflight_sha256": ZERO,
            "recovery_validation_sha256": ZERO,
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_through": "2026-01-02T00:00:00Z",
            "approvals": ["operator"], "nonce": "nonce"})
        with self.assertRaisesRegex(RULES.ContractError, "authority bindings"):
            RULES.validate_contract(receipt)
        receipt.update({"dossier_sha256": ZERO, "mandate_sha256": ZERO})
        RULES.validate_contract(receipt)
        receipt["receipt_kind"] = "OPERATION_GO"
        with self.assertRaisesRegex(RULES.ContractError, "customer authority"):
            RULES.validate_contract(receipt)

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
                            "recovery_validation_sha256": ZERO,
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
                         "recovery_validation_sha256": ZERO,
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
            "rehearsal_id": "reh-1", "strategy": "VERIFIED_ENVIRONMENT_RESTORE",
            "artifact_sha256": ZERO, "configuration_sha256": ZERO,
            "r1_terminal_sha256": ZERO, "r2_terminal_sha256": ZERO,
            "walkthrough_sha256": ZERO, "validation_sha256": ZERO,
            "rehearsal_result": "PASS", "eligibility": "ELIGIBLE_FOR_HUMAN_GRANT",
            "limitations": ["NO_SLA_CLAIM", "NO_RTO_CLAIM", "NO_AUTOMATIC_GATE_GRANT"],
            "state": state()}); documents.append(recovery)
        mandate = base("rehearsal_mandate"); mandate.update({"scope_sha256": ZERO,
            "rehearsal_id": "reh-1", "target_copy_identity_sha256": "1" * 64,
            "target_cluster_id": "copy-a", "target_initial_start_id": "start-1",
            "source_environment_identity_sha256": "2" * 64,
            "recovery_plan_sha256": "3" * 64, "runbook_sha256": "4" * 64,
            "artifact_identity_scheme": "PGQI_STACK_SHA256_V1",
            "candidate_artifact_sha256": "c" * 64,
            "candidate_configuration_sha256": "d" * 64,
            "approved_artifact_sha256": ZERO, "approved_configuration_sha256": ZERO,
            "approved_restore_source_sha256": "2" * 64, "pool_profile": "DIRECT",
            "valid_from": "2026-11-18T09:00:00Z", "valid_through": "2026-11-18T12:00:00Z",
            "primary_operator": "primary", "second_operator": "second", "implementer": "builder",
            "approvals": ["technical", "operation", "environment"],
            "allowed_strategies": ["VERIFIED_ENVIRONMENT_RESTORE"],
            "allowed_actions": RULES.REHEARSAL_ACTIONS, "recoverable_copy_verified": True,
            "lifecycle_executor": "HUMAN_OPERATOR_ONLY"}); documents.append(mandate)
        r1 = base("recovery_r1"); r1.update({"scope_sha256": ZERO, "rehearsal_id": "reh-1",
            "mandate_sha256": ZERO, "run_id": "run-1", "steps": RULES.R1_STEPS,
            "pool_profile": "DIRECT", "pool_action": "NOT_APPLICABLE",
            "pool_proof": artifact("pool"),
            "owned_sessions": ["s1"], "positive_probe": artifact("p1"),
            "off_proof": artifact("p2"), "backend_exit_proof": artifact("p3"),
            "fresh_control": artifact("p4"), "started_at": "2026-11-18T10:00:00Z",
            "finished_at": "2026-11-18T10:01:00Z", "duration_ms": 60000,
            "result": "PASS", "state": state()}); documents.append(r1)
        r2 = base("recovery_r2"); r2.update({"scope_sha256": ZERO, "rehearsal_id": "reh-1",
            "mandate_sha256": ZERO, "strategy": "VERIFIED_ENVIRONMENT_RESTORE",
            "r1_terminal_sha256": ZERO, "steps": RULES.R2_STEPS,
            "source_artifact_sha256": ZERO, "source_configuration_sha256": ZERO,
            "result_artifact_sha256": "a" * 64, "result_configuration_sha256": ZERO,
            "restore_source_sha256": "2" * 64, "before_cluster_id": "copy-a",
            "before_start_id": "start-1", "after_cluster_id": "copy-b",
            "after_start_id": "start-2", "same_data_continuity": False,
            "source_identity_proof": artifact("source-identity"),
            "identity_proof": artifact("identity"),
            "fresh_control": artifact("p5"), "started_at": "2026-11-18T10:02:00Z",
            "finished_at": "2026-11-18T10:03:00Z", "duration_ms": 60000,
            "result": "PASS", "state": state()}); documents.append(r2)
        walk = base("operator_walkthrough"); walk.update({"scope_sha256": ZERO,
            "rehearsal_id": "reh-1", "mandate_sha256": ZERO,
            "primary_operator": "primary", "second_operator": "second", "implementer": "builder",
            "runbook_sha256": ZERO, "r1_terminal_sha256": ZERO, "r2_terminal_sha256": ZERO,
            "actions_performed": ["R1_SESSION_RETIREMENT", "INCIDENT_HANDOFF"],
            "signed_at": "2026-11-18T10:05:00Z", "result": "PASS", "state": state()}); documents.append(walk)
        backend = {"cluster_id": "copy", "start_id": "start-2", "pid": 1,
            "backend_start": "2026-11-18T10:00:00Z", "session_id": "s1"}
        incident = base("recovery_incident"); incident.update({"scope_sha256": ZERO,
            "rehearsal_id": "reh-1", "mandate_sha256": ZERO, "incident_id": "i1",
            "steps": RULES.INCIDENT_STEPS, "poisoned_backend": backend,
            "backend_exit_proof": artifact("p6"), "current_go_invalidated": True,
            "performed_at": "2026-11-18T10:04:00Z",
            "invalidated_go_sha256": ZERO, "invalidated_go_nonce": "prior-go",
            "restart_requires_new_go": True, "result": "PASS", "state": state()}); documents.append(incident)
        validation = base("recovery_validation"); validation.update({"scope_sha256": ZERO,
            "rehearsal_id": "reh-1", "mandate_sha256": ZERO, "r1_terminal_sha256": ZERO,
            "r2_terminal_sha256": ZERO, "walkthrough_sha256": ZERO, "incident_sha256": ZERO,
            "checked_at": "2026-11-18T11:00:00Z", "result": "PASS",
            "decision": "ELIGIBLE_FOR_HUMAN_GRANT", "checks": RULES.RECOVERY_CHECKS,
            "limitations": ["NO_SLA_CLAIM", "NO_RTO_CLAIM", "NO_AUTOMATIC_GATE_GRANT"]}); documents.append(validation)
        for document in documents:
            with self.subTest(kind=document["contract_type"]): RULES.validate_contract(document)


if __name__ == "__main__":
    unittest.main()
