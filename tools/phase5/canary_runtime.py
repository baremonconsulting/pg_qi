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
except ImportError:
    from contract_rules import (CANARY_CHECKS, CANARY_CONTROLS, CANARY_LIMITATIONS,
        ContractError, canonical_bytes, canonical_sha256, resolve_bundle_path,
        strict_load_path, validate_contract, verify_artifact)
    from measurement_runtime import MeasurementError, validate_level_authorization


class CanaryError(RuntimeError):
    """A fixed-message canary refusal."""


UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?:\d{3}|\d{6}))?Z$")


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
        verify_artifact(root, run["positive_spool"])
    except ContractError as exc:
        raise CanaryError("canary_positive_artifact_invalid") from exc
    if (plan["positive_sql"]["sha256"] != plan["positive_literal_sha256"] or
            run["positive_spool"]["bytes"] > plan["maximum_result_bytes"]):
        raise CanaryError("canary_positive_literal_or_limit_mismatch")
    request = _artifact_contract(root, run["positive_request"], "request")
    snapshot = _artifact_contract(root, run["positive_snapshot"], "snapshot")
    if (request["scope_sha256"] != scope or request["query_id"] != plan["positive_query_id"] or
            request["session_id"] != run["session_id"] or
            request["execution_status"] != "SUCCEEDED" or
            request["planning_evidence"] != "FRESH" or
            request["snapshot_relation"] != "CURRENT" or
            request.get("observed_spool") != run["positive_spool"] or
            snapshot["scope_sha256"] != scope or snapshot["session_id"] != run["session_id"] or
            snapshot["snapshot_id"] != request.get("snapshot_id") or
            snapshot["planning_request_id"] != request.get("planning_request_id") or
            snapshot["tuple"] != plan["expected_snapshot"]):
        raise CanaryError("canary_positive_request_or_snapshot_mismatch")

    oracle = _raw(root, run["oracle_proof"], {"document_type", "query_id",
        "spool_sha256", "result"}, "canary_oracle")
    if (oracle["query_id"] != plan["positive_query_id"] or
            oracle["spool_sha256"] != run["positive_spool"]["sha256"] or
            oracle["result"] != "PASS"):
        raise CanaryError("canary_oracle_did_not_pass")

    controls = [_raw(root, item, {"document_type", "control_id", "session_id",
        "execution_status", "planning_evidence", "snapshot_relation", "observed_engine",
        "performed_at", "result"}, "canary_control") for item in run["control_proofs"]]
    if ([item["control_id"] for item in controls] != CANARY_CONTROLS or
            any(item["session_id"] != run["session_id"] or
                item["execution_status"] != "SUCCEEDED" or
                item["planning_evidence"] != "UNAVAILABLE" or
                item["snapshot_relation"] != "NO_SNAPSHOT" or
                item["observed_engine"] != "postgres" or item["result"] != "PASS"
                for item in controls) or
            run["ambient_force_off_proof"] != run["control_proofs"][0]):
        raise CanaryError("canary_negative_controls_did_not_pass")
    control_times = [_time(item["performed_at"], "control") for item in controls]
    if not started < control_times[0] < control_times[1] < control_times[2] < finished:
        raise CanaryError("canary_control_order_invalid")

    exit_proof = _raw(root, retirement["backend_exit_proof"], {"document_type",
        "session_id", "observer_authorized", "observation_complete", "backend_present",
        "observed_at", "result"}, "canary_backend_exit")
    reconnect = _raw(root, retirement["fresh_reconnect_proof"], {"document_type",
        "session_id", "connected_at", "mode", "force_execution", "observed_engine",
        "result"}, "canary_fresh_reconnect")
    exit_time = _time(exit_proof["observed_at"], "canary_exit")
    reconnect_time = _time(reconnect["connected_at"], "canary_reconnect")
    performed = _time(retirement["performed_at"], "canary_retirement")
    if (retirement["owned_session_id"] != run["session_id"] or
            exit_proof["session_id"] != run["session_id"] or
            exit_proof["observer_authorized"] is not True or
            exit_proof["observation_complete"] is not True or
            exit_proof["backend_present"] is not False or exit_proof["result"] != "PASS" or
            reconnect["session_id"] == run["session_id"] or reconnect["mode"] != "off" or
            reconnect["force_execution"] != "off" or
            reconnect["observed_engine"] != "postgres" or reconnect["result"] != "PASS" or
            not (finished < exit_time <= performed < reconnect_time <=
                 checked_utc) or
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an operator-executed canary")
    parser.add_argument("--bundle", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = validate_canary_bundle(arguments.bundle)
        print(json.dumps({"decision": result["decision"], "result": result["result"]},
                         sort_keys=True, separators=(",", ":")))
        return 0
    except (CanaryError, ContractError, MeasurementError, OSError) as exc:
        print(json.dumps({"decision": "REFUSED", "reason": str(exc), "result": "FAIL"},
                         sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
