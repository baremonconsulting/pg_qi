#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.phase5.contract_rules import (INTERNAL_PILOT_LIMITATIONS, canonical_bytes,
    canonical_sha256, validate_contract)
from tools.phase5.internal_pilot_closeout import (CloseoutError, consume_attempt,
    validate_internal_pilot)
from tests.phase5.test_measurement_runtime import plan as measurement_plan


ZERO = "0" * 64


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value)); path.chmod(0o600)


def artifact(root: Path, relative: str) -> dict:
    payload = (root / relative).read_bytes()
    return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload)}


def state(technical: str = "PASS", exit_state: str = "CLEAN") -> dict:
    return {"lifecycle": "CLOSED", "technical_result": technical,
        "evidence_completeness": "COMPLETE", "exit_state": exit_state,
        "value_outcome": "INCONCLUSIVE", "project_decision": "UNDECIDED"}


def bundle_root(name: str) -> Path:
    root = Path(name) / "bundle"; root.mkdir(mode=0o700)
    return root


def report(scope: str, plan_sha: str, result: str = "PASS") -> dict:
    groups = []
    for mode in ("disabled", "off", "allowlist"):
        groups.append({"scenario_id": "scenario-a", "mode": mode, "n": 1,
            "pass": 1 if result == "PASS" else 0,
            "fail": 1 if result == "FAIL" else 0,
            "timeout": 0, "incomplete": 1 if result == "INCOMPLETE" else 0,
            "p50_ns": 10 if result == "PASS" else None, "p95_ns": None,
            "p99_ns": None, "postprocess_p50_ns": 1,
            "p95_status": f"SUPPRESSED(N={1 if result == 'PASS' else 0},MIN=20)",
            "p99_status": f"SUPPRESSED(N={1 if result == 'PASS' else 0},MIN=100)",
            "block_median_min_ns": 10 if result == "PASS" else None,
            "block_median_max_ns": 10 if result == "PASS" else None})
    return {"schema_version": 1, "contract_type": "measurement_report",
        "contract_id": "report-1", "scope_sha256": scope, "plan_sha256": plan_sha,
        "run_id": "run-1", "level": 1,
        "sample_file": {"path": "samples.jsonl", "sha256": ZERO, "bytes": 0},
        "sample_count": 3, "raw_sample_count": 3, "warmup_sample_count": 0,
        "groups": groups, "contrasts": [{"scenario_id": "scenario-a",
            "extension_overhead_p50_ns": 0, "routing_effect_p50_ns": -1}],
        "coverage": {"structural_eligibility": 1.0, "frequency_coverage": 1.0,
            "baseline_measured_share": 1.0, "measured_natural_share": 1.0,
            "missing_natural_items": 0}, "value_outcome": "INCONCLUSIVE",
        "technical_result": result, "stop_reason": None if result == "PASS" else "STOP",
        "limitations": ["HOSTED_CI_NOT_PERFORMANCE_GATE", "NO_GENERAL_SPEEDUP_CLAIM",
            "NO_INFRASTRUCTURE_GENERALISATION", "NO_ROI_CLAIM", "NO_SLA_CLAIM",
            "VALUE_SEPARATE_FROM_SAFETY"]}


