#!/usr/bin/env python3
"""Adversarial tests for the offline evidence validator and exporter."""

from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import sys
import struct
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/phase5"))
import contract_rules as rules  # noqa: E402

SPEC = importlib.util.spec_from_file_location("evidence_tool", ROOT / "tools/phase5/evidence_tool.py")
tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tool)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rules.canonical_bytes(value))


def artifact(root: Path, relative: str) -> dict[str, object]:
    payload = (root / relative).read_bytes()
    return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def write_spool(path: Path, rows: list[tuple[int | None, ...]],
                columns: list[tuple[str, int]] | None = None) -> None:
    columns = columns or [("value", 23)]
    payload = bytearray(b"PGQSP001") + struct.pack(">I", len(columns))
    for name, oid in columns:
        encoded = name.encode()
        payload += struct.pack(">I", len(encoded)) + encoded + struct.pack(">I", oid)
    for row in rows:
        payload += struct.pack(">I", len(row))
        for value in row:
            if value is None:
                payload += struct.pack(">I", 0xFFFFFFFF)
            else:
                encoded = str(value).encode()
                payload += struct.pack(">I", len(encoded)) + encoded
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)


def state(result: str = "PASS", complete: bool = True) -> dict[str, str]:
    return {"lifecycle": "VALIDATED", "technical_result": result,
            "evidence_completeness": "COMPLETE" if complete else "PARTIAL",
            "exit_state": "CLEAN", "value_outcome": "POSITIVE" if result == "PASS" else "INCONCLUSIVE",
            "project_decision": "CONTINUE" if result == "PASS" else "PAUSE"}


def snapshot_tuple() -> dict[str, str]:
    return {"contract_version": "1", "context": "proven_client",
            "policy_intent": "request_delegation", "reason": "delegation_verified",
            "force_origin": "qi_stack", "classifier_status": "candidate",
            "classifier_reason": "candidate", "observed_engine": "duckdb",
            "verification": "verified", "observation": "duckdb_scan_at_downstream_boundary",
            "request_applied": "on", "duckdb_force_execution": "off"}


def contract(kind: str, identity: str, **fields: object) -> dict[str, object]:
    return {"schema_version": 1, "contract_type": kind, "contract_id": identity, **fields}


def measurement_plan(root: Path, query_id: str) -> dict[str, object]:
    input_digest = lambda name: artifact(root, f"inputs/{name}.json")["sha256"]
    return contract("measurement_plan", "measurement-plan-1",
        closure_inputs={
            "workload_definition_sha256": input_digest("workload_definition"),
            "qualification_sha256": input_digest("workload_definition"),
            "baseline_manifest_sha256": input_digest("baseline_manifest"),
            "resource_policy_sha256": input_digest("limits"),
            "release_sha256": input_digest("release_definition"),
            "target_sha256": input_digest("target"),
            "dataset_sha256": input_digest("dataset"),
            "toolkit_sha256": input_digest("toolkit")},
        required_scenarios=[{"scenario_id": "scenario-1", "query_id": query_id,
            "case_id": query_id.replace("-", "_"),
            "protocol": "extended", "cache_state": "warm_cached",
            "oracle_case_id": query_id, "natural_query_id": query_id,
            "expected_result_bytes_max": 1048576}],
        seed=101, order_algorithm="seeded_balanced_williams_v1",
        measurement_blocks=[{"block_id": f"block-{index:04d}", "order": order}
            for index, order in enumerate(rules.measurement_block_orders(101, 6), 1)],
        measurement_repetitions=1,
        warmup_repetitions=1, protocols=["extended"], cache_states=["warm_cached"],
        concurrency_levels=[{"level": 1, "sessions": 1,
            "approval_reference": "measurement-level-one",
            "server_resource_feed_sha256": None}],
        statistics={"percentile_method": "nearest_rank",
            "minimum_n": {"p50": 1, "p95": 20, "p99": 100},
            "outlier_policy": "retain_all", "failure_policy": "retain_and_count",
            "variability": "per_block_median_range"},
        value_rules={"threshold_kind": "descriptive_only",
            "minimum_relative_improvement_bps": None,
            "minimum_paired_blocks": None,
            "frequency_weights_sha256": None, "baseline_weights_sha256": None,
            "attribution": "measured_correct_natural_subset_only",
            "incomplete_weights_outcome": "INCONCLUSIVE"})


