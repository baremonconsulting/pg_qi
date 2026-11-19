#!/usr/bin/env python3
"""Fail-closed offline closeout for the bounded PGQI-5 internal pilot."""

from __future__ import annotations

import argparse
import fcntl
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
    from .contract_rules import (ContractError, INTERNAL_PILOT_LIMITATIONS,
        aggregate_pass, canonical_bytes, canonical_sha256, export_receipt_matches,
        resolve_bundle_path,
        strict_load_path, validate_contract, verify_artifact)
    from .canary_runtime import CanaryError, validate_canary_bundle
    from .evidence_tool import EvidenceError, validate_private, validate_shared
    from .measurement_runtime import MeasurementError, validate_level_authorization
except ImportError:
    from contract_rules import (ContractError, INTERNAL_PILOT_LIMITATIONS,
        aggregate_pass, canonical_bytes, canonical_sha256, export_receipt_matches,
        resolve_bundle_path,
        strict_load_path, validate_contract, verify_artifact)
    from canary_runtime import CanaryError, validate_canary_bundle
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
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077 or
                    info.st_size != value["bytes"] or info.st_size > 1024 * 1024):
                raise CloseoutError(f"invalid_{kind}_artifact")
            payload = os.read(descriptor, info.st_size + 1)
        finally:
            os.close(descriptor)
        if (len(payload) != value["bytes"] or
                hashlib.sha256(payload).hexdigest() != value["sha256"]):
            raise CloseoutError(f"invalid_{kind}_artifact")
        parsed = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=lambda _value: (_ for _ in ()).throw(
                                CloseoutError(f"invalid_{kind}_number")))
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError,
            KeyError, TypeError) as exc:
        raise CloseoutError(f"invalid_{kind}_artifact") from exc
    if not isinstance(parsed, dict) or parsed.get("document_type") != kind:
        raise CloseoutError(f"invalid_{kind}_artifact")
    return parsed


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise CloseoutError("duplicate_pilot_key")
        result[key] = value
    return result


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CloseoutError("invalid_pilot_time") from exc
    if parsed.tzinfo is None:
        raise CloseoutError("invalid_pilot_time")
    return parsed.astimezone(timezone.utc)


def _ledger(root: Path, plan: dict[str, Any], supplied: Path | None = None) -> Path:
    configured = Path(plan["attempt_ledger"]["path"])
    if supplied is not None and supplied.absolute() != configured:
        raise CloseoutError("pilot_attempt_ledger_path_mismatch")
    resolved = configured.resolve(strict=True)
    root_resolved = root.resolve(strict=True)
    info = configured.stat()
    if (configured.is_symlink() or resolved != configured or not configured.is_dir() or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or
            info.st_dev != plan["attempt_ledger"]["device"] or
            info.st_ino != plan["attempt_ledger"]["inode"] or
            resolved == root_resolved or root_resolved in resolved.parents or
            resolved in root_resolved.parents):
        raise CloseoutError("pilot_attempt_ledger_identity_mismatch")
    try:
        verify_artifact(resolved, plan["attempt_ledger"]["identity"])
    except ContractError as exc:
        raise CloseoutError("pilot_attempt_ledger_identity_mismatch") from exc
    return resolved