def build(root: Path, report_result: str = "PASS", retired: bool = True,
          levels: int = 1, write_attempt_record: bool = True) -> datetime:
    root.chmod(0o700)
    (root / "canary-bundle").mkdir(mode=0o700)
    ledger = root.parent / "attempt-ledger"; ledger.mkdir(mode=0o700)
    (ledger / "ledger-id.json").write_bytes(b"pgqi-internal-pilot-ledger-v1\n")
    (ledger / "ledger-id.json").chmod(0o600)
    scope = "1" * 64
    measured = measurement_plan(levels=levels)
    write(root / "measurement-plan.json", measured)
    go = {"schema_version": 1, "contract_type": "approval_receipt",
        "contract_id": "go-1", "receipt_kind": "OPERATION_GO", "scope_sha256": scope,
        "preflight_sha256": ZERO, "recovery_validation_sha256": ZERO,
        "valid_from": "2026-11-18T00:20:00Z", "valid_through": "2026-11-20T00:00:00Z",
        "approvals": ["operator"], "nonce": "go-nonce"}
    write(root / "operation-go.json", go)
    canary = {"schema_version": 1, "contract_type": "canary_validation",
        "contract_id": "canary-validation", "scope_sha256": scope,
        "canary_id": "canary-1", "plan_sha256": "2" * 64, "run_sha256": "3" * 64,
        "retirement_sha256": "4" * 64, "checked_at": "2026-11-18T00:10:00Z",
        "result": "PASS", "decision": "ELIGIBLE_FOR_HUMAN_GRANT",
        "checks": ["OPERATION_GO_CURRENT", "SINGLE_DEDICATED_SESSION",
            "POSITIVE_LITERAL_COMPLETE", "NEGATIVE_CONTROLS_COMPLETE", "FULL_ORACLE_PASS",
            "FRESH_SAME_SESSION_SNAPSHOT", "R1_RETIREMENT_COMPLETE",
            "FRESH_DEFAULT_OFF_RECONNECT"],
        "limitations": ["NO_AUTOMATIC_GATE_GRANT", "NO_CUSTOMER_AUTHORIZATION",
            "NO_PERSISTENT_SETTING_AUTHORIZATION", "NO_PRODUCTION_AUTHORIZATION",
            "NO_PUBLICATION_AUTHORIZATION"]}
    write(root / "canary-validation.json", canary)
    acceptance = {"schema_version": 1, "contract_type": "gate_receipt",
        "contract_id": "canary-acceptance", "gate_id": "CANARY_ACCEPTED",
        "scope_sha256": scope, "evidence_sha256": canonical_sha256(canary),
        "approved_at": "2026-11-18T00:15:00Z", "approvals": ["human-reviewer"],
        "nonce": "canary-accepted-1"}
    write(root / "canary-acceptance.json", acceptance)
    authorization = {"schema_version": 1,
        "document_type": "measurement_level_authorization", "requested_level": 1,
        "operation_go": artifact(root, "operation-go.json")}
    (root / "level-1-authorization.json").write_bytes(canonical_bytes(authorization))
    (root / "level-1-authorization.json").chmod(0o600)
    private = root / "private-level-1"; private.mkdir(mode=0o700)
    result_report = report(scope, canonical_sha256(measured), report_result)
    write(private / "report.json", result_report)
    retirement = {"schema_version": 1, "contract_type": "retirement",
        "contract_id": "retirement-1", "scope_sha256": scope, "run_id": "run-1",
        "owned_sessions": ["session-1"], "backend_absence_verified": retired,
        "cached_plans_retired": retired, "default_off_reconnect": retired,
        "state": state() if retired else state("INCONCLUSIVE", "UNKNOWN")}
    write(private / "retirement.json", retirement)
    stop_proof = {"schema_version": 1, "contract_type": "pilot_dispatch_stop",
        "contract_id": "dispatch-stop-1", "scope_sha256": scope,
        "pilot_id": "pilot-1", "window_id": "window-1", "run_id": "run-1",
        "stopped_at": "2026-11-18T01:00:30Z", "admission_state": "STOPPED"}
    off_proof = {"schema_version": 1, "contract_type": "pilot_fresh_off",
        "contract_id": "fresh-off-1", "scope_sha256": scope,
        "pilot_id": "pilot-1", "window_id": "window-1", "run_id": "run-1",
        "observed_at": "2026-11-18T01:00:45Z", "session_id": "fresh-session-1",
        "pg_qi_enabled": "off", "routing_state": "off",
        "duckdb_force_execution": "off"}
    write(private / "dispatch-stop.json", stop_proof)
    write(private / "fresh-off.json", off_proof)
    index = {"schema_version": 1, "contract_type": "index", "contract_id": "index-1",
        "scope_sha256": scope, "payloads": [artifact(private, "report.json"),
            artifact(private, "retirement.json"), artifact(private, "dispatch-stop.json"),
            artifact(private, "fresh-off.json")], "payload_flush_fsync_complete": True,
        "index_sequence": 1}
    write(private / "index.json", index)
    terminal_state = state() if report_result == "PASS" and retired else \
        state("FAIL" if report_result == "FAIL" else "INCONCLUSIVE",
              "CLEAN" if retired else "UNKNOWN")
    terminal = {"schema_version": 1, "contract_type": "terminal",
        "contract_id": "terminal-1", "scope_sha256": scope,
        "index_sha256": canonical_sha256(index), "state": terminal_state,
        "aggregate_pass": report_result == "PASS" and retired}
    write(private / "terminal.json", terminal)
    receipt_result = ("PASS" if report_result == "PASS" and retired else
                      "INCOMPLETE" if report_result == "PASS" else report_result)
    validation = {"schema_version": 1, "contract_type": "validation",
        "contract_id": "private-validation-1", "scope_sha256": scope,
        "index_sha256": canonical_sha256(index), "terminal_sha256": canonical_sha256(terminal),
        "profile": "private_full", "result": receipt_result, "checks": ["structure"]}
    write(root / "private-level-1-validation.json", validation)
    approved = {"window_id": "window-1", "level": 1,
        "private_bundle_path": "private-level-1",
        "level_authorization": artifact(root, "level-1-authorization.json")}
    approved_windows = [approved]
    if levels == 2:
        authorization_two = {"schema_version": 1,
            "document_type": "measurement_level_authorization", "requested_level": 2,
            "operation_go": artifact(root, "operation-go.json"), "previous_level": {}}
        write(root / "level-2-authorization.json", authorization_two)
        approved_windows.append({"window_id": "window-2", "level": 2,
            "private_bundle_path": "private-level-2",
            "level_authorization": artifact(root, "level-2-authorization.json")})
    pilot_plan = {"schema_version": 1, "contract_type": "internal_pilot_plan",
        "contract_id": "pilot-plan", "scope_sha256": scope, "pilot_id": "pilot-1",
        "measurement_plan": artifact(root, "measurement-plan.json"),
        "operation_go": artifact(root, "operation-go.json"),
        "canary_bundle_path": "canary-bundle",
        "canary_validation": artifact(root, "canary-validation.json"),
        "canary_acceptance": artifact(root, "canary-acceptance.json"),
        "attempt_ledger": {"path": str(ledger.resolve()), "device": ledger.stat().st_dev,
            "inode": ledger.stat().st_ino, "identity": artifact(ledger, "ledger-id.json"),
            "storage_control": "EXTERNAL_APPEND_ONLY", "custodian": "records-custodian"},
        "approved_windows": approved_windows, "retention_owner": "records-owner",
        "retention_due_at": "2026-12-31T00:00:00Z"}
    write(root / "internal-pilot-plan.json", pilot_plan)
    attempt = {"schema_version": 1, "document_type": "internal_pilot_attempt",
        "record_id": "pilot-1-attempt-0001-run-1", "pilot_id": "pilot-1", "sequence": 1,
        "scope_sha256": scope, "plan_sha256": canonical_sha256(pilot_plan),
        "operation_go_sha256": canonical_sha256(go), "previous_record_sha256": ZERO,
        "window_id": "window-1",
        "level": 1, "run_id": "run-1", "consumed_at": "2026-11-18T00:59:00Z"}
    if write_attempt_record:
        write(ledger / "attempt-0001.json", attempt)
    dispatch = {"schema_version": 1, "document_type": "internal_pilot_dispatch",
        "attempt_sha256": canonical_sha256(attempt), "window_id": "window-1",
        "level": 1, "run_id": "run-1", "dispatched_at": "2026-11-18T01:00:00Z"}
    if write_attempt_record:
        write(ledger / "dispatch-0001.json", dispatch)
    result_report["pilot_attempt_sha256"] = canonical_sha256(attempt)
    result_report["pilot_window_id"] = "window-1"
    result_report["pilot_dispatch_sha256"] = canonical_sha256(dispatch)
    result_report["pilot_dispatched_at"] = dispatch["dispatched_at"]
    write(private / "report.json", result_report)
    index["payloads"][0] = artifact(private, "report.json")
    write(private / "index.json", index)
    terminal["index_sha256"] = canonical_sha256(index)
    write(private / "terminal.json", terminal)
    validation["index_sha256"] = canonical_sha256(index)
    validation["terminal_sha256"] = canonical_sha256(terminal)
    write(root / "private-level-1-validation.json", validation)
    window_result = "PASS" if report_result == "PASS" and retired else \
        "INCOMPLETE" if report_result == "INCOMPLETE" or not retired else "FAIL"
    window = {"schema_version": 1, "contract_type": "internal_pilot_window",
        "contract_id": "window-1", "scope_sha256": scope, "pilot_id": "pilot-1",
        "plan_sha256": canonical_sha256(pilot_plan), "window_id": "window-1", "level": 1,
        "run_id": "run-1", "attempt_record_sha256": canonical_sha256(attempt),
        "private_bundle_path": "private-level-1",
        "private_validation_receipt": artifact(root, "private-level-1-validation.json"),
        "level_authorization": approved["level_authorization"],
        "measurement_report": artifact(root, "private-level-1/report.json"),
        "retirement": artifact(root, "private-level-1/retirement.json"),
        "dispatch_stop_proof": artifact(root, "private-level-1/dispatch-stop.json"),
        "fresh_off_proof": artifact(root, "private-level-1/fresh-off.json"),
        "started_at": "2026-11-18T01:00:00Z", "finished_at": "2026-11-18T01:01:00Z",
        "duration_ms": 60000, "result": window_result,
        "state": state() if window_result == "PASS" else
            state("FAIL" if window_result == "FAIL" else "INCONCLUSIVE",
                  "DIRTY" if window_result == "FAIL" else "UNKNOWN")}
    write(root / "window-1.json", window)
    closeout_result = window_result
    closeout = {"schema_version": 1, "contract_type": "internal_pilot_closeout",
        "contract_id": "closeout-1", "scope_sha256": scope, "pilot_id": "pilot-1",
        "plan_sha256": canonical_sha256(pilot_plan), "windows": [artifact(root, "window-1.json")],
        "hard_stop_findings": [], "service_work_records": [{"record_id": "work-1",
            "category": "OPERATION", "duration_minutes": 10, "outcome": "COMPLETED"}],
        "decisions": {"safety": report_result,
            "evidence_completeness": "PASS",
            "exit": "PASS" if retired else "INCOMPLETE",
            "workload_relevance": "RELEVANT", "measured_value": "INCONCLUSIVE",
            "service_work": "ACCEPTABLE",
            "project_decision": "CONTINUE" if closeout_result == "PASS" else "PAUSE"},
        "retention": {"status": "PENDING", "owner": "records-owner",
            "due_at": "2026-12-31T00:00:00Z", "evidence": None},
        "share_release": None, "shared_derived_paths": [],
        "shared_release_state": "NOT_REQUESTED",
        "checked_at": "2026-11-18T01:02:00Z", "result": closeout_result,
        "eligibility": "ELIGIBLE_FOR_HUMAN_GRANT" if closeout_result == "PASS" else "REFUSED",
        "limitations": sorted(INTERNAL_PILOT_LIMITATIONS),
        "state": state() if closeout_result == "PASS" else
            state("FAIL" if closeout_result == "FAIL" else "INCONCLUSIVE",
                  "DIRTY" if closeout_result == "FAIL" else "UNKNOWN")}
    write(root / "internal-pilot-closeout.json", closeout)
    return datetime(2026, 11, 18, 2, tzinfo=timezone.utc)