def build_private(root: Path, outcome: str = "PASS", missing_cell: bool = False,
                  raw_retained: bool = True, query_id: str = "q-1",
                  malformed_spools: bool = False) -> None:
    input_names = ("workload_definition", "measurement_plan", "baseline_manifest", "dataset",
                   "release_definition", "target", "limits",
                   "evidence_policy", "recovery_plan", "export_policy", "toolkit",
                   "role_bindings", "window")
    for name in input_names:
        write_json(root / f"inputs/{name}.json", {"name": name, "revision": 1})
    (root / "raw").mkdir()
    (root / "scratch").mkdir(mode=0o700)
    (root / "raw/q.sql").write_text("SELECT 1\n", encoding="utf-8")
    if malformed_spools:
        for path in (root / "raw/expected.spool", root / "raw/observed.spool"):
            path.write_bytes(b"same invalid spool bytes"); path.chmod(0o600)
    else:
        write_spool(root / "raw/expected.spool", [(1,), (1,), (None,)])
        write_spool(root / "raw/observed.spool",
                    [(None,), (1,), (1,)] if outcome != "FAIL" else [(None,), (1,), (2,)])
    (root / "raw/debug.log").write_text("clean diagnostic\n")
    authority = {"state": "AUTHORISED", "reference": "qualification-approval",
                 "authorized_before_acquisition": True}
    write_json(root / "inputs/workload_definition.json", {"schema_version": 1,
        "document_type": "corpus_qualification", "qualification_id": "qualification-1",
        "authorities": {"inventory": dict(authority), "executed_sql": dict(authority),
                        "dataset": dict(authority)},
        "storage_policy": {"authorization_reference": "storage-approval",
            "raw_root": "/private/raw", "spool_root": "/private/spool",
            "scratch_root": str(root / "scratch"), "required_mode": "0700",
            "retention_class": "retention-30d", "retention_authority": "evidence-owner",
            "authorized_before_acquisition": True},
        "baseline_authority": {"authorization_reference": "baseline-approval",
            "authorized_before_acquisition": True, "clean_instance_attested": True,
            "server_build_sha256": "8" * 64, "schema_sha256": "9" * 64,
            "data_sha256": "a" * 64, "configuration_sha256": "b" * 64,
            "provider_identity": "POSTGRESQL_NATIVE_NO_DELEGATING_PROVIDER",
            "cases": [{"query_id": query_id, "spool_path": "raw/expected.spool",
                "spool_sha256": artifact(root, "raw/expected.spool")["sha256"],
                "spool_bytes": artifact(root, "raw/expected.spool")["bytes"]}]},
        "oracle_limits": {"max_spool_bytes": 1048576, "max_rows": 10000,
            "max_columns": 32, "max_field_bytes": 128, "max_rows_in_memory": 64,
            "max_open_runs": 8},
        "natural_inventory": [{"query_id": query_id, "inventory_origin": "natural_internal",
            "inventory_source_ref": "inventory-1", "eligibility": "ELIGIBLE",
            "reason_family": "supported", "frequency_weight": 1,
            "baseline_time_weight": 1}],
        "execution_corpus": [{"query_id": query_id, "natural_query_id": query_id,
            "executed_sql_origin": "natural_internal", "executed_sql_ref": "sql-1",
            "sql_sha256": artifact(root, "raw/q.sql")["sha256"],
            "dataset_origin": "synthetic_control", "dataset_ref": "dataset-1",
            "concurrent_writes": False}], "synthetic_controls": []})
    baseline_manifest = {"schema_version": 1,
        "document_type": "baseline_manifest", "baseline_id": "baseline-1",
        "server_build_sha256": "8" * 64, "schema_sha256": "9" * 64,
        "data_sha256": "a" * 64, "configuration_sha256": "b" * 64,
        "provider_identity": "POSTGRESQL_NATIVE_NO_DELEGATING_PROVIDER",
        "provider_absence_verified": True, "static_data": True,
        "concurrent_writes": False, "cases": [{"query_id": query_id,
            "spool_path": "raw/expected.spool",
            "spool_sha256": artifact(root, "raw/expected.spool")["sha256"],
            "spool_bytes": artifact(root, "raw/expected.spool")["bytes"]}]}
    write_json(root / "inputs/baseline_manifest.json", baseline_manifest)
    write_json(root / "inputs/dataset.json", {"schema_version": 1,
        "document_type": "dataset_identity", "data_sha256": "a" * 64})
    write_json(root / "inputs/measurement_plan.json", measurement_plan(root, query_id))

    scope = contract("scope", "scope-1",
        scope_inputs={name: artifact(root, f"inputs/{name}.json") for name in input_names},
        gates_required=["SCOPE_AUTHORIZED", "OPERATION_GO"])
    write_json(root / "scope.json", scope); scope_digest = rules.canonical_sha256(scope)
    current_state = state("PASS" if outcome == "PASS" else
                          "FAIL" if outcome == "FAIL" else "INCONCLUSIVE",
                          outcome != "INCOMPLETE")
    preflight = contract("preflight", "preflight-1", scope_sha256=scope_digest,
        target_identity_sha256="1" * 64, checks=["target", "boundary"], state=current_state)
    write_json(root / "payload/preflight.json", preflight)
    approval = contract("approval_receipt", "approval-1", receipt_kind="OPERATION_GO",
        scope_sha256=scope_digest, preflight_sha256=rules.canonical_sha256(preflight),
        valid_from="2026-11-17T00:00:00Z", valid_through="2026-11-18T00:00:00Z",
        approvals=["operation-owner"], nonce="nonce-1")
    write_json(root / "payload/approval.json", approval)
    workload = contract("workload", "workload-1", scope_sha256=scope_digest,
        inventory_sha256=artifact(root, "inputs/workload_definition.json")["sha256"], entries=[{
            "query_id": query_id, "sql": artifact(root, "raw/q.sql"),
            "inventory_origin": "natural_internal", "inventory_authority": "AUTHORISED",
            "executed_sql_origin": "natural_internal", "executed_sql_authority": "AUTHORISED",
            "dataset_origin": "synthetic_control", "dataset_authority": "AUTHORISED",
            "execution_authority": "OPERATION_GO"}])
    write_json(root / "payload/workload.json", workload)
    write_json(root / "payload/release.json", contract("release", "release-1",
        scope_sha256=scope_digest, source_commit="a" * 40, source_dirty=False,
        server_build_sha256="3" * 64, pg_qi_sha256="4" * 64,
        pg_duckdb_sha256="5" * 64, configuration_sha256="6" * 64))
    snapshot = contract("snapshot", "snapshot-1", scope_sha256=scope_digest,
        snapshot_id="snapshot-1", session_id="session-1", planning_request_id="plan-1",
        prepared_object_id="statement-A", prepared_generation=1, decision_id=1,
        tuple=snapshot_tuple())
    write_json(root / "payload/snapshot.json", snapshot)
    write_json(root / "payload/request.json", contract("request", "request-1",
        scope_sha256=scope_digest, run_id="run-1", session_id="session-1",
        request_id="request-1", query_id=query_id,
        execution_status="SUCCEEDED" if outcome in {"PASS", "FAIL"} else "NOT_STARTED",
        planning_evidence="FRESH", snapshot_relation="CURRENT",
        snapshot_id="snapshot-1", planning_request_id="plan-1",
        **({"observed_spool": artifact(root, "raw/observed.spool")}
           if outcome in {"PASS", "FAIL"} else {})))
    write_json(root / "payload/resource.json", contract("resource", "resource-1",
        scope_sha256=scope_digest, run_id="run-1", sample_sequence=1, monotonic_ns=1,
        source_id="proc-stat", values={"rss": 1024}, units={"rss": "bytes"}, source_age_ms=1))
    write_json(root / "payload/retirement.json", contract("retirement", "retirement-1",
        scope_sha256=scope_digest, run_id="run-1", owned_sessions=["session-1"],
        backend_absence_verified=True, cached_plans_retired=True,
        default_off_reconnect=True, state=current_state))
    write_json(root / "payload/recovery.json", contract("recovery", "recovery-1",
        scope_sha256=scope_digest, strategy="VERIFIED_ENVIRONMENT_RESTORE",
        artifact_sha256="7" * 64, configuration_sha256="6" * 64,
        rehearsal_result="PASS" if outcome == "PASS" else
                         "FAIL" if outcome == "FAIL" else "NOT_RUN", state=current_state))

    oracle_result = "PASS" if outcome == "PASS" else "FAIL" if outcome == "FAIL" else "NOT_RUN"
    write_json(root / "payload/baseline.json", baseline_manifest)
    write_json(root / "payload/oracle.json", {"schema_version": 1, "document_type": "oracle",
        "oracle_id": "oracle-1", "cases": [{"query_id": query_id,
        "baseline_spool": artifact(root, "raw/expected.spool"),
        "observed_spool": artifact(root, "raw/observed.spool"),
        "result": oracle_result, "raw_retained": raw_retained,
        "sqlstate": None}]})
    cell_names = sorted(tool.REQUIRED_SITE_CELLS)
    if missing_cell: cell_names.pop()
    cell_result = "PASS" if outcome == "PASS" else "FAIL" if outcome == "FAIL" else "NOT_RUN"
    write_json(root / "payload/site-cells.json", {"schema_version": 1,
        "document_type": "site_cells", "cells": [{"cell_id": name, "result": cell_result,
        "evidence_sha256": artifact(root, "raw/expected.spool")["sha256"]} for name in cell_names]})

    payload_names = [path.relative_to(root).as_posix() for path in root.rglob("*")
                     if path.is_file() and path.name != "scope.json"]
    index = contract("index", "index-1", scope_sha256=scope_digest,
        payloads=[artifact(root, name) for name in sorted(payload_names)],
        payload_flush_fsync_complete=True, index_sequence=1)
    write_json(root / "index.json", index)
    write_json(root / "terminal.json", contract("terminal", "terminal-1",
        scope_sha256=scope_digest, index_sha256=rules.canonical_sha256(index),
        state=current_state, aggregate_pass=rules.aggregate_pass(current_state)))


