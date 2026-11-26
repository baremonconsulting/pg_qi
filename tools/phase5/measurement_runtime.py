#!/usr/bin/env python3
"""Frozen, fail-closed measurement orchestration for the PGQI-5 pilot."""

from __future__ import annotations

import hashlib
import fcntl
import math
import os
import re
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

try:
    from .contract_rules import (ContractError, MEASUREMENT_LIMITATIONS,
        canonical_bytes, canonical_sha256, strict_load_bytes, validate_contract,
        verify_artifact, verify_seal)
    from .control_runtime import ResourcePolicy, StopController
    from .recovery_rehearsal import RehearsalError, validate_rehearsal_contracts
except ImportError:  # direct script/tool execution
    from contract_rules import (ContractError, MEASUREMENT_LIMITATIONS,
        canonical_bytes, canonical_sha256, strict_load_bytes, validate_contract,
        verify_artifact, verify_seal)
    from control_runtime import ResourcePolicy, StopController
    from recovery_rehearsal import RehearsalError, validate_rehearsal_contracts

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class MeasurementError(RuntimeError):
    """Fixed-message refusal or measurement error."""


_CORRECTNESS_TOKEN = object()


@dataclass(frozen=True)
class CorrectnessGate:
    """Opaque result of a complete private correctness validation."""

    scope_sha256: str
    plan_sha256: str
    oracle_cases: dict[str, str]
    qualification_coverage: dict[str, Any]
    frequency_weights: dict[str, int] | None
    baseline_weights: dict[str, int] | None
    _token: object


def validate_correctness_bundle(root: Path, scope_sha256: str,
                                plan: dict[str, Any], plan_sha256: str) -> CorrectnessGate:
    """Re-run the offline validator; assertions or receipts alone are insufficient."""
    try:
        from .evidence_tool import validate_private
    except ImportError:
        from evidence_tool import validate_private
    validated = validate_private(root, scope_sha256)
    if (validated["result"] != "PASS" or canonical_sha256(plan) != plan_sha256 or
            validated["scope"]["scope_inputs"]["measurement_plan"]["sha256"] !=
            plan_sha256):
        raise MeasurementError("correctness_bundle_did_not_pass")
    cases = validated["oracle_cases"]
    if any(cases.get(scenario["oracle_case_id"]) != "PASS"
           for scenario in plan["required_scenarios"]):
        raise MeasurementError("correctness_gate_not_passed")
    return CorrectnessGate(scope_sha256, plan_sha256, dict(cases),
                           dict(validated["qualification_coverage"]),
                           validated["frequency_weights"], validated["baseline_weights"],
                           _CORRECTNESS_TOKEN)


class MeasurementSession(Protocol):
    """One already-preflighted, mode-fixed, independently owned session."""

    def execute(self, scenario: dict[str, Any]) -> dict[str, Any]: ...
    def retire(self) -> bool: ...


class AuditedDispatcher(Protocol):
    """The narrow existing audited-session surface used by the adapter."""

    def dispatch(self, case_id: str) -> dict[str, Any]: ...
    def prepare(self, prepared: str, case_id: str) -> dict[str, Any]: ...
    def execute_prepared(self, prepared: str) -> dict[str, Any]: ...
    def close(self) -> None: ...


class AuditedMeasurementSession:
    """Adapt one audited driver session to the frozen measurement protocol."""

    def __init__(self, dispatcher: AuditedDispatcher, output_root: Path,
                 oracle: Callable[[dict[str, Any], Path], str]) -> None:
        if (output_root.resolve(strict=True) != output_root.absolute() or
                output_root.stat().st_uid != os.getuid() or
                stat.S_IMODE(output_root.stat().st_mode) != 0o700):
            raise MeasurementError("invalid_session_output_root")
        self._dispatcher = dispatcher
        self._output_root = output_root
        self._oracle = oracle
        self._prepared: set[str] = set()
        self._retired = False

    def execute(self, scenario: dict[str, Any]) -> dict[str, Any]:
        if self._retired or scenario["protocol"] != "extended":
            raise MeasurementError("unsupported_measurement_protocol")
        case_id = scenario["case_id"]
        if scenario["cache_state"] == "first_use":
            response = self._dispatcher.dispatch(case_id)
        elif scenario["cache_state"] == "warm_cached":
            prepared = "m_" + hashlib.sha256(
                scenario["scenario_id"].encode("utf-8")).hexdigest()[:32]
            if prepared not in self._prepared:
                prepared_response = self._dispatcher.prepare(prepared, case_id)
                if prepared_response.get("state") != "COMPLETE":
                    raise MeasurementError("measurement_prepare_failed")
                self._prepared.add(prepared)
            response = self._dispatcher.execute_prepared(prepared)
        else:
            raise MeasurementError("unsupported_measurement_cache_state")
        interval = {"started_monotonic_ns": response["request_started_monotonic_ns"],
            "finished_monotonic_ns": response["request_finished_monotonic_ns"],
            "duration_ns": response["request_duration_ns"],
            "state": response["request_interval_state"],
            "censored": response["request_interval_censored"]}
        post_started = time.monotonic_ns()
        result_bytes = 0
        oracle_result = "NOT_RUN"
        terminal = "INCOMPLETE"
        reason = response["reason"]
        try:
            if response["state"] == "COMPLETE":
                spool = self._output_root / f"{response['sequence']:020d}-{case_id}.spool"
                info = spool.lstat()
                if (spool.is_symlink() or not stat.S_ISREG(info.st_mode) or
                        info.st_uid != os.getuid() or info.st_nlink != 1 or
                        stat.S_IMODE(info.st_mode) & 0o077):
                    raise MeasurementError("measurement_spool_is_unsafe")
                result_bytes = info.st_size
                if result_bytes > scenario["expected_result_bytes_max"]:
                    raise MeasurementError("measurement_result_exceeds_plan")
                oracle_result = self._oracle(scenario, spool)
                if oracle_result not in {"PASS", "FAIL"}:
                    raise MeasurementError("invalid_measurement_oracle_result")
                terminal = "PASS" if oracle_result == "PASS" else "FAIL"
            elif interval["state"] == "CENSORED_TIMEOUT":
                terminal = "TIMEOUT"
            elif response["state"] == "FAILED":
                terminal = "FAIL"
        except OSError:
            terminal = "INCOMPLETE"; oracle_result = "INCOMPLETE"
            reason = "measurement_spool_unavailable"
        except Exception as exc:
            terminal = "INCOMPLETE"; oracle_result = "INCOMPLETE"
            reason = (str(exc) if isinstance(exc, MeasurementError) and
                      IDENTIFIER.fullmatch(str(exc)) else "postprocess_exception")
        return {"terminal_state": terminal,
            "reason_family": reason, "result_bytes": result_bytes,
            "oracle_result": oracle_result,
            "postprocess_duration_ns": time.monotonic_ns() - post_started,
            "request_interval": interval}

    def retire(self) -> bool:
        if self._retired:
            return True
        try:
            self._dispatcher.close()
        except Exception:
            self._retired = True
            return False
        self._retired = True
        return True


