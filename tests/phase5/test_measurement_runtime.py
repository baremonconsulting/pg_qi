#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT))

from tools.phase5.contract_rules import (ContractError, canonical_bytes,
    canonical_sha256, measurement_block_orders, validate_contract)
from tools.phase5.control_runtime import ResourcePolicy, ResourceSample, StopController
from tools.phase5.measurement_runtime import (AuditedMeasurementSession,
    MeasurementError, MeasurementRunner,
    load_measurement_plan, resource_feed_sha256, validate_level_authorization)
from tools.phase5.pilot_runtime import SequenceJournal
import tools.phase5.measurement_runtime as measurement_runtime
from tools.phase5.recovery_rehearsal import validate_rehearsal_contracts
from tests.phase5.test_recovery_rehearsal import build_bundle

ZERO = "0" * 64
RELEASE_STACK = canonical_sha256({"server_build_sha256": "3" * 64,
    "pg_qi_sha256": "4" * 64, "pg_duckdb_sha256": "5" * 64})
BASELINE_STACK = canonical_sha256({"server_build_sha256": "8" * 64,
    "pg_qi_sha256": "9" * 64, "pg_duckdb_sha256": "a" * 64})


def write_document(path: Path, value: dict) -> None:
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


def weights_digest(value: dict[str, int]) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def plan(value_mode: str = "descriptive", levels: int = 2,
         repetitions: int | None = None) -> dict:
    scenarios = [{"scenario_id": "scenario-a", "query_id": "query-a", "case_id": "query_a",
        "protocol": "extended", "cache_state": "warm_cached",
        "oracle_case_id": "oracle-a", "natural_query_id": "natural-a",
        "expected_result_bytes_max": 4096},
        {"scenario_id": "scenario-b", "query_id": "query-b", "case_id": "query_b",
        "protocol": "extended", "cache_state": "first_use",
        "oracle_case_id": "oracle-b", "natural_query_id": "natural-b",
        "expected_result_bytes_max": 4096}]
    frequency = {"natural-a": 3, "natural-b": 2}
    baseline = {"natural-a": 10, "natural-b": 20}
    rules = {"threshold_kind": "descriptive_only",
        "minimum_relative_improvement_bps": None,
        "minimum_paired_blocks": None,
        "frequency_weights_sha256": None, "baseline_weights_sha256": None,
        "attribution": "measured_correct_natural_subset_only",
        "incomplete_weights_outcome": "INCONCLUSIVE"}
    if value_mode == "approved":
        rules.update(threshold_kind="approved_relative_improvement",
                     minimum_relative_improvement_bps=1000,
                     minimum_paired_blocks=20,
                     frequency_weights_sha256=weights_digest(frequency),
                     baseline_weights_sha256=weights_digest(baseline))
    level_values = [{"level": 1, "sessions": 1,
        "approval_reference": "level-one-approval",
        "server_resource_feed_sha256": None}]
    if levels == 2:
        level_values.append({"level": 2, "sessions": 2,
            "approval_reference": "level-two-approval",
            "server_resource_feed_sha256": resource_feed_sha256(resource_policy("0" * 64))})
    repetitions = repetitions or (4 if value_mode == "approved" else 1)
    return {"schema_version": 1, "contract_type": "measurement_plan",
        "contract_id": "measurement-plan-1",
        "closure_inputs": {name: character * 64 for name, character in (
            ("workload_definition_sha256", "1"), ("qualification_sha256", "1"),
            ("baseline_manifest_sha256", "3"), ("resource_policy_sha256", "4"),
            ("release_sha256", "5"), ("target_sha256", "6"),
            ("dataset_sha256", "7"), ("toolkit_sha256", "8"),
            ("recovery_plan_sha256", "a"),
            ("release_configuration_sha256", "6"),
            ("baseline_configuration_sha256", "b"))} | {
                "release_artifact_sha256": RELEASE_STACK,
                "baseline_artifact_sha256": BASELINE_STACK},
        "required_scenarios": scenarios, "seed": 8241,
        "order_algorithm": "seeded_balanced_williams_v1",
        "measurement_blocks": [{"block_id": f"block-{index:04d}", "order": order}
            for index, order in enumerate(measurement_block_orders(8241, repetitions * 6), 1)],
        "measurement_repetitions": repetitions,
        "warmup_repetitions": 1, "protocols": ["extended"],
        "cache_states": ["first_use", "warm_cached"],
        "concurrency_levels": level_values,
        "statistics": {"percentile_method": "nearest_rank",
            "minimum_n": {"p50": 1, "p95": 20, "p99": 100},
            "outlier_policy": "retain_all", "failure_policy": "retain_and_count",
            "variability": "per_block_median_range"}, "value_rules": rules}


def measurement_scope(value: dict) -> tuple[dict, str]:
    closure = value["closure_inputs"]
    bindings = {"workload_definition": closure["workload_definition_sha256"],
        "measurement_plan": canonical_sha256(value),
        "baseline_manifest": closure["baseline_manifest_sha256"],
        "dataset": closure["dataset_sha256"],
        "release_definition": closure["release_sha256"],
        "target": closure["target_sha256"], "limits": closure["resource_policy_sha256"],
        "toolkit": closure["toolkit_sha256"], "evidence_policy": "9" * 64,
        "recovery_plan": closure["recovery_plan_sha256"], "export_policy": "b" * 64,
        "role_bindings": "c" * 64, "window": "d" * 64}
    scope = {"schema_version": 1, "contract_type": "scope",
        "contract_id": "measurement-scope-1",
        "scope_inputs": {name: {"path": f"inputs/{name}.json", "sha256": digest,
            "bytes": 1} for name, digest in bindings.items()},
        "gates_required": ["SCOPE_AUTHORIZED", "OPERATION_GO"]}
    scope["scope_inputs"]["measurement_plan"]["bytes"] = len(canonical_bytes(value))
    validate_contract(scope)
    return scope, canonical_sha256(scope)


