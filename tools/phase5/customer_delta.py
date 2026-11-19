#!/usr/bin/env python3
"""Offline, non-executing validator for a conditional customer delta dossier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .contract_rules import (ContractError, CUSTOMER_DELTA_GATES,
        CUSTOMER_DELTA_LIMITATIONS, canonical_bytes, canonical_sha256,
        aggregate_pass, strict_load_path, validate_contract, verify_artifact,
        verify_scope_inputs)
except ImportError:
    from contract_rules import (ContractError, CUSTOMER_DELTA_GATES,
        CUSTOMER_DELTA_LIMITATIONS, canonical_bytes, canonical_sha256,
        aggregate_pass, strict_load_path, validate_contract, verify_artifact,
        verify_scope_inputs)


class CustomerDeltaError(RuntimeError):
    """A fixed-message refusal from the offline customer-delta validator."""


CHANGE_GATES = {
    "workload_definition": {"QUALIFICATION_AUTHORIZED", "ORACLE_QUALIFIED",
                 "CUSTOMER_OPERATION_GO"},
    "dataset": {"DATA_EVIDENCE_AUTHORIZED", "ORACLE_QUALIFIED",
                "CUSTOMER_OPERATION_GO"},
    "target": {"RELEASE_SITE_QUALIFIED", "RESOURCE_ACCEPTED",
             "RECOVERY_REHEARSED", "CUSTOMER_OPERATION_GO"},
    "release_definition": {"RELEASE_SITE_QUALIFIED", "TOOLKIT_ACCEPTED",
              "QUALIFICATION_AUTHORIZED", "CUSTOMER_OPERATION_GO"},
    "limits": {"RESOURCE_ACCEPTED", "RECOVERY_REHEARSED",
             "CUSTOMER_OPERATION_GO"},
    "toolkit": {"TOOLKIT_ACCEPTED", "QUALIFICATION_AUTHORIZED",
                "CUSTOMER_OPERATION_GO"},
    "measurement_plan": {"QUALIFICATION_AUTHORIZED", "CUSTOMER_OPERATION_GO"},
    "baseline_manifest": {"QUALIFICATION_AUTHORIZED", "CUSTOMER_OPERATION_GO"},
    "evidence_policy": {"DATA_EVIDENCE_AUTHORIZED", "CUSTOMER_OPERATION_GO"},
    "recovery_plan": {"RECOVERY_REHEARSED", "CUSTOMER_OPERATION_GO"},
    "export_policy": {"DATA_EVIDENCE_AUTHORIZED", "CUSTOMER_OPERATION_GO"},
    "role_bindings": {"CUSTOMER_OPERATION_GO"},
    "window": {"CUSTOMER_OPERATION_GO"},
}

GATE_SOURCE_INPUTS = {
    "QUALIFICATION_AUTHORIZED": ("workload_definition", "measurement_plan",
                                  "baseline_manifest", "release_definition", "toolkit"),
    "DATA_EVIDENCE_AUTHORIZED": ("dataset", "evidence_policy", "export_policy"),
    "TOOLKIT_ACCEPTED": ("release_definition", "toolkit"),
    "ORACLE_QUALIFIED": ("workload_definition", "dataset", "measurement_plan"),
    "RESOURCE_ACCEPTED": ("target", "limits"),
}


def _root(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    info = path.stat()
    if (path.is_symlink() or resolved != path.absolute() or not path.is_dir() or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise CustomerDeltaError("invalid_customer_delta_root")
    return resolved


def _load(root: Path, relative: str, expected: str) -> dict[str, Any]:
    try:
        value = strict_load_path(Path(relative), root)
    except (ContractError, OSError) as exc:
        raise CustomerDeltaError(f"invalid_{expected}") from exc
    if value.get("contract_type") != expected:
        raise CustomerDeltaError(f"invalid_{expected}")
    return value


def _artifact(root: Path, value: dict[str, Any], expected: str) -> dict[str, Any]:
    try:
        verify_artifact(root, value)
        loaded = _load(root, value["path"], expected)
        if (canonical_sha256(loaded) != value["sha256"] or
                len(canonical_bytes(loaded)) != value["bytes"]):
            raise CustomerDeltaError(f"invalid_{expected}_artifact")
        return loaded
    except (ContractError, CustomerDeltaError, KeyError, TypeError) as exc:
        raise CustomerDeltaError(f"invalid_{expected}_artifact") from exc


def _internal_validation(root: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Load the intentionally non-contract P5-7B validator output safely."""
    try:
        verify_artifact(root, artifact)
        path = (root / artifact["path"]).resolve(strict=True)
        if root not in path.parents or path.is_symlink():
            raise CustomerDeltaError("invalid_internal_closeout_validation")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CustomerDeltaError("invalid_internal_closeout_validation")
            payload = os.read(descriptor, artifact["bytes"] + 1)
        finally:
            os.close(descriptor)
        if (len(payload) != artifact["bytes"] or
                hashlib.sha256(payload).hexdigest() != artifact["sha256"]):
            raise CustomerDeltaError("invalid_internal_closeout_validation")
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = item
            return result
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    except (ContractError, OSError, KeyError, TypeError, ValueError) as exc:
        raise CustomerDeltaError("invalid_internal_closeout_validation") from exc
    required = {"schema_version", "document_type", "scope_sha256", "result",
                "decision", "project_decision", "window_count", "pilot_id",
                "checked_at"}
    if (not isinstance(value, dict) or set(value) != required or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or
            value["document_type"] != "internal_pilot_validation" or
            type(value["window_count"]) is not int or value["window_count"] < 1 or
            not isinstance(value["pilot_id"], str) or not value["pilot_id"]):
        raise CustomerDeltaError("invalid_internal_closeout_validation")
    return value


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CustomerDeltaError("invalid_customer_delta_time") from exc
    if parsed.tzinfo is None:
        raise CustomerDeltaError("invalid_customer_delta_time")
    return parsed.astimezone(timezone.utc)