def reseal(root: Path) -> None:
    scope = json.loads((root / "scope.json").read_text())
    index = json.loads((root / "index.json").read_text())
    index["payloads"] = [artifact(root, entry["path"]) for entry in index["payloads"]]
    write_json(root / "index.json", index)
    terminal = json.loads((root / "terminal.json").read_text())
    terminal["index_sha256"] = rules.canonical_sha256(index)
    terminal["scope_sha256"] = rules.canonical_sha256(scope)
    write_json(root / "terminal.json", terminal)


def add_measurement_evidence(root: Path) -> None:
    scope_digest = scope_hash(root)
    plan = json.loads((root / "inputs/measurement_plan.json").read_text())
    plan_digest = rules.canonical_sha256(plan)
    samples = []
    ordinal = 0
    def add(scenario: str, mode: str, phase: str, block: int, order: int,
            duration: int) -> None:
        nonlocal ordinal
        ordinal += 1
        samples.append(contract("measurement_sample", f"measurement-sample-{ordinal}",
            scope_sha256=scope_digest, plan_sha256=plan_digest,
            run_id="measurement-run-1", level=1, sample_id=f"sample-{ordinal}",
            scenario_id=scenario, mode=mode, phase=phase,
            protocol="extended", cache_state="warm_cached",
            block_index=block, order_index=order, session_slot=1,
            request_interval={"started_monotonic_ns": ordinal * 1000000,
                "finished_monotonic_ns": ordinal * 1000000 + duration,
                "duration_ns": duration, "state": "COMPLETE", "censored": False},
            postprocess_duration_ns=100, terminal_state="PASS", reason_family="cached",
            result_bytes=8, oracle_result="PASS"))
    for block_index, block in enumerate(plan["measurement_blocks"], 1):
        for order_index, mode in enumerate(block["order"], 1):
            duration = {"disabled": 300, "off": 200, "allowlist": 100}[mode]
            add("scenario-1", mode, "WARMUP", block_index, order_index, duration)
            add("scenario-1", mode, "MEASURE", block_index, order_index,
                duration)
    payload = b"".join(rules.canonical_bytes(sample) for sample in samples)
    (root / "measurement-samples.jsonl").write_bytes(payload)
    groups = []
    measured_n = len(plan["measurement_blocks"])
    for mode, duration in (("disabled", 300), ("off", 200), ("allowlist", 100)):
        groups.append({"scenario_id": "scenario-1", "mode": mode, "n": measured_n,
            "pass": measured_n, "fail": 0, "timeout": 0, "incomplete": 0,
            "p50_ns": duration,
            "postprocess_p50_ns": 100,
            "p95_ns": duration if measured_n >= 20 else None,
            "p99_ns": duration if measured_n >= 100 else None,
            "p95_status": "REPORTED" if measured_n >= 20 else
                f"SUPPRESSED(N={measured_n},MIN=20)",
            "p99_status": "REPORTED" if measured_n >= 100 else
                f"SUPPRESSED(N={measured_n},MIN=100)",
            "block_median_min_ns": duration, "block_median_max_ns": duration})
    approved_value = plan["value_rules"]["threshold_kind"] == \
        "approved_relative_improvement"
    report = contract("measurement_report", "measurement-report-1",
        scope_sha256=scope_digest, plan_sha256=plan_digest,
        run_id="measurement-run-1", level=1,
        sample_file=artifact(root, "measurement-samples.jsonl"),
        sample_count=measured_n * 3, raw_sample_count=measured_n * 6,
        warmup_sample_count=measured_n * 3,
        groups=groups, contrasts=[{"scenario_id": "scenario-1",
            "extension_overhead_p50_ns": -100,
            "routing_effect_p50_ns": -100}],
        coverage={"structural_eligibility": 1.0,
            "frequency_coverage": 1.0, "baseline_measured_share": 1.0,
            "measured_natural_share": 1.0, "missing_natural_items": 0},
        value_outcome="POSITIVE" if approved_value else "INCONCLUSIVE",
        technical_result="PASS", stop_reason=None,
        limitations=sorted(rules.MEASUREMENT_LIMITATIONS))
    write_json(root / "payload/measurement-report.json", report)
    index = json.loads((root / "index.json").read_text())
    additions = [artifact(root, "measurement-samples.jsonl"),
                 artifact(root, "payload/measurement-report.json")]
    for relative in ("payload/frequency-weights.json", "payload/baseline-weights.json"):
        if (root / relative).exists():
            additions.append(artifact(root, relative))
    index["payloads"].extend(additions)
    write_json(root / "index.json", index); reseal(root)


def scope_hash(root: Path) -> str:
    return rules.canonical_sha256(json.loads((root / "scope.json").read_text()))


def rebind_scope(root: Path) -> str:
    digest = scope_hash(root)
    for path in (root / "payload").glob("*.json"):
        document = json.loads(path.read_text())
        if "scope_sha256" in document:
            document["scope_sha256"] = digest
            write_json(path, document)
    preflight = json.loads((root / "payload/preflight.json").read_text())
    approval = json.loads((root / "payload/approval.json").read_text())
    approval["preflight_sha256"] = rules.canonical_sha256(preflight)
    write_json(root / "payload/approval.json", approval)
    index = json.loads((root / "index.json").read_text())
    index["scope_sha256"] = digest
    write_json(root / "index.json", index)
    reseal(root)
    return digest


def reseal_shared(root: Path) -> None:
    index = json.loads((root / "index.json").read_text())
    index["payloads"] = [artifact(root, entry["path"]) for entry in index["payloads"]]
    write_json(root / "index.json", index)
    terminal = json.loads((root / "terminal.json").read_text())
    terminal["index_sha256"] = rules.canonical_sha256(index)
    write_json(root / "terminal.json", terminal)