def resource_policy(scope_sha256: str) -> ResourcePolicy:
    return ResourcePolicy.parse({"schema_version": 1, "document_type": "resource_policy",
        "policy_id": "measurement-policy", "scope_sha256": scope_sha256, "feed_version": 1,
        "source_id": "local-proc-feed", "interval_ms": 50, "max_age_ms": 60000,
        "stop_overshoot_ms": 50,
        "limits": {"rss_bytes": 2**40, "cpu_milliseconds": 2**40,
                   "evidence_bytes": 2**30, "inflight_requests": 2},
        "units": {"rss_bytes": "bytes", "cpu_milliseconds": "milliseconds",
                  "evidence_bytes": "bytes", "inflight_requests": "count"}})


def controller(root: Path, policy: ResourcePolicy) -> tuple[SequenceJournal, StopController]:
    journal = SequenceJournal(root)
    control = StopController(journal, policy)
    now = time.monotonic_ns()
    control.accept_sample(ResourceSample(policy.scope_sha256, 1, "local-proc-feed", 1, now,
        {"rss_bytes": 1, "cpu_milliseconds": 1, "evidence_bytes": 1,
         "inflight_requests": 0}, dict(policy.units)))
    return journal, control


def operation_preflight(root: Path, scope_sha256: str) -> dict:
    value = {"schema_version": 1, "contract_type": "preflight",
        "contract_id": "operation-preflight-1", "scope_sha256": scope_sha256,
        "target_identity_sha256": "8" * 64, "checks": ["target_identity"],
        "state": state()}
    path = root / "operation-preflight.json"
    if not path.exists():
        write_document(path, value)
    return artifact(root, "operation-preflight.json")


def operation_recovery_artifacts(root: Path, scope_sha256: str) -> dict[str, dict]:
    names = {"operation_rehearsal_mandate": "operation-mandate.json",
        "operation_recovery_r1": "operation-r1.json",
        "operation_recovery_r2": "operation-r2.json",
        "operation_operator_walkthrough": "operation-walkthrough.json",
        "operation_recovery_incident": "operation-incident.json",
        "operation_recovery_validation": "operation-recovery-validation.json"}
    if not (root / names["operation_recovery_validation"]).exists():
        staging = root / "operation-recovery-stage"
        staging.mkdir(mode=0o700)
        mandate, r1, r2, walkthrough, incident = build_bundle(staging)
        proof_names = ("positive.json", "off.json", "pool.json", "exit.json",
                       "r1-control.json", "r2-source.json", "r2-identity.json", "r2-control.json",
                       "incident-exit.json")
        for name in proof_names:
            shutil.copyfile(staging / name, root / name)
            (root / name).chmod(0o600)
        mandate["scope_sha256"] = scope_sha256
        mandate["recovery_plan_sha256"] = "a" * 64
        mandate["source_environment_identity_sha256"] = "6" * 64
        mandate["approved_artifact_sha256"] = BASELINE_STACK
        mandate["approved_configuration_sha256"] = "b" * 64
        mandate["candidate_artifact_sha256"] = RELEASE_STACK
        mandate["candidate_configuration_sha256"] = "6" * 64
        mandate_sha = canonical_sha256(mandate)
        r1.update(scope_sha256=scope_sha256, mandate_sha256=mandate_sha)
        r1_sha = canonical_sha256(r1)
        r2.update(scope_sha256=scope_sha256, mandate_sha256=mandate_sha,
                  r1_terminal_sha256=r1_sha)
        r2["source_artifact_sha256"] = RELEASE_STACK
        r2["source_configuration_sha256"] = "6" * 64
        source = json.loads((root / "r2-source.json").read_text())
        source["artifact_sha256"] = RELEASE_STACK
        source["configuration_sha256"] = "6" * 64
        write_document(root / "r2-source.json", source)
        r2["source_identity_proof"] = artifact(root, "r2-source.json")
        r2["result_artifact_sha256"] = BASELINE_STACK
        r2["result_configuration_sha256"] = "b" * 64
        identity = json.loads((root / "r2-identity.json").read_text())
        identity["artifact_sha256"] = BASELINE_STACK
        identity["configuration_sha256"] = "b" * 64
        write_document(root / "r2-identity.json", identity)
        r2["identity_proof"] = artifact(root, "r2-identity.json")
        r2_sha = canonical_sha256(r2)
        walkthrough.update(scope_sha256=scope_sha256, mandate_sha256=mandate_sha,
                           r1_terminal_sha256=r1_sha, r2_terminal_sha256=r2_sha)
        incident.update(scope_sha256=scope_sha256, mandate_sha256=mandate_sha)
        validation = validate_rehearsal_contracts(
            root, mandate, r1, r2, walkthrough, incident,
            datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc))
        for key, value in (("operation_rehearsal_mandate", mandate),
                           ("operation_recovery_r1", r1),
                           ("operation_recovery_r2", r2),
                           ("operation_operator_walkthrough", walkthrough),
                           ("operation_recovery_incident", incident),
                           ("operation_recovery_validation", validation)):
            write_document(root / names[key], value)
        shutil.rmtree(staging)
    return {key: artifact(root, name) for key, name in names.items()}