def _exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise MeasurementError(f"invalid_{name}")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise MeasurementError(f"invalid_{name}")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise MeasurementError(f"invalid_{name}")
    return value


def _load_document(path: Path, maximum: int = 1024 * 1024) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise MeasurementError("invalid_document") from exc
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077 or
                info.st_size > maximum):
            raise MeasurementError("invalid_document")
        payload = os.read(descriptor, info.st_size + 1)
        if len(payload) != info.st_size:
            raise MeasurementError("document_changed")
    finally:
        os.close(descriptor)
    try:
        return strict_load_bytes(payload)
    except ContractError as exc:
        raise MeasurementError("invalid_document") from exc


def load_measurement_plan(path: Path) -> tuple[dict[str, Any], str]:
    """Load one caller-owned immutable plan and return its canonical digest."""
    plan = _load_document(path)
    if plan.get("contract_type") != "measurement_plan":
        raise MeasurementError("invalid_measurement_plan")
    return plan, canonical_sha256(plan)


def _read_contract_artifact(root: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    verify_artifact(root, artifact)
    path = root / artifact["path"]
    return _load_document(path)


def validate_level_authorization(value: dict[str, Any], plan: dict[str, Any],
                                 plan_sha256: str, scope_sha256: str,
                                 evidence_root: Path | None,
                                 now: datetime | None = None,
                                 previous_evidence_root: Path | None = None,
                                 previous_path_prefix: str | None = None) -> dict[str, Any]:
    """Validate the current GO and, above level one, the previous clean level."""
    if (previous_evidence_root is None) != (previous_path_prefix is None):
        raise MeasurementError("previous_level_namespace_arguments_incomplete")
    auth = _exact(value, {"schema_version", "document_type", "scope_sha256",
        "plan_sha256", "requested_level", "approval_reference",
        "operation_go", "operation_preflight", "operation_recovery_validation",
        "operation_rehearsal_mandate", "operation_recovery_r1",
        "operation_recovery_r2", "operation_operator_walkthrough",
        "operation_recovery_incident",
        "previous_level"},
        "level_authorization")
    if auth["schema_version"] != 1 or auth["document_type"] != "measurement_level_authorization":
        raise MeasurementError("invalid_level_authorization")
    if auth["scope_sha256"] != scope_sha256 or auth["plan_sha256"] != plan_sha256:
        raise MeasurementError("level_authorization_identity_mismatch")
    if isinstance(auth["requested_level"], bool) or not isinstance(auth["requested_level"], int) or \
            auth["requested_level"] <= 0:
        raise MeasurementError("invalid_requested_level")
    level_definition = next((item for item in plan["concurrency_levels"]
                             if item["level"] == auth["requested_level"]), None)
    if level_definition is None or auth["approval_reference"] != \
            level_definition["approval_reference"]:
        raise MeasurementError("level_not_approved_by_plan")
    if evidence_root is None or evidence_root.resolve(strict=True) != evidence_root.absolute() or \
            evidence_root.stat().st_uid != os.getuid() or \
            stat.S_IMODE(evidence_root.stat().st_mode) != 0o700:
        raise MeasurementError("invalid_authorization_evidence_root")
    operation_go = _read_contract_artifact(evidence_root, auth["operation_go"])
    operation_preflight = _read_contract_artifact(evidence_root,
                                                  auth["operation_preflight"])
    recovery_validation = _read_contract_artifact(
        evidence_root, auth["operation_recovery_validation"])
    recovery_documents = {
        name: _read_contract_artifact(evidence_root, auth[artifact_name])
        for name, artifact_name in (
            ("mandate", "operation_rehearsal_mandate"),
            ("r1", "operation_recovery_r1"),
            ("r2", "operation_recovery_r2"),
            ("walkthrough", "operation_operator_walkthrough"),
            ("incident", "operation_recovery_incident"))}
    try:
        validate_contract(operation_go)
        validate_contract(operation_preflight)
        validate_contract(recovery_validation)
        for document in recovery_documents.values():
            validate_contract(document)
    except ContractError as exc:
        raise MeasurementError("operation_go_contract_invalid") from exc
    if (operation_go.get("contract_type") != "approval_receipt" or
            operation_go.get("receipt_kind") != "OPERATION_GO" or
            operation_go.get("scope_sha256") != scope_sha256 or
            operation_preflight.get("contract_type") != "preflight" or
            operation_preflight.get("scope_sha256") != scope_sha256 or
            operation_preflight.get("state", {}).get("technical_result") != "PASS" or
            operation_go.get("preflight_sha256") != canonical_sha256(operation_preflight) or
            recovery_validation.get("contract_type") != "recovery_validation" or
            recovery_validation.get("scope_sha256") != scope_sha256 or
            recovery_validation.get("result") != "PASS" or
            recovery_validation.get("decision") != "ELIGIBLE_FOR_HUMAN_GRANT" or
            operation_go.get("recovery_validation_sha256") !=
            canonical_sha256(recovery_validation)):
        raise MeasurementError("operation_go_does_not_authorize_scope")
    try:
        checked = datetime.fromisoformat(
            recovery_validation["checked_at"].replace("Z", "+00:00"))
        recomputed_recovery = validate_rehearsal_contracts(
            evidence_root, recovery_documents["mandate"], recovery_documents["r1"],
            recovery_documents["r2"], recovery_documents["walkthrough"],
            recovery_documents["incident"], checked)
    except (KeyError, TypeError, ValueError, RehearsalError) as exc:
        raise MeasurementError("operation_recovery_graph_invalid") from exc
    if recomputed_recovery != recovery_validation:
        raise MeasurementError("operation_recovery_validation_not_reproducible")
    mandate = recovery_documents["mandate"]
    if (mandate["recovery_plan_sha256"] !=
            plan["closure_inputs"]["recovery_plan_sha256"] or
            mandate["source_environment_identity_sha256"] !=
            plan["closure_inputs"]["target_sha256"] or
            mandate["target_copy_identity_sha256"] ==
            plan["closure_inputs"]["target_sha256"]):
        raise MeasurementError("operation_recovery_mandate_differs_from_frozen_scope")
    r2 = recovery_documents["r2"]
    if (r2["source_artifact_sha256"] != plan["closure_inputs"]["release_artifact_sha256"] or
            r2["source_configuration_sha256"] !=
            plan["closure_inputs"]["release_configuration_sha256"] or
            mandate["approved_artifact_sha256"] !=
            plan["closure_inputs"]["baseline_artifact_sha256"] or
            mandate["approved_configuration_sha256"] !=
            plan["closure_inputs"]["baseline_configuration_sha256"]):
        raise MeasurementError("operation_recovery_identity_differs_from_frozen_scope")
    try:
        start = datetime.fromisoformat(operation_go["valid_from"].replace("Z", "+00:00"))
        finish = datetime.fromisoformat(operation_go["valid_through"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise MeasurementError("operation_go_has_invalid_window") from exc
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise MeasurementError("authorization_clock_is_not_utc_aware")
    if not start <= instant.astimezone(timezone.utc) < finish:
        raise MeasurementError("operation_go_is_not_current")
    incident_time = datetime.fromisoformat(
        recovery_documents["incident"]["performed_at"].replace("Z", "+00:00"))
    if (operation_go["nonce"] == recovery_documents["incident"]["invalidated_go_nonce"] or
            start <= incident_time):
        raise MeasurementError("operation_go_was_not_renewed_after_incident")
    previous = auth["previous_level"]
    if auth["requested_level"] == 1:
        if previous is not None:
            raise MeasurementError("level_one_cannot_have_previous_evidence")
        return {**level_definition, "_valid_through": finish}
    previous = _exact(previous, {"level", "index", "terminal", "validation",
        "report", "retirement"}, "previous_level")
    if previous["level"] != auth["requested_level"] - 1:
        raise MeasurementError("previous_level_is_not_immediate")
    previous_root = previous_evidence_root or evidence_root
    if (previous_root is None or previous_root.resolve(strict=True) !=
            previous_root.absolute() or previous_root.stat().st_uid != os.getuid() or
            stat.S_IMODE(previous_root.stat().st_mode) != 0o700):
        raise MeasurementError("invalid_previous_level_evidence_root")
    def previous_artifact(name: str) -> dict[str, Any]:
        artifact = previous[name]
        if name == "validation" or previous_path_prefix is None:
            return artifact
        prefix = previous_path_prefix.rstrip("/") + "/"
        path = artifact.get("path")
        if not isinstance(path, str) or not path.startswith(prefix):
            raise MeasurementError("previous_level_artifact_prefix_mismatch")
        return {**artifact, "path": path[len(prefix):]}
    documents = {name: _read_contract_artifact(
        evidence_root if name == "validation" else previous_root,
        previous_artifact(name)) for name in
        ("index", "terminal", "validation", "report", "retirement")}
    try:
        verify_seal(previous_root, documents["index"], documents["terminal"],
                    documents["validation"])
    except ContractError as exc:
        raise MeasurementError("previous_level_seal_invalid") from exc
    report = documents["report"]
    terminal = documents["terminal"]
    validation = documents["validation"]
    retirement = documents["retirement"]
    payload_paths = {artifact["path"] for artifact in documents["index"]["payloads"]}
    if (report.get("contract_type") != "measurement_report" or
            report.get("scope_sha256") != scope_sha256 or
            report.get("plan_sha256") != plan_sha256 or
            report.get("level") != previous["level"] or
            report.get("technical_result") != "PASS" or
            validation.get("profile") != "private_full" or
            validation.get("result") != "PASS" or
            terminal.get("aggregate_pass") is not True or
            terminal.get("state", {}).get("exit_state") != "CLEAN" or
            retirement.get("contract_type") != "retirement" or
            retirement.get("scope_sha256") != scope_sha256 or
            retirement.get("backend_absence_verified") is not True or
            retirement.get("cached_plans_retired") is not True or
            retirement.get("default_off_reconnect") is not True or
            retirement.get("state", {}).get("exit_state") != "CLEAN" or
            previous_artifact("report")["path"] not in payload_paths or
            report.get("sample_file", {}).get("path") not in payload_paths or
            previous_artifact("retirement")["path"] not in payload_paths):
        raise MeasurementError("previous_level_did_not_pass_cleanly")
    if (documents["index"].get("scope_sha256") != scope_sha256 or
            documents["terminal"].get("scope_sha256") != scope_sha256 or
            documents["validation"].get("scope_sha256") != scope_sha256 or
            retirement.get("run_id") != report.get("run_id")):
        raise MeasurementError("previous_level_identity_graph_mismatch")
    try:
        verify_artifact(previous_root, report["sample_file"])
        try:
            from .evidence_tool import validate_measurement_level
        except ImportError:
            from evidence_tool import validate_measurement_level
        validate_measurement_level(previous_root, documents["index"], report,
                                   plan, scope_sha256)
    except ContractError as exc:
        raise MeasurementError("previous_level_sample_stream_invalid") from exc
    return {**level_definition, "_valid_through": finish}


class _SampleWriter:
    def __init__(self, root: Path, maximum_samples: int, maximum_bytes: int) -> None:
        if (root.resolve(strict=True) != root.absolute() or root.is_symlink() or
                root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700):
            raise MeasurementError("invalid_measurement_root")
        self._root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(self._root_fd); observed = root.stat()
        if ((opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino) or
                opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o700):
            os.close(self._root_fd)
            raise MeasurementError("invalid_measurement_root")
        try:
            fd = os.open("measurement-samples.jsonl", os.O_WRONLY | os.O_CREAT |
                         os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._root_fd)
        except Exception:
            os.close(self._root_fd)
            raise
        self._stream = os.fdopen(fd, "xb", buffering=0)
        self._maximum_samples = maximum_samples
        self._maximum_bytes = maximum_bytes
        self._samples = 0
        self._bytes = 0
        self._digest = hashlib.sha256()
        self._lock = threading.Lock()

    def append(self, sample: dict[str, Any]) -> None:
        validate_contract(sample)
        payload = canonical_bytes(sample)
        with self._lock:
            if self._samples >= self._maximum_samples or \
                    self._bytes + len(payload) > self._maximum_bytes:
                raise MeasurementError("measurement_evidence_limit")
            if self._stream.write(payload) != len(payload):
                raise MeasurementError("measurement_sample_short_write")
            os.fsync(self._stream.fileno())
            self._digest.update(payload)
            self._samples += 1
            self._bytes += len(payload)

    def close(self) -> tuple[int, int, str]:
        with self._lock:
            try:
                self._stream.close()
                os.fsync(self._root_fd)
            finally:
                os.close(self._root_fd)
            return self._samples, self._bytes, self._digest.hexdigest()


def _safe_receipt(value: Any, maximum_bytes: int) -> dict[str, Any]:
    fields = {"terminal_state", "reason_family", "result_bytes", "oracle_result",
              "postprocess_duration_ns", "request_interval"}
    receipt = _exact(value, fields, "session_receipt")
    terminal = receipt["terminal_state"]
    if terminal not in {"PASS", "FAIL", "TIMEOUT", "INCOMPLETE"}:
        raise MeasurementError("invalid_session_terminal")
    _identifier(receipt["reason_family"], "reason_family")
    if isinstance(receipt["result_bytes"], bool) or not isinstance(receipt["result_bytes"], int) or \
            not 0 <= receipt["result_bytes"] <= maximum_bytes:
        raise MeasurementError("invalid_result_bytes")
    if receipt["oracle_result"] not in {"PASS", "FAIL", "NOT_RUN", "INCOMPLETE"}:
        raise MeasurementError("invalid_oracle_result")
    if isinstance(receipt["postprocess_duration_ns"], bool) or \
            not isinstance(receipt["postprocess_duration_ns"], int) or \
            receipt["postprocess_duration_ns"] < 0:
        raise MeasurementError("invalid_postprocess_duration")
    interval = _exact(receipt["request_interval"], {"started_monotonic_ns",
        "finished_monotonic_ns", "duration_ns", "state", "censored"},
        "request_interval")
    for name in ("started_monotonic_ns", "finished_monotonic_ns", "duration_ns"):
        if isinstance(interval[name], bool) or not isinstance(interval[name], int) or interval[name] < 0:
            raise MeasurementError("invalid_request_interval")
    if interval["state"] not in {"NOT_RUN", "COMPLETE", "ERROR", "CENSORED_TIMEOUT"} or \
            not isinstance(interval["censored"], bool):
        raise MeasurementError("invalid_request_interval")
    start, finish, duration = (interval["started_monotonic_ns"],
                               interval["finished_monotonic_ns"], interval["duration_ns"])
    if ((interval["state"] == "NOT_RUN" and
         ((start, finish, duration) != (0, 0, 0) or interval["censored"])) or
        (interval["state"] != "NOT_RUN" and
         (not start or finish < start or duration != finish - start)) or
        interval["censored"] != (interval["state"] == "CENSORED_TIMEOUT") or
        (terminal == "PASS" and (interval["state"] != "COMPLETE" or
                                 receipt["oracle_result"] != "PASS")) or
        (terminal == "TIMEOUT" and interval["state"] != "CENSORED_TIMEOUT")):
        raise MeasurementError("contradictory_session_receipt")
    return receipt


def _nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        raise MeasurementError("empty_percentile")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile / 100 * len(ordered)) - 1)]