def reseal_private_metadata(root: Path) -> None:
    index = json.loads((root / "private-level-1/index.json").read_text())
    terminal = json.loads((root / "private-level-1/terminal.json").read_text())
    terminal["index_sha256"] = canonical_sha256(index)
    write(root / "private-level-1/terminal.json", terminal)
    validation = json.loads((root / "private-level-1-validation.json").read_text())
    validation["index_sha256"] = canonical_sha256(index)
    validation["terminal_sha256"] = canonical_sha256(terminal)
    write(root / "private-level-1-validation.json", validation)
    window = json.loads((root / "window-1.json").read_text())
    window["private_validation_receipt"] = artifact(root, "private-level-1-validation.json")
    write(root / "window-1.json", window)
    closeout = json.loads((root / "internal-pilot-closeout.json").read_text())
    closeout["windows"] = [artifact(root, "window-1.json")]
    write(root / "internal-pilot-closeout.json", closeout)


def enable_share_release(root: Path, wrong_artifact: bool = False) -> None:
    shared = root / "shared"; shared.mkdir(mode=0o700)
    manifest = {"schema_version": 1, "document_type": "shared_manifest",
        "profile": "shared_derived", "source_scope_sha256": "1" * 64,
        "private_validation_sha256": canonical_sha256(json.loads(
            (root / "private-level-1-validation.json").read_text())),
        "raw_reproducibility": "NOT_CLAIMED"}
    write(shared / "manifest.json", manifest)
    index = {"schema_version": 1, "contract_type": "index", "contract_id": "shared-index",
        "scope_sha256": "1" * 64, "payloads": [artifact(shared, "manifest.json")],
        "payload_flush_fsync_complete": True, "index_sequence": 1}
    write(shared / "index.json", index)
    receipt = json.loads((root / "private-level-1-validation.json").read_text())
    release = {"schema_version": 1, "contract_type": "export_receipt",
        "contract_id": "share-release", "receipt_kind": "SHARE_RELEASE",
        "scope_sha256": "1" * 64, "index_sha256": canonical_sha256(index),
        "validation_sha256": canonical_sha256(receipt),
        "artifact_sha256": ("f" * 64 if wrong_artifact else canonical_sha256(manifest)),
        "recipients": ["internal-reviewer"], "approved_at": "2026-11-18T01:01:30Z",
        "approval_reference": "share-approval-1"}
    write(root / "share-release.json", release)
    closeout = json.loads((root / "internal-pilot-closeout.json").read_text())
    closeout["share_release"] = artifact(root, "share-release.json")
    closeout["shared_derived_paths"] = [{"bundle_path": "shared",
        "private_validation_receipt": "private-level-1-validation.json"}]
    closeout["shared_release_state"] = "RELEASED"
    write(root / "internal-pilot-closeout.json", closeout)


