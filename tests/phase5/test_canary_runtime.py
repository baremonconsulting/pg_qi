#!/usr/bin/env python3
"""Fail-closed acceptance tests for the P5-7A single-session canary."""

from __future__ import annotations

import tempfile
import unittest
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_contracts import snapshot_tuple
from test_measurement_runtime import (
    artifact, level_one_authorization, measurement_scope, plan, state, write_document)
from tools.phase5.canary_runtime import CanaryError, build_canary_record, validate_canary_bundle
from tools.phase5.contract_rules import CANARY_CONTROLS, canonical_sha256, validate_contract


def utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def raw(root: Path, name: str, value: dict) -> dict:
    write_document(root / name, value)
    return artifact(root, name)


def build_canary(root: Path) -> tuple[datetime, dict, dict, dict]:
    root.chmod(0o700)
    measurement = plan(levels=1)
    _, scope_sha = measurement_scope(measurement)
    write_document(root / "measurement-plan.json", measurement)
    authorization = level_one_authorization(root, canonical_sha256(measurement), scope_sha)
    write_document(root / "level-authorization.json", authorization)
    operation_go = __import__("json").loads((root / "operation-go.json").read_text())

    sql = b"SELECT count(*) FROM pg_catalog.pg_class /* approved literal */"
    (root / "positive.sql").write_bytes(sql); (root / "positive.sql").chmod(0o600)
    expected = snapshot_tuple()
    plan_doc = {"schema_version": 1, "contract_type": "canary_plan",
        "contract_id": "canary-plan-1", "scope_sha256": scope_sha,
        "canary_id": "canary-1", "measurement_plan": artifact(root, "measurement-plan.json"),
        "level_authorization": artifact(root, "level-authorization.json"),
        "operation_go_sha256": canonical_sha256(operation_go),
        "positive_query_id": "positive-1", "positive_sql": artifact(root, "positive.sql"),
        "positive_literal_sha256": artifact(root, "positive.sql")["sha256"],
        "negative_controls": list(CANARY_CONTROLS), "expected_snapshot": expected,
        "maximum_result_bytes": 4096, "session_count": 1, "pool_profile": "DIRECT"}
    validate_contract(plan_doc); write_document(root / "canary-plan.json", plan_doc)

    base = datetime.now(timezone.utc)
    (root / "positive.spool").write_bytes(b"42\n"); (root / "positive.spool").chmod(0o600)
    spool = artifact(root, "positive.spool")
    request = {"schema_version": 1, "contract_type": "request", "contract_id": "request-1",
        "scope_sha256": scope_sha, "run_id": "canary-1", "session_id": "session-1",
        "request_id": "request-1", "query_id": "positive-1", "execution_status": "SUCCEEDED",
        "planning_evidence": "FRESH", "snapshot_relation": "CURRENT",
        "snapshot_id": "snapshot-1", "planning_request_id": "planning-1",
        "observed_spool": spool}
    snapshot = {"schema_version": 1, "contract_type": "snapshot", "contract_id": "snapshot-1",
        "scope_sha256": scope_sha, "snapshot_id": "snapshot-1", "session_id": "session-1",
        "planning_request_id": "planning-1", "prepared_object_id": "positive-1",
        "prepared_generation": 1, "decision_id": 1, "tuple": expected}
    request_art = raw(root, "positive-request.json", request)
    snapshot_art = raw(root, "positive-snapshot.json", snapshot)
    controls = []
    for index, control_id in enumerate(CANARY_CONTROLS, 1):
        controls.append(raw(root, f"control-{index}.json", {"document_type": "canary_control",
            "control_id": control_id, "session_id": "session-1", "execution_status": "SUCCEEDED",
            "planning_evidence": "UNAVAILABLE", "snapshot_relation": "NO_SNAPSHOT",
            "observed_engine": "postgres", "performed_at": utc(base + timedelta(seconds=index)),
            "result": "PASS"}))
    oracle = raw(root, "oracle.json", {"document_type": "canary_oracle",
        "query_id": "positive-1", "spool_sha256": spool["sha256"], "result": "PASS"})
    run = {"schema_version": 1, "contract_type": "canary_run", "contract_id": "canary-run-1",
        "scope_sha256": scope_sha, "canary_id": "canary-1",
        "plan_sha256": canonical_sha256(plan_doc),
        "operation_go_sha256": canonical_sha256(operation_go), "session_id": "session-1",
        "positive_request": request_art, "positive_snapshot": snapshot_art,
        "positive_spool": spool, "control_proofs": controls,
        "ambient_force_off_proof": controls[0], "oracle_proof": oracle,
        "started_at": utc(base), "finished_at": utc(base + timedelta(seconds=4)),
        "duration_ms": 4000, "result": "PASS", "state": state()}
    validate_contract(run); write_document(root / "canary-run.json", run)
    exit_proof = raw(root, "backend-exit.json", {"document_type": "canary_backend_exit",
        "session_id": "session-1", "observer_authorized": True,
        "observation_complete": True, "backend_present": False,
        "observed_at": utc(base + timedelta(seconds=5)), "result": "PASS"})
    reconnect = raw(root, "fresh-reconnect.json", {"document_type": "canary_fresh_reconnect",
        "session_id": "session-2", "connected_at": utc(base + timedelta(seconds=6)),
        "mode": "off", "force_execution": "off", "observed_engine": "postgres",
        "result": "PASS"})
    retirement = {"schema_version": 1, "contract_type": "canary_retirement",
        "contract_id": "canary-retirement-1", "scope_sha256": scope_sha,
        "canary_id": "canary-1", "plan_sha256": canonical_sha256(plan_doc),
        "run_sha256": canonical_sha256(run), "owned_session_id": "session-1",
        "backend_exit_proof": exit_proof, "fresh_reconnect_proof": reconnect,
        "performed_at": utc(base + timedelta(seconds=5)), "result": "PASS", "state": state()}
    validate_contract(retirement); write_document(root / "canary-retirement.json", retirement)
    return base + timedelta(seconds=7), plan_doc, run, retirement


