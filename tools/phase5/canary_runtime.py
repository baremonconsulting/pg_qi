#!/usr/bin/env python3
"""Fail-closed validation of one operator-executed PGQI-5 internal canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .contract_rules import (CANARY_CHECKS, CANARY_CONTROLS, CANARY_LIMITATIONS,
        ContractError, canonical_bytes, canonical_sha256, resolve_bundle_path,
        strict_load_path, validate_contract, verify_artifact)
    from .measurement_runtime import MeasurementError, validate_level_authorization
    from .evidence_tool import EvidenceError, validate_private
    from .corpus_oracle import OracleError, compare_spools
except ImportError:
    from contract_rules import (CANARY_CHECKS, CANARY_CONTROLS, CANARY_LIMITATIONS,
        ContractError, canonical_bytes, canonical_sha256, resolve_bundle_path,
        strict_load_path, validate_contract, verify_artifact)
    from measurement_runtime import MeasurementError, validate_level_authorization
    from evidence_tool import EvidenceError, validate_private
    from corpus_oracle import OracleError, compare_spools


class CanaryError(RuntimeError):
    """A fixed-message canary refusal."""


UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?:\d{3}|\d{6}))?Z$")
CONTROL_SQL = (b"SHOW duckdb.force_execution\n",
               b"SELECT unsupported_control()\n", b"SELECT $1::integer\n")
CONTROL_SQL_SHA256 = [hashlib.sha256(item).hexdigest() for item in CONTROL_SQL]


def _time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or UTC.fullmatch(value) is None:
        raise CanaryError(f"invalid_{name}_time")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise CanaryError(f"invalid_{name}_time") from exc


def _root(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    if (root.is_symlink() or resolved != root.absolute() or not root.is_dir() or
            root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700):
        raise CanaryError("invalid_canary_root")
    return resolved


def _load_contract(root: Path, relative: str, kind: str) -> dict[str, Any]:
    try:
        value = strict_load_path(Path(relative), root)
        validate_contract(value)
    except (ContractError, OSError) as exc:
        raise CanaryError(f"invalid_{kind}") from exc
    if value.get("contract_type") != kind:
        raise CanaryError(f"invalid_{kind}")
    return value


def _artifact_contract(root: Path, artifact: dict[str, Any], kind: str) -> dict[str, Any]:
    try:
        verify_artifact(root, artifact)
        return _load_contract(root, artifact["path"], kind)
    except (ContractError, KeyError, TypeError) as exc:
        raise CanaryError(f"invalid_{kind}_artifact") from exc


def _raw(root: Path, artifact: dict[str, Any], fields: set[str], kind: str) -> dict[str, Any]:
    try:
        verify_artifact(root, artifact)
        path = resolve_bundle_path(root, Path(artifact["path"]))
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077 or
                    info.st_size > 1024 * 1024):
                raise CanaryError("unsafe_canary_proof")
            payload = os.read(descriptor, info.st_size + 1)
        finally:
            os.close(descriptor)
        if (len(payload) != artifact["bytes"] or
                hashlib.sha256(payload).hexdigest() != artifact["sha256"]):
            raise CanaryError("canary_proof_changed")
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=lambda pairs: _pairs(pairs),
                           parse_constant=lambda _item: (_ for _ in ()).throw(
                               CanaryError("invalid_canary_number")))
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError,
            KeyError, TypeError) as exc:
        raise CanaryError(f"invalid_{kind}") from exc
    if not isinstance(value, dict) or set(value) != fields or value.get("document_type") != kind:
        raise CanaryError(f"invalid_{kind}")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanaryError("duplicate_canary_key")
        result[key] = value
    return result


def _artifact_identity(root: Path, artifact: dict[str, Any]) -> tuple[int, int]:
    """Return a verified, single-link regular file identity."""
    verify_artifact(root, artifact)
    path = resolve_bundle_path(root, Path(artifact["path"]))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise CanaryError("unsafe_canary_artifact")
        return info.st_dev, info.st_ino
    finally:
        os.close(descriptor)


def validate_canary_bundle(root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Recompute a canary result. This function never grants CANARY_ACCEPTED."""
    _root(root)
    plan = _load_contract(root, "canary-plan.json", "canary_plan")
    run = _load_contract(root, "canary-run.json", "canary_run")
    retirement = _load_contract(root, "canary-retirement.json", "canary_retirement")
    scope = plan["scope_sha256"]
    plan_sha = canonical_sha256(plan)
    if (run["scope_sha256"] != scope or retirement["scope_sha256"] != scope or
            run["canary_id"] != plan["canary_id"] or
            retirement["canary_id"] != plan["canary_id"] or
            run["plan_sha256"] != plan_sha or retirement["plan_sha256"] != plan_sha or
            retirement["run_sha256"] != canonical_sha256(run)):
        raise CanaryError("canary_identity_graph_mismatch")

    measurement_plan = _artifact_contract(root, plan["measurement_plan"], "measurement_plan")
    try:
        private_root = resolve_bundle_path(root, Path(plan["private_bundle_path"]))
        private_validation = validate_private(private_root, scope)
    except (ContractError, EvidenceError, OSError) as exc:
        raise CanaryError("canary_private_validation_failed") from exc
    if private_validation["result"] != "PASS":
        raise CanaryError("canary_private_validation_failed")
    private_scope = private_validation["scope"]
    if private_scope["scope_inputs"]["measurement_plan"]["sha256"] != \
            plan["measurement_plan"]["sha256"]:
        raise CanaryError("canary_measurement_plan_outside_private_scope")
    private_index = _load_contract(private_root, "index.json", "index")
    private_requests = []
    private_snapshots = []
    for item in private_index["payloads"]:
        try:
            candidate = strict_load_path(Path(item["path"]), private_root)
        except ContractError:
            continue
        if (candidate.get("contract_type") == "request" and
                candidate.get("query_id") == plan["positive_query_id"]):
            private_requests.append(candidate)
        if candidate.get("contract_type") == "snapshot":
            private_snapshots.append(candidate)
    qualification = _raw(private_root, private_scope["scope_inputs"]["workload_definition"], {
        "schema_version", "document_type", "qualification_id", "authorities",
        "storage_policy", "baseline_authority", "oracle_limits", "natural_inventory",
        "execution_corpus", "synthetic_controls"}, "corpus_qualification")
    corpus = {item["query_id"]: item for item in qualification["execution_corpus"]}
    baselines = {item["query_id"]: item for item in
                 qualification["baseline_authority"]["cases"]}
    authorization = _raw(root, plan["level_authorization"], {
        "schema_version", "document_type", "scope_sha256", "plan_sha256",
        "requested_level", "approval_reference", "operation_go",
        "operation_preflight", "operation_rehearsal_mandate", "operation_recovery_r1",
        "operation_recovery_r2", "operation_operator_walkthrough",
        "operation_recovery_incident", "operation_recovery_validation", "previous_level"},
        "measurement_level_authorization")
    started = _time(run["started_at"], "canary_started")
    try:
        level = validate_level_authorization(authorization, measurement_plan,
            canonical_sha256(measurement_plan), scope, root, started)
    except (MeasurementError, ContractError, OSError) as exc:
        raise CanaryError("canary_operation_go_invalid") from exc
    operation_go = _artifact_contract(root, authorization["operation_go"], "approval_receipt")
    mandate = _artifact_contract(root, authorization["operation_rehearsal_mandate"],
                                 "rehearsal_mandate")
    recovery_r2 = _artifact_contract(root, authorization["operation_recovery_r2"], "recovery_r2")
    if (level["level"] != 1 or level["sessions"] != 1 or
            canonical_sha256(operation_go) != plan["operation_go_sha256"] or
            run["operation_go_sha256"] != plan["operation_go_sha256"]):
        raise CanaryError("canary_operation_go_or_level_mismatch")

    finished = _time(run["finished_at"], "canary_finished")
    go_end = _time(operation_go["valid_through"], "operation_go_end")
    checked = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        raise CanaryError("validation_clock_is_not_utc_aware")
    checked_utc = checked.astimezone(timezone.utc)
    if not (started < finished <= min(go_end, checked_utc)):
        raise CanaryError("canary_timeline_invalid")

    try:
        verify_artifact(root, plan["positive_sql"])
        verify_artifact(root, plan["expected_spool"])
        verify_artifact(root, run["positive_spool"])
        for item in plan["negative_control_sql"]:
            verify_artifact(root, item)
    except ContractError as exc:
        raise CanaryError("canary_positive_artifact_invalid") from exc
    scenarios = {item["query_id"]: item for item in measurement_plan["required_scenarios"]}
    corpus_item = corpus.get(plan["positive_query_id"])
    baseline_item = baselines.get(plan["positive_query_id"])
    if (plan["positive_query_id"] not in scenarios or
            corpus_item is None or baseline_item is None or
            corpus_item["sql_sha256"] != plan["positive_literal_sha256"] or
            baseline_item["spool_sha256"] != plan["expected_spool"]["sha256"] or
            baseline_item["spool_bytes"] != plan["expected_spool"]["bytes"] or
            plan["positive_sql"]["sha256"] != plan["positive_literal_sha256"] or
            run["positive_spool"]["path"] == plan["expected_spool"]["path"] or
            run["positive_spool"]["bytes"] > plan["maximum_result_bytes"]):
        raise CanaryError("canary_positive_literal_or_limit_mismatch")
    if (len(private_requests) != 1 or len(private_snapshots) != 1 or
            "observed_spool" not in private_requests[0]):
        raise CanaryError("canary_private_request_missing")
    private_observed = private_requests[0]["observed_spool"]
    if (private_observed["sha256"] != run["positive_spool"]["sha256"] or
            private_observed["bytes"] != run["positive_spool"]["bytes"] or
            resolve_bundle_path(private_root, Path(private_observed["path"])).stat().st_ino !=
            resolve_bundle_path(root, Path(run["positive_spool"]["path"])).stat().st_ino):
        raise CanaryError("canary_observed_spool_not_private_validated")
    if [item["sha256"] for item in plan["negative_control_sql"]] != CONTROL_SQL_SHA256:
        raise CanaryError("canary_control_sql_not_approved")
    request = _artifact_contract(root, run["positive_request"], "request")
    snapshot = _artifact_contract(root, run["positive_snapshot"], "snapshot")
    private_request = private_requests[0]
    private_snapshot = private_snapshots[0]
    execution = _raw(root, run["positive_execution_proof"], {"document_type", "query_id",
        "session_id", "executed_sql_sha256", "observed_spool", "started_at", "finished_at", "result"},
        "canary_positive_execution")
    interval = request.get("request_interval")
    if (request["scope_sha256"] != scope or request["run_id"] != plan["canary_id"] or
            private_request["run_id"] != plan["canary_id"] or
            private_request["session_id"] != run["session_id"] or
            private_request["request_id"] != request["request_id"] or
            private_request["planning_evidence"] != "FRESH" or
            private_request["snapshot_relation"] != "CURRENT" or
            private_request.get("request_interval") != request.get("request_interval") or
            private_request.get("planning_request_id") != request.get("planning_request_id") or
            private_request.get("observed_spool", {}).get("sha256") !=
            run["positive_spool"]["sha256"] or
            any(name in request for name in ("prepared_object_id", "prepared_generation",
                                              "sqlstate")) or
            request["query_id"] != plan["positive_query_id"] or
            request["session_id"] != run["session_id"] or
            request["execution_status"] != "SUCCEEDED" or
            request["planning_evidence"] != "FRESH" or
            request["snapshot_relation"] != "CURRENT" or
            request.get("observed_spool") != run["positive_spool"] or
            snapshot["scope_sha256"] != scope or snapshot["session_id"] != run["session_id"] or
            private_snapshot["session_id"] != run["session_id"] or
            private_snapshot["snapshot_id"] != snapshot["snapshot_id"] or
            private_snapshot["planning_request_id"] != request["request_id"] or
            private_snapshot["prepared_object_id"] != plan["positive_query_id"] or
            private_snapshot["prepared_generation"] != snapshot["prepared_generation"] or
            private_snapshot["decision_id"] != snapshot["decision_id"] or
            private_snapshot["tuple"] != plan["expected_snapshot"] or
            snapshot["snapshot_id"] != request.get("snapshot_id") or
            snapshot["planning_request_id"] != request.get("planning_request_id") or
            snapshot["planning_request_id"] != request["request_id"] or
            snapshot["prepared_object_id"] != plan["positive_query_id"] or
            snapshot["tuple"] != plan["expected_snapshot"]):
        raise CanaryError("canary_positive_request_or_snapshot_mismatch")
    if not isinstance(interval, dict) or interval.get("state") != "COMPLETE" or \
            interval.get("censored") is not False:
        raise CanaryError("canary_positive_interval_invalid")
    positive_started = _time(execution["started_at"], "positive_started")
    positive_finished = _time(execution["finished_at"], "positive_finished")
    if (execution["query_id"] != plan["positive_query_id"] or
            execution["session_id"] != run["session_id"] or
            execution["executed_sql_sha256"] != plan["positive_literal_sha256"] or
            execution["observed_spool"] != run["positive_spool"] or
            execution["result"] != "PASS" or not started < positive_started <
            positive_finished < finished):
        raise CanaryError("canary_positive_execution_invalid")

    oracle = _raw(root, run["oracle_proof"], {"document_type", "query_id",
        "spool_sha256", "result"}, "canary_oracle")
    try:
        scratch = Path(qualification["storage_policy"]["scratch_root"]).resolve(strict=True)
        scratch.relative_to(private_root)
        comparison = compare_spools(
            resolve_bundle_path(root, Path(plan["expected_spool"]["path"])),
            resolve_bundle_path(root, Path(run["positive_spool"]["path"])),
            scratch,
            **qualification["oracle_limits"])
    except (ContractError, OracleError, OSError, ValueError) as exc:
        raise CanaryError("canary_oracle_comparison_invalid") from exc
    if (comparison["result"] != "PASS" or
            oracle["query_id"] != plan["positive_query_id"] or
            oracle["spool_sha256"] != run["positive_spool"]["sha256"] or
            oracle["result"] != "PASS"):
        raise CanaryError("canary_oracle_did_not_pass")

    controls = [_raw(root, item, {"document_type", "control_id", "session_id",
        "execution_status", "planning_evidence", "snapshot_relation", "observed_engine",
        "executed_sql_sha256", "mode", "force_execution", "performed_at", "result"},
        "canary_control") for item in run["control_proofs"]]
    if ([item["control_id"] for item in controls] != CANARY_CONTROLS or
            any(item["session_id"] != run["session_id"] or
                item["execution_status"] != "SUCCEEDED" or
                item["planning_evidence"] != "UNAVAILABLE" or
                item["snapshot_relation"] != "NO_SNAPSHOT" or
                item["observed_engine"] != "postgres" or item["mode"] != "lab_allowlist" or
                item["force_execution"] != "off" or item["result"] != "PASS" or
                item["executed_sql_sha256"] != plan["negative_control_sql"][index]["sha256"]
                for index, item in enumerate(controls)) or
            run["ambient_force_off_proof"] != run["control_proofs"][0]):
        raise CanaryError("canary_negative_controls_did_not_pass")
    control_times = [_time(item["performed_at"], "control") for item in controls]
    request_start_ns = interval.get("started_monotonic_ns")
    request_finish_ns = interval.get("finished_monotonic_ns")
    if (type(request_start_ns) is not int or type(request_finish_ns) is not int or
            request_start_ns >= request_finish_ns or
            not started < control_times[0] < positive_started < positive_finished <
            control_times[1] < control_times[2] < finished):
        raise CanaryError("canary_control_order_invalid")

    exit_proof = _raw(root, retirement["backend_exit_proof"], {"document_type",
        "cluster_id", "start_id", "pid", "backend_start", "session_id",
        "observer_authorized", "observation_complete", "backend_present",
        "observed_at", "result"}, "canary_backend_exit")
    reconnect = _raw(root, retirement["fresh_reconnect_proof"], {"document_type",
        "cluster_id", "start_id", "pid", "backend_start", "session_id",
        "connected_at", "mode", "force_execution", "observed_engine",
        "result"}, "canary_fresh_reconnect")
    exit_time = _time(exit_proof["observed_at"], "canary_exit")
    reconnect_time = _time(reconnect["connected_at"], "canary_reconnect")
    performed = _time(retirement["performed_at"], "canary_retirement")
    backend = run["backend_identity"]
    for proof in (exit_proof, reconnect):
        if (not all(isinstance(proof[name], str) and proof[name] for name in
                    ("cluster_id", "start_id", "backend_start", "session_id")) or
                type(proof["pid"]) is not int or proof["pid"] <= 0):
            raise CanaryError("canary_backend_identity_invalid")
    backend_start = _time(backend["backend_start"], "canary_backend_start")
    reconnect_backend_start = _time(reconnect["backend_start"], "reconnect_backend_start")
    reconnect_connected = _time(reconnect["connected_at"], "reconnect_connected")
    if (_artifact_identity(root, plan["expected_spool"]) ==
            _artifact_identity(root, run["positive_spool"])):
        raise CanaryError("canary_observed_spool_aliases_baseline")
    if (retirement["owned_session_id"] != run["session_id"] or
            backend["cluster_id"] != mandate["target_cluster_id"] or
            backend["start_id"] != recovery_r2["after_start_id"] or
            not _time(operation_go["valid_from"], "operation_go_start") <= backend_start <= started or
            exit_proof["session_id"] != run["session_id"] or
            any(exit_proof[name] != backend[name] for name in
                ("cluster_id", "start_id", "pid", "backend_start", "session_id")) or
            exit_proof["observer_authorized"] is not True or
            exit_proof["observation_complete"] is not True or
            exit_proof["backend_present"] is not False or exit_proof["result"] != "PASS" or
            reconnect["cluster_id"] != backend["cluster_id"] or
            reconnect["start_id"] != backend["start_id"] or
            reconnect["session_id"] == run["session_id"] or reconnect["pid"] == backend["pid"] or
            not exit_time <= reconnect_backend_start <= reconnect_connected or
            reconnect["mode"] != "off" or
            reconnect["force_execution"] != "off" or
            reconnect["observed_engine"] != "postgres" or reconnect["result"] != "PASS" or
            not (finished < exit_time <= performed < reconnect_time <= min(go_end, checked_utc)) or
            run["result"] != "PASS" or retirement["result"] != "PASS"):
        raise CanaryError("canary_retirement_did_not_pass")

    return {"schema_version": 1, "contract_type": "canary_validation",
        "contract_id": f"{plan['canary_id']}-validation", "scope_sha256": scope,
        "canary_id": plan["canary_id"], "plan_sha256": plan_sha,
        "run_sha256": canonical_sha256(run),
        "retirement_sha256": canonical_sha256(retirement),
        "checked_at": checked_utc.isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "result": "PASS", "decision": "ELIGIBLE_FOR_HUMAN_GRANT",
        "checks": CANARY_CHECKS, "limitations": sorted(CANARY_LIMITATIONS)}


