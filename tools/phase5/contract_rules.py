#!/usr/bin/env python3
"""Normative PGQI-5 contract loader and semantic rules."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("contract_catalog.json")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$")
COMMON = {"schema_version", "contract_type", "contract_id"}


class ContractError(ValueError):
    pass


def _duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ContractError(f"non-finite number: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"non-finite number: {value}")
    return result


def strict_load_bytes(payload: bytes, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog = catalog or load_catalog()
    if len(payload) > catalog["maximum_bytes"]:
        raise ContractError("document exceeds maximum_bytes")
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ContractError("invalid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_duplicate,
                           parse_constant=_constant, parse_float=_finite_float)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ContractError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("contract root must be an object")
    if _depth(value) > catalog["maximum_depth"]:
        raise ContractError("document exceeds maximum_depth")
    _reject_surrogates(value)
    validate_contract(value, catalog)
    return value


def strict_load_path(path: Path, bundle_root: Path) -> dict[str, Any]:
    descriptor = _open_bundle_file(bundle_root, path.as_posix())
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ContractError("contract is not a regular file")
        maximum = load_catalog()["maximum_bytes"]
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    return strict_load_bytes(payload)


def load_catalog() -> dict[str, Any]:
    with CATALOG_PATH.open("rb") as stream:
        return json.load(stream, object_pairs_hook=_duplicate,
                         parse_constant=_constant, parse_float=_finite_float)


def _reject_surrogates(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ContractError("unpaired Unicode surrogate")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_surrogates(key); _reject_surrogates(item)
    elif isinstance(value, list):
        for item in value:
            _reject_surrogates(item)


def _depth(value: Any) -> int:
    maximum = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        maximum = max(maximum, depth)
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        stack.extend((child, depth + 1) for child in children)
    return maximum


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value


def _array(value: Any, name: str, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ContractError(f"{name} must be {'a non-empty' if nonempty else 'an'} array")
    return value


def _identifier_array(value: Any, name: str, nonempty: bool = False) -> list[str]:
    items = _array(value, name, nonempty)
    for item in items:
        _string(item, name)
    if len(items) != len(set(items)):
        raise ContractError(f"duplicate {name}")
    return items


def _string(value: Any, name: str, pattern: re.Pattern[str] = IDENTIFIER) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ContractError(f"invalid {name}")
    return value


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"invalid integer {name}")
    return value


def _digest(value: Any, name: str) -> str:
    return _string(value, name, DIGEST)


def _enum(value: Any, name: str, allowed: set[str] | list[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"invalid {name}")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    text = _string(value, name, UTC)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"invalid {name}") from exc


def _state(value: Any, catalog: dict[str, Any]) -> None:
    state = _object(value, "state")
    expected = set(catalog["state_axes"])
    if set(state) != expected:
        raise ContractError("state axes must be complete and exact")
    for axis, allowed in catalog["state_axes"].items():
        if state[axis] not in allowed:
            raise ContractError(f"invalid state axis {axis}")


def _artifact(value: Any, name: str) -> None:
    artifact = _object(value, name)
    if set(artifact) != {"path", "sha256", "bytes"}:
        raise ContractError(f"{name} must contain path, sha256 and bytes")
    validate_relative_path(artifact["path"])
    _digest(artifact["sha256"], f"{name}.sha256")
    _integer(artifact["bytes"], f"{name}.bytes", 0)


def validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ContractError("invalid relative path")
    path = Path(value)
    if (path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")) or
            path.as_posix() != value):
        raise ContractError("path traversal or absolute path")
    return value


def _open_bundle_file(bundle_root: Path, relative: str) -> int:
    """Open a file beneath root without following any path-component symlink."""
    validate_relative_path(relative)
    root_fd = os.open(bundle_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current_fd = root_fd
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=current_fd)
    except OSError as exc:
        raise ContractError("unsafe or missing bundle path") from exc
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def resolve_bundle_path(bundle_root: Path, relative: Path) -> Path:
    root = bundle_root.resolve(strict=True)
    if bundle_root.is_symlink() or not root.is_dir():
        raise ContractError("bundle root must be a real directory")
    text = validate_relative_path(relative.as_posix())
    current = root
    for part in Path(text).parts:
        current = current / part
        if current.is_symlink():
            raise ContractError("symlink path component")
    resolved = current.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ContractError("path escapes bundle root")
    return resolved


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_contract(value: dict[str, Any], catalog: dict[str, Any] | None = None) -> None:
    catalog = catalog or load_catalog()
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ContractError("schema_version must be integer 1")
    kind = value.get("contract_type")
    if not isinstance(kind, str) or kind not in catalog["contract_fields"]:
        raise ContractError("unknown contract_type")
    _string(value.get("contract_id"), "contract_id")
    definition = catalog["contract_fields"][kind]
    required = COMMON | set(definition["required"])
    allowed = required | set(definition["optional"])
    missing = required - set(value)
    unknown = set(value) - allowed
    if missing:
        raise ContractError(f"missing fields: {sorted(missing)}")
    if unknown:
        raise ContractError(f"unknown controlling fields: {sorted(unknown)}")
    validator = globals()[f"_validate_{kind}"]
    validator(value, catalog)


def _scope_ref(value: dict[str, Any]) -> None:
    _digest(value["scope_sha256"], "scope_sha256")


def _validate_scope(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    inputs = _object(value["scope_inputs"], "scope_inputs")
    required = {"workload_definition", "release_definition", "target", "limits",
                "evidence_policy", "recovery_plan", "export_policy", "toolkit",
                "role_bindings", "window"}
    if set(inputs) != required:
        raise ContractError("scope_inputs closure is incomplete or unknown")
    for name, artifact in inputs.items():
        _artifact(artifact, f"scope_inputs.{name}")
    gates = _identifier_array(value["gates_required"], "gates_required", True)
    for gate in gates:
        _enum(gate, "gate ID", catalog["gate_ids"])


def _validate_approval_receipt(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _enum(value["receipt_kind"], "approval receipt kind",
          catalog["receipt_kinds"]["approval_receipt"])
    _scope_ref(value); _digest(value["preflight_sha256"], "preflight_sha256")
    start = _timestamp(value["valid_from"], "valid_from")
    end = _timestamp(value["valid_through"], "valid_through")
    _identifier_array(value["approvals"], "approvals", True); _string(value["nonce"], "nonce")
    if start >= end:
        raise ContractError("approval validity window is empty or reversed")


def _validate_preflight(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _digest(value["target_identity_sha256"], "target_identity_sha256")
    _identifier_array(value["checks"], "checks", True); _state(value["state"], catalog)


def _validate_workload(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _digest(value["inventory_sha256"], "inventory_sha256")
    entries = _array(value["entries"], "entries", True)
    ids = set()
    for item in entries:
        item = _object(item, "workload entry")
        required = {"query_id", "sql", "inventory_origin", "inventory_authority",
                    "executed_sql_origin", "executed_sql_authority", "dataset_origin",
                    "dataset_authority", "execution_authority"}
        if set(item) != required:
            raise ContractError("workload entry fields are not exact")
        query_id = _string(item["query_id"], "query_id")
        if query_id in ids: raise ContractError("duplicate query_id")
        ids.add(query_id); _artifact(item["sql"], "sql")
        origins = {"natural_internal", "natural_customer", "derived_reproduction",
                   "synthetic_control"}
        executed_origins = origins | {"NOT_EXECUTED", "NOT_APPLICABLE"}
        authorities = {"AUTHORISED", "INVENTORY_ONLY", "PROHIBITED", "UNKNOWN",
                       "NOT_APPLICABLE"}
        domains = {"inventory_origin": origins,
                   "executed_sql_origin": executed_origins,
                   "dataset_origin": executed_origins,
                   "inventory_authority": authorities,
                   "executed_sql_authority": authorities,
                   "dataset_authority": authorities,
                   "execution_authority": {"OPERATION_GO"}}
        for name, allowed in domains.items():
            _enum(item[name], name, allowed)


def _validate_release(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["source_commit"], "source_commit", re.compile(r"^[0-9a-f]{40}$"))
    if not isinstance(value["source_dirty"], bool): raise ContractError("source_dirty must be boolean")
    for name in ("server_build_sha256", "pg_qi_sha256", "pg_duckdb_sha256", "configuration_sha256"):
        _digest(value[name], name)


def _validate_request(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("run_id", "session_id", "request_id", "query_id"):
        _string(value[name], name)
    for axis, allowed in catalog["request_axes"].items():
        _enum(value[axis], axis, allowed)
    planning, relation = value["planning_evidence"], value["snapshot_relation"]
    if planning in {"ERROR", "RETAINED_PREVIOUS"} and relation not in {"PREVIOUS", "NO_SNAPSHOT"}:
        raise ContractError("planning error cannot claim current freshness")
    if planning == "UNAVAILABLE" and relation != "NO_SNAPSHOT":
        raise ContractError("unavailable snapshot must use NO_SNAPSHOT")
    if planning in {"FRESH", "REUSED"} and relation != "CURRENT":
        raise ContractError("fresh or reused evidence must identify CURRENT snapshot")
    attribution = {"snapshot_id", "planning_request_id", "prepared_object_id",
                   "prepared_generation"}
    if relation == "NO_SNAPSHOT" and attribution & set(value):
        raise ContractError("NO_SNAPSHOT cannot carry snapshot attribution")
    if relation != "NO_SNAPSHOT":
        for name in ("snapshot_id", "planning_request_id"):
            _string(value.get(name), name)
    prepared = {"prepared_object_id", "prepared_generation"}
    if planning == "REUSED" and not prepared <= set(value):
        raise ContractError("cached reuse lacks prepared registry identity")
    if "prepared_generation" in value: _integer(value["prepared_generation"], "prepared_generation", 1)
    if "sqlstate" in value:
        _string(value["sqlstate"], "SQLSTATE", re.compile(r"^[0-9A-Z]{5}$"))


def _validate_snapshot(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("snapshot_id", "session_id", "planning_request_id", "prepared_object_id"):
        _string(value[name], name)
    _integer(value["prepared_generation"], "prepared_generation", 1)
    _integer(value["decision_id"], "decision_id", 0)
    snapshot = _object(value["tuple"], "tuple")
    expected = {"contract_version", "context", "policy_intent", "reason",
                "force_origin", "classifier_status", "classifier_reason",
                "observed_engine", "verification", "observation",
                "request_applied", "duckdb_force_execution"}
    if set(snapshot) != expected:
        raise ContractError("snapshot tuple fields are not exact")
    for name, item in snapshot.items():
        _string(item, f"tuple.{name}")


def _validate_resource(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["run_id"], "run_id"); _string(value["source_id"], "source_id")
    for name in ("sample_sequence", "monotonic_ns", "source_age_ms"):
        _integer(value[name], name, 0)
    values = _object(value["values"], "values"); units = _object(value["units"], "units")
    if not values or set(values) != set(units):
        raise ContractError("resource values and units differ")
    for name, item in values.items():
        _string(name, "resource metric")
        if (isinstance(item, bool) or not isinstance(item, (int, float)) or
                (isinstance(item, float) and not math.isfinite(item))):
            raise ContractError("resource values must be finite numbers")
        _string(units[name], f"units.{name}")


def _validate_retirement(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["run_id"], "run_id")
    _identifier_array(value["owned_sessions"], "owned_sessions")
    for name in ("backend_absence_verified", "cached_plans_retired", "default_off_reconnect"):
        if not isinstance(value[name], bool): raise ContractError(f"{name} must be boolean")
    _state(value["state"], catalog)


def _validate_recovery(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    _enum(value["strategy"], "recovery strategy",
          {"EXACT_BINARY_CONFIG_DOWNGRADE", "VERIFIED_ENVIRONMENT_RESTORE"})
    _digest(value["artifact_sha256"], "artifact_sha256")
    _digest(value["configuration_sha256"], "configuration_sha256")
    _enum(value["rehearsal_result"], "rehearsal_result", {"PASS", "FAIL", "NOT_RUN"})
    _state(value["state"], catalog)


def _validate_index(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    payloads = _array(value["payloads"], "payloads", True)
    paths = set()
    for artifact in payloads:
        _artifact(artifact, "payload")
        path = artifact["path"]
        if path in {"index.json", "terminal.json", "validation.json", "export-receipt.json"}:
            raise ContractError("index self/cyclic seal member")
        if path in paths: raise ContractError("duplicate index path")
        paths.add(path)
    if value["payload_flush_fsync_complete"] is not True:
        raise ContractError("payload flush/fsync is not complete")
    _integer(value["index_sequence"], "index_sequence", 1)


def aggregate_pass(state: dict[str, Any]) -> bool:
    return (state.get("technical_result") == "PASS" and
            state.get("evidence_completeness") == "COMPLETE" and
            state.get("exit_state") == "CLEAN" and
            state.get("lifecycle") in {"VALIDATED", "CLOSED"})


def _validate_terminal(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _digest(value["index_sha256"], "index_sha256")
    _state(value["state"], catalog)
    if not isinstance(value["aggregate_pass"], bool): raise ContractError("aggregate_pass must be boolean")
    if value["aggregate_pass"] != aggregate_pass(value["state"]):
        raise ContractError("aggregate_pass contradicts independent state axes")


def _validate_validation(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _digest(value["index_sha256"], "index_sha256")
    _digest(value["terminal_sha256"], "terminal_sha256")
    _enum(value["profile"], "profile", {"private_full", "shared_derived"})
    _enum(value["result"], "validation result", {"PASS", "FAIL", "INCOMPLETE"})
    _identifier_array(value["checks"], "checks", True)


def _validate_export_receipt(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _enum(value["receipt_kind"], "export receipt kind",
          catalog["receipt_kinds"]["export_receipt"])
    _scope_ref(value)
    for name in ("index_sha256", "validation_sha256", "artifact_sha256"):
        _digest(value[name], name)
    _identifier_array(value["recipients"], "recipients", True)
    _timestamp(value["approved_at"], "approved_at")
    _string(value["approval_reference"], "approval_reference")


def validate_cached_sequence(requests: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> None:
    registry: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    snapshots_by_id: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        validate_contract(snapshot)
        key = (snapshot["scope_sha256"], snapshot["session_id"],
               snapshot["prepared_object_id"], snapshot["prepared_generation"])
        if key in registry:
            raise ContractError("duplicate prepared registry generation")
        if snapshot["snapshot_id"] in snapshots_by_id:
            raise ContractError("duplicate snapshot_id")
        registry[key] = snapshot
        snapshots_by_id[snapshot["snapshot_id"]] = snapshot
    latest_snapshot: dict[tuple[str, str], str] = {}
    for request in requests:
        validate_contract(request)
        session_key = (request["scope_sha256"], request["session_id"])
        if request["snapshot_relation"] != "NO_SNAPSHOT":
            attributed = snapshots_by_id.get(request["snapshot_id"])
            if (not attributed or attributed["scope_sha256"] != request["scope_sha256"] or
                    attributed["session_id"] != request["session_id"] or
                    attributed["planning_request_id"] != request["planning_request_id"]):
                raise ContractError("request snapshot attribution is not in the session registry")
        if request["snapshot_relation"] == "CURRENT":
            if request["planning_evidence"] == "REUSED":
                key = (request["scope_sha256"], request["session_id"],
                       request["prepared_object_id"], request["prepared_generation"])
                expected = registry.get(key)
                if (not expected or request["snapshot_id"] != expected["snapshot_id"] or
                        request["planning_request_id"] != expected["planning_request_id"]):
                    raise ContractError("cached request attributed to wrong snapshot generation")
            else:
                latest_snapshot[session_key] = request["snapshot_id"]
        elif (request["snapshot_relation"] == "PREVIOUS" and
              request.get("snapshot_id") != latest_snapshot.get(session_key)):
            raise ContractError("previous snapshot relation does not match session history")


def receipt_matches_scope(receipt: dict[str, Any], scope: dict[str, Any],
                          expected_kind: str) -> bool:
    validate_contract(scope); validate_contract(receipt)
    if (receipt["contract_type"] != "approval_receipt" or
            expected_kind not in load_catalog()["receipt_kinds"]["approval_receipt"] or
            receipt["receipt_kind"] != expected_kind):
        return False
    return receipt["scope_sha256"] == canonical_sha256(scope)


def export_receipt_matches(receipt: dict[str, Any], expected_kind: str,
                           scope_sha256: str, index_sha256: str,
                           validation_sha256: str, artifact_sha256: str) -> bool:
    validate_contract(receipt)
    return (receipt["contract_type"] == "export_receipt" and
            expected_kind in load_catalog()["receipt_kinds"]["export_receipt"] and
            receipt["receipt_kind"] == expected_kind and
            receipt["scope_sha256"] == scope_sha256 and
            receipt["index_sha256"] == index_sha256 and
            receipt["validation_sha256"] == validation_sha256 and
            receipt["artifact_sha256"] == artifact_sha256)


def verify_artifact(bundle_root: Path, artifact: dict[str, Any]) -> None:
    """Verify a referenced regular file by exact byte length and SHA-256."""
    _artifact(artifact, "artifact")
    descriptor = _open_bundle_file(bundle_root, artifact["path"])
    digest = hashlib.sha256()
    size = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ContractError("artifact is not a regular file")
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(descriptor)
    if size != artifact["bytes"] or digest.hexdigest() != artifact["sha256"]:
        raise ContractError("artifact exact-byte identity mismatch")


def verify_scope_inputs(bundle_root: Path, scope: dict[str, Any]) -> None:
    validate_contract(scope)
    for artifact in scope["scope_inputs"].values():
        verify_artifact(bundle_root, artifact)


def verify_seal(bundle_root: Path, index: dict[str, Any], terminal: dict[str, Any],
                validation: dict[str, Any]) -> None:
    """Validate the acyclic payload -> index -> terminal -> receipt chain."""
    for document in (index, terminal, validation):
        validate_contract(document)
    scope = index["scope_sha256"]
    if terminal["scope_sha256"] != scope or validation["scope_sha256"] != scope:
        raise ContractError("seal scope mismatch")
    for artifact in index["payloads"]:
        verify_artifact(bundle_root, artifact)
    index_digest = canonical_sha256(index)
    if terminal["index_sha256"] != index_digest or validation["index_sha256"] != index_digest:
        raise ContractError("seal index digest mismatch")
    if validation["terminal_sha256"] != canonical_sha256(terminal):
        raise ContractError("seal terminal digest mismatch")
    if validation["result"] == "PASS" and not terminal["aggregate_pass"]:
        raise ContractError("validation cannot promote a non-passing terminal")