class PrivateValidationTests(unittest.TestCase):
    def test_correctness_gate_is_issued_only_by_full_private_validation(self) -> None:
        from measurement_runtime import MeasurementError, validate_correctness_bundle
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            plan = json.loads((root / "inputs/measurement_plan.json").read_text())
            gate = validate_correctness_bundle(root, scope_hash(root), plan,
                                               rules.canonical_sha256(plan))
            self.assertEqual(gate.oracle_cases, {"q-1": "PASS"})
            changed = json.loads(json.dumps(plan)); changed["seed"] += 1
            changed["measurement_blocks"] = [
                {"block_id": f"block-{index:04d}", "order": order}
                for index, order in enumerate(rules.measurement_block_orders(
                    changed["seed"], changed["measurement_repetitions"] * 6), 1)]
            with self.assertRaisesRegex(MeasurementError, "did_not_pass"):
                validate_correctness_bundle(root, scope_hash(root), changed,
                                            rules.canonical_sha256(changed))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root, outcome="FAIL")
            plan = json.loads((root / "inputs/measurement_plan.json").read_text())
            with self.assertRaisesRegex(MeasurementError, "did_not_pass"):
                validate_correctness_bundle(root, scope_hash(root), plan,
                                            rules.canonical_sha256(plan))

    def test_measurement_report_is_recomputed_from_indexed_raw_samples(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root); add_measurement_evidence(root)
            self.assertEqual(tool.validate_private(root, scope_hash(root))["result"], "PASS")
            report = json.loads((root / "payload/measurement-report.json").read_text())
            report["groups"][0]["p50_ns"] += 1
            write_json(root / "payload/measurement-report.json", report); reseal(root)
            with self.assertRaisesRegex(rules.ContractError, "statistics differ"):
                tool.validate_private(root, scope_hash(root))

    def test_measurement_pass_refuses_unproven_warm_cache_label(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root); add_measurement_evidence(root)
            lines = (root / "measurement-samples.jsonl").read_bytes().splitlines()
            sample = rules.strict_load_bytes(lines[1] + b"\n")
            self.assertEqual(sample["phase"], "MEASURE")
            sample["reason_family"] = "planned"
            lines[1] = rules.canonical_bytes(sample).rstrip(b"\n")
            (root / "measurement-samples.jsonl").write_bytes(
                b"\n".join(lines) + b"\n")
            report = json.loads((root / "payload/measurement-report.json").read_text())
            report["sample_file"] = artifact(root, "measurement-samples.jsonl")
            write_json(root / "payload/measurement-report.json", report); reseal(root)
            with self.assertRaisesRegex(rules.ContractError,
                                        "technical PASS contradicts"):
                tool.validate_private(root, scope_hash(root))

    def test_positive_measurement_value_requires_indexed_frozen_weights(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            frequency_weights = {"q-1": 1}
            write_json(root / "payload/frequency-weights.json", frequency_weights)
            plan = json.loads((root / "inputs/measurement_plan.json").read_text())
            plan["value_rules"].update(
                threshold_kind="approved_relative_improvement",
                minimum_relative_improvement_bps=1000,
                minimum_paired_blocks=20,
                frequency_weights_sha256=artifact(
                    root, "payload/frequency-weights.json")["sha256"],
                baseline_weights_sha256=artifact(
                    root, "payload/frequency-weights.json")["sha256"])
            plan["measurement_repetitions"] = 4
            plan["measurement_blocks"] = [
                {"block_id": f"block-{index:04d}", "order": order}
                for index, order in enumerate(
                    rules.measurement_block_orders(plan["seed"], 24), 1)]
            write_json(root / "inputs/measurement_plan.json", plan)
            scope = json.loads((root / "scope.json").read_text())
            scope["scope_inputs"]["measurement_plan"] = artifact(
                root, "inputs/measurement_plan.json")
            write_json(root / "scope.json", scope); rebind_scope(root)
            add_measurement_evidence(root)
            self.assertEqual(tool.validate_private(root, scope_hash(root))["result"], "PASS")
            (root / "payload/frequency-weights.json").unlink()
            index = json.loads((root / "index.json").read_text())
            index["payloads"] = [item for item in index["payloads"]
                if item["path"] != "payload/frequency-weights.json"]
            write_json(root / "index.json", index); reseal(root)
            with self.assertRaisesRegex(rules.ContractError, "not reproducible"):
                tool.validate_private(root, scope_hash(root))

    def test_golden_pass_and_external_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "bundle"; root.mkdir(); build_private(root)
            validated = tool.validate_private(root, scope_hash(root)); self.assertEqual(validated["result"], "PASS")
            receipt = Path(name) / "validation.json"
            tool.write_validation_receipt(validated, receipt, "validation-1")
            self.assertEqual(rules.strict_load_path(Path(receipt.name), receipt.parent)["result"], "PASS")

    def test_documented_fail_and_partial_are_distinct(self) -> None:
        for outcome, missing, expected in (("FAIL", False, "FAIL"),
                                           ("INCOMPLETE", True, "INCOMPLETE")):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root, outcome=outcome, missing_cell=missing)
                self.assertEqual(tool.validate_private(root, scope_hash(root))["result"], expected)

    def test_missing_extra_tampered_and_recomputed_inconsistent_are_invalid(self) -> None:
        for mutation in ("missing", "extra", "tampered", "recomputed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root)
                if mutation == "missing": (root / "payload/resource.json").unlink()
                elif mutation == "extra": (root / "unindexed.txt").write_text("extra")
                elif mutation == "tampered": (root / "raw/observed.spool").write_bytes(b"tampered")
                else:
                    oracle = json.loads((root / "payload/oracle.json").read_text())
                    oracle["cases"][0]["observed_spool"]["sha256"] = "f" * 64
                    write_json(root / "payload/oracle.json", oracle); reseal(root)
                with self.assertRaises(rules.ContractError): tool.validate_private(root, scope_hash(root))

    def test_missing_raw_retention_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root, raw_retained=False)
            terminal = json.loads((root / "terminal.json").read_text())
            terminal["state"] = state("INCONCLUSIVE", False); terminal["aggregate_pass"] = False
            write_json(root / "terminal.json", terminal); reseal(root)
            self.assertEqual(tool.validate_private(root, scope_hash(root))["result"], "INCOMPLETE")

    def test_hash_equality_and_forged_pass_cannot_replace_typed_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root, outcome="FAIL")
            oracle = json.loads((root / "payload/oracle.json").read_text())
            oracle["cases"][0]["result"] = "PASS"
            write_json(root / "payload/oracle.json", oracle); reseal(root)
            with self.assertRaisesRegex(rules.ContractError, "contradicts complete comparison"):
                tool.validate_private(root, scope_hash(root))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            for relative in ("raw/expected.spool", "raw/observed.spool"):
                (root / relative).write_bytes(b"same invalid non-spool bytes")
                (root / relative).chmod(0o600)
            baseline_manifest = json.loads((root / "payload/baseline.json").read_text())
            baseline_manifest["cases"][0].update({
                "spool_sha256": artifact(root, "raw/expected.spool")["sha256"],
                "spool_bytes": artifact(root, "raw/expected.spool")["bytes"]})
            write_json(root / "payload/baseline.json", baseline_manifest)
            oracle = json.loads((root / "payload/oracle.json").read_text())
            oracle["cases"][0]["baseline_spool"] = artifact(root, "raw/expected.spool")
            oracle["cases"][0]["observed_spool"] = artifact(root, "raw/observed.spool")
            write_json(root / "payload/oracle.json", oracle)
            request = json.loads((root / "payload/request.json").read_text())
            request["observed_spool"] = artifact(root, "raw/observed.spool")
            write_json(root / "payload/request.json", request)
            cells = json.loads((root / "payload/site-cells.json").read_text())
            for cell in cells["cells"]:
                cell["evidence_sha256"] = artifact(root, "raw/expected.spool")["sha256"]
            write_json(root / "payload/site-cells.json", cells); reseal(root)
            with self.assertRaisesRegex(rules.ContractError, "baseline"):
                tool.validate_private(root, scope_hash(root))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root, malformed_spools=True)
            with self.assertRaisesRegex(rules.ContractError, "invalid typed oracle evidence"):
                tool.validate_private(root, scope_hash(root))

    def test_provider_or_static_control_cannot_claim_verified_baseline(self) -> None:
        for field, value in (("provider_identity", "ROUTING_OFF_CONTROL"),
                             ("provider_absence_verified", False),
                             ("static_data", False), ("concurrent_writes", True)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root)
                manifest = json.loads((root / "payload/baseline.json").read_text())
                manifest[field] = value
                write_json(root / "payload/baseline.json", manifest); reseal(root)
                with self.assertRaisesRegex(rules.ContractError, "invalid baseline manifest"):
                    tool.validate_private(root, scope_hash(root))

    def test_observed_spool_cannot_alias_scope_approved_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            baseline_artifact = artifact(root, "raw/expected.spool")
            oracle = json.loads((root / "payload/oracle.json").read_text())
            oracle["cases"][0]["observed_spool"] = baseline_artifact
            write_json(root / "payload/oracle.json", oracle)
            request = json.loads((root / "payload/request.json").read_text())
            request["observed_spool"] = baseline_artifact
            write_json(root / "payload/request.json", request); reseal(root)
            with self.assertRaisesRegex(rules.ContractError, "cannot alias the baseline"):
                tool.validate_private(root, scope_hash(root))

    def test_scope_approved_scratch_root_must_itself_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            scratch = root / "scratch"; scratch.chmod(0o755)
            with self.assertRaisesRegex(rules.ContractError, "scratch root must be an owned 0700"):
                tool.validate_private(root, scope_hash(root))

    def test_external_scope_anchor_rejects_recomputed_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root); trusted = scope_hash(root)
            definition = json.loads((root / "inputs/workload_definition.json").read_text())
            definition["revision"] = 2
            write_json(root / "inputs/workload_definition.json", definition)
            scope = json.loads((root / "scope.json").read_text())
            scope["scope_inputs"]["workload_definition"] = artifact(
                root, "inputs/workload_definition.json")
            write_json(root / "scope.json", scope)
            with self.assertRaisesRegex(rules.ContractError, "not trusted"):
                tool.validate_private(root, trusted)

    def test_frozen_measurement_plan_is_typed_and_bound_to_scope_inputs(self) -> None:
        for mutation in ("invalid-order", "wrong-workload-digest"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root)
                plan = json.loads((root / "inputs/measurement_plan.json").read_text())
                if mutation == "invalid-order":
                    plan["measurement_blocks"][0]["order"].reverse()
                else:
                    plan["closure_inputs"]["workload_definition_sha256"] = "f" * 64
                write_json(root / "inputs/measurement_plan.json", plan)
                scope = json.loads((root / "scope.json").read_text())
                scope["scope_inputs"]["measurement_plan"] = artifact(
                    root, "inputs/measurement_plan.json")
                write_json(root / "scope.json", scope)
                digest = rebind_scope(root)
                with self.assertRaisesRegex(rules.ContractError,
                                            "measurement plan|measurement_plan"):
                    tool.validate_private(root, digest)

    def test_cross_run_resources_and_retirement_are_rejected(self) -> None:
        for relative in ("payload/resource.json", "payload/retirement.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root)
                document = json.loads((root / relative).read_text())
                document["run_id"] = "foreign-run"
                write_json(root / relative, document); reseal(root)
                with self.assertRaisesRegex(rules.ContractError, "another run"):
                    tool.validate_private(root, scope_hash(root))

    def test_workload_provenance_must_match_scope_frozen_definition(self) -> None:
        for field, value in (("executed_sql_origin", "derived_reproduction"),
                             ("dataset_origin", "natural_customer")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root)
                workload = json.loads((root / "payload/workload.json").read_text())
                workload["entries"][0][field] = value
                write_json(root / "payload/workload.json", workload); reseal(root)
                with self.assertRaisesRegex(rules.ContractError, "frozen definition"):
                    tool.validate_private(root, scope_hash(root))

    def test_mandatory_site_not_run_forces_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            cells = json.loads((root / "payload/site-cells.json").read_text())
            cells["cells"][0]["result"] = "NOT_RUN"
            write_json(root / "payload/site-cells.json", cells)
            reseal(root)
            self.assertEqual(tool.validate_private(root, scope_hash(root))["result"], "INCOMPLETE")

    def test_closed_authority_identity_and_session_graph(self) -> None:
        for mutation in ("authority", "retirement", "duplicate-request"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root)
                if mutation == "authority":
                    workload = json.loads((root / "payload/workload.json").read_text())
                    workload["entries"][0]["executed_sql_authority"] = "PROHIBITED"
                    write_json(root / "payload/workload.json", workload)
                elif mutation == "retirement":
                    retirement = json.loads((root / "payload/retirement.json").read_text())
                    retirement["owned_sessions"] = []
                    write_json(root / "payload/retirement.json", retirement)
                else:
                    request = json.loads((root / "payload/request.json").read_text())
                    request["contract_id"] = "request-2"
                    write_json(root / "payload/request-2.json", request)
                    index = json.loads((root / "index.json").read_text())
                    index["payloads"].append(artifact(root, "payload/request-2.json"))
                    write_json(root / "index.json", index)
                reseal(root)
                with self.assertRaises(rules.ContractError):
                    tool.validate_private(root, scope_hash(root))

    def test_every_mandatory_workload_case_requires_request_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            (root / "raw/q2.sql").write_text("SELECT 2\n")
            write_spool(root / "raw/expected2.spool", [(2,)])
            write_spool(root / "raw/observed2.spool", [(2,)])
            definition = json.loads((root / "inputs/workload_definition.json").read_text())
            definition["natural_inventory"].append({"query_id": "q-2",
                "inventory_origin": "natural_internal", "inventory_source_ref": "inventory-1",
                "eligibility": "ELIGIBLE", "reason_family": "supported",
                "frequency_weight": 1, "baseline_time_weight": 1})
            definition["execution_corpus"].append({"query_id": "q-2",
                "natural_query_id": "q-2", "executed_sql_origin": "natural_internal",
                "executed_sql_ref": "sql-2",
                "sql_sha256": artifact(root, "raw/q2.sql")["sha256"],
                "dataset_origin": "synthetic_control", "dataset_ref": "dataset-1",
                "concurrent_writes": False})
            definition["baseline_authority"]["cases"].append({"query_id": "q-2",
                "spool_path": "raw/expected2.spool",
                "spool_sha256": artifact(root, "raw/expected2.spool")["sha256"],
                "spool_bytes": artifact(root, "raw/expected2.spool")["bytes"]})
            write_json(root / "inputs/workload_definition.json", definition)
            frozen_baseline = json.loads((root / "inputs/baseline_manifest.json").read_text())
            frozen_baseline["cases"].append({"query_id": "q-2",
                "spool_path": "raw/expected2.spool",
                "spool_sha256": artifact(root, "raw/expected2.spool")["sha256"],
                "spool_bytes": artifact(root, "raw/expected2.spool")["bytes"]})
            write_json(root / "inputs/baseline_manifest.json", frozen_baseline)
            plan = json.loads((root / "inputs/measurement_plan.json").read_text())
            plan["closure_inputs"]["workload_definition_sha256"] = artifact(
                root, "inputs/workload_definition.json")["sha256"]
            plan["closure_inputs"]["qualification_sha256"] = artifact(
                root, "inputs/workload_definition.json")["sha256"]
            plan["closure_inputs"]["baseline_manifest_sha256"] = artifact(
                root, "inputs/baseline_manifest.json")["sha256"]
            plan["required_scenarios"].append({"scenario_id": "scenario-2",
                "query_id": "q-2", "case_id": "q_2", "protocol": "extended",
                "cache_state": "warm_cached",
                "oracle_case_id": "q-2", "natural_query_id": "q-2",
                "expected_result_bytes_max": 1048576})
            write_json(root / "inputs/measurement_plan.json", plan)
            scope = json.loads((root / "scope.json").read_text())
            scope["scope_inputs"]["workload_definition"] = artifact(root, "inputs/workload_definition.json")
            scope["scope_inputs"]["measurement_plan"] = artifact(root, "inputs/measurement_plan.json")
            scope["scope_inputs"]["baseline_manifest"] = artifact(
                root, "inputs/baseline_manifest.json")
            write_json(root / "scope.json", scope); digest = scope_hash(root)
            workload = json.loads((root / "payload/workload.json").read_text())
            workload["inventory_sha256"] = artifact(
                root, "inputs/workload_definition.json")["sha256"]
            second = dict(workload["entries"][0]); second["query_id"] = "q-2"
            second["sql"] = artifact(root, "raw/q2.sql"); workload["entries"].append(second)
            write_json(root / "payload/workload.json", workload)
            oracle = json.loads((root / "payload/oracle.json").read_text())
            oracle["cases"].append({"query_id": "q-2",
                "baseline_spool": artifact(root, "raw/expected2.spool"),
                "observed_spool": artifact(root, "raw/observed2.spool"),
                "result": "PASS", "raw_retained": True, "sqlstate": None})
            write_json(root / "payload/oracle.json", oracle)
            baseline_manifest = json.loads((root / "payload/baseline.json").read_text())
            baseline_manifest["cases"].append({"query_id": "q-2",
                "spool_path": "raw/expected2.spool",
                "spool_sha256": artifact(root, "raw/expected2.spool")["sha256"],
                "spool_bytes": artifact(root, "raw/expected2.spool")["bytes"]})
            write_json(root / "payload/baseline.json", baseline_manifest)
            for path in (root / "payload").glob("*.json"):
                document = json.loads(path.read_text())
                if "scope_sha256" in document:
                    document["scope_sha256"] = digest; write_json(path, document)
            preflight = json.loads((root / "payload/preflight.json").read_text())
            approval = json.loads((root / "payload/approval.json").read_text())
            approval["preflight_sha256"] = rules.canonical_sha256(preflight)
            write_json(root / "payload/approval.json", approval)
            index = json.loads((root / "index.json").read_text()); index["scope_sha256"] = digest
            for relative in ("raw/q2.sql", "raw/expected2.spool", "raw/observed2.spool"):
                index["payloads"].append(artifact(root, relative))
            write_json(root / "index.json", index); reseal(root)
            self.assertEqual(tool.validate_private(root, digest)["result"], "INCOMPLETE")

    def test_missing_request_is_incomplete_not_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            (root / "payload/request.json").unlink()
            index = json.loads((root / "index.json").read_text())
            index["payloads"] = [item for item in index["payloads"]
                                 if item["path"] != "payload/request.json"]
            write_json(root / "index.json", index); reseal(root)
            self.assertEqual(tool.validate_private(root, scope_hash(root))["result"], "INCOMPLETE")

    def test_unknown_site_cell_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            cells = json.loads((root / "payload/site-cells.json").read_text())
            cells["cells"].append({"cell_id": "CANARY_PRIVATE_SECRET_92847", "result": "PASS",
                                   "evidence_sha256": artifact(root, "raw/expected.spool")["sha256"]})
            write_json(root / "payload/site-cells.json", cells); reseal(root)
            with self.assertRaisesRegex(rules.ContractError, "unknown site cell"):
                tool.validate_private(root, scope_hash(root))


class ExportTests(unittest.TestCase):
    def _export(self, base: Path) -> tuple[Path, Path]:
        private = base / "private"; private.mkdir(); build_private(private)
        validated = tool.validate_private(private, scope_hash(private)); receipt = base / "private-validation.json"
        tool.write_validation_receipt(validated, receipt, "private-validation")
        shared = base / "shared"; tool.export_shared(private, receipt, shared, ("CANARY-DO-NOT-LEAK",))
        return shared, receipt

    def test_export_and_shared_validation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            shared, receipt = self._export(Path(name))
            self.assertEqual(tool.validate_shared(shared, receipt)["result"], "PASS")
            output = "\n".join(path.read_text() for path in shared.iterdir())
            self.assertNotIn("SELECT 1", output); self.assertIn("NOT_CLAIMED", output)

    def test_shared_extra_tamper_and_existing_destination_fail(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); shared, receipt = self._export(base)
            (shared / "extra.json").write_text("{}")
            with self.assertRaises(rules.ContractError): tool.validate_shared(shared, receipt)
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); private = base / "private"; private.mkdir(); build_private(private)
            validated = tool.validate_private(private, scope_hash(private)); receipt = base / "receipt.json"
            tool.write_validation_receipt(validated, receipt, "receipt")
            output = base / "shared"; output.mkdir()
            with self.assertRaises(rules.ContractError): tool.export_shared(private, receipt, output, ())

    def test_recomputed_shared_result_is_rejected_by_private_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            shared, receipt = self._export(Path(name))
            results = json.loads((shared / "results.json").read_text())
            results["results"][0]["result"] = "FAIL"
            write_json(shared / "results.json", results); reseal_shared(shared)
            with self.assertRaisesRegex(rules.ContractError, "not bound"):
                tool.validate_shared(shared, receipt)

    def test_wrong_private_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); shared, _ = self._export(base)
            other = base / "other"; other.mkdir(); build_private(other, outcome="FAIL")
            validated = tool.validate_private(other, scope_hash(other))
            receipt = base / "other-receipt.json"
            tool.write_validation_receipt(validated, receipt, "other-receipt")
            with self.assertRaisesRegex(rules.ContractError, "linkage"):
                tool.validate_shared(shared, receipt)

    def test_incomplete_private_export_round_trips_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); private = base / "private"; private.mkdir()
            build_private(private, missing_cell=True)
            validated = tool.validate_private(private, scope_hash(private))
            self.assertEqual(validated["result"], "INCOMPLETE")
            receipt = base / "receipt.json"; tool.write_validation_receipt(validated, receipt, "receipt")
            shared = base / "shared"; tool.export_shared(private, receipt, shared, ())
            self.assertEqual(tool.validate_shared(shared, receipt)["result"], "INCOMPLETE")

    def test_shared_fail_preserves_and_binds_dirty_partial_axes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); private = base / "private"; private.mkdir()
            build_private(private, outcome="FAIL")
            terminal = json.loads((private / "terminal.json").read_text())
            terminal["state"]["exit_state"] = "DIRTY"
            terminal["state"]["evidence_completeness"] = "PARTIAL"
            terminal["aggregate_pass"] = False
            write_json(private / "terminal.json", terminal); reseal(private)
            validated = tool.validate_private(private, scope_hash(private)); receipt = base / "receipt.json"
            tool.write_validation_receipt(validated, receipt, "receipt")
            shared = base / "shared"; tool.export_shared(private, receipt, shared, ())
            shared_terminal = json.loads((shared / "terminal.json").read_text())
            self.assertEqual(shared_terminal["state"]["exit_state"], "DIRTY")
            self.assertEqual(shared_terminal["state"]["evidence_completeness"], "PARTIAL")
            self.assertEqual(tool.validate_shared(shared, receipt)["result"], "FAIL")
            shared_terminal["state"]["exit_state"] = "CLEAN"
            write_json(shared / "terminal.json", shared_terminal)
            with self.assertRaisesRegex(rules.ContractError, "not bound"):
                tool.validate_shared(shared, receipt)

    def test_shared_types_versions_and_secret_bearing_seal_are_rejected(self) -> None:
        for mutation in ("null-results", "boolean-version", "secret-index", "terminal-substitution"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                shared, receipt = self._export(Path(name))
                if mutation == "null-results":
                    document = json.loads((shared / "results.json").read_text()); document["results"] = None
                    write_json(shared / "results.json", document)
                elif mutation == "boolean-version":
                    document = json.loads((shared / "manifest.json").read_text()); document["schema_version"] = True
                    write_json(shared / "manifest.json", document)
                else:
                    if mutation == "secret-index":
                        document = json.loads((shared / "index.json").read_text())
                        document["contract_id"] = "password=CANARY"
                        write_json(shared / "index.json", document)
                    else:
                        (shared / "terminal.json").write_bytes((shared / "index.json").read_bytes())
                if mutation != "terminal-substitution":
                    reseal_shared(shared)
                with self.assertRaises(rules.ContractError):
                    tool.validate_shared(shared, receipt)

    def test_failed_export_removes_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); private = base / "private"; private.mkdir(); build_private(private)
            validated = tool.validate_private(private, scope_hash(private)); receipt = base / "receipt.json"
            tool.write_validation_receipt(validated, receipt, "receipt")
            output = base / "shared"; original = tool._write_exclusive_at; calls = 0

            def fail_second(directory_fd: int, path: str, payload: bytes, mode: int = 0o600) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected write failure")
                original(directory_fd, path, payload, mode)

            with mock.patch.object(tool, "_write_exclusive_at", side_effect=fail_second):
                with self.assertRaises(OSError):
                    tool.export_shared(private, receipt, output, ())
            self.assertFalse(output.exists())

    def test_cleanup_never_follows_replaced_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); private = base / "private"; private.mkdir(); build_private(private)
            validated = tool.validate_private(private, scope_hash(private)); receipt = base / "receipt.json"
            tool.write_validation_receipt(validated, receipt, "receipt")
            output = base / "shared"; moved = base / "owned-moved"; outside = base / "outside"
            outside.mkdir(); sentinel = outside / "sentinel"; sentinel.write_text("preserve")
            original = tool._write_exclusive_at; calls = 0

            def replace_path(directory_fd: int, path: str, payload: bytes, mode: int = 0o600) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    output.rename(moved); output.symlink_to(outside, target_is_directory=True)
                    raise OSError("injected path replacement")
                original(directory_fd, path, payload, mode)

            with mock.patch.object(tool, "_write_exclusive_at", side_effect=replace_path):
                with self.assertRaises(OSError):
                    tool.export_shared(private, receipt, output, ())
            self.assertEqual(sentinel.read_text(), "preserve")
            self.assertTrue(output.is_symlink()); self.assertEqual(list(moved.iterdir()), [])

    def test_partial_write_failure_removes_unregistered_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); private = base / "private"; private.mkdir(); build_private(private)
            validated = tool.validate_private(private, scope_hash(private)); receipt = base / "receipt.json"
            tool.write_validation_receipt(validated, receipt, "receipt")
            output = base / "shared"; original_write = tool.os.write; calls = 0

            def partial_then_fail(descriptor: int, payload: object) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return original_write(descriptor, bytes(payload)[:8])
                raise OSError("injected partial-write failure")

            with mock.patch.object(tool.os, "write", side_effect=partial_then_fail):
                with self.assertRaises(OSError):
                    tool.export_shared(private, receipt, output, ())
            self.assertFalse(output.exists())

    def test_partial_validation_receipt_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "private"; root.mkdir(); build_private(root)
            validated = tool.validate_private(root, scope_hash(root)); receipt = Path(name) / "receipt.json"
            original_write = tool.os.write; calls = 0

            def partial_then_fail(descriptor: int, payload: object) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return original_write(descriptor, bytes(payload)[:8])
                raise OSError("injected receipt failure")

            with mock.patch.object(tool.os, "write", side_effect=partial_then_fail):
                with self.assertRaises(OSError):
                    tool.write_validation_receipt(validated, receipt, "receipt")
            self.assertFalse(receipt.exists())

    def test_receipt_write_failure_never_removes_replacement_path(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "private"; root.mkdir(); build_private(root)
            validated = tool.validate_private(root, scope_hash(root)); receipt = Path(name) / "receipt.json"
            original_write = tool.os.write; calls = 0

            def install_sentinel_then_fail(descriptor: int, payload: object) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    written = original_write(descriptor, bytes(payload)[:8])
                    receipt.write_text("pre-existing-sentinel")
                    return written
                raise OSError("injected receipt failure")

            with mock.patch.object(tool.os, "write", side_effect=install_sentinel_then_fail):
                with self.assertRaises(OSError):
                    tool.write_validation_receipt(validated, receipt, "receipt")
            self.assertEqual(receipt.read_text(), "pre-existing-sentinel")
            self.assertEqual(list(Path(name).glob(".receipt.json.*")), [])

    def test_receipt_rejects_replaced_temporary_source(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "private"; root.mkdir(); build_private(root)
            validated = tool.validate_private(root, scope_hash(root)); receipt = Path(name) / "receipt.json"
            untrusted = Path(name) / "untrusted.json"; untrusted.write_text("untrusted")
            original_link = tool.os.link

            def replace_source(source: object, destination: object, **kwargs: object) -> None:
                source_path = Path(source)
                source_path.unlink(); source_path.symlink_to(untrusted)
                original_link(source, destination, **kwargs)

            with mock.patch.object(tool.os, "link", side_effect=replace_source):
                with self.assertRaisesRegex(rules.ContractError, "identity mismatch"):
                    tool.write_validation_receipt(validated, receipt, "receipt")
            self.assertEqual(untrusted.read_text(), "untrusted")
            self.assertTrue(receipt.is_symlink())


class CliTests(unittest.TestCase):
    def _validate(self, root: Path, receipt: Path) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = tool.main(["validate", "--profile", "private_full", "--bundle", str(root),
                              "--trusted-scope-sha256", scope_hash(root), "--receipt", str(receipt),
                              "--receipt-id", "cli-receipt"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_exit_codes_for_pass_fail_incomplete_and_invalid(self) -> None:
        for outcome, missing, expected in (("PASS", False, 0), ("FAIL", False, 1),
                                           ("INCOMPLETE", True, 3)):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as name:
                root = Path(name) / "bundle"; root.mkdir()
                build_private(root, outcome=outcome, missing_cell=missing)
                code, stdout, stderr = self._validate(root, Path(name) / "receipt.json")
                self.assertEqual(code, expected); self.assertIn(outcome, stdout); self.assertEqual(stderr, "")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "password=CANARY"; root.mkdir(); build_private(root)
            (root / "unindexed-CANARY").write_text("secret")
            code, stdout, stderr = self._validate(root, Path(name) / "receipt.json")
            self.assertEqual(code, 2); self.assertEqual(stdout, "")
            self.assertIn("evidence_validation_failed", stderr)
            self.assertNotIn("CANARY", stderr); self.assertNotIn(str(root), stderr)


class SecretAndOfflineTests(unittest.TestCase):
    def test_secret_canaries_in_content_and_path_are_rejected(self) -> None:
        for canary in ("SQL password=CANARY", "conninfo postgresql://user:CANARY@host/db",
                       "NOTICE token=CANARY", "DETAIL password=CANARY", "HINT token=CANARY",
                       "CONTEXT password=CANARY", "traceback token=CANARY",
                       "stderr password=CANARY", "-----BEGIN PRIVATE KEY-----"):
            with self.subTest(canary=canary), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root)
                (root / "raw/debug.log").write_text(canary); reseal(root)
                with self.assertRaisesRegex(rules.ContractError, "credential-like"):
                    tool.validate_private(root, scope_hash(root))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_private(root)
            (root / "raw/debug.log").rename(root / "raw/password=CANARY.log")
            index = json.loads((root / "index.json").read_text())
            for entry in index["payloads"]:
                if entry["path"] == "raw/debug.log": entry["path"] = "raw/password=CANARY.log"
            write_json(root / "index.json", index)
            with self.assertRaisesRegex(rules.ContractError, "credential-like"):
                tool.validate_private(root, scope_hash(root))

    def test_archive_container_in_raw_evidence_is_rejected(self) -> None:
        for prefixed in (False, True):
            with self.subTest(prefixed=prefixed), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_private(root)
                archive_path = root / "raw/debug.zip"
                with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("debug.log", "password=CANARY_COMPRESSED_SECRET")
                if prefixed:
                    archive_path.write_bytes(b"PREFIX" + archive_path.read_bytes())
                index = json.loads((root / "index.json").read_text())
                index["payloads"].append(artifact(root, "raw/debug.zip"))
                write_json(root / "index.json", index); reseal(root)
                with self.assertRaisesRegex(rules.ContractError, "archive containers"):
                    tool.validate_private(root, scope_hash(root))

    def test_private_identifier_is_pseudonymised_in_default_export(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name); private = base / "private"; private.mkdir()
            build_private(private, query_id="CANARY_PRIVATE_SECRET_92847")
            validated = tool.validate_private(private, scope_hash(private)); receipt = base / "receipt.json"
            tool.write_validation_receipt(validated, receipt, "receipt")
            shared = base / "shared"; tool.export_shared(private, receipt, shared, ())
            output = "\n".join(path.read_text() for path in shared.iterdir())
            self.assertNotIn("CANARY_PRIVATE_SECRET_92847", output)

    def test_source_has_no_network_database_or_shell_capability(self) -> None:
        source = (ROOT / "tools/phase5/evidence_tool.py").read_text()
        for forbidden in ("import socket", "import subprocess", "import urllib", "import requests",
                          "psycopg", "libpq", "os.system", "subprocess."):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
