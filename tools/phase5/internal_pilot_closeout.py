#!/usr/bin/env python3
"""Fail-closed offline closeout for the bounded PGQI-5 internal pilot."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .contract_rules import (ContractError, INTERNAL_PILOT_LIMITATIONS,
        aggregate_pass, canonical_bytes, canonical_sha256, resolve_bundle_path,
        strict_load_path, validate_contract, verify_artifact)
    from .evidence_tool import EvidenceError, validate_private, validate_shared
    from .measurement_runtime import MeasurementError, validate_level_authorization
except ImportError:
    from contract_rules import (ContractError, INTERNAL_PILOT_LIMITATIONS,
        aggregate_pass, canonical_bytes, canonical_sha256, resolve_bundle_path,
        strict_load_path, validate_contract, verify_artifact)
    from evidence_tool import EvidenceError, validate_private, validate_shared
    from measurement_runtime import MeasurementError, validate_level_authorization


class CloseoutError(RuntimeError):
    """A fixed-message pilot closeout refusal."""


def _root(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    info = path.stat()
    if (path.is_symlink() or resolved != path.absolute() or not path.is_dir() or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise CloseoutError("invalid_pilot_root")
    return resolved


def _load(root: Path, relative: str, kind: str) -> dict[str, Any]:
    try:
        value = strict_load_path(Path(relative), root)
    except (ContractError, OSError) as exc:
        raise CloseoutError(f"invalid_{kind}") from exc
    if value.get("contract_type") != kind:
        raise CloseoutError(f"invalid_{kind}")
    return value


def _artifact(root: Path, value: dict[str, Any], kind: str) -> dict[str, Any]:
    try:
        verify_artifact(root, value)
        return _load(root, value["path"], kind)
    except (ContractError, KeyError, TypeError) as exc:
        raise CloseoutError(f"invalid_{kind}_artifact") from exc


def _raw_artifact(root: Path, value: dict[str, Any], kind: str) -> dict[str, Any]:
    try:
        verify_artifact(root, value)
        path = resolve_bundle_path(root, Path(value["path"]))
        payload = path.read_bytes()
        parsed = json.loads(payload.decode("utf-8"))
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError,
            KeyError, TypeError) as exc:
        raise CloseoutError(f"invalid_{kind}_artifact") from exc
    if not isinstance(parsed, dict) or parsed.get("document_type") != kind:
        raise CloseoutError(f"invalid_{kind}_artifact")
    return parsed


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CloseoutError("invalid_pilot_time") from exc
    if parsed.tzinfo is None:
        raise CloseoutError("invalid_pilot_time")
    return parsed.astimezone(timezone.utc)


def _inside_private(root: Path, private_relative: str,
                    artifact: dict[str, Any]) -> tuple[Path, str]:
    private = resolve_bundle_path(root, Path(private_relative))
    target = resolve_bundle_path(root, Path(artifact["path"]))
    try:
        relative = target.relative_to(private).as_posix()
    except ValueError as exc:
        raise CloseoutError("window_artifact_outside_private_bundle") from exc
    return private, relative


def _indexed(private: Path, relative: str, artifact: dict[str, Any]) -> None:
    index = _load(private, "index.json", "index")
    matches = [item for item in index["payloads"] if item["path"] == relative]
    if len(matches) != 1 or matches[0]["sha256"] != artifact["sha256"] or \
            matches[0]["bytes"] != artifact["bytes"]:
        raise CloseoutError("window_artifact_not_sealed_in_private_bundle")


def _derive_value(reports: list[dict[str, Any]]) -> str:
    values = [item["value_outcome"] for item in reports]
    if values and all(item == "POSITIVE" for item in values):
        return "POSITIVE"
    if "NEGATIVE" in values:
        return "NEGATIVE"
    if all(item == "NOT_EVALUATED" for item in values):
        return "NOT_EVALUATED"
    return "INCONCLUSIVE"


def validate_internal_pilot(root: Path,
                            now: datetime | None = None) -> dict[str, Any]:
    """Recompute closeout truth; never grant INTERNAL_CLOSEOUT."""
    root = _root(root)
    plan = _load(root, "internal-pilot-plan.json", "internal_pilot_plan")
    closeout = _load(root, "internal-pilot-closeout.json", "internal_pilot_closeout")
    scope = plan["scope_sha256"]
    plan_sha = canonical_sha256(plan)
    if (closeout["scope_sha256"] != scope or
            closeout["pilot_id"] != plan["pilot_id"] or
            closeout["plan_sha256"] != plan_sha):
        raise CloseoutError("pilot_identity_graph_mismatch")

    measurement_plan = _artifact(root, plan["measurement_plan"], "measurement_plan")
    operation_go = _artifact(root, plan["operation_go"], "approval_receipt")
    canary_validation = _artifact(root, plan["canary_validation"], "canary_validation")
    canary_acceptance = _artifact(root, plan["canary_acceptance"], "gate_receipt")
    if (operation_go["receipt_kind"] != "OPERATION_GO" or
            operation_go["scope_sha256"] != scope or
            canary_validation["scope_sha256"] != scope or
            canary_validation["result"] != "PASS" or
            canary_validation["decision"] != "ELIGIBLE_FOR_HUMAN_GRANT" or
            canary_acceptance["gate_id"] != "CANARY_ACCEPTED" or
            canary_acceptance["scope_sha256"] != scope or
            canary_acceptance["evidence_sha256"] != canonical_sha256(canary_validation)):
        raise CloseoutError("pilot_authority_invalid")
    levels = measurement_plan["concurrency_levels"]
    approved = plan["approved_windows"]
    if (len(approved) != len(levels) or
            [item["level"] for item in approved] != [item["level"] for item in levels]):
        raise CloseoutError("pilot_does_not_cover_frozen_levels")
    if len(closeout["windows"]) != len(approved):
        raise CloseoutError("pilot_window_count_mismatch")

    checked_input = now or datetime.now(timezone.utc)
    if checked_input.tzinfo is None:
        raise CloseoutError("validation_clock_is_not_utc_aware")
    checked = checked_input.astimezone(timezone.utc)
    recorded_checked = _time(closeout["checked_at"])
    if recorded_checked > checked:
        raise CloseoutError("closeout_checked_at_is_in_the_future")
    go_start = _time(operation_go["valid_from"])
    go_end = _time(operation_go["valid_through"])
    windows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    private_results: list[dict[str, Any]] = []
    retirements: list[dict[str, Any]] = []
    prior_finish: datetime | None = None
    first_start: datetime | None = None
    run_ids: set[str] = set()
    for expected, artifact in zip(approved, closeout["windows"]):
        window = _artifact(root, artifact, "internal_pilot_window")
        if (window["scope_sha256"] != scope or window["pilot_id"] != plan["pilot_id"] or
                window["plan_sha256"] != plan_sha or
                window["window_id"] != expected["window_id"] or
                window["level"] != expected["level"] or
                window["private_bundle_path"] != expected["private_bundle_path"] or
                window["level_authorization"] != expected["level_authorization"]):
            raise CloseoutError("pilot_window_identity_mismatch")
        if window["run_id"] in run_ids:
            raise CloseoutError("pilot_run_history_overwritten")
        run_ids.add(window["run_id"])
        started, finished = _time(window["started_at"]), _time(window["finished_at"])
        if first_start is None:
            first_start = started
        if not (go_start <= started < finished <= min(go_end, checked)) or \
                (prior_finish is not None and started < prior_finish):
            raise CloseoutError("pilot_window_timeline_invalid")
        prior_finish = finished

        authorization = _raw_artifact(root, expected["level_authorization"],
                                      "measurement_level_authorization")
        try:
            authorized_level = validate_level_authorization(authorization,
                measurement_plan, canonical_sha256(measurement_plan), scope, root, started)
        except (MeasurementError, ContractError, OSError) as exc:
            raise CloseoutError("pilot_level_authorization_invalid") from exc
        if authorized_level["level"] != window["level"]:
            raise CloseoutError("pilot_level_authorization_mismatch")
        if authorization.get("operation_go") != plan["operation_go"]:
            raise CloseoutError("pilot_operation_go_substitution")

        private, report_relative = _inside_private(root, window["private_bundle_path"],
                                                   window["measurement_report"])
        _, retirement_relative = _inside_private(root, window["private_bundle_path"],
                                                  window["retirement"])
        _, stop_relative = _inside_private(root, window["private_bundle_path"],
                                           window["dispatch_stop_proof"])
        _, off_relative = _inside_private(root, window["private_bundle_path"],
                                          window["fresh_off_proof"])
        _indexed(private, report_relative, window["measurement_report"])
        _indexed(private, retirement_relative, window["retirement"])
        _indexed(private, stop_relative, window["dispatch_stop_proof"])
        _indexed(private, off_relative, window["fresh_off_proof"])
        try:
            private_result = validate_private(private, scope)
        except (EvidenceError, ContractError, OSError) as exc:
            raise CloseoutError("pilot_private_validation_failed") from exc
        report = _artifact(root, window["measurement_report"], "measurement_report")
        retirement = _artifact(root, window["retirement"], "retirement")
        stop_proof = _raw_artifact(root, window["dispatch_stop_proof"],
                                   "pilot_dispatch_stop")
        off_proof = _raw_artifact(root, window["fresh_off_proof"],
                                  "pilot_fresh_off")
        if (report["scope_sha256"] != scope or
                report["plan_sha256"] != canonical_sha256(measurement_plan) or
                report["level"] != window["level"] or report["run_id"] != window["run_id"] or
                retirement["scope_sha256"] != scope or
                retirement["run_id"] != window["run_id"]):
            raise CloseoutError("pilot_window_evidence_mismatch")
        identity = (plan["pilot_id"], window["window_id"], window["run_id"])
        if (set(stop_proof) != {"schema_version", "document_type", "pilot_id",
                "window_id", "run_id", "stopped_at", "admission_state"} or
                (stop_proof["pilot_id"], stop_proof["window_id"], stop_proof["run_id"]) !=
                identity or stop_proof["admission_state"] != "STOPPED"):
            raise CloseoutError("dispatch_stop_proof_invalid")
        if (set(off_proof) != {"schema_version", "document_type", "pilot_id",
                "window_id", "run_id", "observed_at", "session_id", "pg_qi_enabled",
                "routing_state", "duckdb_force_execution"} or
                (off_proof["pilot_id"], off_proof["window_id"], off_proof["run_id"]) !=
                identity or not isinstance(off_proof["session_id"], str) or
                not off_proof["session_id"] or
                (off_proof["pg_qi_enabled"], off_proof["routing_state"],
                 off_proof["duckdb_force_execution"]) != ("off", "off", "off")):
            raise CloseoutError("fresh_off_proof_invalid")
        stopped_at = _time(stop_proof["stopped_at"])
        off_at = _time(off_proof["observed_at"])
        if not (started <= stopped_at <= off_at <= finished):
            raise CloseoutError("window_shutdown_timeline_invalid")
        retired = (retirement["backend_absence_verified"] is True and
                   retirement["cached_plans_retired"] is True and
                   retirement["default_off_reconnect"] is True and
                   aggregate_pass(retirement["state"]))
        expected_result = ("PASS" if report["technical_result"] == "PASS" and retired and
                           private_result["result"] == "PASS" else
                           "FAIL" if report["technical_result"] == "FAIL" or
                           private_result["result"] == "FAIL" or
                           retirement["state"]["exit_state"] == "DIRTY" else "INCOMPLETE")
        if window["result"] != expected_result or \
                (expected_result == "PASS") != aggregate_pass(window["state"]):
            raise CloseoutError("pilot_window_result_false_pass")
        windows.append(window); reports.append(report)
        private_results.append(private_result); retirements.append(retirement)

    if prior_finish is None or recorded_checked < prior_finish or \
            _time(canary_acceptance["approved_at"]) > (first_start or prior_finish):
        raise CloseoutError("pilot_closeout_or_acceptance_timeline_invalid")

    technical_pass = all(item["result"] == "PASS" for item in windows)
    decisions = closeout["decisions"]
    for finding in closeout["hard_stop_findings"]:
        try:
            verify_artifact(root, finding["evidence"])
        except ContractError as exc:
            raise CloseoutError("hard_stop_evidence_invalid") from exc
    open_hard_stop = any(item["status"] == "OPEN"
                         for item in closeout["hard_stop_findings"])
    safety = ("FAIL" if any(item["technical_result"] == "FAIL" for item in reports) else
              "INCOMPLETE" if any(item["technical_result"] == "INCOMPLETE"
                                  for item in reports) else "PASS")
    evidence = ("FAIL" if any(item["terminal"]["state"]["evidence_completeness"] == "INVALID"
                              for item in private_results) else
                "INCOMPLETE" if any(item["terminal"]["state"]["evidence_completeness"] !=
                                    "COMPLETE" for item in private_results) else "PASS")
    exit_decision = ("FAIL" if any(item["state"]["exit_state"] == "DIRTY"
                                   for item in retirements) else
                     "INCOMPLETE" if any(not (item["backend_absence_verified"] and
                         item["cached_plans_retired"] and item["default_off_reconnect"] and
                         item["state"]["exit_state"] == "CLEAN") for item in retirements)
                     else "PASS")
    if (decisions["safety"], decisions["evidence_completeness"], decisions["exit"]) != \
            (safety, evidence, exit_decision):
        raise CloseoutError("core_decisions_not_derived_from_evidence")
    if not technical_pass and closeout["result"] == "PASS":
        raise CloseoutError("administrative_close_is_not_technical_pass")
    if open_hard_stop and closeout["result"] == "PASS":
        raise CloseoutError("open_hard_stop_cannot_pass")
    if decisions["measured_value"] != _derive_value(reports):
        raise CloseoutError("measured_value_not_derived_from_reports")
    shares = [item["coverage"]["measured_natural_share"] for item in reports]
    expected_relevance = ("INCONCLUSIVE" if any(item is None for item in shares) else
                          "NOT_RELEVANT" if all(item == 0 for item in shares) else
                          "RELEVANT")
    if decisions["workload_relevance"] != expected_relevance:
        raise CloseoutError("workload_relevance_not_derived_from_reports")
    service_outcomes = [item["outcome"] for item in closeout["service_work_records"]]
    service_decision = ("UNACCEPTABLE" if "FAILED" in service_outcomes else
                        "INCONCLUSIVE" if "INCOMPLETE" in service_outcomes else
                        "ACCEPTABLE")
    if decisions["service_work"] != service_decision:
        raise CloseoutError("service_work_not_derived_from_records")
    if closeout["retention"]["owner"] != plan["retention_owner"] or \
            closeout["retention"]["due_at"] != plan["retention_due_at"]:
        raise CloseoutError("retention_commitment_mismatch")

    if plan["share_release"] is None:
        if plan["shared_derived_paths"] or closeout["shared_release_state"] != "NOT_REQUESTED":
            raise CloseoutError("shared_evidence_without_release")
    else:
        release = _artifact(root, plan["share_release"], "export_receipt")
        if release["receipt_kind"] != "SHARE_RELEASE" or release["scope_sha256"] != scope or \
                _time(release["approved_at"]) > recorded_checked or \
                not plan["shared_derived_paths"] or \
                closeout["shared_release_state"] != "RELEASED":
            raise CloseoutError("share_release_invalid")
        for item in plan["shared_derived_paths"]:
            shared = resolve_bundle_path(root, Path(item["bundle_path"]))
            receipt = resolve_bundle_path(root, Path(item["private_validation_receipt"]))
            try:
                shared_result = validate_shared(shared, receipt)
            except (EvidenceError, ContractError, OSError) as exc:
                raise CloseoutError("shared_derived_validation_failed") from exc
            if shared_result["scope_sha256"] != scope:
                raise CloseoutError("shared_derived_scope_mismatch")

    expected_result = "PASS" if technical_pass and not open_hard_stop else \
        "INCOMPLETE" if any(item["result"] == "INCOMPLETE" for item in windows) else "FAIL"
    if closeout["result"] != expected_result or \
            (expected_result == "PASS") != (closeout["eligibility"] ==
                                             "ELIGIBLE_FOR_HUMAN_GRANT"):
        raise CloseoutError("closeout_result_false_pass")
    if closeout["limitations"] != sorted(INTERNAL_PILOT_LIMITATIONS):
        raise CloseoutError("closeout_limitations_not_canonical")
    return {"result": expected_result,
            "decision": "ELIGIBLE_FOR_HUMAN_GRANT" if expected_result == "PASS" else "REFUSED",
            "project_decision": decisions["project_decision"],
            "window_count": len(windows), "scope_sha256": scope,
            "pilot_id": plan["pilot_id"], "checked_at": checked.isoformat()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_internal_pilot(args.bundle)
        payload = canonical_bytes({"schema_version": 1,
            "document_type": "internal_pilot_validation", **result})
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload); os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return 0
    except (CloseoutError, ContractError, OSError) as exc:
        print(f"internal pilot closeout refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
