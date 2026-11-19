#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.phase5.contract_rules import (CUSTOMER_DELTA_GATES, ContractError,
    canonical_bytes, canonical_sha256, validate_contract)
from tools.phase5.customer_delta import CustomerDeltaError, validate_customer_delta

ZERO = "0" * 64
ONE = "1" * 64
CHECKED = datetime(2026, 11, 19, 1, 30, tzinfo=timezone.utc)


def write(path: Path, value: dict | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value if isinstance(value, bytes) else canonical_bytes(value)
    path.write_bytes(payload); path.chmod(0o600)


def artifact(root: Path, relative: str) -> dict:
    payload = (root / relative).read_bytes()
    return {"path": relative, "sha256": __import__("hashlib").sha256(payload).hexdigest(),
            "bytes": len(payload)}


def state() -> dict:
    return {"lifecycle": "CLOSED", "technical_result": "NOT_RUN",
            "evidence_completeness": "NONE", "exit_state": "NOT_EXITED",
            "value_outcome": "NOT_EVALUATED", "project_decision": "PAUSE"}


def pass_state() -> dict:
    return {"lifecycle": "CLOSED", "technical_result": "PASS",
            "evidence_completeness": "COMPLETE", "exit_state": "CLEAN",
            "value_outcome": "POSITIVE", "project_decision": "CONTINUE"}


def matrix() -> list[dict]:
    return [{"gate_id": gate, "status": "NOT_RUN", "evidence": None, "receipt": None}
            for gate in sorted(CUSTOMER_DELTA_GATES)]


def closeout() -> dict:
    return {"schema_version": 1, "contract_type": "customer_delta_closeout",
        "contract_id": "customer-delta-closeout", "internal_scope_sha256": None,
        "dossier": None, "sponsor_mandate": None, "internal_closeout_validation": None,
        "internal_closeout_receipt": None,
        "site_preflight": None, "recovery_validation": None,
        "customer_operation_go": None, "retirement_evidence": None,
        "gate_reruns": matrix(), "windows": [],
        "failures": [], "not_run_reasons": ["CUSTOMER_EXECUTION_UNAVAILABLE",
            "INTERNAL_CLOSEOUT_NOT_GRANTED", "NO_CUSTOMER", "NO_MANDATE",
            "NO_SPONSOR_DECISION"],
        "generalisation_limits": ["INTERNAL_ONLY_RESULTS", "NO_CUSTOMER_EVIDENCE"],
        "final_environment_state": "NOT_TOUCHED", "share_release": None,
        "publication_release": None, "checked_at": "2026-11-19T00:30:00Z",
        "result": "NOT_RUN", "eligibility": "REFUSED", "state": state()}


def build_not_run(root: Path) -> None:
    root.mkdir(mode=0o700)
    write(root / "customer-delta-closeout.json", closeout())


def gate_receipt(gate: str, scope: str, evidence: str, approved: str,
                 subject: str | None = None) -> dict:
    value = {"schema_version": 1, "contract_type": "gate_receipt",
        "contract_id": f"{gate.lower()}-receipt", "gate_id": gate,
        "scope_sha256": scope, "evidence_sha256": evidence,
        "approved_at": approved, "approvals": [f"{gate.lower()}-reviewer"],
        "nonce": f"{gate.lower()}-nonce"}
    if subject is not None: value["subject_id"] = subject
    return value


def build_prepared(root: Path, *, preflight_pass: bool = True) -> None:
    root.mkdir(mode=0o700)
    scope_names = ("workload_definition", "measurement_plan", "baseline_manifest",
        "dataset", "release_definition", "target", "limits", "evidence_policy",
        "recovery_plan", "export_policy", "toolkit", "role_bindings", "window")
    changed_names = {"workload_definition", "dataset", "release_definition",
                     "target", "limits", "role_bindings", "window"}
    for prefix in ("internal", "customer"):
        for name in scope_names:
            variant = "customer" if prefix == "customer" and name in changed_names else "shared"
            write(root / f"{prefix}-{name}.json", f"{name}:{variant}\n".encode())
    internal_scope = {"schema_version": 1, "contract_type": "scope",
        "contract_id": "internal-scope", "scope_inputs": {
            name: artifact(root, f"internal-{name}.json") for name in scope_names},
        "gates_required": ["INTERNAL_CLOSEOUT"]}
    customer_scope = {"schema_version": 1, "contract_type": "scope",
        "contract_id": "customer-scope", "scope_inputs": {
            name: artifact(root, f"customer-{name}.json") for name in scope_names},
        "gates_required": sorted(CUSTOMER_DELTA_GATES)}
    write(root / "internal-scope.json", internal_scope)
    write(root / "customer-scope.json", customer_scope)
    internal_sha, customer_sha = canonical_sha256(internal_scope), canonical_sha256(customer_scope)
    preflight = {"schema_version": 1, "contract_type": "preflight",
        "contract_id": "customer-preflight", "scope_sha256": customer_sha,
        "target_identity_sha256": customer_scope["scope_inputs"]["target"]["sha256"],
        "checked_at": "2026-11-19T00:12:00Z",
        "checks": ["target_identity"], "state": pass_state() if preflight_pass else {
            **pass_state(), "technical_result": "FAIL", "value_outcome": "NEGATIVE",
            "project_decision": "STOP"}}
    from tools.phase5.contract_rules import RECOVERY_CHECKS
    recovery = {"schema_version": 1, "contract_type": "recovery_validation",
        "contract_id": "customer-recovery", "scope_sha256": customer_sha,
        "rehearsal_id": "customer-rehearsal", "mandate_sha256": "a" * 64,
        "r1_terminal_sha256": "b" * 64, "r2_terminal_sha256": "c" * 64,
        "walkthrough_sha256": "d" * 64, "incident_sha256": "e" * 64,
        "checked_at": "2026-11-19T00:13:00Z", "result": "PASS",
        "decision": "ELIGIBLE_FOR_HUMAN_GRANT", "checks": RECOVERY_CHECKS,
        "limitations": ["NO_AUTOMATIC_GATE_GRANT", "NO_RTO_CLAIM", "NO_SLA_CLAIM"]}
    write(root / "customer-preflight.json", preflight)
    write(root / "customer-recovery.json", recovery)
    write(root / "support.txt", b"support\n"); write(root / "incident.txt", b"incident\n")
    roles = {"technical": "technical-reviewer", "operational": "operations-reviewer",
             "data": "data-reviewer", "legal": "legal-reviewer",
             "customer": "customer-authority"}
    customer_roles = {"schema_version": 1, "contract_type": "customer_role_bindings",
        "contract_id": "customer-role-bindings", "roles": roles}
    customer_window = {"schema_version": 1, "contract_type": "customer_window",
        "contract_id": "customer-window", "valid_from": "2026-11-19T00:00:00Z",
        "valid_through": "2026-11-20T00:00:00Z"}
    write(root / "customer-role_bindings.json", customer_roles)
    write(root / "customer-window.json", customer_window)
    customer_scope["scope_inputs"]["role_bindings"] = artifact(
        root, "customer-role_bindings.json")
    customer_scope["scope_inputs"]["window"] = artifact(root, "customer-window.json")
    write(root / "customer-scope.json", customer_scope)
    customer_sha = canonical_sha256(customer_scope)
    preflight["scope_sha256"] = customer_sha
    recovery["scope_sha256"] = customer_sha
    write(root / "customer-preflight.json", preflight)
    write(root / "customer-recovery.json", recovery)
    dossier = {"schema_version": 1, "contract_type": "customer_delta_dossier",
        "contract_id": "customer-dossier",
        "internal_scope": artifact(root, "internal-scope.json"),
        "customer_scope": artifact(root, "customer-scope.json"),
        "internal_scope_sha256": internal_sha, "customer_scope_sha256": customer_sha,
        "customer_reference": "cust-" + "f" * 64,
        "charter_revision": "charter-v1", "environment_class": "NON_PRODUCTION",
        "production_scope": "NONE",
        "site_sha256": customer_scope["scope_inputs"]["target"]["sha256"],
        "target_sha256": customer_scope["scope_inputs"]["target"]["sha256"],
        "build_sha256": customer_scope["scope_inputs"]["release_definition"]["sha256"],
        "pool_sha256": customer_scope["scope_inputs"]["limits"]["sha256"],
        "workload_authority": customer_scope["scope_inputs"]["workload_definition"],
        "data_authority": customer_scope["scope_inputs"]["dataset"],
        "evidence_policy": customer_scope["scope_inputs"]["evidence_policy"],
        "recovery_plan": customer_scope["scope_inputs"]["recovery_plan"],
        "support_plan": artifact(root, "support.txt"),
        "incident_plan": artifact(root, "incident.txt"), "role_approvals": roles,
        "valid_from": "2026-11-19T00:00:00Z", "valid_through": "2026-11-20T00:00:00Z",
        "changes": [{"scope_input": name,
            "before_sha256": internal_scope["scope_inputs"][name]["sha256"],
            "after_sha256": customer_scope["scope_inputs"][name]["sha256"]}
            for name in sorted(changed_names)],
        "affected_gates": sorted(CUSTOMER_DELTA_GATES),
        "confidentiality_classes": ["CUSTOMER_DATA", "CUSTOMER_IDENTITY",
            "CUSTOMER_QUOTES", "CUSTOMER_RESULTS", "CUSTOMER_SQL"]}
    write(root / "dossier.json", dossier)
    dossier_artifact = artifact(root, "dossier.json")
    internal_validation = {"schema_version": 1,
        "document_type": "internal_pilot_validation", "scope_sha256": internal_sha,
        "result": "PASS", "decision": "ELIGIBLE_FOR_HUMAN_GRANT",
        "project_decision": "CONTINUE", "window_count": 1, "pilot_id": "pilot-1",
        "checked_at": "2026-11-19T00:00:00+00:00"}
    write(root / "internal-validation.json", internal_validation)
    internal_validation_artifact = artifact(root, "internal-validation.json")
    internal = gate_receipt("INTERNAL_CLOSEOUT", internal_sha,
                            internal_validation_artifact["sha256"],
                            "2026-11-19T00:00:00Z")
    mandate = gate_receipt("CUSTOMER_PILOT_MANDATE", customer_sha,
                           dossier_artifact["sha256"], "2026-11-19T00:10:00Z",
                           "cust-" + "f" * 64)
    mandate["approvals"] = ["independent-sponsor"]
    recovery["mandate_sha256"] = canonical_sha256(mandate)
    write(root / "customer-recovery.json", recovery)
    go = {"schema_version": 1, "contract_type": "approval_receipt",
        "contract_id": "customer-go", "receipt_kind": "CUSTOMER_OPERATION_GO",
        "scope_sha256": customer_sha, "preflight_sha256": canonical_sha256(preflight),
        "recovery_validation_sha256": canonical_sha256(recovery),
        "dossier_sha256": dossier_artifact["sha256"],
        "mandate_sha256": canonical_sha256(mandate),
        "valid_from": "2026-11-19T00:20:00Z", "valid_through": "2026-11-19T01:20:00Z",
        "approvals": sorted(roles.values()), "nonce": "customer-go-nonce"}
    write(root / "internal-closeout.json", internal); write(root / "mandate.json", mandate)
    write(root / "customer-go.json", go)
    retirement = {"schema_version": 1,
        "contract_type": "customer_retirement_evidence",
        "contract_id": "customer-retirement", "scope_sha256": customer_sha,
        "target_sha256": dossier["target_sha256"],
        "retired_at": "2026-11-19T01:25:00Z", "result": "PASS"}
    write(root / "customer-retirement.json", retirement)
    reruns = matrix()
    for item in reruns:
        if item["gate_id"] == "CUSTOMER_OPERATION_GO":
            item.update(status="PASS", evidence=dossier_artifact,
                        receipt=artifact(root, "customer-go.json"))
        else:
            if item["gate_id"] == "RELEASE_SITE_QUALIFIED":
                gate_evidence = artifact(root, "customer-preflight.json")
            elif item["gate_id"] == "RECOVERY_REHEARSED":
                gate_evidence = artifact(root, "customer-recovery.json")
            else:
                evidence_path = f"{item['gate_id'].lower()}-evidence.json"
                from tools.phase5.customer_delta import GATE_SOURCE_INPUTS
                evidence_value = {"schema_version": 1,
                    "contract_type": "customer_gate_evidence",
                    "contract_id": f"{item['gate_id'].lower()}-evidence",
                    "gate_id": item["gate_id"], "scope_sha256": customer_sha,
                    "source_sha256": canonical_sha256({"gate_id": item["gate_id"],
                        "inputs": {source: customer_scope["scope_inputs"][source]["sha256"]
                                   for source in GATE_SOURCE_INPUTS[item["gate_id"]]}}),
                    "checked_at": "2026-11-19T00:14:00Z", "result": "PASS"}
                write(root / evidence_path, evidence_value)
                gate_evidence = artifact(root, evidence_path)
            receipt = gate_receipt(item["gate_id"], customer_sha,
                                   gate_evidence["sha256"], "2026-11-19T00:15:00Z")
            receipt_path = f"{item['gate_id'].lower()}-receipt.json"
            write(root / receipt_path, receipt)
            item.update(status="PASS", evidence=gate_evidence,
                        receipt=artifact(root, receipt_path))
    value = closeout()
    value.update(internal_scope_sha256=internal_sha, dossier=dossier_artifact,
        sponsor_mandate=artifact(root, "mandate.json"),
        internal_closeout_validation=internal_validation_artifact,
        internal_closeout_receipt=artifact(root, "internal-closeout.json"),
        site_preflight=artifact(root, "customer-preflight.json"),
        recovery_validation=artifact(root, "customer-recovery.json"),
        customer_operation_go=artifact(root, "customer-go.json"),
        retirement_evidence=artifact(root, "customer-retirement.json"), gate_reruns=reruns,
        not_run_reasons=["CUSTOMER_EXECUTION_UNAVAILABLE"],
        final_environment_state="RETIRED_OFF", checked_at="2026-11-19T01:30:00Z")
    write(root / "customer-delta-closeout.json", value)


class CustomerDeltaTests(unittest.TestCase):
    def test_committed_not_run_record_is_exact_current_state(self) -> None:
        path = ROOT / "docs/phase5/current/p5-8-customer-delta-not-run.json"
        committed = json.loads(path.read_text())
        validate_contract(committed)
        self.assertEqual(committed, closeout())

    def test_empty_authority_bundle_is_explicit_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_not_run(root)
            result = validate_customer_delta(root, CHECKED)
            self.assertEqual((result["result"], result["decision"]), ("NOT_RUN", "REFUSED"))
            self.assertIsNone(result["dossier_sha256"])

    def test_complete_dossier_is_still_not_run_without_execution_path(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_prepared(root)
            result = validate_customer_delta(root, CHECKED)
            self.assertEqual(result["result"], "NOT_RUN")
            self.assertIn("CUSTOMER_EXECUTION_UNAVAILABLE", result["checks"])

    def test_partial_authority_graph_and_false_pass_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_not_run(root)
            value = json.loads((root / "customer-delta-closeout.json").read_text())
            value["internal_scope_sha256"] = ZERO
            write(root / "customer-delta-closeout.json", value)
            with self.assertRaisesRegex(CustomerDeltaError, "false_claim"):
                validate_customer_delta(root, CHECKED)
        value = closeout(); value["result"] = "PASS"
        with self.assertRaises(ContractError): validate_contract(value)

    def test_internal_receipt_cannot_replace_customer_mandate(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_prepared(root)
            value = json.loads((root / "customer-delta-closeout.json").read_text())
            value["sponsor_mandate"] = value["internal_closeout_receipt"]
            write(root / "customer-delta-closeout.json", value)
            with self.assertRaisesRegex(CustomerDeltaError, "authority_graph_invalid"):
                validate_customer_delta(root, CHECKED)

    def test_declared_delta_and_internal_validation_are_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_prepared(root)
            dossier = json.loads((root / "dossier.json").read_text())
            dossier["changes"] = dossier["changes"][:-1]
            write(root / "dossier.json", dossier)
            value = json.loads((root / "customer-delta-closeout.json").read_text())
            value["dossier"] = artifact(root, "dossier.json")
            mandate = json.loads((root / "mandate.json").read_text())
            mandate["evidence_sha256"] = value["dossier"]["sha256"]
            write(root / "mandate.json", mandate)
            value["sponsor_mandate"] = artifact(root, "mandate.json")
            value["gate_reruns"] = [({**row, "evidence": value["dossier"]}
                if row["gate_id"] == "CUSTOMER_OPERATION_GO" else row)
                for row in value["gate_reruns"]]
            write(root / "customer-delta-closeout.json", value)
            with self.assertRaisesRegex(CustomerDeltaError, "scope_or_gate_claim_invalid"):
                validate_customer_delta(root, CHECKED)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_prepared(root)
            validation = json.loads((root / "internal-validation.json").read_text())
            validation["result"] = "FAIL"
            write(root / "internal-validation.json", validation)
            value = json.loads((root / "customer-delta-closeout.json").read_text())
            value["internal_closeout_validation"] = artifact(root, "internal-validation.json")
            receipt = json.loads((root / "internal-closeout.json").read_text())
            receipt["evidence_sha256"] = value["internal_closeout_validation"]["sha256"]
            write(root / "internal-closeout.json", receipt)
            value["internal_closeout_receipt"] = artifact(root, "internal-closeout.json")
            write(root / "customer-delta-closeout.json", value)
            with self.assertRaisesRegex(CustomerDeltaError, "authority_graph_invalid"):
                validate_customer_delta(root, CHECKED)

    def test_failing_preflight_cannot_support_customer_go(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_prepared(root, preflight_pass=False)
            with self.assertRaisesRegex(CustomerDeltaError, "authority_graph_invalid"):
                validate_customer_delta(root, CHECKED)

    def test_approval_order_independence_and_final_state_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_prepared(root)
            value = json.loads((root / "customer-delta-closeout.json").read_text())
            receipt_path = "qualification_authorized-receipt.json"
            receipt = json.loads((root / receipt_path).read_text())
            mandate = json.loads((root / "mandate.json").read_text())
            receipt["nonce"] = mandate["nonce"]
            write(root / receipt_path, receipt)
            for row in value["gate_reruns"]:
                if row["gate_id"] == "QUALIFICATION_AUTHORIZED":
                    row["receipt"] = artifact(root, receipt_path)
            write(root / "customer-delta-closeout.json", value)
            with self.assertRaisesRegex(CustomerDeltaError, "not_independent"):
                validate_customer_delta(root, CHECKED)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_prepared(root)
            value = json.loads((root / "customer-delta-closeout.json").read_text())
            value["final_environment_state"] = "NOT_TOUCHED"
            write(root / "customer-delta-closeout.json", value)
            with self.assertRaisesRegex(CustomerDeltaError, "false_execution_claim"):
                validate_customer_delta(root, CHECKED)

    def test_internal_artifact_cannot_stand_in_for_customer_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_prepared(root)
            value = json.loads((root / "customer-delta-closeout.json").read_text())
            internal_evidence = value["internal_closeout_validation"]
            receipt_path = "qualification_authorized-receipt.json"
            receipt = json.loads((root / receipt_path).read_text())
            receipt["evidence_sha256"] = internal_evidence["sha256"]
            write(root / receipt_path, receipt)
            for row in value["gate_reruns"]:
                if row["gate_id"] == "QUALIFICATION_AUTHORIZED":
                    row["evidence"] = internal_evidence
                    row["receipt"] = artifact(root, receipt_path)
            write(root / "customer-delta-closeout.json", value)
            with self.assertRaisesRegex(CustomerDeltaError,
                                        "invalid_customer_gate_evidence_artifact"):
                validate_customer_delta(root, CHECKED)

    def test_customer_go_never_unlocks_existing_measurement_runtime(self) -> None:
        source = (ROOT / "tools/phase5/measurement_runtime.py").read_text()
        self.assertIn('receipt_kind") != "OPERATION_GO"', source)
        self.assertNotIn('receipt_kind") not in {"OPERATION_GO", "CUSTOMER_OPERATION_GO"}', source)

    def test_cli_writes_once_and_has_no_execute_interface(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; build_not_run(root)
            output = Path(name) / "validation.json"
            command = [sys.executable, str(ROOT / "tools/phase5/customer_delta.py"),
                       "--bundle", str(root), "--output", str(output)]
            self.assertEqual(subprocess.run(command, check=False).returncode, 0)
            self.assertEqual(subprocess.run(command, check=False).returncode, 1)
            help_text = subprocess.run(command[:2] + ["--help"], check=False,
                capture_output=True, text=True).stdout
            self.assertNotIn("execute", help_text.lower())
            self.assertNotIn("conninfo", help_text.lower())


if __name__ == "__main__":
    unittest.main()
