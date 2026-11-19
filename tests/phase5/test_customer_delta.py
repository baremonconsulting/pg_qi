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
CHECKED = datetime(2026, 11, 19, 0, 30, tzinfo=timezone.utc)


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


def matrix() -> list[dict]:
    return [{"gate_id": gate, "status": "NOT_RUN", "evidence": None, "receipt": None}
            for gate in sorted(CUSTOMER_DELTA_GATES)]


def closeout() -> dict:
    return {"schema_version": 1, "contract_type": "customer_delta_closeout",
        "contract_id": "customer-delta-closeout", "internal_scope_sha256": None,
        "dossier": None, "sponsor_mandate": None, "internal_closeout_receipt": None,
        "customer_operation_go": None, "gate_reruns": matrix(), "windows": [],
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


def build_prepared(root: Path) -> None:
    root.mkdir(mode=0o700)
    for name in ("workload", "data", "evidence", "recovery", "support", "incident"):
        write(root / f"{name}.txt", f"{name}\n".encode())
    roles = {"technical": "technical-reviewer", "operational": "operations-reviewer",
             "data": "data-reviewer", "legal": "legal-reviewer",
             "customer": "customer-authority"}
    dossier = {"schema_version": 1, "contract_type": "customer_delta_dossier",
        "contract_id": "customer-dossier", "internal_scope_sha256": ZERO,
        "customer_scope_sha256": ONE, "customer_reference": "customer-ref-1",
        "charter_revision": "charter-v1", "environment_class": "NON_PRODUCTION",
        "production_scope": "NONE", "site_sha256": "2" * 64,
        "target_sha256": "3" * 64, "build_sha256": "4" * 64,
        "pool_sha256": "5" * 64,
        "workload_authority": artifact(root, "workload.txt"),
        "data_authority": artifact(root, "data.txt"),
        "evidence_policy": artifact(root, "evidence.txt"),
        "recovery_plan": artifact(root, "recovery.txt"),
        "support_plan": artifact(root, "support.txt"),
        "incident_plan": artifact(root, "incident.txt"), "role_approvals": roles,
        "valid_from": "2026-11-19T00:00:00Z", "valid_through": "2026-11-20T00:00:00Z",
        "changes": [{"dimension": "resource", "before_sha256": "6" * 64,
                     "after_sha256": "7" * 64}],
        "affected_gates": ["CUSTOMER_OPERATION_GO", "RESOURCE_ACCEPTED"],
        "confidentiality_classes": ["CUSTOMER_DATA", "CUSTOMER_IDENTITY",
            "CUSTOMER_QUOTES", "CUSTOMER_RESULTS", "CUSTOMER_SQL"]}
    write(root / "dossier.json", dossier)
    dossier_artifact = artifact(root, "dossier.json")
    internal = gate_receipt("INTERNAL_CLOSEOUT", ZERO, "8" * 64,
                            "2026-11-19T00:00:00Z")
    mandate = gate_receipt("CUSTOMER_PILOT_MANDATE", ONE, dossier_artifact["sha256"],
                           "2026-11-19T00:10:00Z", "customer-ref-1")
    go = {"schema_version": 1, "contract_type": "approval_receipt",
        "contract_id": "customer-go", "receipt_kind": "CUSTOMER_OPERATION_GO",
        "scope_sha256": ONE, "preflight_sha256": dossier["site_sha256"],
        "recovery_validation_sha256": dossier["recovery_plan"]["sha256"],
        "valid_from": "2026-11-19T00:20:00Z", "valid_through": "2026-11-19T01:20:00Z",
        "approvals": sorted(roles.values()), "nonce": "customer-go-nonce"}
    write(root / "internal-closeout.json", internal); write(root / "mandate.json", mandate)
    write(root / "customer-go.json", go); write(root / "resource-evidence.txt", b"resource\n")
    resource_evidence = artifact(root, "resource-evidence.txt")
    resource = gate_receipt("RESOURCE_ACCEPTED", ONE, resource_evidence["sha256"],
                            "2026-11-19T00:15:00Z")
    write(root / "resource-receipt.json", resource)
    reruns = matrix()
    for item in reruns:
        if item["gate_id"] == "RESOURCE_ACCEPTED":
            item.update(status="PASS", evidence=resource_evidence,
                        receipt=artifact(root, "resource-receipt.json"))
        elif item["gate_id"] == "CUSTOMER_OPERATION_GO":
            item.update(status="PASS", evidence=dossier_artifact,
                        receipt=artifact(root, "customer-go.json"))
    value = closeout()
    value.update(internal_scope_sha256=ZERO, dossier=dossier_artifact,
        sponsor_mandate=artifact(root, "mandate.json"),
        internal_closeout_receipt=artifact(root, "internal-closeout.json"),
        customer_operation_go=artifact(root, "customer-go.json"), gate_reruns=reruns,
        not_run_reasons=["CUSTOMER_EXECUTION_UNAVAILABLE"])
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