def _attempt_records(ledger: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    identity_name = plan["attempt_ledger"]["identity"]["path"]
    for path in sorted(ledger.iterdir()):
        if path.name == identity_name:
            continue
        if re.fullmatch(r"dispatch-[0-9]{4}\.json", path.name):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise CloseoutError("unexpected_pilot_attempt_ledger_entry")
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or
                        info.st_size > 65536):
                    raise CloseoutError("unsafe_pilot_attempt_record")
                payload = os.read(descriptor, info.st_size + 1)
            finally:
                os.close(descriptor)
            record = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    CloseoutError("invalid_pilot_attempt_number")))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CloseoutError("invalid_pilot_attempt_record") from exc
        expected = {"schema_version", "document_type", "record_id", "pilot_id",
            "sequence", "scope_sha256", "plan_sha256", "operation_go_sha256",
            "previous_record_sha256", "window_id", "level", "run_id", "consumed_at"}
        if (not isinstance(record, dict) or set(record) != expected or
                record["schema_version"] != 1 or
                record["document_type"] != "internal_pilot_attempt" or
                record["pilot_id"] != plan["pilot_id"] or
                record["scope_sha256"] != plan["scope_sha256"] or
                record["plan_sha256"] != canonical_sha256(plan) or
                not isinstance(record["sequence"], int) or isinstance(record["sequence"], bool) or
                record["sequence"] < 1 or not isinstance(record["level"], int) or
                isinstance(record["level"], bool) or record["level"] < 1 or
                not isinstance(record["consumed_at"], str)):
            raise CloseoutError("invalid_pilot_attempt_record")
        if (path.name != f"attempt-{record['sequence']:04d}.json" or
                not isinstance(record["record_id"], str) or
                record["record_id"] !=
                f"{plan['pilot_id']}-attempt-{record['sequence']:04d}-{record['run_id']}" or
                not isinstance(record["window_id"], str) or not record["window_id"] or
                not isinstance(record["run_id"], str) or not record["run_id"] or
                not isinstance(record["previous_record_sha256"], str) or
                len(record["previous_record_sha256"]) != 64):
            raise CloseoutError("invalid_pilot_attempt_record")
        _time(record["consumed_at"])
        records.append(record)
    if [item["sequence"] for item in records] != list(range(1, len(records) + 1)) or \
            len({item["record_id"] for item in records}) != len(records) or \
            len({item["run_id"] for item in records}) != len(records):
        raise CloseoutError("pilot_attempt_history_invalid")
    previous_digest = "0" * 64
    for record in records:
        if record["previous_record_sha256"] != previous_digest:
            raise CloseoutError("pilot_attempt_hash_chain_invalid")
        previous_digest = canonical_sha256(record)
    return records


def _dispatch_record(ledger: Path, attempt: dict[str, Any]) -> dict[str, Any]:
    path = ledger / f"dispatch-{attempt['sequence']:04d}.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or
                    info.st_size > 65536):
                raise CloseoutError("unsafe_pilot_dispatch_record")
            payload = os.read(descriptor, info.st_size + 1)
        finally:
            os.close(descriptor)
        marker = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                CloseoutError("invalid_pilot_dispatch_number")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CloseoutError("invalid_pilot_dispatch_record") from exc
    expected = {"schema_version", "document_type", "attempt_sha256", "window_id",
                "level", "run_id", "dispatched_at"}
    if (not isinstance(marker, dict) or set(marker) != expected or
            marker["schema_version"] != 1 or
            marker["document_type"] != "internal_pilot_dispatch" or
            marker["attempt_sha256"] != canonical_sha256(attempt) or
            (marker["window_id"], marker["level"], marker["run_id"]) !=
            (attempt["window_id"], attempt["level"], attempt["run_id"])):
        raise CloseoutError("invalid_pilot_dispatch_record")
    _time(marker["dispatched_at"])
    return marker