def build_canary_record(validation: dict[str, Any]) -> dict[str, Any]:
    validate_contract(validation)
    if validation["result"] != "PASS":
        raise CanaryError("canary_validation_did_not_pass")
    return {"schema_version": 1, "contract_type": "canary",
        "contract_id": f"{validation['canary_id']}-canary",
        "scope_sha256": validation["scope_sha256"],
        "canary_id": validation["canary_id"], "plan_sha256": validation["plan_sha256"],
        "run_sha256": validation["run_sha256"],
        "retirement_sha256": validation["retirement_sha256"],
        "validation_sha256": canonical_sha256(validation), "result": "PASS",
        "eligibility": "ELIGIBLE_FOR_HUMAN_GRANT",
        "limitations": validation["limitations"],
        "state": {"lifecycle": "VALIDATED", "technical_result": "PASS",
            "evidence_completeness": "COMPLETE", "exit_state": "CLEAN",
            "value_outcome": "NOT_EVALUATED", "project_decision": "UNDECIDED"}}


def _write_outcome(path: Path, value: dict[str, Any]) -> None:
    payload = canonical_bytes(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise CanaryError("canary_outcome_write_failed")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _consume_attempt(ledger: Path, plan: dict[str, Any]) -> Path:
    resolved = _root(ledger)
    expected = plan["attempt_ledger"]
    info = resolved.stat()
    if (str(resolved) != expected["path"] or info.st_dev != expected["device"] or
            info.st_ino != expected["inode"]):
        raise CanaryError("canary_attempt_ledger_identity_mismatch")
    try:
        verify_artifact(resolved, expected["identity"])
    except ContractError as exc:
        raise CanaryError("canary_attempt_ledger_identity_mismatch") from exc
    identity = hashlib.sha256(canonical_bytes({"scope_sha256": plan["scope_sha256"],
        "canary_id": plan["canary_id"],
        "operation_go_sha256": plan["operation_go_sha256"]})).hexdigest()
    marker = resolved / f"{identity}.json"
    _write_outcome(marker, {"schema_version": 1, "document_type": "canary_attempt",
        "scope_sha256": plan["scope_sha256"], "canary_id": plan["canary_id"],
        "operation_go_sha256": plan["operation_go_sha256"], "state": "CONSUMED"})
    return marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an operator-executed canary")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    arguments = parser.parse_args(argv)
    outcome = arguments.bundle / "canary-outcome.json"
    if any(path.name == "canary-outcome.json" for path in arguments.bundle.rglob("canary-outcome.json")):
        print(json.dumps({"decision": "REFUSED", "reason": "canary_attempt_already_recorded",
                          "result": "FAIL"}, sort_keys=True, separators=(",", ":")),
              file=sys.stderr)
        return 1
    try:
        bundle_root = _root(arguments.bundle)
        ledger_root = _root(arguments.ledger)
        try:
            ledger_root.relative_to(bundle_root)
        except ValueError:
            pass
        else:
            raise CanaryError("canary_ledger_must_be_outside_bundle")
        plan = _load_contract(bundle_root, "canary-plan.json", "canary_plan")
        _consume_attempt(ledger_root, plan)
        result = validate_canary_bundle(arguments.bundle)
        _write_outcome(outcome, {"schema_version": 1, "document_type": "canary_outcome",
            "decision": result["decision"], "result": result["result"],
            "reason": None, "validation": result})
        print(json.dumps({"decision": result["decision"], "result": result["result"]},
                         sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        try:
            _write_outcome(outcome, {"schema_version": 1, "document_type": "canary_outcome",
                "decision": "REFUSED", "result": "FAIL", "reason": str(exc),
                "validation": None})
        except (CanaryError, OSError):
            pass
        print(json.dumps({"decision": "REFUSED", "reason": str(exc), "result": "FAIL"},
                         sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