def _state(result: str) -> dict[str, str]:
    return {"lifecycle": "CLOSED", "technical_result": result,
            "evidence_completeness": "NONE" if result == "NOT_RUN" else "INVALID",
            "exit_state": "NOT_EXITED", "value_outcome": "NOT_EVALUATED",
            "project_decision": "PAUSE"}


def _expected_affected(dossier: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for change in dossier["changes"]:
        result.update(CHANGE_GATES[change["scope_input"]])
    return result


def _scope_delta(internal: dict[str, Any], customer: dict[str, Any]) -> list[dict[str, str]]:
    changes = []
    for name in sorted(internal["scope_inputs"]):
        before = internal["scope_inputs"][name]["sha256"]
        after = customer["scope_inputs"][name]["sha256"]
        if before != after:
            changes.append({"scope_input": name, "before_sha256": before,
                            "after_sha256": after})
    return changes


def validate_customer_delta(bundle: Path,
                            now: datetime | None = None) -> dict[str, Any]:
    """Validate a preparation dossier; this tranche can only return NOT_RUN."""
    root = _root(bundle)
    closeout = _load(root, "customer-delta-closeout.json", "customer_delta_closeout")
    checked = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        raise CustomerDeltaError("customer_delta_clock_is_not_utc_aware")
    recorded = _time(closeout["checked_at"])
    if recorded > checked.astimezone(timezone.utc):
        raise CustomerDeltaError("customer_delta_closeout_from_future")
    matrix = {item["gate_id"]: item for item in closeout["gate_reruns"]}
    if set(matrix) != CUSTOMER_DELTA_GATES:
        raise CustomerDeltaError("customer_gate_matrix_incomplete")

    authority_names = ("dossier", "sponsor_mandate", "internal_closeout_validation",
                       "internal_closeout_receipt", "site_preflight",
                       "recovery_validation", "customer_operation_go",
                       "retirement_evidence")
    present = {name for name in authority_names if closeout[name] is not None}
    if not present:
        expected_reasons = {"INTERNAL_CLOSEOUT_NOT_GRANTED", "NO_CUSTOMER",
                            "NO_MANDATE", "NO_SPONSOR_DECISION",
                            "CUSTOMER_EXECUTION_UNAVAILABLE"}
        if (closeout["internal_scope_sha256"] is not None or
                set(closeout["not_run_reasons"]) != expected_reasons or
                any(item["status"] != "NOT_RUN" for item in matrix.values()) or
                closeout["failures"] or closeout["final_environment_state"] != "NOT_TOUCHED" or
                closeout["share_release"] is not None or
                closeout["publication_release"] is not None):
            raise CustomerDeltaError("customer_not_run_false_claim")
        dossier_sha = None
        checks = ["NO_CUSTOMER_MATERIAL", "ALL_CUSTOMER_GATES_NOT_RUN",
                  "ENVIRONMENT_NOT_TOUCHED", "NO_RELEASE_AUTHORITY"]
    else:
        if present != set(authority_names):
            raise CustomerDeltaError("customer_authority_graph_incomplete")
        dossier = _artifact(root, closeout["dossier"], "customer_delta_dossier")
        dossier_sha = canonical_sha256(dossier)
        internal_scope = _artifact(root, dossier["internal_scope"], "scope")
        customer_scope = _artifact(root, dossier["customer_scope"], "scope")
        try:
            verify_scope_inputs(root, internal_scope)
            verify_scope_inputs(root, customer_scope)
        except ContractError as exc:
            raise CustomerDeltaError("customer_scope_input_invalid") from exc
        for name in ("workload_authority", "data_authority", "evidence_policy",
                     "recovery_plan", "support_plan", "incident_plan"):
            try:
                verify_artifact(root, dossier[name])
            except ContractError as exc:
                raise CustomerDeltaError("customer_dossier_support_artifact_invalid") from exc
        actual_changes = _scope_delta(internal_scope, customer_scope)
        if (closeout["internal_scope_sha256"] != dossier["internal_scope_sha256"] or
                canonical_sha256(internal_scope) != dossier["internal_scope_sha256"] or
                canonical_sha256(customer_scope) != dossier["customer_scope_sha256"] or
                dossier["changes"] != actual_changes or
                not {"workload_definition", "dataset", "target"} <=
                    {item["scope_input"] for item in actual_changes} or
                set(dossier["affected_gates"]) != _expected_affected(dossier)):
            raise CustomerDeltaError("customer_delta_scope_or_gate_claim_invalid")
        if set(customer_scope["gates_required"]) != set(dossier["affected_gates"]):
            raise CustomerDeltaError("customer_scope_gate_closure_invalid")
        bindings = {"workload_authority": "workload_definition",
                    "data_authority": "dataset", "evidence_policy": "evidence_policy",
                    "recovery_plan": "recovery_plan"}
        if any(dossier[field]["sha256"] != customer_scope["scope_inputs"][scope_name]["sha256"]
               for field, scope_name in bindings.items()) or \
                dossier["site_sha256"] != customer_scope["scope_inputs"]["target"]["sha256"] or \
                dossier["target_sha256"] != customer_scope["scope_inputs"]["target"]["sha256"] or \
                dossier["build_sha256"] != customer_scope["scope_inputs"]["release_definition"]["sha256"] or \
                dossier["pool_sha256"] != customer_scope["scope_inputs"]["limits"]["sha256"]:
            raise CustomerDeltaError("customer_delta_support_binding_invalid")
        role_bindings = _artifact(root, customer_scope["scope_inputs"]["role_bindings"],
                                  "customer_role_bindings")
        scope_window = _artifact(root, customer_scope["scope_inputs"]["window"],
                                 "customer_window")
        if (role_bindings["roles"] != dossier["role_approvals"] or
                scope_window["valid_from"] != dossier["valid_from"] or
                scope_window["valid_through"] != dossier["valid_through"]):
            raise CustomerDeltaError("customer_scope_authority_binding_invalid")
        internal_validation = _internal_validation(
            root, closeout["internal_closeout_validation"])
        internal = _artifact(root, closeout["internal_closeout_receipt"], "gate_receipt")
        mandate = _artifact(root, closeout["sponsor_mandate"], "gate_receipt")
        customer_go = _artifact(root, closeout["customer_operation_go"],
                                "approval_receipt")
        preflight = _artifact(root, closeout["site_preflight"], "preflight")
        recovery = _artifact(root, closeout["recovery_validation"], "recovery_validation")
        retirement = _artifact(root, closeout["retirement_evidence"],
                               "customer_retirement_evidence")
        if (internal_validation["result"] != "PASS" or
                internal_validation["decision"] != "ELIGIBLE_FOR_HUMAN_GRANT" or
                internal_validation["project_decision"] != "CONTINUE" or
                internal_validation["scope_sha256"] != dossier["internal_scope_sha256"] or
                internal["gate_id"] != "INTERNAL_CLOSEOUT" or
                internal["scope_sha256"] != dossier["internal_scope_sha256"] or
                internal["evidence_sha256"] != closeout["internal_closeout_validation"]["sha256"] or
                mandate["gate_id"] != "CUSTOMER_PILOT_MANDATE" or
                mandate["scope_sha256"] != dossier["customer_scope_sha256"] or
                mandate["evidence_sha256"] != closeout["dossier"]["sha256"] or
                mandate.get("subject_id") != dossier["customer_reference"] or
                customer_go["receipt_kind"] != "CUSTOMER_OPERATION_GO" or
                customer_go["scope_sha256"] != dossier["customer_scope_sha256"] or
                customer_go["dossier_sha256"] != closeout["dossier"]["sha256"] or
                customer_go["mandate_sha256"] != closeout["sponsor_mandate"]["sha256"] or
                preflight["scope_sha256"] != dossier["customer_scope_sha256"] or
                preflight["target_identity_sha256"] != dossier["target_sha256"] or
                "checked_at" not in preflight or
                not aggregate_pass(preflight["state"]) or
                recovery["scope_sha256"] != dossier["customer_scope_sha256"] or
                recovery["mandate_sha256"] != closeout["sponsor_mandate"]["sha256"] or
                recovery["result"] != "PASS" or
                recovery["decision"] != "ELIGIBLE_FOR_HUMAN_GRANT" or
                customer_go["preflight_sha256"] != canonical_sha256(preflight) or
                customer_go["recovery_validation_sha256"] != canonical_sha256(recovery) or
                len({internal["nonce"], mandate["nonce"], customer_go["nonce"]}) != 3 or
                len({internal["contract_id"], mandate["contract_id"],
                     customer_go["contract_id"]}) != 3):
            raise CustomerDeltaError("customer_authority_graph_invalid")
        mandate_time = _time(mandate["approved_at"])
        internal_validation_time = _time(internal_validation["checked_at"])
        internal_approval_time = _time(internal["approved_at"])
        preflight_time = _time(preflight["checked_at"])
        recovery_time = _time(recovery["checked_at"])
        retirement_time = _time(retirement["retired_at"])
        go_start, go_finish = _time(customer_go["valid_from"]), _time(customer_go["valid_through"])
        charter_start, charter_finish = (_time(dossier["valid_from"]),
                                         _time(dossier["valid_through"]))
        if (len(set(mandate["approvals"])) != len(mandate["approvals"]) or
                not set(mandate["approvals"]).isdisjoint(dossier["role_approvals"].values()) or
                not set(mandate["approvals"]).isdisjoint(internal["approvals"]) or
                not set(internal["approvals"]).isdisjoint(
                    dossier["role_approvals"].values()) or
                not internal_validation_time <= internal_approval_time <= mandate_time or
                not charter_start <= mandate_time or
                not mandate_time <= preflight_time <= go_start or
                not mandate_time <= recovery_time <= go_start or
                go_start < mandate_time or go_finish <= go_start or
                go_finish - go_start > timedelta(days=14) or
                not charter_start <= go_start < go_finish <= charter_finish or
                not go_finish <= recorded <= charter_finish or
                not go_finish <= retirement_time <= recorded or
                retirement["scope_sha256"] != dossier["customer_scope_sha256"] or
                retirement["target_sha256"] != dossier["target_sha256"] or
                retirement["result"] != "PASS" or
                set(customer_go["approvals"]) != set(dossier["role_approvals"].values())):
            raise CustomerDeltaError("customer_operation_window_or_roles_invalid")
        affected = set(dossier["affected_gates"])
        receipt_ids = {internal["contract_id"], mandate["contract_id"],
                       customer_go["contract_id"]}
        receipt_nonces = {internal["nonce"], mandate["nonce"], customer_go["nonce"]}
        evidence_digests: set[str] = set()
        for gate, item in matrix.items():
            expected_status = "PASS" if gate in affected else "NOT_RUN"
            if item["status"] != expected_status:
                raise CustomerDeltaError("customer_gate_rerun_status_invalid")
            if expected_status == "PASS":
                evidence = item["evidence"]
                receipt = _artifact(root, item["receipt"],
                                    "approval_receipt" if gate == "CUSTOMER_OPERATION_GO"
                                    else "gate_receipt")
                verify_artifact(root, evidence)
                if gate == "CUSTOMER_OPERATION_GO":
                    if receipt != customer_go or evidence != closeout["dossier"]:
                        raise CustomerDeltaError("customer_go_rerun_differs_from_go")
                elif (receipt["gate_id"] != gate or
                      receipt["scope_sha256"] != dossier["customer_scope_sha256"] or
                      receipt["evidence_sha256"] != evidence["sha256"] or
                      not mandate_time <= _time(receipt["approved_at"]) <= go_start):
                    raise CustomerDeltaError("customer_gate_rerun_invalid")
                if gate != "CUSTOMER_OPERATION_GO":
                    expected_evidence = {
                        "RELEASE_SITE_QUALIFIED": closeout["site_preflight"],
                        "RECOVERY_REHEARSED": closeout["recovery_validation"],
                    }.get(gate)
                    if ((expected_evidence is not None and evidence != expected_evidence) or
                            evidence["sha256"] in evidence_digests):
                        raise CustomerDeltaError("customer_gate_evidence_not_specific")
                    if expected_evidence is None:
                        gate_evidence = _artifact(root, evidence, "customer_gate_evidence")
                        source_sha = canonical_sha256({"gate_id": gate,
                            "inputs": {name: customer_scope["scope_inputs"][name]["sha256"]
                                       for name in GATE_SOURCE_INPUTS[gate]}})
                        if (gate_evidence["gate_id"] != gate or
                                gate_evidence["scope_sha256"] !=
                                    dossier["customer_scope_sha256"] or
                                gate_evidence["source_sha256"] != source_sha or
                                gate_evidence["result"] != "PASS" or
                                not mandate_time <= _time(gate_evidence["checked_at"]) <=
                                    _time(receipt["approved_at"])):
                            raise CustomerDeltaError("customer_gate_evidence_invalid")
                    elif gate == "RECOVERY_REHEARSED" and \
                            _time(receipt["approved_at"]) < recovery_time:
                        raise CustomerDeltaError("customer_gate_evidence_predates_validation")
                    elif gate == "RELEASE_SITE_QUALIFIED" and \
                            _time(receipt["approved_at"]) < preflight_time:
                        raise CustomerDeltaError("customer_gate_evidence_predates_validation")
                    if (receipt["contract_id"] in receipt_ids or
                            receipt["nonce"] in receipt_nonces or
                            not set(receipt["approvals"]).isdisjoint(
                                set(internal["approvals"]) | set(mandate["approvals"]) |
                                set(dossier["role_approvals"].values()))):
                        raise CustomerDeltaError("customer_gate_rerun_not_independent")
                    receipt_ids.add(receipt["contract_id"])
                    receipt_nonces.add(receipt["nonce"])
                    evidence_digests.add(evidence["sha256"])
        if (closeout["not_run_reasons"] != ["CUSTOMER_EXECUTION_UNAVAILABLE"] or
                closeout["failures"] or
                closeout["final_environment_state"] != "RETIRED_OFF" or
                closeout["share_release"] is not None or
                closeout["publication_release"] is not None):
            raise CustomerDeltaError("prepared_customer_delta_false_execution_claim")
        checks = ["INTERNAL_CLOSEOUT_BOUND", "CUSTOMER_MANDATE_BOUND",
                  "DELTA_GATE_MATRIX_COMPLETE", "CUSTOMER_GO_BOUNDED",
                  "CUSTOMER_EXECUTION_UNAVAILABLE"]

    validation = {"schema_version": 1, "contract_type": "customer_delta_validation",
        "contract_id": "customer-delta-validation", "closeout_sha256": canonical_sha256(closeout),
        "dossier_sha256": dossier_sha, "checked_at": closeout["checked_at"],
        "result": "NOT_RUN", "decision": "REFUSED", "checks": checks,
        "limitations": sorted(CUSTOMER_DELTA_LIMITATIONS), "state": _state("NOT_RUN")}
    validate_contract(validation)
    return validation


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_customer_delta(args.bundle)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_bytes(result)); stream.flush(); os.fsync(stream.fileno())
        print(json.dumps({"result": result["result"], "decision": result["decision"]},
                         sort_keys=True, separators=(",", ":")))
        return 0
    except (CustomerDeltaError, ContractError, OSError) as exc:
        print(json.dumps({"result": "REFUSED", "reason": str(exc)}, sort_keys=True,
                         separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
