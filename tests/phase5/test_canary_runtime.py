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
from test_evidence_tool import build_private, reseal as reseal_private
from test_measurement_runtime import (
    artifact, level_one_authorization, measurement_scope, plan, state, write_document)
from tools.phase5.canary_runtime import CanaryError, build_canary_record, main, validate_canary_bundle
from tools.phase5.contract_rules import CANARY_CONTROLS, canonical_sha256, validate_contract
from tools.phase5.recovery_rehearsal import validate_rehearsal_contracts


def utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def raw(root: Path, name: str, value: dict) -> dict:
    write_document(root / name, value)
    return artifact(root, name)


def reseal_run(root: Path, run: dict) -> None:
    write_document(root / "canary-run.json", run)
    retirement = __import__("json").loads((root / "canary-retirement.json").read_text())
    retirement["run_sha256"] = canonical_sha256(run)
    write_document(root / "canary-retirement.json", retirement)


def reseal_all(root: Path, plan_value: dict, run: dict) -> None:
    write_document(root / "canary-plan.json", plan_value)
    run["plan_sha256"] = canonical_sha256(plan_value)
    write_document(root / "canary-run.json", run)
    retirement = __import__("json").loads((root / "canary-retirement.json").read_text())
    retirement["plan_sha256"] = canonical_sha256(plan_value)
    retirement["run_sha256"] = canonical_sha256(run)
    write_document(root / "canary-retirement.json", retirement)


def align_authorization(root: Path, measurement: dict, scope_sha: str, authorization: dict) -> dict:
    load = lambda name: __import__("json").loads((root / name).read_text())
    mandate, r1, r2 = (load("operation-mandate.json"), load("operation-r1.json"),
                       load("operation-r2.json"))
    walkthrough, incident = load("operation-walkthrough.json"), load("operation-incident.json")
    closure = measurement["closure_inputs"]
    mandate.update(recovery_plan_sha256=closure["recovery_plan_sha256"],
        source_environment_identity_sha256=closure["target_sha256"],
        approved_artifact_sha256=closure["baseline_artifact_sha256"],
        approved_configuration_sha256=closure["baseline_configuration_sha256"],
        candidate_artifact_sha256=closure["release_artifact_sha256"],
        candidate_configuration_sha256=closure["release_configuration_sha256"])
    mandate_sha = canonical_sha256(mandate)
    r1.update(scope_sha256=scope_sha, mandate_sha256=mandate_sha)
    r1_sha = canonical_sha256(r1)
    source = load("r2-source.json")
    source.update(artifact_sha256=closure["release_artifact_sha256"],
                  configuration_sha256=closure["release_configuration_sha256"])
    write_document(root / "r2-source.json", source)
    identity = load("r2-identity.json")
    identity.update(artifact_sha256=closure["baseline_artifact_sha256"],
                    configuration_sha256=closure["baseline_configuration_sha256"])
    write_document(root / "r2-identity.json", identity)
    r2.update(scope_sha256=scope_sha, mandate_sha256=mandate_sha, r1_terminal_sha256=r1_sha,
        source_artifact_sha256=closure["release_artifact_sha256"],
        source_configuration_sha256=closure["release_configuration_sha256"],
        source_identity_proof=artifact(root, "r2-source.json"),
        result_artifact_sha256=closure["baseline_artifact_sha256"],
        result_configuration_sha256=closure["baseline_configuration_sha256"],
        identity_proof=artifact(root, "r2-identity.json"))
    r2_sha = canonical_sha256(r2)
    walkthrough.update(scope_sha256=scope_sha, mandate_sha256=mandate_sha,
                       r1_terminal_sha256=r1_sha, r2_terminal_sha256=r2_sha)
    incident.update(scope_sha256=scope_sha, mandate_sha256=mandate_sha)
    validation = validate_rehearsal_contracts(root, mandate, r1, r2, walkthrough, incident,
        datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc))
    for name, value in (("operation-mandate.json", mandate), ("operation-r1.json", r1),
                        ("operation-r2.json", r2), ("operation-walkthrough.json", walkthrough),
                        ("operation-incident.json", incident),
                        ("operation-recovery-validation.json", validation)):
        write_document(root / name, value)
    go = load("operation-go.json")
    go["recovery_validation_sha256"] = canonical_sha256(validation)
    write_document(root / "operation-go.json", go)
    mapping = {"operation_go": "operation-go.json",
        "operation_rehearsal_mandate": "operation-mandate.json",
        "operation_recovery_r1": "operation-r1.json", "operation_recovery_r2": "operation-r2.json",
        "operation_operator_walkthrough": "operation-walkthrough.json",
        "operation_recovery_incident": "operation-incident.json",
        "operation_recovery_validation": "operation-recovery-validation.json"}
    authorization.update(plan_sha256=canonical_sha256(measurement),
                         approval_reference=measurement["concurrency_levels"][0]["approval_reference"])
    for key, name in mapping.items(): authorization[key] = artifact(root, name)
    return authorization