class CanaryRuntimeTests(unittest.TestCase):
    def test_complete_canary_is_only_eligible_for_human_grant(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            result = validate_canary_bundle(root, checked)
            self.assertEqual(result["decision"], "ELIGIBLE_FOR_HUMAN_GRANT")
            self.assertNotIn("CANARY_ACCEPTED", str(result))
            record = build_canary_record(result); validate_contract(record)
            self.assertEqual(record["eligibility"], "ELIGIBLE_FOR_HUMAN_GRANT")

    def test_cross_session_snapshot_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            value = __import__("json").loads((root / "positive-snapshot.json").read_text())
            value["session_id"] = "other-session"; write_document(root / "positive-snapshot.json", value)
            run = __import__("json").loads((root / "canary-run.json").read_text())
            run["positive_snapshot"] = artifact(root, "positive-snapshot.json")
            write_document(root / "canary-run.json", run)
            with self.assertRaisesRegex(CanaryError, "identity_graph_mismatch"):
                validate_canary_bundle(root, checked)

    def test_wrong_control_order_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            run = __import__("json").loads((root / "canary-run.json").read_text())
            run["control_proofs"][1], run["control_proofs"][2] = (
                run["control_proofs"][2], run["control_proofs"][1])
            write_document(root / "canary-run.json", run)
            with self.assertRaises(CanaryError): validate_canary_bundle(root, checked)

    def test_client_close_without_observed_backend_exit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            proof = __import__("json").loads((root / "backend-exit.json").read_text())
            proof["observation_complete"] = False; write_document(root / "backend-exit.json", proof)
            retirement = __import__("json").loads((root / "canary-retirement.json").read_text())
            retirement["backend_exit_proof"] = artifact(root, "backend-exit.json")
            write_document(root / "canary-retirement.json", retirement)
            with self.assertRaises(CanaryError): validate_canary_bundle(root, checked)

    def test_tampering_and_partial_run_are_not_relabelled_pass(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            (root / "positive.spool").write_bytes(b"changed\n")
            with self.assertRaises(CanaryError): validate_canary_bundle(root, checked)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            run = __import__("json").loads((root / "canary-run.json").read_text())
            run["result"] = "INCOMPLETE"
            run["state"] = state("INCONCLUSIVE", "UNKNOWN")
            write_document(root / "canary-run.json", run)
            with self.assertRaises(CanaryError): validate_canary_bundle(root, checked)


if __name__ == "__main__":
    unittest.main()