class InternalPilotCloseoutTests(unittest.TestCase):
    def validate(self, root: Path, checked: datetime,
                 canary_result: dict | None = None) -> dict:
        receipt = json.loads((root / "private-level-1-validation.json").read_text())
        terminal = json.loads((root / "private-level-1/terminal.json").read_text())
        canary = json.loads((root / "canary-validation.json").read_text())
        canary_result = canary_result or {**canary,
            "checked_at": "2026-11-18T00:15:00.000Z"}
        with patch("tools.phase5.internal_pilot_closeout.validate_private",
                   return_value={"result": receipt["result"], "terminal": terminal,
                       "scope": {"scope_inputs": {"measurement_plan":
                           artifact(root, "measurement-plan.json")}}}), patch(
                   "tools.phase5.internal_pilot_closeout.validate_level_authorization",
                   return_value={"level": 1, "sessions": 1}), patch(
                   "tools.phase5.internal_pilot_closeout.validate_canary_bundle",
                   return_value=canary_result):
            return validate_internal_pilot(root, checked)

    def test_complete_pilot_is_only_eligible_for_human_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            result = self.validate(root, checked)
            self.assertEqual(result["result"], "PASS")
            self.assertEqual(result["decision"], "ELIGIBLE_FOR_HUMAN_GRANT")
            self.assertNotIn("INTERNAL_CLOSEOUT", json.dumps(result))

    def test_failed_prefix_of_frozen_levels_closes_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root, report_result="FAIL", levels=2)
            result = self.validate(root, checked)
            self.assertEqual(result["result"], "FAIL")
            self.assertEqual(result["decision"], "REFUSED")
            self.assertEqual(result["window_count"], 1)

    def test_accepted_canary_must_equal_recomputed_canary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            canary = json.loads((root / "canary-validation.json").read_text())
            canary["canary_id"] = "different-canary"
            with self.assertRaisesRegex(CloseoutError, "accepted_canary_differs"):
                self.validate(root, checked, canary)

    def test_attempt_must_be_consumed_once_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); build(root, write_attempt_record=False)
            ledger = root.parent / "attempt-ledger"
            record = consume_attempt(root, ledger, "window-1", 1, "fresh-run",
                datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc))
            self.assertEqual(record["sequence"], 1)
            with self.assertRaisesRegex(CloseoutError, "retry_or_reorder"):
                consume_attempt(root, ledger, "window-1", 1, "retry-run",
                    datetime(2026, 11, 18, 0, 31, tzinfo=timezone.utc))

    def test_attempt_hash_chain_and_window_binding_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            path = root.parent / "attempt-ledger/attempt-0001.json"
            record = json.loads(path.read_text()); record["previous_record_sha256"] = "f" * 64
            write(path, record)
            with self.assertRaisesRegex(CloseoutError, "hash_chain"):
                self.validate(root, checked)

    def test_unretired_window_cannot_be_administratively_relabelled_pass(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root, retired=False)
            closeout = json.loads((root / "internal-pilot-closeout.json").read_text())
            closeout["result"] = "PASS"; closeout["eligibility"] = "ELIGIBLE_FOR_HUMAN_GRANT"
            closeout["state"] = state()
            write(root / "internal-pilot-closeout.json", closeout)
            with self.assertRaises(CloseoutError): self.validate(root, checked)

    def test_window_history_and_frozen_level_identity_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            window = json.loads((root / "window-1.json").read_text())
            window["run_id"] = "substituted-run"; write(root / "window-1.json", window)
            closeout = json.loads((root / "internal-pilot-closeout.json").read_text())
            closeout["windows"] = [artifact(root, "window-1.json")]
            write(root / "internal-pilot-closeout.json", closeout)
            with self.assertRaisesRegex(CloseoutError, "attempt_window_mismatch"):
                self.validate(root, checked)

    def test_shared_path_without_share_release_is_rejected_by_contract(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); build(root)
            value = json.loads((root / "internal-pilot-closeout.json").read_text())
            value["shared_derived_paths"] = [{"bundle_path": "shared",
                "private_validation_receipt": "private-validation.json"}]
            with self.assertRaisesRegex(Exception, "SHARE_RELEASE"):
                validate_contract(value)

    def test_approved_private_bundles_must_be_distinct_and_non_nested(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); build(root)
            value = json.loads((root / "internal-pilot-plan.json").read_text())
            second = deepcopy(value["approved_windows"][0])
            second.update(window_id="window-2", level=2,
                          private_bundle_path="private-level-1/nested")
            value["approved_windows"].append(second)
            with self.assertRaisesRegex(Exception, "non-nested"):
                validate_contract(value)

    def test_share_release_is_bound_to_final_pilot_receipt_and_shared_manifest(self) -> None:
        for wrong in (False, True):
            with self.subTest(wrong=wrong), tempfile.TemporaryDirectory() as name:
                root = bundle_root(name); checked = build(root); enable_share_release(root, wrong)
                with patch("tools.phase5.internal_pilot_closeout.validate_shared",
                           return_value={"scope_sha256": "1" * 64, "result": "PASS"}):
                    if wrong:
                        with self.assertRaisesRegex(CloseoutError, "share_release_not_bound"):
                            self.validate(root, checked)
                    else:
                        self.assertEqual(self.validate(root, checked)["result"], "PASS")

    def test_value_and_relevance_cannot_be_invented(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            closeout = json.loads((root / "internal-pilot-closeout.json").read_text())
            closeout["decisions"]["measured_value"] = "POSITIVE"
            write(root / "internal-pilot-closeout.json", closeout)
            with self.assertRaisesRegex(CloseoutError, "measured_value"):
                self.validate(root, checked)

    def test_shutdown_and_fresh_off_proofs_are_recomputed(self) -> None:
        for filename, field, value, message in (
                ("dispatch-stop.json", "admission_state", "RUNNING", "dispatch_stop"),
                ("fresh-off.json", "duckdb_force_execution", "on", "fresh_off")):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as name:
                root = bundle_root(name); checked = build(root)
                proof = json.loads((root / "private-level-1" / filename).read_text())
                proof[field] = value; write(root / "private-level-1" / filename, proof)
                index = json.loads((root / "private-level-1/index.json").read_text())
                target = next(item for item in index["payloads"] if item["path"] == filename)
                target.update(artifact(root / "private-level-1", filename))
                write(root / "private-level-1/index.json", index)
                window = json.loads((root / "window-1.json").read_text())
                key = "dispatch_stop_proof" if filename.startswith("dispatch") else "fresh_off_proof"
                window[key] = artifact(root, f"private-level-1/{filename}")
                write(root / "window-1.json", window)
                reseal_private_metadata(root)
                with self.assertRaisesRegex(CloseoutError, message):
                    self.validate(root, checked)

    def test_retired_session_cannot_be_used_as_fresh_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            proof = json.loads((root / "private-level-1/fresh-off.json").read_text())
            proof["session_id"] = "session-1"; write(root / "private-level-1/fresh-off.json", proof)
            index = json.loads((root / "private-level-1/index.json").read_text())
            next(item for item in index["payloads"] if item["path"] == "fresh-off.json").update(
                artifact(root / "private-level-1", "fresh-off.json"))
            write(root / "private-level-1/index.json", index)
            window = json.loads((root / "window-1.json").read_text())
            window["fresh_off_proof"] = artifact(root, "private-level-1/fresh-off.json")
            write(root / "window-1.json", window)
            reseal_private_metadata(root)
            with self.assertRaisesRegex(CloseoutError, "fresh_off"):
                self.validate(root, checked)

    def test_open_hard_stop_and_invented_service_decision_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            (root / "finding.txt").write_text("open\n"); (root / "finding.txt").chmod(0o600)
            closeout = json.loads((root / "internal-pilot-closeout.json").read_text())
            closeout["hard_stop_findings"] = [{"finding_id": "hard-1", "status": "OPEN",
                "evidence": artifact(root, "finding.txt"), "closure_approval": None}]
            write(root / "internal-pilot-closeout.json", closeout)
            with self.assertRaisesRegex(CloseoutError, "core_decisions|hard_stop"):
                self.validate(root, checked)
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            closeout = json.loads((root / "internal-pilot-closeout.json").read_text())
            closeout["service_work_records"][0]["outcome"] = "FAILED"
            write(root / "internal-pilot-closeout.json", closeout)
            with self.assertRaisesRegex(CloseoutError, "service_work"):
                self.validate(root, checked)

    def test_closed_hard_stop_requires_finding_bound_post_pilot_approval(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = bundle_root(name); checked = build(root)
            (root / "finding.txt").write_text("closed\n"); (root / "finding.txt").chmod(0o600)
            evidence = artifact(root, "finding.txt")
            closeout = json.loads((root / "internal-pilot-closeout.json").read_text())
            approval = {"schema_version": 1, "contract_type": "gate_receipt",
                "contract_id": "hard-stop-approval", "gate_id": "HARD_STOP_CLOSED",
                "subject_id": "hard-1", "scope_sha256": closeout["scope_sha256"],
                "evidence_sha256": evidence["sha256"],
                "approved_at": "2026-11-18T01:02:00Z",
                "approvals": ["human-reviewer"], "nonce": "hard-stop-approval-1"}
            write(root / "hard-stop-approval.json", approval)
            closeout["hard_stop_findings"] = [{"finding_id": "hard-1", "status": "CLOSED",
                "evidence": evidence,
                "closure_approval": artifact(root, "hard-stop-approval.json")}]
            write(root / "internal-pilot-closeout.json", closeout)
            self.assertEqual(self.validate(root, checked)["result"], "PASS")

            approval["subject_id"] = "hard-2"
            write(root / "hard-stop-approval.json", approval)
            closeout["hard_stop_findings"][0]["closure_approval"] = \
                artifact(root, "hard-stop-approval.json")
            write(root / "internal-pilot-closeout.json", closeout)
            with self.assertRaisesRegex(CloseoutError, "hard_stop_closure_not_authorized"):
                self.validate(root, checked)


if __name__ == "__main__":
    unittest.main()