def build_canary(root: Path) -> tuple[datetime, dict, dict, dict]:
    root.chmod(0o700)
    private = root / "private"; private.mkdir(mode=0o700)
    build_private(private, query_id="query-a")
    for relative, changes in (("payload/request.json", {"run_id": "canary-1",
            "request_id": "request-1", "planning_request_id": "request-1"}),
            ("payload/snapshot.json", {"planning_request_id": "request-1"}),
            ("payload/resource.json", {"run_id": "canary-1"}),
            ("payload/retirement.json", {"run_id": "canary-1"})):
        document = __import__("json").loads((private / relative).read_text())
        document.update(changes); write_document(private / relative, document)
    reseal_private(private)
    for path in private.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    measurement = __import__("json").loads((private / "inputs/measurement_plan.json").read_text())
    scope_doc = __import__("json").loads((private / "scope.json").read_text())
    scope_sha = canonical_sha256(scope_doc)
    write_document(root / "measurement-plan.json", measurement)
    authorization = level_one_authorization(root, canonical_sha256(measurement), scope_sha)
    authorization = align_authorization(root, measurement, scope_sha, authorization)
    write_document(root / "level-authorization.json", authorization)
    operation_go = __import__("json").loads((root / "operation-go.json").read_text())

    expected = snapshot_tuple()
    for index, sql in enumerate((b"SHOW duckdb.force_execution\n",
                                  b"SELECT unsupported_control()\n",
                                  b"SELECT $1::integer\n"), 1):
        (root / f"control-{index}.sql").write_bytes(sql)
        (root / f"control-{index}.sql").chmod(0o600)
    plan_doc = {"schema_version": 1, "contract_type": "canary_plan",
        "contract_id": "canary-plan-1", "scope_sha256": scope_sha,
        "canary_id": "canary-1", "measurement_plan": artifact(root, "measurement-plan.json"),
        "level_authorization": artifact(root, "level-authorization.json"),
        "operation_go_sha256": canonical_sha256(operation_go),
        "private_bundle_path": "private", "positive_query_id": "query-a",
        "positive_sql": artifact(root, "private/raw/q.sql"),
        "positive_literal_sha256": artifact(root, "private/raw/q.sql")["sha256"],
        "expected_spool": artifact(root, "private/raw/expected.spool"),
        "negative_controls": list(CANARY_CONTROLS), "expected_snapshot": expected,
        "negative_control_sql": [artifact(root, f"control-{index}.sql") for index in range(1, 4)],
        "maximum_result_bytes": 4096, "session_count": 1, "pool_profile": "DIRECT"}
    validate_contract(plan_doc); write_document(root / "canary-plan.json", plan_doc)

    base = datetime.now(timezone.utc) - timedelta(seconds=10)
    spool = artifact(root, "private/raw/observed.spool")
    request = {"schema_version": 1, "contract_type": "request", "contract_id": "request-1",
        "scope_sha256": scope_sha, "run_id": "canary-1", "session_id": "session-1",
        "request_id": "request-1", "query_id": "query-a", "execution_status": "SUCCEEDED",
        "planning_evidence": "FRESH", "snapshot_relation": "CURRENT",
        "snapshot_id": "snapshot-1", "planning_request_id": "request-1",
        "observed_spool": spool, "request_interval": {"started_monotonic_ns": 100,
            "finished_monotonic_ns": 200, "duration_ns": 100,
            "state": "COMPLETE", "censored": False}}
    snapshot = {"schema_version": 1, "contract_type": "snapshot", "contract_id": "snapshot-1",
        "scope_sha256": scope_sha, "snapshot_id": "snapshot-1", "session_id": "session-1",
        "planning_request_id": "request-1", "prepared_object_id": "query-a",
        "prepared_generation": 1, "decision_id": 1, "tuple": expected}
    request_art = raw(root, "positive-request.json", request)
    snapshot_art = raw(root, "positive-snapshot.json", snapshot)
    controls = []
    for index, control_id in enumerate(CANARY_CONTROLS, 1):
        controls.append(raw(root, f"control-{index}.json", {"document_type": "canary_control",
            "control_id": control_id, "session_id": "session-1", "execution_status": "SUCCEEDED",
            "planning_evidence": "UNAVAILABLE", "snapshot_relation": "NO_SNAPSHOT",
            "observed_engine": "postgres", "executed_sql_sha256":
            plan_doc["negative_control_sql"][index - 1]["sha256"], "mode": "lab_allowlist",
            "force_execution": "off", "performed_at": utc(base + timedelta(seconds=(1, 4, 5)[index - 1])),
            "result": "PASS"}))
    oracle = raw(root, "oracle.json", {"document_type": "canary_oracle",
        "query_id": "query-a", "spool_sha256": spool["sha256"], "result": "PASS"})
    positive_execution = raw(root, "positive-execution.json", {
        "document_type": "canary_positive_execution", "query_id": "query-a",
        "session_id": "session-1", "executed_sql_sha256": plan_doc["positive_literal_sha256"],
        "observed_spool": spool,
        "started_at": utc(base + timedelta(seconds=2)),
        "finished_at": utc(base + timedelta(seconds=3)), "result": "PASS"})
    mandate = __import__("json").loads((root / "operation-mandate.json").read_text())
    recovery_r2 = __import__("json").loads((root / "operation-r2.json").read_text())
    backend = {"cluster_id": mandate["target_cluster_id"],
        "start_id": recovery_r2["after_start_id"], "pid": 1234,
        "backend_start": utc(base - timedelta(seconds=10)), "session_id": "session-1"}
    run = {"schema_version": 1, "contract_type": "canary_run", "contract_id": "canary-run-1",
        "scope_sha256": scope_sha, "canary_id": "canary-1",
        "plan_sha256": canonical_sha256(plan_doc),
        "operation_go_sha256": canonical_sha256(operation_go), "session_id": "session-1",
        "backend_identity": backend, "positive_execution_proof": positive_execution,
        "positive_request": request_art, "positive_snapshot": snapshot_art,
        "positive_spool": spool, "control_proofs": controls,
        "ambient_force_off_proof": controls[0], "oracle_proof": oracle,
        "started_at": utc(base), "finished_at": utc(base + timedelta(seconds=6)),
        "duration_ms": 6000, "result": "PASS", "state": state()}
    validate_contract(run); write_document(root / "canary-run.json", run)
    exit_proof = raw(root, "backend-exit.json", {"document_type": "canary_backend_exit",
        **backend, "observer_authorized": True,
        "observation_complete": True, "backend_present": False,
        "observed_at": utc(base + timedelta(seconds=7)), "result": "PASS"})
    reconnect = raw(root, "fresh-reconnect.json", {"document_type": "canary_fresh_reconnect",
        "cluster_id": mandate["target_cluster_id"],
        "start_id": recovery_r2["after_start_id"], "pid": 2345,
        "backend_start": utc(base + timedelta(seconds=8)), "session_id": "session-2",
        "connected_at": utc(base + timedelta(seconds=8)),
        "mode": "off", "force_execution": "off", "observed_engine": "postgres",
        "result": "PASS"})
    retirement = {"schema_version": 1, "contract_type": "canary_retirement",
        "contract_id": "canary-retirement-1", "scope_sha256": scope_sha,
        "canary_id": "canary-1", "plan_sha256": canonical_sha256(plan_doc),
        "run_sha256": canonical_sha256(run), "owned_session_id": "session-1",
        "backend_exit_proof": exit_proof, "fresh_reconnect_proof": reconnect,
        "performed_at": utc(base + timedelta(seconds=7)), "result": "PASS", "state": state()}
    validate_contract(retirement); write_document(root / "canary-retirement.json", retirement)
    return base + timedelta(seconds=9), plan_doc, run, retirement


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
            reseal_run(root, run)
            with self.assertRaisesRegex(CanaryError, "positive_request_or_snapshot_mismatch"):
                validate_canary_bundle(root, checked)

    def test_wrong_control_order_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            run = __import__("json").loads((root / "canary-run.json").read_text())
            run["control_proofs"][1], run["control_proofs"][2] = (
                run["control_proofs"][2], run["control_proofs"][1])
            reseal_run(root, run)
            with self.assertRaisesRegex(CanaryError, "negative_controls_did_not_pass"):
                validate_canary_bundle(root, checked)

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
            (root / "private/raw/expected.spool").write_bytes(b"changed\n")
            with self.assertRaises(CanaryError): validate_canary_bundle(root, checked)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            run = __import__("json").loads((root / "canary-run.json").read_text())
            run["result"] = "INCOMPLETE"
            run["state"] = state("INCONCLUSIVE", "UNKNOWN")
            reseal_run(root, run)
            with self.assertRaisesRegex(CanaryError, "retirement_did_not_pass"):
                validate_canary_bundle(root, checked)

    def test_self_reported_oracle_cannot_bless_wrong_spool(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, plan_value, run, _ = build_canary(root)
            wrong = root / "wrong.spool"; wrong.write_bytes(b"wrong-result\n"); wrong.chmod(0o600)
            wrong_artifact = artifact(root, "wrong.spool")
            plan_value["expected_spool"] = wrong_artifact
            run["positive_spool"] = wrong_artifact
            request = __import__("json").loads((root / "positive-request.json").read_text())
            request["observed_spool"] = wrong_artifact
            write_document(root / "positive-request.json", request)
            run["positive_request"] = artifact(root, "positive-request.json")
            oracle = __import__("json").loads((root / "oracle.json").read_text())
            oracle["spool_sha256"] = wrong_artifact["sha256"]
            write_document(root / "oracle.json", oracle); run["oracle_proof"] = artifact(root, "oracle.json")
            reseal_all(root, plan_value, run)
            with self.assertRaisesRegex(CanaryError, "literal_or_limit_mismatch|oracle"):
                validate_canary_bundle(root, checked)

    def test_unapproved_sql_and_unsafe_expected_tuple_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, plan_value, run, _ = build_canary(root)
            rogue = root / "rogue.sql"; rogue.write_bytes(b"SELECT 987654321\n"); rogue.chmod(0o600)
            plan_value["positive_sql"] = artifact(root, "rogue.sql")
            plan_value["positive_literal_sha256"] = plan_value["positive_sql"]["sha256"]
            execution = __import__("json").loads((root / "positive-execution.json").read_text())
            execution["executed_sql_sha256"] = plan_value["positive_literal_sha256"]
            write_document(root / "positive-execution.json", execution)
            run["positive_execution_proof"] = artifact(root, "positive-execution.json")
            reseal_all(root, plan_value, run)
            with self.assertRaisesRegex(CanaryError, "literal_or_limit_mismatch"):
                validate_canary_bundle(root, checked)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); _, plan_value, _, _ = build_canary(root)
            plan_value["expected_snapshot"]["observed_engine"] = "postgres"
            with self.assertRaisesRegex(Exception, "safe delegation"):
                validate_contract(plan_value)

    def test_missing_control_sql_and_invalid_reconnect_identity_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, _ = build_canary(root)
            (root / "control-2.sql").unlink()
            with self.assertRaises(CanaryError): validate_canary_bundle(root, checked)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, _, _, retirement = build_canary(root)
            reconnect = __import__("json").loads((root / "fresh-reconnect.json").read_text())
            reconnect["session_id"] = None
            write_document(root / "fresh-reconnect.json", reconnect)
            retirement["fresh_reconnect_proof"] = artifact(root, "fresh-reconnect.json")
            write_document(root / "canary-retirement.json", retirement)
            with self.assertRaisesRegex(CanaryError, "backend_identity_invalid"):
                validate_canary_bundle(root, checked)

    def test_measurement_plan_must_be_the_private_scope_plan(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); checked, plan_value, run, _ = build_canary(root)
            other = plan(levels=1); write_document(root / "other-plan.json", other)
            plan_value["measurement_plan"] = artifact(root, "other-plan.json")
            reseal_all(root, plan_value, run)
            with self.assertRaisesRegex(CanaryError, "outside_private_scope"):
                validate_canary_bundle(root, checked)

    def test_cli_persists_terminal_outcome_and_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_canary(root)
            self.assertEqual(main(["--bundle", str(root)]), 0)
            outcome = __import__("json").loads((root / "canary-outcome.json").read_text())
            self.assertEqual(outcome["decision"], "ELIGIBLE_FOR_HUMAN_GRANT")
            self.assertEqual(main(["--bundle", str(root)]), 1)


if __name__ == "__main__":
    unittest.main()
