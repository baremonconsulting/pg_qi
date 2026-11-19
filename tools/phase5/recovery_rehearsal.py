#!/usr/bin/env python3
"""Offline validation for operator-executed PGQI-5 recovery rehearsals.

This module deliberately has no lifecycle capability.  It reads a sealed set of
operator observations and can only report eligibility for a later human gate.
"""

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
    from .contract_rules import (ContractError, RECOVERY_CHECKS, RECOVERY_LIMITATIONS, canonical_bytes,
        canonical_sha256,
        resolve_bundle_path, strict_load_path, validate_contract, validate_relative_path,
        verify_artifact)
except ImportError:
    from contract_rules import (ContractError, RECOVERY_CHECKS, RECOVERY_LIMITATIONS, canonical_bytes,
        canonical_sha256,
        resolve_bundle_path, strict_load_path, validate_contract, validate_relative_path,
        verify_artifact)


class RehearsalError(RuntimeError):
    """A fixed-message rehearsal refusal."""


CHECKS = RECOVERY_CHECKS
PROOF_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?:\d{3}|\d{6}))?Z$")


def _load_contract(root: Path, name: str, expected: str) -> dict[str, Any]:
    try:
        value = strict_load_path(Path(name), root)
    except ContractError as exc:
        raise RehearsalError(f"invalid_{expected}") from exc
    if value.get("contract_type") != expected:
        raise RehearsalError(f"invalid_{expected}")
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise RehearsalError("invalid_proof_json")
        value[key] = item
    return value


def _proof(root: Path, artifact: dict[str, Any], expected_type: str,
           fields: set[str]) -> dict[str, Any]:
    try:
        verify_artifact(root, artifact)
        relative = validate_relative_path(artifact["path"])
        safe_path = resolve_bundle_path(root, Path(relative))
        descriptor = os.open(safe_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077 or
                    info.st_size > 1024 * 1024):
                raise RehearsalError("unsafe_rehearsal_proof")
            payload = os.read(descriptor, info.st_size + 1)
            if len(payload) != info.st_size:
                raise RehearsalError("rehearsal_proof_changed")
            if (len(payload) != artifact["bytes"] or
                    hashlib.sha256(payload).hexdigest() != artifact["sha256"]):
                raise RehearsalError("rehearsal_proof_changed")
        finally:
            os.close(descriptor)
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                           parse_constant=lambda item: (_ for _ in ()).throw(
                               RehearsalError(f"invalid_number_{item}")))
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError) as exc:
        raise RehearsalError("invalid_rehearsal_proof") from exc
    if not isinstance(value, dict) or set(value) != fields or \
            value.get("document_type") != expected_type:
        raise RehearsalError(f"invalid_{expected_type}")
    return value


def _identity(value: Any, name: str) -> dict[str, Any]:
    fields = {"cluster_id", "start_id", "pid", "backend_start", "session_id"}
    if not isinstance(value, dict) or set(value) != fields:
        raise RehearsalError(f"invalid_{name}_identity")
    if (not all(isinstance(value[item], str) and value[item] for item in
                ("cluster_id", "start_id", "backend_start", "session_id")) or
            isinstance(value["pid"], bool) or not isinstance(value["pid"], int) or
            value["pid"] <= 0):
        raise RehearsalError(f"invalid_{name}_identity")
    return value