def consume_attempt(root: Path, ledger_path: Path, window_id: str, level: int,
                    run_id: str, now: datetime | None = None,
                    previous_window_path: str | None = None) -> dict[str, Any]:
    """Durably consume one attempt before any measurement dispatch."""
    root = _root(root)
    plan = _load(root, "internal-pilot-plan.json", "internal_pilot_plan")
    ledger = _ledger(root, plan, ledger_path)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise CloseoutError("attempt_clock_is_not_utc_aware")
    approved = next((item for item in plan["approved_windows"]
                     if item["window_id"] == window_id and item["level"] == level), None)
    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
    if (approved is None or not isinstance(run_id, str) or
            not identifier.fullmatch(run_id) or not isinstance(window_id, str) or
            not identifier.fullmatch(window_id)):
        raise CloseoutError("attempt_outside_approved_windows")
    lock_fd = os.open(ledger / plan["attempt_ledger"]["identity"]["path"],
                      os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing = _attempt_records(ledger, plan)
        if any(item["run_id"] == run_id for item in existing):
            raise CloseoutError("pilot_run_id_reuse_refused")
        if existing and existing[-1]["level"] >= level:
            raise CloseoutError("pilot_level_retry_or_reorder_refused")
        if level != len(existing) + 1:
            raise CloseoutError("pilot_attempt_level_gap")
        operation_go = _artifact(root, plan["operation_go"], "approval_receipt")
        instant_utc = instant.astimezone(timezone.utc)
        if not (_time(operation_go["valid_from"]) <= instant_utc <
                _time(operation_go["valid_through"])):
            raise CloseoutError("pilot_attempt_outside_operation_window")
        if level == 1 and previous_window_path is not None:
            raise CloseoutError("level_one_attempt_has_previous_window")
        if level > 1:
            if previous_window_path is None:
                raise CloseoutError("next_level_attempt_requires_previous_window")
            previous_window = _load(root, previous_window_path, "internal_pilot_window")
            previous_attempt = existing[-1]
            if (previous_window["result"] != "PASS" or
                    previous_window["scope_sha256"] != plan["scope_sha256"] or
                    previous_window["pilot_id"] != plan["pilot_id"] or
                    previous_window["plan_sha256"] != canonical_sha256(plan) or
                    (previous_window["window_id"], previous_window["level"],
                     previous_window["run_id"]) !=
                    (previous_attempt["window_id"], previous_attempt["level"],
                     previous_attempt["run_id"]) or
                    previous_window["attempt_record_sha256"] !=
                    canonical_sha256(previous_attempt)):
                raise CloseoutError("previous_window_not_eligible_for_next_attempt")
        sequence = len(existing) + 1
        previous_digest = canonical_sha256(existing[-1]) if existing else "0" * 64
        record = {"schema_version": 1, "document_type": "internal_pilot_attempt",
            "record_id": f"{plan['pilot_id']}-attempt-{sequence:04d}-{run_id}",
            "pilot_id": plan["pilot_id"], "sequence": sequence,
            "scope_sha256": plan["scope_sha256"], "plan_sha256": canonical_sha256(plan),
            "operation_go_sha256": plan["operation_go"]["sha256"],
            "previous_record_sha256": previous_digest,
            "window_id": window_id, "level": level, "run_id": run_id,
            "consumed_at": instant_utc.isoformat().replace("+00:00", "Z")}
        filename = f"attempt-{sequence:04d}.json"
        descriptor = os.open(ledger / filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            payload = canonical_bytes(record)
            os.write(descriptor, payload); os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(ledger, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return record
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)


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
    try:
        canary_result = validate_canary_bundle(
            resolve_bundle_path(root, Path(plan["canary_bundle_path"])),
            _time(canary_acceptance["approved_at"]))
    except (CanaryError, ContractError, OSError) as exc:
        raise CloseoutError("pilot_canary_recomputation_failed") from exc
    if (operation_go["receipt_kind"] != "OPERATION_GO" or
            operation_go["scope_sha256"] != scope or
            canary_validation["scope_sha256"] != scope or
            canary_validation["result"] != "PASS" or
            canary_validation["decision"] != "ELIGIBLE_FOR_HUMAN_GRANT" or
            canary_acceptance["gate_id"] != "CANARY_ACCEPTED" or
            canary_acceptance["scope_sha256"] != scope or
            canary_acceptance["evidence_sha256"] != canonical_sha256(canary_validation) or
            canary_result["result"] != "PASS" or
            canary_result["decision"] != "ELIGIBLE_FOR_HUMAN_GRANT" or
            canary_result["scope_sha256"] != scope):
        raise CloseoutError("pilot_authority_invalid")
    if ({key: value for key, value in canary_result.items() if key != "checked_at"} !=
            {key: value for key, value in canary_validation.items() if key != "checked_at"}):
        raise CloseoutError("accepted_canary_differs_from_recomputed_canary")
    levels = measurement_plan["concurrency_levels"]
    approved = plan["approved_windows"]
    if (len(approved) != len(levels) or
            [item["level"] for item in approved] != [item["level"] for item in levels]):
        raise CloseoutError("pilot_does_not_cover_frozen_levels")
    if not closeout["windows"] or len(closeout["windows"]) > len(approved):
        raise CloseoutError("pilot_window_count_mismatch")
    ledger = _ledger(root, plan)
    attempts = _attempt_records(ledger, plan)
    dispatch_names = {path.name for path in ledger.glob("dispatch-*.json")}
    expected_dispatch_names = {f"dispatch-{item['sequence']:04d}.json" for item in attempts}
    if dispatch_names != expected_dispatch_names:
        raise CloseoutError("pilot_dispatch_history_mismatch")
    dispatches = [_dispatch_record(ledger, attempt) for attempt in attempts]
    if len(attempts) != len(closeout["windows"]):
        raise CloseoutError("pilot_attempt_history_mismatch")

    checked_input = now or datetime.now(timezone.utc)
    if checked_input.tzinfo is None:
        raise CloseoutError("validation_clock_is_not_utc_aware")
    checked = checked_input.astimezone(timezone.utc)
    recorded_checked = _time(closeout["checked_at"])
    if recorded_checked > checked:
        raise CloseoutError("closeout_checked_at_is_in_the_future")
    go_start = _time(operation_go["valid_from"])
    go_end = _time(operation_go["valid_through"])
    acceptance_time = _time(canary_acceptance["approved_at"])
    if _time(canary_validation["checked_at"]) > acceptance_time or go_start <= acceptance_time:
        raise CloseoutError("pilot_requires_new_operation_window")
    windows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    private_results: list[dict[str, Any]] = []
    retirements: list[dict[str, Any]] = []
    retired_results: list[bool] = []
    pilot_session_ids: set[str] = set()
    fresh_session_ids: set[str] = set()
    prior_finish: datetime | None = None
    first_start: datetime | None = None
    run_ids: set[str] = set()
    previous_window: dict[str, Any] | None = None
    for expected, attempt, dispatch, artifact in zip(
            approved, attempts, dispatches, closeout["windows"]):
        window = _artifact(root, artifact, "internal_pilot_window")
        if (window["scope_sha256"] != scope or window["pilot_id"] != plan["pilot_id"] or
                window["plan_sha256"] != plan_sha or
                window["window_id"] != expected["window_id"] or
                window["level"] != expected["level"] or
                window["private_bundle_path"] != expected["private_bundle_path"] or
                window["level_authorization"] != expected["level_authorization"]):
            raise CloseoutError("pilot_window_identity_mismatch")
        if ((attempt["window_id"], attempt["level"], attempt["run_id"]) !=
                (window["window_id"], window["level"], window["run_id"]) or
                attempt["operation_go_sha256"] != plan["operation_go"]["sha256"] or
                window["attempt_record_sha256"] != canonical_sha256(attempt)):
            raise CloseoutError("pilot_attempt_window_mismatch")
        if window["run_id"] in run_ids:
            raise CloseoutError("pilot_run_history_overwritten")
        run_ids.add(window["run_id"])
        started, finished = _time(window["started_at"]), _time(window["finished_at"])
        if first_start is None:
            first_start = started
        if not (go_start <= started < finished <= min(go_end, checked)) or \
                (prior_finish is not None and started < prior_finish):
            raise CloseoutError("pilot_window_timeline_invalid")
        consumed_at = _time(attempt["consumed_at"])
        dispatched_at = _time(dispatch["dispatched_at"])
        if (consumed_at > dispatched_at or dispatched_at != started or
                consumed_at < go_start or
                (prior_finish is not None and consumed_at < prior_finish)):
            raise CloseoutError("pilot_attempt_not_consumed_before_dispatch")
        prior_finish = finished

        authorization = _raw_artifact(root, expected["level_authorization"],
                                      "measurement_level_authorization")
        try:
            authorized_level = validate_level_authorization(authorization,
                measurement_plan, canonical_sha256(measurement_plan), scope, root, started,
                previous_evidence_root=(None if previous_window is None else
                    resolve_bundle_path(root, Path(previous_window["private_bundle_path"]))),
                previous_path_prefix=(None if previous_window is None else
                    previous_window["private_bundle_path"]))
        except (MeasurementError, ContractError, OSError) as exc:
            raise CloseoutError("pilot_level_authorization_invalid") from exc
        if authorized_level["level"] != window["level"]:
            raise CloseoutError("pilot_level_authorization_mismatch")
        if authorization.get("operation_go") != plan["operation_go"]:
            raise CloseoutError("pilot_operation_go_substitution")
        if previous_window is None:
            if authorization.get("previous_level") is not None:
                raise CloseoutError("level_one_has_previous_pilot_evidence")
        else:
            if previous_window["result"] != "PASS":
                raise CloseoutError("pilot_continued_after_nonpassing_window")
            previous = authorization.get("previous_level")
            if (not isinstance(previous, dict) or
                    previous.get("level") != previous_window["level"] or
                    previous.get("report") != previous_window["measurement_report"] or
                    previous.get("retirement") != previous_window["retirement"] or
                    previous.get("validation") != previous_window["private_validation_receipt"]):
                raise CloseoutError("previous_level_not_bound_to_pilot_window")
            previous_root = Path(previous_window["private_bundle_path"])
            for name in ("index", "terminal", "report", "retirement"):
                path = previous.get(name, {}).get("path") if isinstance(previous.get(name), dict) else None
                if not isinstance(path, str) or not Path(path).is_relative_to(previous_root):
                    raise CloseoutError("previous_level_outside_pilot_bundle")

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
        private_receipt = _artifact(root, window["private_validation_receipt"], "validation")
        private_index = _load(private, "index.json", "index")
        private_terminal = _load(private, "terminal.json", "terminal")
        stop_proof = _artifact(root, window["dispatch_stop_proof"],
                               "pilot_dispatch_stop")
        off_proof = _artifact(root, window["fresh_off_proof"], "pilot_fresh_off")
        if (report["scope_sha256"] != scope or
                report["plan_sha256"] != canonical_sha256(measurement_plan) or
                report["level"] != window["level"] or report["run_id"] != window["run_id"] or
                report.get("pilot_attempt_sha256") != canonical_sha256(attempt) or
                report.get("pilot_window_id") != window["window_id"] or
                report.get("pilot_dispatch_sha256") != canonical_sha256(dispatch) or
                report.get("pilot_dispatched_at") != dispatch["dispatched_at"] or
                retirement["scope_sha256"] != scope or
                retirement["run_id"] != window["run_id"]):
            raise CloseoutError("pilot_window_evidence_mismatch")
        if (private_receipt["scope_sha256"] != scope or
                private_receipt["profile"] != "private_full" or
                private_receipt["result"] != private_result["result"] or
                private_receipt["index_sha256"] != canonical_sha256(private_index) or
                private_receipt["terminal_sha256"] != canonical_sha256(private_terminal)):
            raise CloseoutError("private_validation_receipt_mismatch")
        private_scope = private_result.get("scope", {})
        if private_scope.get("scope_inputs", {}).get("measurement_plan", {}).get("sha256") != \
                plan["measurement_plan"]["sha256"]:
            raise CloseoutError("pilot_measurement_plan_outside_private_scope")
        identity = (plan["pilot_id"], window["window_id"], window["run_id"])
        if ((stop_proof["pilot_id"], stop_proof["window_id"], stop_proof["run_id"]) !=
                identity or stop_proof["scope_sha256"] != scope):
            raise CloseoutError("dispatch_stop_proof_invalid")
        owned_sessions = set(retirement["owned_sessions"])
        if owned_sessions & fresh_session_ids:
            raise CloseoutError("pilot_session_reused_a_fresh_control_identity")
        if ((off_proof["pilot_id"], off_proof["window_id"], off_proof["run_id"]) !=
                identity or not isinstance(off_proof["session_id"], str) or
                not off_proof["session_id"] or
                off_proof["session_id"] in owned_sessions | pilot_session_ids | fresh_session_ids or
                off_proof["scope_sha256"] != scope or
                (off_proof["pg_qi_enabled"], off_proof["routing_state"],
                 off_proof["duckdb_force_execution"]) != ("off", "off", "off")):
            raise CloseoutError("fresh_off_proof_invalid")
        stopped_at = _time(stop_proof["stopped_at"])
        off_at = _time(off_proof["observed_at"])
        if not (started <= stopped_at <= off_at <= finished):
            raise CloseoutError("window_shutdown_timeline_invalid")
        retired = (retirement["backend_absence_verified"] is True and
                   len(retirement["owned_sessions"]) >= authorized_level["sessions"] and
                   retirement["cached_plans_retired"] is True and
                   retirement["default_off_reconnect"] is True and
                   aggregate_pass(retirement["state"]))
        if not retired:
            raise CloseoutError("pilot_retirement_unverified")
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
        retired_results.append(retired); pilot_session_ids.update(owned_sessions)
        fresh_session_ids.add(off_proof["session_id"])
        previous_window = window

    if prior_finish is None or recorded_checked < prior_finish or \
            acceptance_time > (first_start or prior_finish):
        raise CloseoutError("pilot_closeout_or_acceptance_timeline_invalid")

    full_scope = len(windows) == len(approved)
    if (not full_scope and windows[-1]["result"] == "PASS" and
            not any(item["status"] == "OPEN" for item in closeout["hard_stop_findings"])):
        raise CloseoutError("pilot_prefix_stopped_without_terminal_failure")
    technical_pass = full_scope and all(item["result"] == "PASS" for item in windows)
    decisions = closeout["decisions"]
    closed_evidence: set[str] = set()
    closed_receipts: set[str] = set()
    for finding in closeout["hard_stop_findings"]:
        try:
            verify_artifact(root, finding["evidence"])
        except ContractError as exc:
            raise CloseoutError("hard_stop_evidence_invalid") from exc
        if finding["status"] == "CLOSED":
            approval = _artifact(root, finding["closure_approval"], "gate_receipt")
            evidence_digest = finding["evidence"]["sha256"]
            approval_digest = finding["closure_approval"]["sha256"]
            if (approval["gate_id"] != "HARD_STOP_CLOSED" or
                    approval.get("subject_id") != finding["finding_id"] or
                    approval["scope_sha256"] != scope or
                    approval["evidence_sha256"] != evidence_digest or
                    not prior_finish <= _time(approval["approved_at"]) <= recorded_checked or
                    evidence_digest in closed_evidence or
                    approval_digest in closed_receipts):
                raise CloseoutError("hard_stop_closure_not_authorized")
            closed_evidence.add(evidence_digest)
            closed_receipts.add(approval_digest)
    open_hard_stop = any(item["status"] == "OPEN"
                         for item in closeout["hard_stop_findings"])
    safety = ("FAIL" if open_hard_stop or
              any(item["technical_result"] == "FAIL" for item in reports) else
              "INCOMPLETE" if any(item["technical_result"] == "INCOMPLETE"
                                  for item in reports) else "PASS")
    evidence = ("FAIL" if any(item["terminal"]["state"]["evidence_completeness"] == "INVALID"
                              for item in private_results) else
                "INCOMPLETE" if any(item["terminal"]["state"]["evidence_completeness"] !=
                                    "COMPLETE" for item in private_results) else "PASS")
    exit_decision = "PASS" if all(retired_results) else "INCOMPLETE"
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
    if (closeout["retention"]["status"] == "PENDING" and
            _time(closeout["retention"]["due_at"]) < checked):
        raise CloseoutError("retention_commitment_overdue")
    if closeout["retention"]["evidence"] is not None:
        retention_evidence = _artifact(root, closeout["retention"]["evidence"],
                                       "pilot_retention")
        if (retention_evidence["scope_sha256"] != scope or
                retention_evidence["pilot_id"] != plan["pilot_id"] or
                retention_evidence["owner"] != plan["retention_owner"] or
                _time(retention_evidence["performed_at"]) > recorded_checked):
            raise CloseoutError("retention_evidence_invalid")

    if closeout["share_release"] is None:
        if closeout["shared_derived_paths"] or closeout["shared_release_state"] != "NOT_REQUESTED":
            raise CloseoutError("shared_evidence_without_release")
    else:
        release = _artifact(root, closeout["share_release"], "export_receipt")
        if len(closeout["shared_derived_paths"]) != 1 or \
                release["receipt_kind"] != "SHARE_RELEASE" or \
                release["scope_sha256"] != scope or \
                _time(release["approved_at"]) > recorded_checked or \
                not closeout["shared_derived_paths"] or \
                closeout["shared_release_state"] != "RELEASED":
            raise CloseoutError("share_release_invalid")
        for item in closeout["shared_derived_paths"]:
            shared = resolve_bundle_path(root, Path(item["bundle_path"]))
            receipt = resolve_bundle_path(root, Path(item["private_validation_receipt"]))
            try:
                shared_result = validate_shared(shared, receipt)
            except (EvidenceError, ContractError, OSError) as exc:
                raise CloseoutError("shared_derived_validation_failed") from exc
            if shared_result["scope_sha256"] != scope:
                raise CloseoutError("shared_derived_scope_mismatch")
            receipt_contract = _load(root, item["private_validation_receipt"], "validation")
            # The receipt must validate the last recorded pilot window, not an unrelated scope peer.
            if item["private_validation_receipt"] != \
                    windows[-1]["private_validation_receipt"]["path"]:
                raise CloseoutError("shared_bundle_not_derived_from_pilot_window")
            manifest = json.loads((shared / "manifest.json").read_text("utf-8"),
                                  object_pairs_hook=_pairs)
            index = _load(shared, "index.json", "index")
            if not export_receipt_matches(release, "SHARE_RELEASE", scope,
                    canonical_sha256(index), canonical_sha256(receipt_contract),
                    canonical_sha256(manifest)):
                raise CloseoutError("share_release_not_bound_to_shared_bundle")
            if _time(release["approved_at"]) < prior_finish:
                raise CloseoutError("share_release_predates_pilot_evidence")

    expected_result = "PASS" if technical_pass and not open_hard_stop else \
        "FAIL" if open_hard_stop or any(item["result"] == "FAIL" for item in windows) else \
        "INCOMPLETE"
    if closeout["result"] != expected_result or \
            (expected_result == "PASS") != (closeout["eligibility"] ==
                                             "ELIGIBLE_FOR_HUMAN_GRANT"):
        raise CloseoutError("closeout_result_false_pass")
    if closeout["limitations"] != sorted(INTERNAL_PILOT_LIMITATIONS):
        raise CloseoutError("closeout_limitations_not_canonical")
    if decisions["project_decision"] == "CONTINUE" and (not technical_pass or
            (safety, evidence, exit_decision) != ("PASS", "PASS", "PASS")):
        raise CloseoutError("continue_requires_clean_core_decisions")
    return {"result": expected_result,
            "decision": "ELIGIBLE_FOR_HUMAN_GRANT" if expected_result == "PASS" else "REFUSED",
            "project_decision": decisions["project_decision"],
            "window_count": len(windows), "scope_sha256": scope,
            "pilot_id": plan["pilot_id"], "checked_at": checked.isoformat()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--consume-attempt", action="store_true")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--window-id")
    parser.add_argument("--level", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--previous-window")
    args = parser.parse_args(argv)
    try:
        if args.consume_attempt:
            if (args.ledger is None or args.window_id is None or args.level is None or
                    args.run_id is None or args.output is not None):
                raise CloseoutError("invalid_consume_attempt_arguments")
            record = consume_attempt(args.bundle, args.ledger, args.window_id,
                                     args.level, args.run_id,
                                     previous_window_path=args.previous_window)
            print(json.dumps({"record_id": record["record_id"], "result": "CONSUMED"},
                             sort_keys=True, separators=(",", ":")))
            return 0
        if args.output is None or any(item is not None for item in
                (args.ledger, args.window_id, args.level, args.run_id,
                 args.previous_window)):
            raise CloseoutError("invalid_closeout_arguments")
        result = validate_internal_pilot(args.bundle)
        payload = canonical_bytes({"schema_version": 1,
            "document_type": "internal_pilot_validation", **result})
        descriptor = os.open(args.output,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
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