def operation_recovery_validation(root: Path, scope_sha256: str) -> dict:
    return operation_recovery_artifacts(root, scope_sha256)[
        "operation_recovery_validation"]


def operation_go(root: Path, scope_sha256: str) -> dict:
    operation_preflight(root, scope_sha256)
    operation_recovery_validation(root, scope_sha256)
    preflight = json.loads((root / "operation-preflight.json").read_text())
    recovery = json.loads((root / "operation-recovery-validation.json").read_text())
    instant = datetime.now(timezone.utc)
    value = {"schema_version": 1, "contract_type": "approval_receipt",
        "contract_id": "operation-go-1", "receipt_kind": "OPERATION_GO",
        "scope_sha256": scope_sha256, "preflight_sha256": canonical_sha256(preflight),
        "recovery_validation_sha256": canonical_sha256(recovery),
        "valid_from": (instant - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "valid_through": (instant + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "approvals": ["operation-owner"], "nonce": "measurement-window-1"}
    write_document(root / "operation-go.json", value)
    return artifact(root, "operation-go.json")


def level_one_authorization(root: Path, plan_digest: str, scope_sha256: str) -> dict:
    return {"schema_version": 1, "document_type": "measurement_level_authorization",
        "scope_sha256": scope_sha256, "plan_sha256": plan_digest, "requested_level": 1,
        "approval_reference": "level-one-approval",
        "operation_go": operation_go(root, scope_sha256),
        "operation_preflight": operation_preflight(root, scope_sha256),
        **operation_recovery_artifacts(root, scope_sha256),
        "previous_level": None}


def coverage(complete: bool = True) -> dict:
    return {"denominator": 2, "confirmed_ratio": 1.0,
        "frequency_ratio": 1.0 if complete else None,
        "baseline_time_ratio": 1.0 if complete else None,
        "measured_natural_ratio": 1.0 if complete else 0.5,
        "natural_items_linked_to_execution": 2 if complete else 1}


def correctness_gate(plan_digest: str, scope_digest: str,
                     complete: bool = True, failed: bool = False) -> measurement_runtime.CorrectnessGate:
    return measurement_runtime.CorrectnessGate(scope_digest, plan_digest,
        {"oracle-a": "FAIL" if failed else "PASS", "oracle-b": "PASS"},
        coverage(complete),
        {"natural-a": 3, "natural-b": 2} if complete else None,
        {"natural-a": 10, "natural-b": 20} if complete else None,
        measurement_runtime._CORRECTNESS_TOKEN)


class FakeSession:
    active = 0
    maximum_active = 0
    sequence = 0
    lock = threading.Lock()

    def __init__(self, mode: str, control: StopController,
                 fail_measurement: bool = False, secret_exception: bool = False) -> None:
        self.mode = mode; self.control = control
        self.fail_measurement = fail_measurement; self.secret_exception = secret_exception
        self.calls = 0; self.retired = False

    def execute(self, scenario: dict) -> dict:
        self.calls += 1
        if self.secret_exception and self.calls > 1:
            raise RuntimeError("password=do-not-export host=secret.example")
        with self.control.admission():
            with self.lock:
                type(self).sequence += 1
                intent = type(self).sequence + 100
            self.control.intent_started(intent)
        started = time.monotonic_ns()
        with self.lock:
            type(self).active += 1
            type(self).maximum_active = max(type(self).maximum_active, type(self).active)
        time.sleep(0.005)
        duration = {"disabled": 300_000, "off": 200_000,
                    "allowlist": 100_000}[self.mode]
        finished = started + duration
        with self.lock:
            type(self).active -= 1
        self.control.intent_finished(intent)
        if self.fail_measurement and self.calls > 1 and self.mode == "allowlist":
            return {"terminal_state": "TIMEOUT", "reason_family": "query_deadline",
                "result_bytes": 0, "oracle_result": "NOT_RUN",
                "postprocess_duration_ns": 10,
                "request_interval": {"started_monotonic_ns": started,
                    "finished_monotonic_ns": finished, "duration_ns": duration,
                    "state": "CENSORED_TIMEOUT", "censored": True}}
        return {"terminal_state": "PASS",
            "reason_family": "cached" if scenario["cache_state"] == "warm_cached" else "fresh",
            "result_bytes": 64, "oracle_result": "PASS", "postprocess_duration_ns": 50,
            "request_interval": {"started_monotonic_ns": started,
                "finished_monotonic_ns": finished, "duration_ns": duration,
                "state": "COMPLETE", "censored": False}}

    def retire(self) -> bool:
        self.retired = True
        return True


class FakeDispatcher:
    def __init__(self, root: Path) -> None:
        self.root = root; self.sequence = 0; self.case_id = ""; self.closed = False
        self.prepares = 0; self.dispatches = 0; self.cached = 0

    def _response(self, case_id: str) -> dict:
        self.sequence += 1; self.case_id = case_id
        spool = self.root / f"{self.sequence:020d}-{case_id}.spool"
        spool.write_bytes(b"result"); spool.chmod(0o600)
        started = time.monotonic_ns(); finished = started + 100
        return {"sequence": self.sequence, "state": "COMPLETE", "reason": "fresh",
            "request_started_monotonic_ns": started,
            "request_finished_monotonic_ns": finished, "request_duration_ns": 100,
            "request_interval_state": "COMPLETE", "request_interval_censored": False}

    def dispatch(self, case_id: str) -> dict:
        self.dispatches += 1
        return self._response(case_id)

    def prepare(self, _prepared: str, case_id: str) -> dict:
        self.prepares += 1; self.case_id = case_id
        return {"state": "COMPLETE"}

    def execute_prepared(self, _prepared: str) -> dict:
        self.cached += 1
        response = self._response(self.case_id)
        response["reason"] = "cached"
        return response

    def close(self) -> None:
        self.closed = True


def make_runner(base: Path, plan_value: dict) -> tuple[MeasurementRunner, SequenceJournal,
                                                       StopController, Path, str, str]:
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    control_root = base / "control"; control_root.mkdir(mode=0o700)
    output_root = base / "output"; output_root.mkdir(mode=0o700)
    evidence_root = base / "evidence"; evidence_root.mkdir(mode=0o700)
    scope, scope_digest = measurement_scope(plan_value)
    policy = resource_policy(scope_digest); journal, stop = controller(control_root, policy)
    plan_digest = canonical_sha256(plan_value)
    runner = MeasurementRunner(plan_value, plan_digest, scope, scope_digest, policy,
        plan_value["closure_inputs"]["resource_policy_sha256"], stop,
        output_root, "measurement-run-1")
    return runner, journal, stop, evidence_root, plan_digest, scope_digest


class PlanTests(unittest.TestCase):
    def test_plan_is_balanced_reproducible_and_has_no_scope_cycle(self) -> None:
        value = plan(); validate_contract(value)
        self.assertNotIn("scope_sha256", value)
        positions = [{mode: 0 for mode in ("disabled", "off", "allowlist")}
                     for _ in range(3)]
        for block in value["measurement_blocks"]:
            for index, mode in enumerate(block["order"]): positions[index][mode] += 1
        self.assertTrue(all(set(item.values()) == {2} for item in positions))

    def test_warm_cached_plan_requires_at_least_one_warmup(self) -> None:
        value = plan(); value["warmup_repetitions"] = 0
        with self.assertRaisesRegex(ContractError, "requires a warmup"):
            validate_contract(value)

    def test_plan_refuses_mutated_order_tail_rules_and_unbounded_concurrency(self) -> None:
        mutations = []
        bad = plan(); bad["measurement_blocks"][0]["order"].reverse(); mutations.append(bad)
        bad = plan(); bad["statistics"]["minimum_n"]["p99"] = 10; mutations.append(bad)
        bad = plan(); bad["concurrency_levels"][1]["server_resource_feed_sha256"] = None
        mutations.append(bad)
        bad = plan(); bad["concurrency_levels"][1]["sessions"] = 3; mutations.append(bad)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_contract(value)

    def test_strict_plan_file_refuses_permissions_symlink_and_digest_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); value = plan(); path = root / "plan.json"
            write_document(path, value)
            loaded, digest = load_measurement_plan(path)
            self.assertEqual(loaded, value); self.assertEqual(digest, canonical_sha256(value))
            path.chmod(0o644)
            with self.assertRaises(MeasurementError): load_measurement_plan(path)
            path.chmod(0o600); link = root / "link.json"; link.symlink_to(path)
            with self.assertRaises(MeasurementError): load_measurement_plan(link)

    def test_expired_operation_go_is_refused_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700)
            value = plan(); digest = canonical_sha256(value)
            scope, scope_digest = measurement_scope(value)
            authorization = level_one_authorization(root, digest, scope_digest)
            with self.assertRaisesRegex(MeasurementError, "not_current"):
                validate_level_authorization(authorization, value, digest, scope_digest, root,
                    datetime(2030, 1, 1, tzinfo=timezone.utc))

    def test_malformed_operation_go_and_unbound_preflight_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700)
            value = plan(); digest = canonical_sha256(value)
            _scope, scope_digest = measurement_scope(value)
            authorization = level_one_authorization(root, digest, scope_digest)
            receipt_path = root / "operation-go.json"
            receipt = json.loads(receipt_path.read_text()); receipt.pop("approvals")
            write_document(receipt_path, receipt)
            authorization["operation_go"] = artifact(root, "operation-go.json")
            with self.assertRaisesRegex(MeasurementError, "invalid_document"):
                validate_level_authorization(authorization, value, digest,
                                             scope_digest, root)

    def test_customer_go_cannot_authorize_internal_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700)
            value = plan(); digest = canonical_sha256(value)
            _scope, scope_digest = measurement_scope(value)
            authorization = level_one_authorization(root, digest, scope_digest)
            receipt_path = root / "operation-go.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["receipt_kind"] = "CUSTOMER_OPERATION_GO"
            receipt["dossier_sha256"] = "d" * 64
            receipt["mandate_sha256"] = "e" * 64
            write_document(receipt_path, receipt)
            authorization["operation_go"] = artifact(root, "operation-go.json")
            with self.assertRaisesRegex(MeasurementError, "does_not_authorize"):
                validate_level_authorization(authorization, value, digest,
                                             scope_digest, root)
            authorization = level_one_authorization(root, digest, scope_digest)
            receipt = json.loads(receipt_path.read_text())
            receipt["approvals"] = ["operation-owner"]
            receipt["preflight_sha256"] = "f" * 64
            write_document(receipt_path, receipt)
            authorization["operation_go"] = artifact(root, "operation-go.json")
            with self.assertRaisesRegex(MeasurementError, "does_not_authorize"):
                validate_level_authorization(authorization, value, digest,
                                             scope_digest, root)

    def test_operation_go_must_have_new_nonce_and_postdate_incident(self) -> None:
        for mutation in ("nonce", "time"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as name:
                    root = Path(name); root.chmod(0o700)
                    value = plan(); digest = canonical_sha256(value)
                    _scope, scope_digest = measurement_scope(value)
                    authorization = level_one_authorization(root, digest, scope_digest)
                    receipt_path = root / "operation-go.json"
                    receipt = json.loads(receipt_path.read_text())
                    if mutation == "nonce":
                        receipt["nonce"] = "prior-operation-go"
                    else:
                        receipt["valid_from"] = "2026-11-18T10:04:00Z"
                    write_document(receipt_path, receipt)
                    authorization["operation_go"] = artifact(root, "operation-go.json")
                    with self.assertRaisesRegex(MeasurementError, "not_renewed"):
                        validate_level_authorization(authorization, value, digest,
                                                     scope_digest, root)
    def test_changed_plan_is_rejected_by_old_scope_before_operation_go(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700)
            original = plan(levels=1); scope, scope_digest = measurement_scope(original)
            changed = deepcopy(original); changed["seed"] += 1
            changed["measurement_blocks"] = [
                {"block_id": f"block-{index:04d}", "order": order}
                for index, order in enumerate(measurement_block_orders(
                    changed["seed"], changed["measurement_repetitions"] * 6), 1)]
            validate_contract(changed)
            policy = resource_policy(scope_digest); journal, stop = controller(root, policy)
            output = root / "output"; output.mkdir(mode=0o700)
            with self.assertRaisesRegex(MeasurementError, "not_frozen"):
                MeasurementRunner(changed, canonical_sha256(changed), scope, scope_digest,
                    policy, original["closure_inputs"]["resource_policy_sha256"],
                    stop, output, "mutated-plan-run")
            journal.close()


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSession.active = FakeSession.maximum_active = FakeSession.sequence = 0

    def test_audited_adapter_maps_first_use_and_cached_execution_with_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); dispatcher = FakeDispatcher(root)
            seen = []
            session = AuditedMeasurementSession(dispatcher, root,
                lambda scenario, spool: seen.append((scenario["scenario_id"], spool.name)) or "PASS")
            scenarios = plan(levels=1)["required_scenarios"]
            first = next(item for item in scenarios if item["cache_state"] == "first_use")
            cached = next(item for item in scenarios if item["cache_state"] == "warm_cached")
            first_receipt = session.execute(first)
            cached_receipt = session.execute(cached)
            cached_receipt_two = session.execute(cached)
            self.assertEqual(first_receipt["terminal_state"], "PASS")
            self.assertEqual(cached_receipt["terminal_state"], "PASS")
            self.assertEqual(cached_receipt_two["terminal_state"], "PASS")
            self.assertEqual((dispatcher.dispatches, dispatcher.prepares, dispatcher.cached),
                             (1, 1, 2))
            self.assertEqual(len(seen), 3)
            self.assertTrue(session.retire()); self.assertTrue(dispatcher.closed)

            failing_dispatcher = FakeDispatcher(root)
            failing = AuditedMeasurementSession(failing_dispatcher, root,
                lambda _scenario, _spool: (_ for _ in ()).throw(
                    RuntimeError("password=must-not-leak")))
            receipt = failing.execute(first)
            self.assertEqual(receipt["terminal_state"], "INCOMPLETE")
            self.assertEqual(receipt["reason_family"], "postprocess_exception")
            self.assertEqual(receipt["request_interval"]["state"], "COMPLETE")
            self.assertNotIn("password", json.dumps(receipt))
            self.assertTrue(failing.retire())

    def test_level_one_preserves_raw_warmup_and_suppresses_small_tail(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); runner, journal, stop, evidence, digest, scope_digest = make_runner(base, plan())
            authorization = level_one_authorization(evidence, digest, scope_digest)
            report = runner.run(authorization, evidence,
                correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: FakeSession(mode, control))
            journal.close()
            self.assertEqual(report["technical_result"], "PASS")
            self.assertEqual(report["value_outcome"], "INCONCLUSIVE")
            self.assertEqual(report["sample_count"], 36)
            self.assertEqual(report["warmup_sample_count"], 18)
            self.assertEqual(report["raw_sample_count"], 54)
            self.assertTrue(all(group["p99_ns"] is None and
                                group["p99_status"] == "SUPPRESSED(N=6,MIN=100)"
                                for group in report["groups"]))
            raw = (base / "output/measurement-samples.jsonl").read_text()
            self.assertIn('"phase":"WARMUP"', raw)
            self.assertIn('"phase":"MEASURE"', raw)
            self.assertNotIn("password", raw)
            self.assertFalse(stop.stopped)

    def test_expiry_inside_mode_stops_before_next_batch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); value = plan()
            scope, scope_digest = measurement_scope(value)
            policy = resource_policy(scope_digest)
            control_root = base / "control"; control_root.mkdir(parents=True, mode=0o700)
            output = base / "output"; output.mkdir(mode=0o700)
            evidence = base / "evidence"; evidence.mkdir(mode=0o700)
            journal, stop = controller(control_root, policy)
            calls = 0
            def advancing_clock() -> datetime:
                nonlocal calls
                calls += 1
                return (datetime.now(timezone.utc) if calls < 3 else
                        datetime(2030, 1, 1, tzinfo=timezone.utc))
            digest = canonical_sha256(value)
            runner = MeasurementRunner(value, digest, scope, scope_digest, policy,
                value["closure_inputs"]["resource_policy_sha256"], stop, output,
                "expiry-mid-mode", clock=advancing_clock)
            report = runner.run(level_one_authorization(evidence, digest, scope_digest),
                evidence, correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: FakeSession(mode, control))
            journal.close()
            self.assertTrue(stop.stopped)
            self.assertEqual(report["sample_count"], 0)
            self.assertEqual(report["technical_result"], "INCOMPLETE")

    def test_unproven_warm_cache_cannot_pass(self) -> None:
        class PlannedWarmSession(FakeSession):
            def execute(self, scenario: dict) -> dict:
                receipt = super().execute(scenario)
                if scenario["cache_state"] == "warm_cached":
                    receipt["reason_family"] = "planned"
                return receipt
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); runner, journal, stop, evidence, digest, scope_digest = \
                make_runner(base, plan())
            report = runner.run(level_one_authorization(evidence, digest, scope_digest),
                evidence, correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: PlannedWarmSession(mode, control))
            journal.close()
            self.assertTrue(stop.stopped)
            self.assertEqual(report["technical_result"], "FAIL")
            self.assertIn("warm_cache_unproven",
                          (base / "output/measurement-samples.jsonl").read_text())

    def test_correctness_gate_refuses_before_session_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); runner, journal, _stop, evidence, digest, scope_digest = make_runner(base, plan())
            called = []
            with self.assertRaisesRegex(MeasurementError, "correctness"):
                runner.run(level_one_authorization(evidence, digest, scope_digest), evidence,
                    correctness_gate(digest, scope_digest, failed=True),
                    lambda *args: called.append(args))
            journal.close()
            self.assertEqual(called, [])
            self.assertEqual(list((base / "output").iterdir()), [])

    def test_timeout_and_exception_are_retained_sanitized_and_stop_new_blocks(self) -> None:
        for secret in (False, True):
            with self.subTest(secret=secret), tempfile.TemporaryDirectory() as name:
                base = Path(name); runner, journal, stop, evidence, digest, scope_digest = make_runner(base, plan())
                report = runner.run(level_one_authorization(evidence, digest, scope_digest), evidence,
                    correctness_gate(digest, scope_digest),
                    lambda mode, _slot, control: FakeSession(mode, control,
                        fail_measurement=not secret, secret_exception=secret))
                journal.close(); raw = (base / "output/measurement-samples.jsonl").read_text()
                self.assertEqual(report["technical_result"], "FAIL")
                self.assertTrue(stop.stopped)
                self.assertLess(report["sample_count"], 36)
                self.assertIn("session_exception" if secret else "query_deadline", raw)
                self.assertNotIn("do-not-export", raw); self.assertNotIn("secret.example", raw)

    def test_complete_weights_can_produce_only_scoped_positive_value(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); value = plan("approved")
            runner, journal, _stop, evidence, digest, scope_digest = make_runner(base, value)
            report = runner.run(level_one_authorization(evidence, digest, scope_digest), evidence,
                correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: FakeSession(mode, control))
            journal.close()
            self.assertEqual(report["value_outcome"], "POSITIVE")
            self.assertIn("NO_ROI_CLAIM", report["limitations"])
            self.assertIn("VALUE_SEPARATE_FROM_SAFETY", report["limitations"])

    def test_missing_coverage_never_becomes_zero_or_positive(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); runner, journal, _stop, evidence, digest, scope_digest = make_runner(base, plan())
            report = runner.run(level_one_authorization(evidence, digest, scope_digest), evidence,
                correctness_gate(digest, scope_digest, complete=False),
                lambda mode, _slot, control: FakeSession(mode, control))
            journal.close()
            self.assertIsNone(report["coverage"]["frequency_coverage"])
            self.assertIsNone(report["coverage"]["baseline_measured_share"])
            self.assertEqual(report["value_outcome"], "INCONCLUSIVE")

    def test_stop_latched_during_final_request_cannot_report_pass(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); value = plan(levels=1)
            runner, journal, _stop, evidence, digest, scope_digest = make_runner(base, value)
            calls = [0]
            class StopOnLast(FakeSession):
                def execute(self, scenario: dict) -> dict:
                    receipt = super().execute(scenario); calls[0] += 1
                    if calls[0] == 54:
                        self.control.stop("USER_STOP")
                    return receipt
            report = runner.run(level_one_authorization(evidence, digest, scope_digest),
                evidence, correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: StopOnLast(mode, control))
            journal.close()
            self.assertEqual(calls[0], 54)
            self.assertEqual(report["technical_result"], "INCOMPLETE")
            self.assertEqual(report["stop_reason"], "USER_STOP")