def _proof_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or PROOF_UTC.fullmatch(value) is None:
        raise RehearsalError(f"invalid_{name}_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RehearsalError(f"invalid_{name}_time") from exc
    if parsed.tzinfo is None:
        raise RehearsalError(f"invalid_{name}_time")
    return parsed.astimezone(timezone.utc)


def _fresh_control(root: Path, artifact: dict[str, Any], rehearsal_id: str,
                   owned: set[str], required_after: datetime,
                   required_by: datetime) -> dict[str, Any]:
    proof = _proof(root, artifact, "fresh_safe_control", {"document_type",
        "rehearsal_id", "identity", "connected_at", "mode", "force_execution",
        "observed_engine", "result"})
    identity = _identity(proof["identity"], "fresh_control")
    backend_start = _proof_time(identity["backend_start"], "fresh_control_backend_start")
    connected = _proof_time(proof["connected_at"], "fresh_control")
    if (proof["rehearsal_id"] != rehearsal_id or identity["session_id"] in owned or
            connected.tzinfo is None or not (required_after <
            connected.astimezone(timezone.utc) <= required_by) or
            not (required_after < backend_start <= connected) or
            proof["mode"] != "off" or proof["force_execution"] != "off" or
            proof["observed_engine"] != "postgres" or proof["result"] != "PASS"):
        raise RehearsalError("fresh_default_off_control_failed")
    return proof


def validate_rehearsal_bundle(root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Validate evidence only; return eligibility, never grant an operational gate."""
    resolved = root.resolve(strict=True)
    if (root.is_symlink() or resolved != root.absolute() or not root.is_dir() or
            root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700):
        raise RehearsalError("invalid_rehearsal_root")
    mandate = _load_contract(root, "mandate.json", "rehearsal_mandate")
    r1 = _load_contract(root, "r1-terminal.json", "recovery_r1")
    r2 = _load_contract(root, "r2-terminal.json", "recovery_r2")
    walkthrough = _load_contract(root, "walkthrough.json", "operator_walkthrough")
    incident = _load_contract(root, "incident.json", "recovery_incident")
    return validate_rehearsal_contracts(root, mandate, r1, r2, walkthrough,
                                        incident, now)


def validate_rehearsal_contracts(root: Path, mandate: dict[str, Any],
        r1: dict[str, Any], r2: dict[str, Any], walkthrough: dict[str, Any],
        incident: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Recompute one indexed rehearsal graph from its typed documents."""
    resolved = root.resolve(strict=True)
    if (root.is_symlink() or resolved != root.absolute() or not root.is_dir() or
            root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700):
        raise RehearsalError("invalid_rehearsal_root")
    expected = ((mandate, "rehearsal_mandate"), (r1, "recovery_r1"),
                (r2, "recovery_r2"), (walkthrough, "operator_walkthrough"),
                (incident, "recovery_incident"))
    try:
        for document, kind in expected:
            if document.get("contract_type") != kind:
                raise RehearsalError(f"invalid_{kind}")
            validate_contract(document)
    except ContractError as exc:
        raise RehearsalError("invalid_rehearsal_contract_graph") from exc
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise RehearsalError("validation_clock_is_not_utc_aware")
    valid_from = datetime.fromisoformat(mandate["valid_from"].replace("Z", "+00:00"))
    valid_through = datetime.fromisoformat(mandate["valid_through"].replace("Z", "+00:00"))
    if not valid_from <= instant.astimezone(timezone.utc) < valid_through:
        raise RehearsalError("rehearsal_mandate_not_current")

    scope = mandate["scope_sha256"]; rehearsal = mandate["rehearsal_id"]
    mandate_sha = canonical_sha256(mandate)
    for document in (r1, r2, walkthrough, incident):
        if document["scope_sha256"] != scope or document["rehearsal_id"] != rehearsal or \
                document["mandate_sha256"] != mandate_sha:
            raise RehearsalError("rehearsal_identity_graph_mismatch")
    if (r2["strategy"] not in mandate["allowed_strategies"] or
            r2["r1_terminal_sha256"] != canonical_sha256(r1) or
            walkthrough["r1_terminal_sha256"] != canonical_sha256(r1) or
            walkthrough["r2_terminal_sha256"] != canonical_sha256(r2) or
            walkthrough["runbook_sha256"] != mandate["runbook_sha256"] or
            [walkthrough[name] for name in ("primary_operator", "second_operator", "implementer")] !=
            [mandate[name] for name in ("primary_operator", "second_operator", "implementer")]):
        raise RehearsalError("rehearsal_terminal_linkage_mismatch")
    if (r2["source_artifact_sha256"] != mandate["candidate_artifact_sha256"] or
            r2["source_configuration_sha256"] !=
            mandate["candidate_configuration_sha256"]):
        raise RehearsalError("r2_source_is_not_the_mandated_candidate")

    r1_started = datetime.fromisoformat(r1["started_at"].replace("Z", "+00:00"))
    r1_finished = datetime.fromisoformat(r1["finished_at"].replace("Z", "+00:00"))
    r2_started = datetime.fromisoformat(r2["started_at"].replace("Z", "+00:00"))
    r2_finished = datetime.fromisoformat(r2["finished_at"].replace("Z", "+00:00"))
    incident_at = datetime.fromisoformat(incident["performed_at"].replace("Z", "+00:00"))
    signed_at = datetime.fromisoformat(walkthrough["signed_at"].replace("Z", "+00:00"))
    if not (valid_from <= r1_started < r1_finished <= r2_started < r2_finished <=
            incident_at <= signed_at <= instant.astimezone(timezone.utc) < valid_through):
        raise RehearsalError("rehearsal_timeline_outside_mandate_or_out_of_order")

    owned = set(r1["owned_sessions"])
    pool = _proof(root, r1["pool_proof"], "r1_pool_proof",
        {"document_type", "rehearsal_id", "pool_profile", "pool_action",
         "observation_complete", "recycle_completed_at", "observed_backend_starts",
         "observed_at", "result"})
    positive = _proof(root, r1["positive_probe"], "r1_positive_probe",
        {"document_type", "rehearsal_id", "session_id", "observed_engine",
         "observed_at", "result"})
    off = _proof(root, r1["off_proof"], "r1_off_proof",
        {"document_type", "rehearsal_id", "mode", "new_owned_request_admitted",
         "cached_plan_persistence_observed", "observed_at", "result"})
    exit_proof = _proof(root, r1["backend_exit_proof"], "r1_backend_exit",
        {"document_type", "rehearsal_id", "observer_authorized", "observation_complete",
         "cluster_id", "start_id", "owned_sessions", "active_owned_sessions",
         "observed_at", "result"})
    if (not all(isinstance(item, str) for item in
                (positive["rehearsal_id"], positive["session_id"],
                 positive["observed_engine"], positive["result"],
                 off["rehearsal_id"], off["mode"], off["result"],
                 pool["rehearsal_id"], pool["pool_profile"], pool["pool_action"],
                 pool["result"], exit_proof["rehearsal_id"],
                 exit_proof["cluster_id"], exit_proof["start_id"], exit_proof["result"])) or
            not isinstance(off["new_owned_request_admitted"], bool) or
            not isinstance(off["cached_plan_persistence_observed"], bool) or
            not isinstance(exit_proof["owned_sessions"], list) or
            not all(isinstance(item, str) for item in exit_proof["owned_sessions"]) or
            not isinstance(exit_proof["active_owned_sessions"], list) or
            not all(isinstance(item, str) for item in exit_proof["active_owned_sessions"])):
        raise RehearsalError("invalid_r1_proof_value")
    observations = [_proof_time(item["observed_at"], "r1_observation")
                    for item in (positive, off, pool, exit_proof)]
    recycle_time = _proof_time(pool["recycle_completed_at"], "pool_recycle")
    if (not isinstance(pool["observed_backend_starts"], list) or
            not all(isinstance(item, str) for item in pool["observed_backend_starts"])):
        raise RehearsalError("invalid_pool_backend_observation")
    observed_pool_starts = [_proof_time(item, "pool_backend_start")
                            for item in pool["observed_backend_starts"]]
    pool_observed = observations[2]
    pool_semantics_ok = ((pool["pool_profile"] == "DIRECT" and
                          pool["pool_action"] == "NOT_APPLICABLE" and
                          observed_pool_starts == []) or
                         (pool["pool_profile"] in {"SESSION_POOL", "TRANSACTION_POOL"} and
                          pool["pool_action"] == "RECYCLED" and
                          len(observed_pool_starts) > 0 and
                          observations[1] < recycle_time < pool_observed and
                          all(recycle_time < item <= pool_observed
                              for item in observed_pool_starts)))
    if (not (r1_started < observations[0] < observations[1] < observations[2] <
             observations[3] < r1_finished) or
            pool["observation_complete"] is not True or not pool_semantics_ok or
            pool["rehearsal_id"] != rehearsal or
            pool["pool_profile"] != r1["pool_profile"] or
            pool["pool_profile"] != mandate["pool_profile"] or
            pool["pool_action"] != r1["pool_action"] or pool["result"] != "PASS" or
            positive["rehearsal_id"] != rehearsal or positive["session_id"] not in owned or
            positive["observed_engine"] != "duckdb" or positive["result"] != "PASS" or
            off["rehearsal_id"] != rehearsal or off["mode"] != "off" or
            off["new_owned_request_admitted"] is not False or off["result"] != "PASS" or
            not isinstance(off["cached_plan_persistence_observed"], bool) or
            exit_proof["rehearsal_id"] != rehearsal or
            exit_proof["observer_authorized"] is not True or
            exit_proof["observation_complete"] is not True or
            set(exit_proof["owned_sessions"]) != owned or
            exit_proof["active_owned_sessions"] != [] or exit_proof["result"] != "PASS"):
        raise RehearsalError("r1_server_retirement_not_proven")
    r1_control = _fresh_control(root, r1["fresh_control"], rehearsal, owned,
                                observations[3], r1_finished)
    if (r1_control["identity"]["cluster_id"] != exit_proof["cluster_id"] or
            r1_control["identity"]["start_id"] != exit_proof["start_id"] or
            exit_proof["cluster_id"] != mandate["target_cluster_id"] or
            exit_proof["start_id"] != mandate["target_initial_start_id"] or
            r2["before_cluster_id"] != exit_proof["cluster_id"] or
            r2["before_start_id"] != exit_proof["start_id"]):
        raise RehearsalError("r1_exit_and_fresh_control_identity_mismatch")
    if r1["result"] != "PASS":
        raise RehearsalError("r1_terminal_did_not_pass")

    source_proof = _proof(root, r2["source_identity_proof"], "r2_source_identity_proof",
        {"document_type", "rehearsal_id", "cluster_id", "start_id",
         "artifact_identity_scheme", "artifact_sha256", "configuration_sha256",
         "observed_at", "result"})
    source_time = _proof_time(source_proof["observed_at"], "r2_source_identity")
    if (source_proof["rehearsal_id"] != rehearsal or
            source_proof["cluster_id"] != r2["before_cluster_id"] or
            source_proof["start_id"] != r2["before_start_id"] or
            source_proof["artifact_identity_scheme"] != mandate["artifact_identity_scheme"] or
            source_proof["artifact_sha256"] != r2["source_artifact_sha256"] or
            source_proof["configuration_sha256"] != r2["source_configuration_sha256"] or
            source_proof["result"] != "PASS" or
            not (r2_started < source_time < r2_finished)):
        raise RehearsalError("r2_source_identity_not_proven")
    identity_proof = _proof(root, r2["identity_proof"], "r2_identity_proof",
        {"document_type", "rehearsal_id", "cluster_id", "start_id",
         "artifact_identity_scheme", "artifact_sha256", "configuration_sha256",
         "observed_at", "result"})
    identity_time = _proof_time(identity_proof["observed_at"], "r2_identity")
    if (identity_proof["rehearsal_id"] != rehearsal or
            identity_proof["cluster_id"] != r2["after_cluster_id"] or
            identity_proof["start_id"] != r2["after_start_id"] or
            identity_proof["artifact_identity_scheme"] != mandate["artifact_identity_scheme"] or
            identity_proof["artifact_sha256"] != r2["result_artifact_sha256"] or
            identity_proof["configuration_sha256"] != r2["result_configuration_sha256"] or
            identity_proof["result"] != "PASS" or
            not (source_time < identity_time < r2_finished)):
        raise RehearsalError("r2_approved_identity_not_proven")
    r2_control = _fresh_control(root, r2["fresh_control"], rehearsal, owned,
                                identity_time, r2_finished)
    if (r2_control["identity"]["cluster_id"] != r2["after_cluster_id"] or
            r2_control["identity"]["start_id"] != r2["after_start_id"] or
            r2["after_cluster_id"] != mandate["target_cluster_id"] or
            r2["result"] != "PASS"):
        raise RehearsalError("r2_fresh_control_or_terminal_failed")
    if (r1["fresh_control"]["path"] == r2["fresh_control"]["path"] or
            r1["fresh_control"]["sha256"] == r2["fresh_control"]["sha256"] or
            (r1_control["identity"]["pid"], r1_control["identity"]["backend_start"]) ==
            (r2_control["identity"]["pid"], r2_control["identity"]["backend_start"])):
        raise RehearsalError("r1_and_r2_fresh_controls_are_not_distinct")
    if (r2["result_artifact_sha256"] != mandate["approved_artifact_sha256"] or
            r2["result_configuration_sha256"] != mandate["approved_configuration_sha256"]):
        raise RehearsalError("recovery_did_not_restore_approved_identity")
    if r2["strategy"] == "VERIFIED_ENVIRONMENT_RESTORE" and \
            r2["restore_source_sha256"] != mandate["approved_restore_source_sha256"]:
        raise RehearsalError("restore_source_not_approved")

    backend = _identity(incident["poisoned_backend"], "poisoned_backend")
    incident_exit = _proof(root, incident["backend_exit_proof"], "incident_backend_exit",
        {"document_type", "rehearsal_id", "identity", "observer_authorized",
         "observation_complete", "observation_cluster_id", "observation_start_id",
         "backend_present", "bystander_identity", "bystander_observed_before",
         "bystander_present", "isolation_started_at", "observed_at", "result"})
    bystander = _identity(incident_exit["bystander_identity"], "incident_bystander")
    observed_at = _proof_time(incident_exit["observed_at"], "incident_exit")
    isolation_at = _proof_time(incident_exit["isolation_started_at"], "incident_isolation")
    physical = lambda item: (item["cluster_id"], item["start_id"], item["pid"],
                             item["backend_start"])
    backend_started = _proof_time(backend["backend_start"], "poisoned_backend_start")
    bystander_started = _proof_time(bystander["backend_start"], "bystander_backend_start")
    r2_control_time = _proof_time(r2_control["connected_at"], "r2_control")
    if (_identity(incident_exit["identity"], "incident_exit") != backend or
            physical(backend) == physical(bystander) or
            physical(backend) == physical(r2_control["identity"]) or
            backend["cluster_id"] != r2["after_cluster_id"] or
            backend["start_id"] != r2["after_start_id"] or
            incident_exit["observation_cluster_id"] != r2["after_cluster_id"] or
            incident_exit["observation_start_id"] != r2["after_start_id"] or
            bystander["cluster_id"] != r2["after_cluster_id"] or
            bystander["start_id"] != r2["after_start_id"] or
            incident_exit["rehearsal_id"] != rehearsal or
            incident_exit["observer_authorized"] is not True or
            incident_exit["observation_complete"] is not True or
            not (r2_control_time < backend_started <= isolation_at < observed_at <= incident_at) or
            not (r2_control_time < bystander_started <= isolation_at) or
            incident_exit["backend_present"] is not False or
            incident_exit["bystander_observed_before"] is not True or
            incident_exit["bystander_present"] is not True or
            incident_exit["result"] != "PASS" or incident["result"] != "PASS"):
        raise RehearsalError("incident_exit_or_handoff_not_proven")
    if walkthrough["result"] != "PASS":
        raise RehearsalError("second_operator_walkthrough_did_not_pass")

    limitations = set(RECOVERY_LIMITATIONS)
    if off["cached_plan_persistence_observed"] is True:
        limitations.add("CACHED_PLAN_PERSISTENCE_OBSERVED")
    return {"schema_version": 1, "contract_type": "recovery_validation",
        "contract_id": f"{rehearsal}-validation", "scope_sha256": scope,
        "rehearsal_id": rehearsal, "mandate_sha256": mandate_sha,
        "r1_terminal_sha256": canonical_sha256(r1),
        "r2_terminal_sha256": canonical_sha256(r2),
        "walkthrough_sha256": canonical_sha256(walkthrough),
        "incident_sha256": canonical_sha256(incident),
        "checked_at": instant.astimezone(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "result": "PASS", "decision": "ELIGIBLE_FOR_HUMAN_GRANT",
        "checks": CHECKS, "limitations": sorted(limitations)}


def build_recovery_record(validation: dict[str, Any], r2: dict[str, Any],
                          walkthrough: dict[str, Any]) -> dict[str, Any]:
    """Build the linked aggregate; this is evidence, not a gate receipt."""
    if validation.get("contract_type") != "recovery_validation" or \
            validation.get("result") != "PASS":
        raise RehearsalError("recovery_validation_did_not_pass")
    return {"schema_version": 1, "contract_type": "recovery",
        "contract_id": f"{validation['rehearsal_id']}-recovery",
        "scope_sha256": validation["scope_sha256"],
        "rehearsal_id": validation["rehearsal_id"], "strategy": r2["strategy"],
        "artifact_sha256": r2["result_artifact_sha256"],
        "configuration_sha256": r2["result_configuration_sha256"],
        "r1_terminal_sha256": validation["r1_terminal_sha256"],
        "r2_terminal_sha256": validation["r2_terminal_sha256"],
        "walkthrough_sha256": canonical_sha256(walkthrough),
        "validation_sha256": canonical_sha256(validation),
        "rehearsal_result": "PASS", "eligibility": "ELIGIBLE_FOR_HUMAN_GRANT",
        "limitations": validation["limitations"],
        "state": {"lifecycle": "VALIDATED", "technical_result": "PASS",
            "evidence_completeness": "COMPLETE", "exit_state": "CLEAN",
            "value_outcome": "NOT_EVALUATED", "project_decision": "UNDECIDED"}}


def write_validation(path: Path, validation: dict[str, Any]) -> None:
    """Create one private validation receipt without replacing existing evidence."""
    validate_contract(validation)
    parent = path.parent.resolve(strict=True)
    if parent.stat().st_uid != os.getuid() or stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise RehearsalError("invalid_validation_output_parent")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent_fd)
    except Exception:
        os.close(parent_fd)
        raise
    try:
        payload = canonical_bytes(validation)
        if os.write(descriptor, payload) != len(payload):
            raise RehearsalError("validation_receipt_short_write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        os.fsync(parent_fd)
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate operator-produced recovery evidence")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        validation = validate_rehearsal_bundle(arguments.bundle)
        if arguments.output is not None:
            write_validation(arguments.output, validation)
        print(json.dumps({"decision": validation["decision"], "result": validation["result"]},
                         sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, RehearsalError, ContractError) as exc:
        print(json.dumps({"decision": "REFUSED", "reason": str(exc), "result": "FAIL"},
                         sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