def _ratio(numerator: int, denominator: int | None) -> float | None:
    return None if denominator is None or denominator <= 0 else numerator / denominator


def resource_feed_sha256(policy: ResourcePolicy) -> str:
    """Identity of the approved live feed definition, excluding mutable samples."""
    return hashlib.sha256(canonical_bytes({"feed_version": policy.feed_version,
        "source_id": policy.source_id, "interval_ms": policy.interval_ms,
        "max_age_ms": policy.max_age_ms, "units": dict(policy.units)})).hexdigest()


def resource_policy_sha256(policy: ResourcePolicy) -> str:
    """Digest of every enforced policy field, without the cyclic scope link."""
    return canonical_sha256(policy.frozen_definition())


class MeasurementRunner:
    """Run exactly one pre-approved concurrency level; never escalates itself."""

    def __init__(self, plan: dict[str, Any], plan_sha256: str,
                 scope: dict[str, Any], scope_sha256: str,
                 resource_policy: ResourcePolicy, approved_resource_policy_sha256: str,
                 controller: StopController, output_root: Path,
                 run_id: str, maximum_evidence_bytes: int = 64 * 1024 * 1024,
                 clock: Callable[[], datetime] | None = None) -> None:
        try:
            validate_contract(plan)
        except ContractError as exc:
            raise MeasurementError("invalid_measurement_plan") from exc
        if plan.get("contract_type") != "measurement_plan" or \
                canonical_sha256(plan) != plan_sha256:
            raise MeasurementError("measurement_plan_digest_mismatch")
        try:
            validate_contract(scope)
        except ContractError as exc:
            raise MeasurementError("invalid_measurement_scope") from exc
        if scope.get("contract_type") != "scope" or canonical_sha256(scope) != scope_sha256:
            raise MeasurementError("measurement_scope_digest_mismatch")
        direct_bindings = {
            "workload_definition_sha256": "workload_definition",
            "qualification_sha256": "workload_definition",
            "baseline_manifest_sha256": "baseline_manifest",
            "resource_policy_sha256": "limits", "release_sha256": "release_definition",
            "target_sha256": "target", "dataset_sha256": "dataset",
            "toolkit_sha256": "toolkit", "recovery_plan_sha256": "recovery_plan"}
        plan_artifact = scope["scope_inputs"]["measurement_plan"]
        if (plan_artifact["sha256"] != plan_sha256 or
                plan_artifact["bytes"] != len(canonical_bytes(plan))) or any(
                plan["closure_inputs"][plan_name] !=
                scope["scope_inputs"][scope_name]["sha256"]
                for plan_name, scope_name in direct_bindings.items()):
            raise MeasurementError("measurement_plan_is_not_frozen_by_scope")
        _digest(scope_sha256, "scope_sha256"); _identifier(run_id, "run_id")
        if resource_policy.scope_sha256 != scope_sha256:
            raise MeasurementError("resource_policy_scope_mismatch")
        if (plan["closure_inputs"]["resource_policy_sha256"] !=
                approved_resource_policy_sha256 or
                approved_resource_policy_sha256 != resource_policy_sha256(resource_policy)):
            raise MeasurementError("resource_policy_digest_mismatch")
        if controller.policy is not resource_policy:
            raise MeasurementError("resource_controller_policy_mismatch")
        self.plan = plan; self.plan_sha256 = plan_sha256; self.scope_sha256 = scope_sha256
        self.policy = resource_policy; self.controller = controller
        self.output_root = output_root; self.run_id = run_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        scenario_count = len(plan["required_scenarios"])
        self.maximum_samples = scenario_count * 3 * (
            plan["warmup_repetitions"] * max(item["sessions"] for item in
                                             plan["concurrency_levels"]) *
            len(plan["measurement_blocks"]) +
            len(plan["measurement_blocks"]))
        self.maximum_evidence_bytes = maximum_evidence_bytes
        self.writer: _SampleWriter | None = None

    def run(self, level_authorization: dict[str, Any], evidence_root: Path,
            correctness_gate: CorrectnessGate,
            session_factory: Callable[[str, int, StopController], MeasurementSession], *,
            previous_evidence_root: Path | None = None,
            previous_path_prefix: str | None = None,
            pilot_attempt: dict[str, Any] | None = None,
            pilot_window_id: str | None = None,
            pilot_plan_sha256: str | None = None,
            pilot_ledger_root: Path | None = None,
            pilot_plan: dict[str, Any] | None = None) -> dict[str, Any]:
        if len({pilot_attempt is None, pilot_window_id is None,
                pilot_plan_sha256 is None, pilot_ledger_root is None,
                pilot_plan is None}) != 1:
            raise MeasurementError("pilot_attempt_arguments_incomplete")
        level_definition = validate_level_authorization(level_authorization, self.plan,
            self.plan_sha256, self.scope_sha256, evidence_root,
            previous_evidence_root=previous_evidence_root,
            previous_path_prefix=previous_path_prefix)
        level = level_definition["level"]
        self._pilot_attempt_sha256: str | None = None
        self._pilot_window_id: str | None = None
        self._pilot_dispatch_sha256: str | None = None
        self._pilot_dispatched_at: str | None = None
        if pilot_attempt is not None:
            attempt = _exact(pilot_attempt, {"schema_version", "document_type", "record_id",
                "pilot_id", "sequence", "scope_sha256", "plan_sha256",
                "operation_go_sha256", "previous_record_sha256", "window_id", "level",
                "run_id", "consumed_at"}, "pilot_attempt")
            _identifier(pilot_window_id, "pilot_window_id")
            _digest(pilot_plan_sha256, "pilot_plan_sha256")
            if (not isinstance(pilot_plan, dict) or
                    canonical_sha256(pilot_plan) != pilot_plan_sha256):
                raise MeasurementError("pilot_plan_invalid")
            try:
                consumed_at = datetime.fromisoformat(
                    attempt["consumed_at"].replace("Z", "+00:00"))
            except (AttributeError, ValueError) as exc:
                raise MeasurementError("pilot_attempt_invalid") from exc
            if (attempt["schema_version"] != 1 or
                    attempt["document_type"] != "internal_pilot_attempt" or
                    attempt["scope_sha256"] != self.scope_sha256 or
                    attempt["plan_sha256"] != pilot_plan_sha256 or
                    attempt["operation_go_sha256"] !=
                    level_authorization["operation_go"]["sha256"] or
                    attempt["window_id"] != pilot_window_id or
                    attempt["level"] != level or attempt["run_id"] != self.run_id or
                    consumed_at.tzinfo is None):
                raise MeasurementError("pilot_attempt_invalid")
            self._pilot_attempt_sha256 = canonical_sha256(attempt)
            self._pilot_window_id = pilot_window_id
            ledger = pilot_ledger_root.resolve(strict=True)
            ledger_info = ledger.stat()
            ledger_binding = pilot_plan.get("attempt_ledger", {})
            if (ledger != pilot_ledger_root.absolute() or not ledger.is_dir() or
                    ledger_info.st_uid != os.getuid() or
                    stat.S_IMODE(ledger_info.st_mode) != 0o700 or
                    ledger_binding.get("path") != str(ledger) or
                    ledger_binding.get("device") != ledger_info.st_dev or
                    ledger_binding.get("inode") != ledger_info.st_ino or
                    ledger_binding.get("storage_control") != "EXTERNAL_APPEND_ONLY"):
                raise MeasurementError("pilot_ledger_invalid")
            try:
                verify_artifact(ledger, ledger_binding["identity"])
                identity_path = ledger / ledger_binding["identity"]["path"]
                lock_descriptor = os.open(identity_path, os.O_RDONLY | os.O_NOFOLLOW)
            except (KeyError, TypeError, OSError, ContractError) as exc:
                raise MeasurementError("pilot_ledger_invalid") from exc
            sequence = attempt["sequence"]
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise MeasurementError("pilot_attempt_invalid")
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                attempt_names = sorted(path.name for path in ledger.glob("attempt-*.json"))
                if not attempt_names or attempt_names[-1] != f"attempt-{sequence:04d}.json":
                    raise MeasurementError("pilot_attempt_not_current")
                attempt_path = ledger / f"attempt-{sequence:04d}.json"
                try:
                    descriptor = os.open(attempt_path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, "rb") as stream:
                        if stream.read() != canonical_bytes(attempt):
                            raise MeasurementError("pilot_attempt_not_in_ledger")
                except OSError as exc:
                    raise MeasurementError("pilot_attempt_not_in_ledger") from exc
                dispatched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                marker = {"schema_version": 1, "document_type": "internal_pilot_dispatch",
                    "attempt_sha256": self._pilot_attempt_sha256,
                    "window_id": pilot_window_id, "level": level, "run_id": self.run_id,
                    "dispatched_at": dispatched_at}
                marker_path = ledger / f"dispatch-{sequence:04d}.json"
                try:
                    descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                         os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(canonical_bytes(marker)); stream.flush(); os.fsync(stream.fileno())
                    directory = os.open(ledger, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except FileExistsError as exc:
                    raise MeasurementError("pilot_attempt_already_dispatched") from exc
                except OSError as exc:
                    raise MeasurementError("pilot_dispatch_write_failed") from exc
            finally:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                os.close(lock_descriptor)
            self._pilot_dispatch_sha256 = canonical_sha256(marker)
            self._pilot_dispatched_at = dispatched_at
        self._authorization_valid_through = level_definition["_valid_through"]
        self._active_level = level
        sessions_count = level_definition["sessions"]
        if sessions_count > self.policy.limits["inflight_requests"]:
            raise MeasurementError("concurrency_exceeds_resource_policy")
        if level > 1 and resource_feed_sha256(self.policy) != \
                level_definition["server_resource_feed_sha256"]:
            raise MeasurementError("server_resource_feed_identity_mismatch")
        if (not isinstance(correctness_gate, CorrectnessGate) or
                correctness_gate._token is not _CORRECTNESS_TOKEN or
                correctness_gate.scope_sha256 != self.scope_sha256 or
                correctness_gate.plan_sha256 != self.plan_sha256 or
                any(correctness_gate.oracle_cases.get(item["oracle_case_id"]) != "PASS"
                    for item in self.plan["required_scenarios"])):
            raise MeasurementError("correctness_gate_not_passed")
        coverage = self._coverage(correctness_gate.qualification_coverage)
        frequency_weights = correctness_gate.frequency_weights
        baseline_weights = correctness_gate.baseline_weights
        self._validate_weights(frequency_weights, baseline_weights)
        self.writer = _SampleWriter(self.output_root, self.maximum_samples,
                                    self.maximum_evidence_bytes)

        samples: list[dict[str, Any]] = []
        retirement_ok = True
        stopped = False
        fatal: BaseException | None = None
        try:
            for block_index, block in enumerate(self.plan["measurement_blocks"], 1):
                for order_index, mode in enumerate(block["order"], 1):
                    if self._authorization_expired():
                        stopped = True; break
                    sessions: dict[tuple[str, int], MeasurementSession] = {}
                    try:
                        for slot in range(1, sessions_count + 1):
                            sessions[(mode, slot)] = session_factory(
                                mode, slot, self.controller)
                        for _ in range(self.plan["warmup_repetitions"]):
                            if not self._run_warmup(mode, block_index, order_index,
                                                    sessions_count, sessions, samples):
                                stopped = True; break
                        if not stopped and not self._run_batch(
                                mode, "MEASURE", block_index, order_index,
                                sessions_count, sessions, samples):
                            stopped = True
                    finally:
                        for session in sessions.values():
                            try:
                                retirement_ok = bool(session.retire()) and retirement_ok
                            except Exception:
                                retirement_ok = False
                    if not retirement_ok and not self.controller.stopped:
                        self.controller.stop("SESSION_FAILURE")
                    if stopped or self.controller.stopped:
                        stopped = True; break
                if stopped: break
        except Exception:
            if not self.controller.stopped:
                self.controller.stop("SESSION_FAILURE")
            stopped = True
        except BaseException as exc:
            fatal = exc
            stopped = True
        stopped = stopped or self.controller.stopped

        assert self.writer is not None
        raw_count, sample_bytes, sample_digest = self.writer.close()
        if fatal is not None:
            raise fatal
        report = self._report(level, samples, raw_count, sample_bytes, sample_digest, coverage,
                              retirement_ok, stopped, frequency_weights, baseline_weights)
        self._write_report(report)
        return report

    def _authorization_expired(self) -> bool:
        instant = self._clock()
        if instant.tzinfo is None or \
                instant.astimezone(timezone.utc) >= self._authorization_valid_through:
            self.controller.stop("AUTHORIZATION_EXPIRED")
            return True
        return False

    def _run_warmup(self, mode: str, block_index: int, order_index: int,
                    sessions_count: int,
                    sessions: dict[tuple[str, int], MeasurementSession],
                    samples: list[dict[str, Any]]) -> bool:
        for scenario in self.plan["required_scenarios"]:
            if scenario["cache_state"] == "first_use":
                continue
            if self.controller.stopped or self._authorization_expired():
                return False
            with ThreadPoolExecutor(max_workers=sessions_count,
                                    thread_name_prefix="pgqi-warmup") as pool:
                futures = [(slot, pool.submit(sessions[(mode, slot)].execute, scenario))
                           for slot in range(1, sessions_count + 1)]
                passed = True
                for slot, future in futures:
                    try:
                        receipt = _safe_receipt(future.result(),
                            scenario["expected_result_bytes_max"])
                    except Exception:
                        receipt = {"terminal_state": "INCOMPLETE",
                            "reason_family": "session_exception", "result_bytes": 0,
                            "oracle_result": "INCOMPLETE", "postprocess_duration_ns": 0,
                            "request_interval": {"started_monotonic_ns": 0,
                                "finished_monotonic_ns": 0, "duration_ns": 0,
                                "state": "NOT_RUN", "censored": False}}
                    sample = self._sample(len(samples) + 1, scenario, mode, "WARMUP",
                                          block_index, order_index, slot, receipt)
                    try:
                        assert self.writer is not None
                        self.writer.append(sample)
                    except Exception:
                        self.controller.stop("EVIDENCE_LIMIT")
                        raise
                    samples.append(sample)
                    if receipt["terminal_state"] != "PASS" or \
                            receipt["oracle_result"] != "PASS":
                        passed = False
                if not passed:
                    self.controller.stop("SESSION_FAILURE")
                    return False
        return True

    def _run_batch(self, mode: str, phase: str, block_index: int, order_index: int,
                   sessions_count: int,
                   sessions: dict[tuple[str, int], MeasurementSession],
                   samples: list[dict[str, Any]]) -> bool:
        scenarios = self.plan["required_scenarios"]
        shift = ((block_index - 1) * 3 + order_index - 1) % len(scenarios)
        scenarios = scenarios[shift:] + scenarios[:shift]
        for offset in range(0, len(scenarios), sessions_count):
            batch = scenarios[offset:offset + sessions_count]
            if self.controller.stopped or self._authorization_expired():
                return False
            with ThreadPoolExecutor(max_workers=len(batch),
                                    thread_name_prefix="pgqi-measurement") as pool:
                futures = []
                for index, scenario in enumerate(batch):
                    slot = index + 1
                    futures.append((slot, scenario, pool.submit(
                        sessions[(mode, slot)].execute, scenario)))
                batch_pass = True
                for slot, scenario, future in futures:
                    try:
                        receipt = _safe_receipt(future.result(),
                            scenario["expected_result_bytes_max"])
                    except Exception:
                        receipt = {"terminal_state": "INCOMPLETE",
                            "reason_family": "session_exception", "result_bytes": 0,
                            "oracle_result": "INCOMPLETE", "postprocess_duration_ns": 0,
                            "request_interval": {"started_monotonic_ns": 0,
                                "finished_monotonic_ns": 0, "duration_ns": 0,
                                "state": "NOT_RUN", "censored": False}}
                    if (phase == "MEASURE" and
                            scenario["cache_state"] == "warm_cached" and
                            receipt["terminal_state"] == "PASS" and
                            receipt["reason_family"] != "cached"):
                        receipt = {**receipt, "terminal_state": "INCOMPLETE",
                                   "oracle_result": "INCOMPLETE",
                                   "reason_family": "warm_cache_unproven"}
                    sample = self._sample(len(samples) + 1, scenario, mode, phase,
                                          block_index, order_index, slot, receipt)
                    try:
                        assert self.writer is not None
                        self.writer.append(sample)
                    except Exception:
                        self.controller.stop("EVIDENCE_LIMIT")
                        raise
                    samples.append(sample)
                    if receipt["terminal_state"] != "PASS" or \
                            receipt["oracle_result"] != "PASS":
                        batch_pass = False
                if not batch_pass:
                    self.controller.stop("SESSION_FAILURE")
                    return False
        return True

    def _sample(self, ordinal: int, scenario: dict[str, Any], mode: str, phase: str,
                block_index: int, order_index: int, slot: int,
                receipt: dict[str, Any]) -> dict[str, Any]:
        sample = {"schema_version": 1, "contract_type": "measurement_sample",
            "contract_id": f"{self.run_id}-sample-{ordinal:08d}",
            "scope_sha256": self.scope_sha256, "plan_sha256": self.plan_sha256,
            "run_id": self.run_id, "level": self._active_level,
            "sample_id": f"sample-{ordinal:08d}",
            "scenario_id": scenario["scenario_id"],
            "protocol": scenario["protocol"], "cache_state": scenario["cache_state"],
            "mode": mode, "phase": phase,
            "block_index": block_index, "order_index": order_index,
            "session_slot": slot, "request_interval": receipt["request_interval"],
            "postprocess_duration_ns": receipt["postprocess_duration_ns"],
            "terminal_state": receipt["terminal_state"],
            "reason_family": receipt["reason_family"],
            "result_bytes": receipt["result_bytes"],
            "oracle_result": receipt["oracle_result"]}
        return sample

    @property
    def _active_level(self) -> int:
        # The validated authorisation sets this immediately before dispatch.
        return self.__active_level

    @_active_level.setter
    def _active_level(self, value: int) -> None:
        self.__active_level = value

    def _coverage(self, raw: dict[str, Any]) -> dict[str, Any]:
        required = {"denominator", "confirmed_ratio", "frequency_ratio",
                    "baseline_time_ratio", "measured_natural_ratio",
                    "natural_items_linked_to_execution"}
        coverage = _exact(raw, required, "corpus_coverage")
        denominator = coverage["denominator"]
        linked = coverage["natural_items_linked_to_execution"]
        if (isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 0 or
                isinstance(linked, bool) or not isinstance(linked, int) or
                linked < 0 or linked > denominator):
            raise MeasurementError("invalid_corpus_coverage")
        for name in ("confirmed_ratio", "frequency_ratio", "baseline_time_ratio",
                     "measured_natural_ratio"):
            value = coverage[name]
            if value is not None and (isinstance(value, bool) or
                    not isinstance(value, (int, float)) or not math.isfinite(value) or
                    value < 0 or value > 1):
                raise MeasurementError("invalid_corpus_coverage")
        return {"structural_eligibility": coverage["confirmed_ratio"],
            "frequency_coverage": coverage["frequency_ratio"],
            "baseline_measured_share": coverage["baseline_time_ratio"],
            "measured_natural_share": coverage["measured_natural_ratio"],
            "missing_natural_items": denominator - linked}

    def _validate_weights(self, frequency: dict[str, int] | None,
                          baseline: dict[str, int] | None) -> None:
        rules = self.plan["value_rules"]
        for values, digest_name in ((frequency, "frequency_weights_sha256"),
                                    (baseline, "baseline_weights_sha256")):
            expected = rules[digest_name]
            if expected is None:
                continue
            if not isinstance(values, dict) or not values or any(
                    not isinstance(key, str) or not IDENTIFIER.fullmatch(key) or
                    isinstance(item, bool) or not isinstance(item, int) or item < 0
                    for key, item in values.items()):
                raise MeasurementError("invalid_value_weights")
            actual = hashlib.sha256(canonical_bytes(values)).hexdigest()
            if actual != expected:
                raise MeasurementError("value_weight_digest_mismatch")

    def _report(self, level: int, samples: list[dict[str, Any]], raw_count: int,
                sample_bytes: int, sample_digest: str, coverage: dict[str, Any], retirement_ok: bool,
                stopped: bool, frequency_weights: dict[str, int] | None,
                baseline_weights: dict[str, int] | None) -> dict[str, Any]:
        self._active_level = level
        measured = [item for item in samples if item["phase"] == "MEASURE"]
        groups: list[dict[str, Any]] = []
        minimum = self.plan["statistics"]["minimum_n"]
        for scenario in self.plan["required_scenarios"]:
            for mode in ("disabled", "off", "allowlist"):
                subset = [item for item in measured if
                          item["scenario_id"] == scenario["scenario_id"] and item["mode"] == mode]
                counts = {state.lower(): sum(item["terminal_state"] == state for item in subset)
                          for state in ("PASS", "FAIL", "TIMEOUT", "INCOMPLETE")}
                values = [item["request_interval"]["duration_ns"] for item in subset
                          if item["terminal_state"] == "PASS"]
                postprocess = [item["postprocess_duration_ns"] for item in subset]
                block_medians = []
                for block in sorted({item["block_index"] for item in subset}):
                    block_values = [item["request_interval"]["duration_ns"] for item in subset
                                    if item["block_index"] == block and
                                    item["terminal_state"] == "PASS"]
                    if block_values:
                        block_medians.append(_nearest_rank(block_values, 50))
                def published(percentile: int) -> tuple[int | None, str]:
                    required_n = minimum[f"p{percentile}"]
                    return ((_nearest_rank(values, percentile), "REPORTED") if len(values) >= required_n
                            else (None, f"SUPPRESSED(N={len(values)},MIN={required_n})"))
                p95, p95_status = published(95); p99, p99_status = published(99)
                groups.append({"scenario_id": scenario["scenario_id"], "mode": mode,
                    "n": len(subset), **counts,
                    "p50_ns": _nearest_rank(values, 50) if len(values) >= minimum["p50"] else None,
                    "postprocess_p50_ns": _nearest_rank(postprocess, 50) if postprocess else None,
                    "p95_ns": p95, "p99_ns": p99,
                    "p95_status": p95_status, "p99_status": p99_status,
                    "block_median_min_ns": min(block_medians) if block_medians else None,
                    "block_median_max_ns": max(block_medians) if block_medians else None})
        by_group = {(item["scenario_id"], item["mode"]): item for item in groups}
        contrasts = []
        for scenario in self.plan["required_scenarios"]:
            disabled = by_group[(scenario["scenario_id"], "disabled")]["p50_ns"]
            off = by_group[(scenario["scenario_id"], "off")]["p50_ns"]
            allow = by_group[(scenario["scenario_id"], "allowlist")]["p50_ns"]
            contrasts.append({"scenario_id": scenario["scenario_id"],
                "extension_overhead_p50_ns": (off - disabled
                    if off is not None and disabled is not None else None),
                "routing_effect_p50_ns": (allow - off
                    if allow is not None and off is not None else None)})
        failures = any(item["terminal_state"] != "PASS" or item["oracle_result"] != "PASS"
                       for item in samples)
        technical = "FAIL" if failures else "INCOMPLETE" if stopped or not retirement_ok else "PASS"
        coverage = self._actual_coverage(coverage, measured,
                                         frequency_weights, baseline_weights)
        value = self._value_outcome(measured, coverage, technical,
                                    frequency_weights, baseline_weights)
        report = {"schema_version": 1, "contract_type": "measurement_report",
            "contract_id": f"{self.run_id}-measurement-report",
            "scope_sha256": self.scope_sha256, "plan_sha256": self.plan_sha256,
            "run_id": self.run_id, "level": level,
            "sample_file": {"path": "measurement-samples.jsonl",
                "sha256": sample_digest, "bytes": sample_bytes},
            "sample_count": len(measured),
            "raw_sample_count": raw_count,
            "warmup_sample_count": raw_count - len(measured),
            "groups": groups, "contrasts": contrasts, "coverage": coverage,
            "value_outcome": value,
            "technical_result": technical,
            "stop_reason": self.controller.reason,
            "limitations": sorted(MEASUREMENT_LIMITATIONS)}
        if self._pilot_attempt_sha256 is not None:
            report["pilot_attempt_sha256"] = self._pilot_attempt_sha256
            report["pilot_window_id"] = self._pilot_window_id
            report["pilot_dispatch_sha256"] = self._pilot_dispatch_sha256
            report["pilot_dispatched_at"] = self._pilot_dispatched_at
        validate_contract(report)
        return report

    def _actual_coverage(self, frozen: dict[str, Any], measured: list[dict[str, Any]],
                         frequency: dict[str, int] | None,
                         baseline: dict[str, int] | None) -> dict[str, Any]:
        expected_per_scenario = 3 * len(self.plan["measurement_blocks"])
        passed_natural: set[str] = set()
        planned_natural = {item["natural_query_id"] for item in
                           self.plan["required_scenarios"] if item["natural_query_id"] is not None}
        for natural in planned_natural:
            scenarios = [item["scenario_id"] for item in self.plan["required_scenarios"]
                         if item["natural_query_id"] == natural]
            subset = [item for item in measured if item["scenario_id"] in scenarios]
            if (len(subset) == expected_per_scenario * len(scenarios) and
                    all(item["terminal_state"] == "PASS" and
                        item["oracle_result"] == "PASS" for item in subset)):
                passed_natural.add(natural)
        denominator = len(planned_natural) + frozen["missing_natural_items"]
        actual = dict(frozen)
        actual["measured_natural_share"] = _ratio(len(passed_natural), denominator)
        actual["missing_natural_items"] = denominator - len(passed_natural)
        if passed_natural != planned_natural:
            actual["frequency_coverage"] = (_ratio(sum(frequency.get(key, 0)
                for key in passed_natural), sum(frequency.values())) if frequency else None)
            actual["baseline_measured_share"] = (_ratio(sum(baseline.get(key, 0)
                for key in passed_natural), sum(baseline.values())) if baseline else None)
        return actual

    def _value_outcome(self, measured: list[dict[str, Any]], coverage: dict[str, Any],
                       technical: str, frequency: dict[str, int] | None,
                       baseline: dict[str, int] | None) -> str:
        rules = self.plan["value_rules"]
        if technical != "PASS" or rules["threshold_kind"] == "descriptive_only" or \
                frequency is None or baseline is None or any(
                    coverage[name] is None or coverage[name] < 1 for name in
                    ("frequency_coverage", "baseline_measured_share", "measured_natural_share")):
            return "INCONCLUSIVE"
        block_improvements: list[int] = []
        for block in range(1, len(self.plan["measurement_blocks"]) + 1):
            weighted_delta = weighted_base = 0
            for scenario in self.plan["required_scenarios"]:
                natural = scenario["natural_query_id"]
                if natural is None or natural not in frequency or natural not in baseline:
                    return "INCONCLUSIVE"
                pair = {item["mode"]: item["request_interval"]["duration_ns"]
                        for item in measured if item["block_index"] == block and
                        item["scenario_id"] == scenario["scenario_id"] and
                        item["terminal_state"] == "PASS" and
                        item["mode"] in {"off", "allowlist"}}
                if set(pair) != {"off", "allowlist"} or pair["off"] <= 0:
                    return "INCONCLUSIVE"
                weight = frequency[natural]
                weighted_base += pair["off"] * weight
                weighted_delta += (pair["off"] - pair["allowlist"]) * weight
            if weighted_base <= 0:
                return "INCONCLUSIVE"
            block_improvements.append(weighted_delta * 10000 // weighted_base)
        if len(block_improvements) < rules["minimum_paired_blocks"]:
            return "INCONCLUSIVE"
        conservative_bps = _nearest_rank(block_improvements, 5)
        return ("POSITIVE" if conservative_bps >=
                self.plan["value_rules"]["minimum_relative_improvement_bps"] else "NEGATIVE")

    def _write_report(self, report: dict[str, Any]) -> None:
        root_fd = os.open(self.output_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(root_fd)
            path_info = self.output_root.stat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) != 0o700 or
                    (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino) or
                    self.output_root.resolve(strict=True) != self.output_root.absolute()):
                raise MeasurementError("invalid_measurement_root")
            fd = os.open("measurement-report.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_bytes(report)); stream.flush(); os.fsync(stream.fileno())
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