class EscalationTests(unittest.TestCase):
    def _previous_evidence(self, root: Path, report_source: Path,
                           plan_digest: str, scope_digest: str) -> dict:
        shutil.copy2(report_source, root / "level1-report.json")
        report = json.loads((root / "level1-report.json").read_text())
        sample_source = report_source.parent / report["sample_file"]["path"]
        shutil.copy2(sample_source, root / "level1-samples.jsonl")
        report["sample_file"] = artifact(root, "level1-samples.jsonl")
        write_document(root / "level1-report.json", report)
        retirement = {"schema_version": 1, "contract_type": "retirement",
            "contract_id": "retirement-level-1", "scope_sha256": scope_digest,
            "run_id": "measurement-run-1", "owned_sessions": ["session-1"],
            "backend_absence_verified": True, "cached_plans_retired": True,
            "default_off_reconnect": True, "state": state()}
        write_document(root / "level1-retirement.json", retirement)
        payloads = [artifact(root, "level1-report.json"),
                    artifact(root, "level1-samples.jsonl"),
                    artifact(root, "level1-retirement.json")]
        index = {"schema_version": 1, "contract_type": "index",
            "contract_id": "index-level-1", "scope_sha256": scope_digest,
            "payloads": payloads, "payload_flush_fsync_complete": True,
            "index_sequence": 1}
        write_document(root / "level1-index.json", index)
        terminal = {"schema_version": 1, "contract_type": "terminal",
            "contract_id": "terminal-level-1", "scope_sha256": scope_digest,
            "index_sha256": canonical_sha256(index), "state": state(),
            "aggregate_pass": True}
        write_document(root / "level1-terminal.json", terminal)
        validation = {"schema_version": 1, "contract_type": "validation",
            "contract_id": "validation-level-1", "scope_sha256": scope_digest,
            "index_sha256": canonical_sha256(index),
            "terminal_sha256": canonical_sha256(terminal), "profile": "private_full",
            "result": "PASS", "checks": ["measurement", "retirement"]}
        write_document(root / "level1-validation.json", validation)
        return {"level": 1, "index": artifact(root, "level1-index.json"),
            "terminal": artifact(root, "level1-terminal.json"),
            "validation": artifact(root, "level1-validation.json"),
            "report": artifact(root, "level1-report.json"),
            "retirement": artifact(root, "level1-retirement.json")}

    def test_level_two_requires_bound_private_full_clean_level_one(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); value = plan(); runner1, journal1, _stop1, evidence, digest, scope_digest = \
                make_runner(base / "first", value)
            report = runner1.run(level_one_authorization(evidence, digest, scope_digest), evidence,
                correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: FakeSession(mode, control))
            journal1.close(); self.assertEqual(report["technical_result"], "PASS")

            second = base / "second"; second.mkdir(); evidence2 = second / "evidence"
            evidence2.mkdir(mode=0o700); previous = self._previous_evidence(
                evidence2, base / "first/output/measurement-report.json", digest,
                scope_digest)
            authorization = {"schema_version": 1,
                "document_type": "measurement_level_authorization",
                "scope_sha256": scope_digest, "plan_sha256": digest, "requested_level": 2,
                "approval_reference": "level-two-approval",
                "operation_go": operation_go(evidence2, scope_digest),
                "operation_preflight": operation_preflight(evidence2, scope_digest),
                **operation_recovery_artifacts(evidence2, scope_digest),
                "previous_level": previous}
            control_root = second / "control"; control_root.mkdir(mode=0o700)
            output = second / "output"; output.mkdir(mode=0o700)
            scope, checked_scope_digest = measurement_scope(value)
            self.assertEqual(checked_scope_digest, scope_digest)
            policy = resource_policy(scope_digest); journal2, stop2 = controller(control_root, policy)
            runner2 = MeasurementRunner(value, digest, scope, scope_digest, policy,
                value["closure_inputs"]["resource_policy_sha256"], stop2, output,
                "measurement-run-2")
            FakeSession.maximum_active = 0
            result = runner2.run(authorization, evidence2,
                correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: FakeSession(mode, control))
            journal2.close()
            self.assertEqual(result["technical_result"], "PASS")
            self.assertEqual(FakeSession.maximum_active, 2)

            forged = deepcopy(authorization)
            forged["previous_level"]["validation"]["sha256"] = "f" * 64
            with self.assertRaises((MeasurementError, ContractError)):
                validate_level_authorization(forged, value, digest, scope_digest, evidence2)

            report_path = evidence2 / "level1-report.json"
            forged_report = json.loads(report_path.read_text())
            forged_report["groups"][0]["p50_ns"] += 1
            write_document(report_path, forged_report)
            index_path = evidence2 / "level1-index.json"
            forged_index = json.loads(index_path.read_text())
            forged_index["payloads"] = [artifact(evidence2, item["path"])
                for item in forged_index["payloads"]]
            write_document(index_path, forged_index)
            terminal_path = evidence2 / "level1-terminal.json"
            forged_terminal = json.loads(terminal_path.read_text())
            forged_terminal["index_sha256"] = canonical_sha256(forged_index)
            write_document(terminal_path, forged_terminal)
            validation_path = evidence2 / "level1-validation.json"
            forged_validation = json.loads(validation_path.read_text())
            forged_validation["index_sha256"] = canonical_sha256(forged_index)
            forged_validation["terminal_sha256"] = canonical_sha256(forged_terminal)
            write_document(validation_path, forged_validation)
            forged_seal = deepcopy(authorization)
            for name, path in (("index", "level1-index.json"),
                               ("terminal", "level1-terminal.json"),
                               ("validation", "level1-validation.json"),
                               ("report", "level1-report.json")):
                forged_seal["previous_level"][name] = artifact(evidence2, path)
            with self.assertRaisesRegex(MeasurementError, "sample_stream_invalid"):
                validate_level_authorization(forged_seal, value, digest,
                                             scope_digest, evidence2)

    def test_level_two_refuses_missing_server_feed_or_previous_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); value = plan(); digest = canonical_sha256(value)
            _scope, scope_digest = measurement_scope(value)
            auth = {"schema_version": 1, "document_type": "measurement_level_authorization",
                "scope_sha256": scope_digest, "plan_sha256": digest, "requested_level": 2,
                "approval_reference": "level-two-approval",
                "operation_go": operation_go(root, scope_digest),
                "operation_preflight": operation_preflight(root, scope_digest),
                **operation_recovery_artifacts(root, scope_digest),
                "previous_level": None}
            with self.assertRaises(MeasurementError):
                validate_level_authorization(auth, value, digest, scope_digest, root)

    def test_level_two_accepts_namespaced_previous_private_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700)
            private = root / "private-level-1"; private.mkdir(mode=0o700)
            value = plan(); digest = canonical_sha256(value)
            _scope, scope_digest = measurement_scope(value)
            runner, journal, _stop, evidence, _digest, _scope_digest = \
                make_runner(root / "runner", value)
            generated = runner.run(level_one_authorization(evidence, digest, scope_digest),
                evidence, correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: FakeSession(mode, control))
            journal.close()
            previous = self._previous_evidence(private,
                root / "runner/output/measurement-report.json", digest, scope_digest)
            validation_payload = (private / "level1-validation.json").read_bytes()
            (root / "private-level-1-validation.json").write_bytes(validation_payload)
            (root / "private-level-1-validation.json").chmod(0o600)
            (private / "level1-validation.json").unlink()
            for key in ("index", "terminal", "report", "retirement"):
                previous[key] = {**previous[key],
                    "path": f"private-level-1/{previous[key]['path']}"}
            previous["validation"] = artifact(root, "private-level-1-validation.json")
            authorization = {"schema_version": 1,
                "document_type": "measurement_level_authorization",
                "scope_sha256": scope_digest, "plan_sha256": digest,
                "requested_level": 2, "approval_reference": "level-two-approval",
                "operation_go": operation_go(root, scope_digest),
                "operation_preflight": operation_preflight(root, scope_digest),
                **operation_recovery_artifacts(root, scope_digest),
                "previous_level": previous}
            level = validate_level_authorization(authorization, value, digest, scope_digest,
                root, previous_evidence_root=private,
                previous_path_prefix="private-level-1")
            self.assertEqual(level["level"], 2)
            runner2, journal2, _stop2, _evidence2, _digest2, _scope2 = \
                make_runner(root / "second", value)
            pilot_attempt = {"schema_version": 1,
                "document_type": "internal_pilot_attempt", "record_id": "attempt-2",
                "pilot_id": "pilot-1", "sequence": 2, "scope_sha256": scope_digest,
                "plan_sha256": "e" * 64,
                "operation_go_sha256": authorization["operation_go"]["sha256"],
                "previous_record_sha256": "0" * 64, "window_id": "window-2",
                "level": 2, "run_id": "measurement-run-1",
                "consumed_at": "2026-11-18T00:30:00Z"}
            ledger = root / "pilot-ledger"; ledger.mkdir(mode=0o700)
            write_document(ledger / "ledger-id.json", {"ledger_id": "pilot-ledger-1"})
            pilot_plan = {"attempt_ledger": {"path": str(ledger),
                "device": ledger.stat().st_dev, "inode": ledger.stat().st_ino,
                "identity": artifact(ledger, "ledger-id.json"),
                "storage_control": "EXTERNAL_APPEND_ONLY"}}
            pilot_plan_sha256 = canonical_sha256(pilot_plan)
            pilot_attempt["plan_sha256"] = pilot_plan_sha256
            write_document(ledger / "attempt-0002.json", pilot_attempt)
            report2 = runner2.run(authorization, root,
                correctness_gate(digest, scope_digest),
                lambda mode, _slot, control: FakeSession(mode, control),
                previous_evidence_root=private,
                previous_path_prefix="private-level-1",
                pilot_attempt=pilot_attempt, pilot_window_id="window-2",
                pilot_plan_sha256=pilot_plan_sha256, pilot_ledger_root=ledger,
                pilot_plan=pilot_plan)
            journal2.close()
            self.assertEqual(report2["technical_result"], "PASS")
            self.assertEqual(report2["level"], 2)
            self.assertEqual(report2["pilot_attempt_sha256"],
                             canonical_sha256(pilot_attempt))
            self.assertTrue((ledger / "dispatch-0002.json").is_file())
            with self.assertRaisesRegex(MeasurementError, "already_dispatched"):
                runner2.run(authorization, root,
                    correctness_gate(digest, scope_digest),
                    lambda mode, _slot, control: FakeSession(mode, control),
                    previous_evidence_root=private,
                    previous_path_prefix="private-level-1",
                    pilot_attempt=pilot_attempt, pilot_window_id="window-2",
                    pilot_plan_sha256=pilot_plan_sha256, pilot_ledger_root=ledger,
                    pilot_plan=pilot_plan)

    def test_previous_namespace_arguments_must_be_supplied_together(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700)
            value = plan(); digest = canonical_sha256(value)
            _scope, scope_digest = measurement_scope(value)
            authorization = level_one_authorization(root, digest, scope_digest)
            with self.assertRaisesRegex(
                    MeasurementError, "previous_level_namespace_arguments_incomplete"):
                validate_level_authorization(
                    authorization, value, digest, scope_digest, root,
                    previous_evidence_root=root)
            with self.assertRaisesRegex(
                    MeasurementError, "previous_level_namespace_arguments_incomplete"):
                validate_level_authorization(
                    authorization, value, digest, scope_digest, root,
                    previous_path_prefix="private-level-1")


if __name__ == "__main__":
    unittest.main()
