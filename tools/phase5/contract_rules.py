#!/usr/bin/env python3
"""Normative PGQI-5 contract loader and semantic rules."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("contract_catalog.json")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(?:\d{3}|\d{6}))?Z$")
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
    required = {"workload_definition", "measurement_plan", "baseline_manifest", "dataset",
                "release_definition", "target", "limits",
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
    _digest(value["recovery_validation_sha256"], "recovery_validation_sha256")
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
    if "observed_spool" in value:
        _artifact(value["observed_spool"], "observed_spool")
        if value["execution_status"] != "SUCCEEDED":
            raise ContractError("only a successful request can bind an observed spool")
    if "request_interval" in value:
        _validate_request_interval(value["request_interval"])


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


MEASUREMENT_MODES = ("disabled", "off", "allowlist")
MEASUREMENT_LIMITATIONS = {
    "NO_SLA_CLAIM", "NO_ROI_CLAIM", "NO_INFRASTRUCTURE_GENERALISATION",
    "NO_GENERAL_SPEEDUP_CLAIM", "HOSTED_CI_NOT_PERFORMANCE_GATE",
    "VALUE_SEPARATE_FROM_SAFETY",
}


def measurement_block_orders(seed: int, block_count: int) -> list[list[str]]:
    """Return the versioned balanced order used by measurement plans."""
    _integer(seed, "seed", 0); _integer(block_count, "block_count", 6)
    if block_count % 6:
        raise ContractError("measurement block count must be a multiple of six")
    orders = [list(order) for order in itertools.permutations(MEASUREMENT_MODES)]
    result: list[list[str]] = []
    for group in range(block_count // 6):
        ranked = sorted(orders, key=lambda order: hashlib.sha256(
            f"pgqi5-balanced-williams-v1:{seed}:{group}:{','.join(order)}".encode("ascii")
        ).digest())
        result.extend([list(order) for order in ranked])
    return result


def _validate_measurement_plan(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    closure = _object(value["closure_inputs"], "closure_inputs")
    closure_fields = {"workload_definition_sha256", "qualification_sha256",
        "baseline_manifest_sha256", "resource_policy_sha256", "release_sha256",
        "target_sha256", "dataset_sha256", "toolkit_sha256", "recovery_plan_sha256",
        "release_artifact_sha256", "release_configuration_sha256",
        "baseline_artifact_sha256", "baseline_configuration_sha256"}
    if set(closure) != closure_fields:
        raise ContractError("measurement closure inputs are incomplete or unknown")
    for name, digest in closure.items():
        _digest(digest, f"closure_inputs.{name}")

    scenarios = _array(value["required_scenarios"], "required_scenarios", True)
    scenario_ids: set[str] = set()
    protocols: set[str] = set()
    cache_states: set[str] = set()
    for item in scenarios:
        scenario = _object(item, "measurement scenario")
        expected = {"scenario_id", "query_id", "case_id", "protocol", "cache_state",
                    "oracle_case_id", "natural_query_id", "expected_result_bytes_max"}
        if set(scenario) != expected:
            raise ContractError("measurement scenario fields are not exact")
        scenario_id = _string(scenario["scenario_id"], "scenario_id")
        if scenario_id in scenario_ids:
            raise ContractError("duplicate measurement scenario_id")
        scenario_ids.add(scenario_id)
        _string(scenario["query_id"], "query_id")
        _string(scenario["case_id"], "case_id", re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"))
        _string(scenario["oracle_case_id"], "oracle_case_id")
        if scenario["natural_query_id"] is not None:
            _string(scenario["natural_query_id"], "natural_query_id")
        protocols.add(_enum(scenario["protocol"], "protocol", {"extended"}))
        cache_states.add(_enum(scenario["cache_state"], "cache_state",
            {"first_use", "warm_cached"}))
        _integer(scenario["expected_result_bytes_max"], "expected_result_bytes_max", 1)
    declared_protocols = set(_identifier_array(value["protocols"], "protocols", True))
    declared_caches = set(_identifier_array(value["cache_states"], "cache_states", True))
    if declared_protocols != protocols or declared_caches != cache_states:
        raise ContractError("declared protocol or cache-state set does not match scenarios")

    seed = _integer(value["seed"], "seed", 0)
    if value["order_algorithm"] != "seeded_balanced_williams_v1":
        raise ContractError("unsupported measurement order algorithm")
    blocks = _array(value["measurement_blocks"], "measurement_blocks", True)
    repetitions = _integer(value["measurement_repetitions"],
                           "measurement_repetitions", 1)
    if len(blocks) != repetitions * 6:
        raise ContractError("measurement block count contradicts repetitions")
    expected_orders = measurement_block_orders(seed, len(blocks))
    for index, (raw, expected_order) in enumerate(zip(blocks, expected_orders), 1):
        block = _object(raw, "measurement block")
        if set(block) != {"block_id", "order"}:
            raise ContractError("measurement block fields are not exact")
        if block["block_id"] != f"block-{index:04d}" or block["order"] != expected_order:
            raise ContractError("measurement block order is not reproducible and balanced")
    warmup_repetitions = _integer(value["warmup_repetitions"],
                                  "warmup_repetitions", 0)
    if "warm_cached" in cache_states and warmup_repetitions < 1:
        raise ContractError("warm-cached measurement requires a warmup")

    levels = _array(value["concurrency_levels"], "concurrency_levels", True)
    prior = 0
    for index, raw in enumerate(levels):
        level = _object(raw, "concurrency level")
        expected = {"level", "sessions", "approval_reference",
                    "server_resource_feed_sha256"}
        if set(level) != expected:
            raise ContractError("concurrency level fields are not exact")
        number = _integer(level["level"], "level", 1)
        sessions = _integer(level["sessions"], "sessions", 1)
        _string(level["approval_reference"], "approval_reference")
        if index == 0 and number != 1 or index and number != prior + 1:
            raise ContractError("concurrency levels must be consecutive from one")
        if sessions != number:
            raise ContractError("each concurrency level must bind the same session count")
        if len(scenarios) % sessions:
            raise ContractError("scenario count must be divisible by every concurrency level")
        server_feed = level["server_resource_feed_sha256"]
        if number == 1:
            if server_feed is not None:
                _digest(server_feed, "server_resource_feed_sha256")
        elif server_feed is None:
            raise ContractError("concurrency above one requires an approved server resource feed")
        else:
            _digest(server_feed, "server_resource_feed_sha256")
        prior = number
    if max(item["sessions"] for item in levels) > len(scenarios):
        raise ContractError("concurrency level exceeds the scenario set")

    statistics = _object(value["statistics"], "statistics")
    if set(statistics) != {"percentile_method", "minimum_n", "outlier_policy",
                           "failure_policy", "variability"}:
        raise ContractError("statistics fields are not exact")
    if statistics["percentile_method"] != "nearest_rank" or \
            statistics["outlier_policy"] != "retain_all" or \
            statistics["failure_policy"] != "retain_and_count" or \
            statistics["variability"] != "per_block_median_range":
        raise ContractError("unsupported statistical rule")
    minimum_n = _object(statistics["minimum_n"], "minimum_n")
    if set(minimum_n) != {"p50", "p95", "p99"}:
        raise ContractError("minimum_n fields are not exact")
    p50 = _integer(minimum_n["p50"], "minimum_n.p50", 1)
    p95 = _integer(minimum_n["p95"], "minimum_n.p95", 1)
    p99 = _integer(minimum_n["p99"], "minimum_n.p99", 1)
    if not (p50 <= p95 <= p99) or p95 < 20 or p99 < 100:
        raise ContractError("tail percentile sample thresholds are not conservative")

    rules = _object(value["value_rules"], "value_rules")
    if set(rules) != {"threshold_kind", "minimum_relative_improvement_bps",
                      "minimum_paired_blocks",
                      "frequency_weights_sha256", "baseline_weights_sha256",
                      "attribution", "incomplete_weights_outcome"}:
        raise ContractError("value_rules fields are not exact")
    kind = _enum(rules["threshold_kind"], "threshold_kind",
                 {"descriptive_only", "approved_relative_improvement"})
    if rules["attribution"] != "measured_correct_natural_subset_only" or \
            rules["incomplete_weights_outcome"] != "INCONCLUSIVE":
        raise ContractError("unsafe value attribution rule")
    for name in ("frequency_weights_sha256", "baseline_weights_sha256"):
        if rules[name] is not None:
            _digest(rules[name], name)
    threshold = rules["minimum_relative_improvement_bps"]
    minimum_blocks = rules["minimum_paired_blocks"]
    if kind == "descriptive_only":
        if threshold is not None or minimum_blocks is not None:
            raise ContractError("descriptive measurement cannot define a value threshold")
    else:
        _integer(threshold, "minimum_relative_improvement_bps", 1)
        _integer(minimum_blocks, "minimum_paired_blocks", 20)
        if rules["frequency_weights_sha256"] is None or rules["baseline_weights_sha256"] is None:
            raise ContractError("approved value threshold requires complete weight identities")


def _validate_request_interval(value: Any) -> None:
    interval = _object(value, "request_interval")
    if set(interval) != {"started_monotonic_ns", "finished_monotonic_ns",
                         "duration_ns", "state", "censored"}:
        raise ContractError("request_interval fields are not exact")
    started = _integer(interval["started_monotonic_ns"], "started_monotonic_ns", 0)
    finished = _integer(interval["finished_monotonic_ns"], "finished_monotonic_ns", 0)
    duration = _integer(interval["duration_ns"], "duration_ns", 0)
    state = _enum(interval["state"], "request interval state",
                  {"NOT_RUN", "COMPLETE", "ERROR", "CENSORED_TIMEOUT"})
    if not isinstance(interval["censored"], bool):
        raise ContractError("request interval censored must be boolean")
    if state == "NOT_RUN":
        if (started, finished, duration) != (0, 0, 0) or interval["censored"]:
            raise ContractError("NOT_RUN request interval contains timing")
    elif not started or finished < started or duration != finished - started:
        raise ContractError("request interval timing is inconsistent")
    if interval["censored"] != (state == "CENSORED_TIMEOUT"):
        raise ContractError("request interval censorship contradicts state")


def _validate_measurement_sample(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _digest(value["plan_sha256"], "plan_sha256")
    for name in ("run_id", "sample_id", "scenario_id", "reason_family"):
        _string(value[name], name)
    _integer(value["level"], "level", 1)
    _enum(value["mode"], "mode", set(MEASUREMENT_MODES))
    _enum(value["protocol"], "protocol", {"extended"})
    _enum(value["cache_state"], "cache_state",
          {"first_use", "warm_cached"})
    phase = _enum(value["phase"], "phase", {"WARMUP", "MEASURE"})
    block = _integer(value["block_index"], "block_index", 0)
    order = _integer(value["order_index"], "order_index", 0)
    _integer(value["session_slot"], "session_slot", 1)
    if not block or not order:
        raise ContractError("sample phase and block position contradict")
    _validate_request_interval(value["request_interval"])
    _integer(value["postprocess_duration_ns"], "postprocess_duration_ns", 0)
    terminal = _enum(value["terminal_state"], "terminal_state",
                     {"PASS", "FAIL", "TIMEOUT", "INCOMPLETE"})
    oracle = _enum(value["oracle_result"], "oracle_result",
                   {"PASS", "FAIL", "NOT_RUN", "INCOMPLETE"})
    _integer(value["result_bytes"], "result_bytes", 0)
    if terminal == "PASS" and oracle != "PASS":
        raise ContractError("passing measurement lacks a passing oracle")
    if terminal == "TIMEOUT" and value["request_interval"]["state"] != "CENSORED_TIMEOUT":
        raise ContractError("timeout sample is not censored")


def _ratio(value: Any, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not math.isfinite(value) or value < 0 or value > 1:
        raise ContractError(f"invalid ratio {name}")


def _validate_measurement_report(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _digest(value["plan_sha256"], "plan_sha256")
    _string(value["run_id"], "run_id"); _integer(value["level"], "level", 1)
    pilot_fields = {"pilot_attempt_sha256", "pilot_window_id",
                    "pilot_dispatch_sha256", "pilot_dispatched_at"}
    if pilot_fields.intersection(value) not in (set(), pilot_fields):
        raise ContractError("pilot measurement binding is incomplete")
    if "pilot_attempt_sha256" in value:
        _digest(value["pilot_attempt_sha256"], "pilot_attempt_sha256")
        _string(value["pilot_window_id"], "pilot_window_id")
        _digest(value["pilot_dispatch_sha256"], "pilot_dispatch_sha256")
        _timestamp(value["pilot_dispatched_at"], "pilot_dispatched_at")
    _artifact(value["sample_file"], "sample_file")
    sample_count = _integer(value["sample_count"], "sample_count", 0)
    raw_sample_count = _integer(value["raw_sample_count"], "raw_sample_count", 0)
    warmup_sample_count = _integer(value["warmup_sample_count"],
                                   "warmup_sample_count", 0)
    if raw_sample_count != sample_count + warmup_sample_count:
        raise ContractError("raw, measured and warmup sample counts contradict")
    groups = _array(value["groups"], "groups", True)
    total = 0
    for raw in groups:
        group = _object(raw, "measurement group")
        expected = {"scenario_id", "mode", "n", "pass", "fail", "timeout",
                    "incomplete", "p50_ns", "p95_ns", "p99_ns",
                    "postprocess_p50_ns",
                    "p95_status", "p99_status", "block_median_min_ns",
                    "block_median_max_ns"}
        if set(group) != expected:
            raise ContractError("measurement group fields are not exact")
        _string(group["scenario_id"], "scenario_id")
        _enum(group["mode"], "mode", set(MEASUREMENT_MODES))
        counts = [_integer(group[name], name, 0)
                  for name in ("pass", "fail", "timeout", "incomplete")]
        n = _integer(group["n"], "n", 0)
        if n != sum(counts):
            raise ContractError("measurement group count mismatch")
        total += n
        for name in ("p50_ns", "p95_ns", "p99_ns", "postprocess_p50_ns", "block_median_min_ns",
                     "block_median_max_ns"):
            if group[name] is not None:
                _integer(group[name], name, 0)
        for name in ("p95_status", "p99_status"):
            if not isinstance(group[name], str) or not re.fullmatch(
                    r"(?:REPORTED|SUPPRESSED\(N=[0-9]+,MIN=[0-9]+\))", group[name]):
                raise ContractError("invalid percentile publication status")
    if total != sample_count:
        raise ContractError("report sample count does not match groups")
    contrasts = _array(value["contrasts"], "contrasts", True)
    contrast_ids: set[str] = set()
    for raw in contrasts:
        contrast = _object(raw, "measurement contrast")
        if set(contrast) != {"scenario_id", "extension_overhead_p50_ns",
                             "routing_effect_p50_ns"}:
            raise ContractError("measurement contrast fields are not exact")
        identity = _string(contrast["scenario_id"], "scenario_id")
        if identity in contrast_ids:
            raise ContractError("duplicate measurement contrast")
        contrast_ids.add(identity)
        for name in ("extension_overhead_p50_ns", "routing_effect_p50_ns"):
            if contrast[name] is not None and (isinstance(contrast[name], bool) or
                    not isinstance(contrast[name], int)):
                raise ContractError("invalid signed measurement contrast")
    coverage = _object(value["coverage"], "coverage")
    expected_coverage = {"structural_eligibility", "frequency_coverage",
                         "baseline_measured_share", "measured_natural_share",
                         "missing_natural_items"}
    if set(coverage) != expected_coverage:
        raise ContractError("measurement coverage fields are not exact")
    for name in expected_coverage - {"missing_natural_items"}:
        _ratio(coverage[name], name)
    _integer(coverage["missing_natural_items"], "missing_natural_items", 0)
    technical = _enum(value["technical_result"], "technical_result",
                      {"PASS", "FAIL", "INCOMPLETE"})
    if value["stop_reason"] is not None:
        _string(value["stop_reason"], "stop_reason")
    if technical == "PASS" and value["stop_reason"] is not None:
        raise ContractError("technical PASS cannot carry a stop reason")
    _enum(value["value_outcome"], "value_outcome",
          {"POSITIVE", "NEGATIVE", "INCONCLUSIVE", "NOT_EVALUATED"})
    if technical == "PASS" and any(group[name] for group in groups
                                    for name in ("fail", "timeout", "incomplete")):
        raise ContractError("technical PASS excludes failed measurement samples")
    limitations = set(_identifier_array(value["limitations"], "limitations", True))
    if limitations != MEASUREMENT_LIMITATIONS:
        raise ContractError("measurement report limitations are incomplete")


def _validate_retirement(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["run_id"], "run_id")
    _identifier_array(value["owned_sessions"], "owned_sessions")
    for name in ("backend_absence_verified", "cached_plans_retired", "default_off_reconnect"):
        if not isinstance(value[name], bool): raise ContractError(f"{name} must be boolean")
    _state(value["state"], catalog)


def _validate_recovery(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    _string(value["rehearsal_id"], "rehearsal_id")
    _enum(value["strategy"], "recovery strategy",
          {"EXACT_BINARY_CONFIG_DOWNGRADE", "VERIFIED_ENVIRONMENT_RESTORE"})
    for name in ("artifact_sha256", "configuration_sha256", "r1_terminal_sha256",
                 "r2_terminal_sha256", "walkthrough_sha256", "validation_sha256"):
        _digest(value[name], name)
    rehearsal_result = _enum(value["rehearsal_result"], "rehearsal_result",
                             {"PASS", "FAIL", "NOT_RUN"})
    eligibility = _enum(value["eligibility"], "recovery eligibility",
                        {"ELIGIBLE_FOR_HUMAN_GRANT", "REFUSED"})
    limitations = set(_identifier_array(value["limitations"], "recovery limitations", True))
    if not RECOVERY_LIMITATIONS <= limitations or not limitations <= \
            RECOVERY_LIMITATIONS | {"CACHED_PLAN_PERSISTENCE_OBSERVED"}:
        raise ContractError("recovery limitations are incomplete")
    if (value["rehearsal_result"] == "PASS") != \
            (eligibility == "ELIGIBLE_FOR_HUMAN_GRANT"):
        raise ContractError("recovery PASS and eligibility disagree")
    _state(value["state"], catalog)
    if value["state"]["technical_result"] != {
            "PASS": "PASS", "FAIL": "FAIL", "NOT_RUN": "NOT_RUN"}[rehearsal_result]:
        raise ContractError("recovery result and state disagree")
    if rehearsal_result == "PASS" and not aggregate_pass(value["state"]):
        raise ContractError("passing recovery state is not clean and complete")


REHEARSAL_ACTIONS = ["STOP_ADMISSION", "ROUTING_OFF", "RETIRE_OWNED_SESSIONS",
    "RECYCLE_POOL_IF_APPLICABLE", "OBSERVE_BACKEND_EXIT", "RESTART_ENVIRONMENT",
    "RESTORE_APPROVED_ARTIFACTS", "RUN_FRESH_SAFE_CONTROL"]
R1_STEPS = ["STOP_ADMISSION", "ROUTING_OFF", "RETIRE_OR_DEALLOCATE",
            "POOL_RECYCLE_OR_NOT_APPLICABLE", "SERVER_EXIT_PROOF", "FRESH_DEFAULT_OFF_CONTROL"]
R2_STEPS = ["R1_CONFIRMED", "STOP_SERVER", "APPLY_APPROVED_RECOVERY",
            "START_SERVER", "VERIFY_START_AND_IDENTITY", "FRESH_DEFAULT_OFF_CONTROL"]
INCIDENT_STEPS = ["STOP_LATCHED", "EVIDENCE_PRESERVED", "POISONED_BACKEND_ISOLATED",
                  "SERVER_EXIT_PROVEN", "GO_INVALIDATED"]
RECOVERY_LIMITATIONS = {"NO_SLA_CLAIM", "NO_RTO_CLAIM", "NO_AUTOMATIC_GATE_GRANT"}
RECOVERY_CHECKS = ["MANDATE_CURRENT", "RECOVERABLE_COPY", "R1_ORDER_AND_PROOFS",
    "R2_STRATEGY_AND_IDENTITY", "SECOND_OPERATOR_WALKTHROUGH",
    "INCIDENT_HANDOFF_AND_NEW_GO", "FRESH_DEFAULT_OFF_CONTROLS"]
CANARY_CONTROLS = ["AMBIENT_FORCE_OFF", "UNSUPPORTED_QUERY", "PARAMETERIZED_QUERY"]
CANARY_CHECKS = ["OPERATION_GO_CURRENT", "SINGLE_DEDICATED_SESSION",
    "POSITIVE_LITERAL_COMPLETE", "NEGATIVE_CONTROLS_COMPLETE", "FULL_ORACLE_PASS",
    "FRESH_SAME_SESSION_SNAPSHOT", "R1_RETIREMENT_COMPLETE",
    "FRESH_DEFAULT_OFF_RECONNECT"]
CANARY_LIMITATIONS = {"NO_CUSTOMER_AUTHORIZATION", "NO_PRODUCTION_AUTHORIZATION",
    "NO_PUBLICATION_AUTHORIZATION", "NO_PERSISTENT_SETTING_AUTHORIZATION",
    "NO_AUTOMATIC_GATE_GRANT"}
INTERNAL_PILOT_LIMITATIONS = {"NO_CUSTOMER_AUTHORIZATION",
    "NO_PRODUCTION_AUTHORIZATION", "NO_PUBLICATION_AUTHORIZATION",
    "NO_PERSISTENT_SETTING_AUTHORIZATION", "NO_AUTOMATIC_GATE_GRANT",
    "MEASURED_SCOPE_ONLY", "VALUE_SEPARATE_FROM_SAFETY"}
CANARY_EXPECTED_SNAPSHOT = {"contract_version": "1", "context": "proven_client",
    "policy_intent": "request_delegation", "reason": "delegation_verified",
    "force_origin": "qi_stack", "classifier_status": "candidate",
    "classifier_reason": "candidate", "observed_engine": "duckdb",
    "verification": "verified", "observation": "duckdb_scan_at_downstream_boundary",
    "request_applied": "on", "duckdb_force_execution": "off"}


def _exact_sequence(value: Any, expected: list[str], name: str) -> None:
    if value != expected:
        raise ContractError(f"{name} is incomplete or out of order")


def _timed_terminal(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    start = _timestamp(value["started_at"], "started_at")
    finish = _timestamp(value["finished_at"], "finished_at")
    duration = _integer(value["duration_ms"], "duration_ms")
    if start >= finish or duration != (finish - start) // timedelta(milliseconds=1):
        raise ContractError("terminal duration does not match observed timestamps")
    result = _enum(value["result"], "rehearsal terminal result",
                   {"PASS", "FAIL", "INCOMPLETE"})
    _state(value["state"], catalog)
    technical = value["state"]["technical_result"]
    expected = {"PASS": "PASS", "FAIL": "FAIL", "INCOMPLETE": "INCONCLUSIVE"}[result]
    if technical != expected:
        raise ContractError("terminal result and state disagree")
    if result == "PASS" and not aggregate_pass(value["state"]):
        raise ContractError("passing terminal state is not clean and complete")


def _validate_rehearsal_mandate(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["rehearsal_id"], "rehearsal_id")
    _string(value["target_cluster_id"], "target_cluster_id")
    _string(value["target_initial_start_id"], "target_initial_start_id")
    if value["artifact_identity_scheme"] != "PGQI_STACK_SHA256_V1":
        raise ContractError("unknown recovery artifact identity scheme")
    _enum(value["pool_profile"], "pool_profile",
          {"DIRECT", "SESSION_POOL", "TRANSACTION_POOL"})
    for name in ("target_copy_identity_sha256", "source_environment_identity_sha256",
                 "recovery_plan_sha256", "runbook_sha256", "approved_artifact_sha256",
                 "approved_configuration_sha256", "candidate_artifact_sha256",
                 "candidate_configuration_sha256"):
        _digest(value[name], name)
    if value["approved_restore_source_sha256"] is not None:
        _digest(value["approved_restore_source_sha256"], "approved_restore_source_sha256")
    start = _timestamp(value["valid_from"], "valid_from")
    finish = _timestamp(value["valid_through"], "valid_through")
    if start >= finish:
        raise ContractError("rehearsal mandate window is empty or reversed")
    primary = _string(value["primary_operator"], "primary_operator")
    second = _string(value["second_operator"], "second_operator")
    implementer = _string(value["implementer"], "implementer")
    if len({primary, second, implementer}) != 3:
        raise ContractError("rehearsal roles must be independent")
    approvals = set(_identifier_array(value["approvals"], "approvals", True))
    if len(approvals) < 3:
        raise ContractError("rehearsal mandate requires three approvals")
    if approvals & {primary, second, implementer}:
        raise ContractError("rehearsal approvers must be independent from execution roles")
    strategies = set(_identifier_array(value["allowed_strategies"],
                                       "allowed_strategies", True))
    if not strategies <= {"EXACT_BINARY_CONFIG_DOWNGRADE", "VERIFIED_ENVIRONMENT_RESTORE"}:
        raise ContractError("unknown rehearsal recovery strategy")
    if ("VERIFIED_ENVIRONMENT_RESTORE" in strategies) != \
            (value["approved_restore_source_sha256"] is not None):
        raise ContractError("restore strategy and approved source disagree")
    _exact_sequence(value["allowed_actions"], REHEARSAL_ACTIONS, "allowed_actions")
    if value["recoverable_copy_verified"] is not True or \
            value["target_copy_identity_sha256"] == value["source_environment_identity_sha256"]:
        raise ContractError("rehearsal target is not a verified distinct recoverable copy")
    if value["lifecycle_executor"] != "HUMAN_OPERATOR_ONLY":
        raise ContractError("rehearsal lifecycle cannot be delegated to tooling")


def _validate_recovery_r1(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("rehearsal_id", "run_id"):
        _string(value[name], name)
    _digest(value["mandate_sha256"], "mandate_sha256")
    _exact_sequence(value["steps"], R1_STEPS, "R1 steps")
    pool = _enum(value["pool_profile"], "pool_profile",
                 {"DIRECT", "SESSION_POOL", "TRANSACTION_POOL"})
    action = _enum(value["pool_action"], "pool_action", {"NOT_APPLICABLE", "RECYCLED"})
    if (pool == "DIRECT") != (action == "NOT_APPLICABLE"):
        raise ContractError("pool profile and action disagree")
    _identifier_array(value["owned_sessions"], "owned_sessions", True)
    for name in ("pool_proof", "positive_probe", "off_proof", "backend_exit_proof", "fresh_control"):
        _artifact(value[name], name)
    _timed_terminal(value, catalog)


def _validate_recovery_r2(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["rehearsal_id"], "rehearsal_id")
    for name in ("mandate_sha256", "r1_terminal_sha256", "source_artifact_sha256",
                 "source_configuration_sha256", "result_artifact_sha256",
                 "result_configuration_sha256"):
        _digest(value[name], name)
    strategy = _enum(value["strategy"], "recovery strategy",
                     {"EXACT_BINARY_CONFIG_DOWNGRADE", "VERIFIED_ENVIRONMENT_RESTORE"})
    if value["restore_source_sha256"] is not None:
        _digest(value["restore_source_sha256"], "restore_source_sha256")
    for name in ("before_cluster_id", "before_start_id", "after_cluster_id", "after_start_id"):
        _string(value[name], name)
    if not isinstance(value["same_data_continuity"], bool):
        raise ContractError("same_data_continuity must be boolean")
    if value["before_start_id"] == value["after_start_id"]:
        raise ContractError("R2 requires a new server start identity")
    if strategy == "EXACT_BINARY_CONFIG_DOWNGRADE":
        if (value["restore_source_sha256"] is not None or
                value["before_cluster_id"] != value["after_cluster_id"] or
                not value["same_data_continuity"]):
            raise ContractError("downgrade evidence has restore semantics")
    elif value["restore_source_sha256"] is None:
        raise ContractError("environment restore lacks its approved source")
    if (value["source_artifact_sha256"] == value["result_artifact_sha256"] and
            value["source_configuration_sha256"] == value["result_configuration_sha256"]):
        raise ContractError("R2 recovery must change artifact or configuration identity")
    _exact_sequence(value["steps"], R2_STEPS, "R2 steps")
    _artifact(value["source_identity_proof"], "source_identity_proof")
    _artifact(value["identity_proof"], "identity_proof")
    _artifact(value["fresh_control"], "fresh_control")
    _timed_terminal(value, catalog)


def _validate_operator_walkthrough(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["rehearsal_id"], "rehearsal_id")
    for name in ("mandate_sha256", "runbook_sha256", "r1_terminal_sha256",
                 "r2_terminal_sha256"):
        _digest(value[name], name)
    roles = [_string(value[name], name) for name in
             ("primary_operator", "second_operator", "implementer")]
    if len(set(roles)) != 3:
        raise ContractError("walkthrough roles must be independent")
    actions = set(_identifier_array(value["actions_performed"],
                                    "actions_performed", True))
    if actions != {"R1_SESSION_RETIREMENT", "INCIDENT_HANDOFF"}:
        raise ContractError("second-operator walkthrough is incomplete")
    _timestamp(value["signed_at"], "signed_at")
    result = _enum(value["result"], "walkthrough result", {"PASS", "FAIL", "INCOMPLETE"})
    _state(value["state"], catalog)
    if value["state"]["technical_result"] != {
            "PASS": "PASS", "FAIL": "FAIL", "INCOMPLETE": "INCONCLUSIVE"}[result]:
        raise ContractError("walkthrough result and state disagree")
    if result == "PASS" and not aggregate_pass(value["state"]):
        raise ContractError("passing walkthrough state is not clean and complete")


def _validate_recovery_incident(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("rehearsal_id", "incident_id"):
        _string(value[name], name)
    _digest(value["mandate_sha256"], "mandate_sha256")
    _exact_sequence(value["steps"], INCIDENT_STEPS, "incident steps")
    backend = _object(value["poisoned_backend"], "poisoned_backend")
    if set(backend) != {"cluster_id", "start_id", "pid", "backend_start", "session_id"}:
        raise ContractError("poisoned backend identity is not exact")
    for name in ("cluster_id", "start_id", "backend_start", "session_id"):
        _string(backend[name], name, UTC if name == "backend_start" else IDENTIFIER)
    _integer(backend["pid"], "backend pid", 1)
    _artifact(value["backend_exit_proof"], "backend_exit_proof")
    _timestamp(value["performed_at"], "performed_at")
    _digest(value["invalidated_go_sha256"], "invalidated_go_sha256")
    _string(value["invalidated_go_nonce"], "invalidated_go_nonce")
    if value["current_go_invalidated"] is not True or value["restart_requires_new_go"] is not True:
        raise ContractError("incident must consume the current GO")
    result = _enum(value["result"], "incident result", {"PASS", "FAIL", "INCOMPLETE"})
    _state(value["state"], catalog)
    if value["state"]["technical_result"] != {
            "PASS": "PASS", "FAIL": "FAIL", "INCOMPLETE": "INCONCLUSIVE"}[result]:
        raise ContractError("incident result and state disagree")
    if result == "PASS" and not aggregate_pass(value["state"]):
        raise ContractError("passing incident state is not clean and complete")


def _validate_recovery_validation(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["rehearsal_id"], "rehearsal_id")
    for name in ("mandate_sha256", "r1_terminal_sha256", "r2_terminal_sha256",
                 "walkthrough_sha256"):
        _digest(value[name], name)
    if value["incident_sha256"] is not None:
        _digest(value["incident_sha256"], "incident_sha256")
    _timestamp(value["checked_at"], "checked_at")
    result = _enum(value["result"], "recovery validation result", {"PASS", "FAIL"})
    decision = _enum(value["decision"], "recovery validation decision",
                     {"ELIGIBLE_FOR_HUMAN_GRANT", "REFUSED"})
    _identifier_array(value["checks"], "recovery validation checks", True)
    limitations = set(_identifier_array(value["limitations"], "recovery limitations", True))
    if not RECOVERY_LIMITATIONS <= limitations or not limitations <= \
            RECOVERY_LIMITATIONS | {"CACHED_PLAN_PERSISTENCE_OBSERVED"}:
        raise ContractError("recovery validation limitations are incomplete")
    if (result == "PASS") != (decision == "ELIGIBLE_FOR_HUMAN_GRANT"):
        raise ContractError("recovery validation result and decision disagree")
    if result == "PASS" and (value["incident_sha256"] is None or
            value["checks"] != RECOVERY_CHECKS):
        raise ContractError("passing recovery validation is incomplete")


def _snapshot_tuple(value: Any, name: str) -> None:
    snapshot = _object(value, name)
    expected = {"contract_version", "context", "policy_intent", "reason",
                "force_origin", "classifier_status", "classifier_reason",
                "observed_engine", "verification", "observation",
                "request_applied", "duckdb_force_execution"}
    if set(snapshot) != expected:
        raise ContractError(f"{name} fields are not exact")
    for field, item in snapshot.items():
        _string(item, f"{name}.{field}")


def _validate_canary_plan(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["canary_id"], "canary_id")
    _artifact(value["measurement_plan"], "measurement_plan")
    _artifact(value["level_authorization"], "level_authorization")
    _digest(value["operation_go_sha256"], "operation_go_sha256")
    ledger = _object(value["attempt_ledger"], "attempt_ledger")
    if set(ledger) != {"path", "device", "inode", "identity"}:
        raise ContractError("attempt ledger identity is not exact")
    if (not isinstance(ledger["path"], str) or not ledger["path"].startswith("/") or
            "\x00" in ledger["path"]):
        raise ContractError("attempt ledger path is not absolute")
    _integer(ledger["device"], "attempt ledger device", 1)
    _integer(ledger["inode"], "attempt ledger inode", 1)
    _artifact(ledger["identity"], "attempt ledger identity")
    _string(value["private_bundle_path"], "private_bundle_path",
            re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$"))
    _string(value["positive_query_id"], "positive_query_id")
    _artifact(value["positive_sql"], "positive_sql")
    _digest(value["positive_literal_sha256"], "positive_literal_sha256")
    _artifact(value["expected_spool"], "expected_spool")
    _exact_sequence(value["negative_controls"], CANARY_CONTROLS, "negative_controls")
    control_sql = _array(value["negative_control_sql"], "negative_control_sql", True)
    if len(control_sql) != len(CANARY_CONTROLS):
        raise ContractError("canary control SQL count differs from controls")
    for item in control_sql:
        _artifact(item, "negative_control_sql")
    _snapshot_tuple(value["expected_snapshot"], "expected_snapshot")
    expected = value["expected_snapshot"]
    if expected != CANARY_EXPECTED_SNAPSHOT:
        raise ContractError("canary expected snapshot does not prove safe delegation")
    _integer(value["maximum_result_bytes"], "maximum_result_bytes", 1)
    if value["session_count"] != 1 or type(value["session_count"]) is not int:
        raise ContractError("canary requires exactly one session")
    if value["pool_profile"] != "DIRECT":
        raise ContractError("canary does not permit a pool")


def _validate_canary_run(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("canary_id", "session_id"):
        _string(value[name], name)
    for name in ("plan_sha256", "operation_go_sha256"):
        _digest(value[name], name)
    backend = _object(value["backend_identity"], "backend_identity")
    if set(backend) != {"cluster_id", "start_id", "pid", "backend_start", "session_id"}:
        raise ContractError("canary backend identity is not exact")
    for name in ("cluster_id", "start_id", "backend_start", "session_id"):
        _string(backend[name], name, UTC if name == "backend_start" else IDENTIFIER)
    _integer(backend["pid"], "backend pid", 1)
    if backend["session_id"] != value["session_id"]:
        raise ContractError("canary backend and run session differ")
    for name in ("positive_execution_proof", "positive_request", "positive_snapshot", "positive_spool",
                 "ambient_force_off_proof", "oracle_proof"):
        _artifact(value[name], name)
    controls = _array(value["control_proofs"], "control_proofs", True)
    if len(controls) != len(CANARY_CONTROLS):
        raise ContractError("canary control proof count differs from plan")
    for control in controls:
        _artifact(control, "control_proof")
    _timed_terminal(value, catalog)


def _validate_canary_retirement(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("canary_id", "owned_session_id"):
        _string(value[name], name)
    for name in ("plan_sha256", "run_sha256"):
        _digest(value[name], name)
    _artifact(value["backend_exit_proof"], "backend_exit_proof")
    _artifact(value["fresh_reconnect_proof"], "fresh_reconnect_proof")
    _timestamp(value["performed_at"], "performed_at")
    result = _enum(value["result"], "canary retirement result",
                   {"PASS", "FAIL", "INCOMPLETE"})
    _state(value["state"], catalog)
    expected = {"PASS": "PASS", "FAIL": "FAIL", "INCOMPLETE": "INCONCLUSIVE"}[result]
    if value["state"]["technical_result"] != expected or \
            (result == "PASS" and not aggregate_pass(value["state"])):
        raise ContractError("canary retirement result and state disagree")


def _validate_canary_validation(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["canary_id"], "canary_id")
    for name in ("plan_sha256", "run_sha256", "retirement_sha256"):
        _digest(value[name], name)
    _timestamp(value["checked_at"], "checked_at")
    result = _enum(value["result"], "canary validation result", {"PASS", "FAIL"})
    decision = _enum(value["decision"], "canary validation decision",
                     {"ELIGIBLE_FOR_HUMAN_GRANT", "REFUSED"})
    _exact_sequence(value["checks"], CANARY_CHECKS, "canary checks")
    limitations = set(_identifier_array(value["limitations"], "canary limitations", True))
    if limitations != CANARY_LIMITATIONS:
        raise ContractError("canary limitations are incomplete")
    if (result == "PASS") != (decision == "ELIGIBLE_FOR_HUMAN_GRANT"):
        raise ContractError("canary validation result and decision disagree")


def _validate_canary(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["canary_id"], "canary_id")
    for name in ("plan_sha256", "run_sha256", "retirement_sha256", "validation_sha256"):
        _digest(value[name], name)
    result = _enum(value["result"], "canary result", {"PASS", "FAIL", "NOT_RUN"})
    eligibility = _enum(value["eligibility"], "canary eligibility",
                        {"ELIGIBLE_FOR_HUMAN_GRANT", "REFUSED"})
    limitations = set(_identifier_array(value["limitations"], "canary limitations", True))
    if limitations != CANARY_LIMITATIONS:
        raise ContractError("canary limitations are incomplete")
    _state(value["state"], catalog)
    technical = value["state"]["technical_result"]
    expected_technical = {"PASS": "PASS", "FAIL": "FAIL", "NOT_RUN": "NOT_RUN"}[result]
    if ((result == "PASS") != (eligibility == "ELIGIBLE_FOR_HUMAN_GRANT") or
            technical != expected_technical or
            (result == "PASS" and not aggregate_pass(value["state"]))):
        raise ContractError("canary result, eligibility and state disagree")


def _validate_gate_receipt(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _enum(value["gate_id"], "gate_id", catalog["gate_ids"])
    _scope_ref(value); _digest(value["evidence_sha256"], "evidence_sha256")
    _timestamp(value["approved_at"], "approved_at")
    if not _identifier_array(value["approvals"], "approvals", True):
        raise ContractError("gate receipt lacks an approver")
    _string(value["nonce"], "nonce")
    if "subject_id" in value:
        _string(value["subject_id"], "subject_id")
    if value["gate_id"] == "HARD_STOP_CLOSED" and "subject_id" not in value:
        raise ContractError("hard-stop closure receipt lacks subject_id")


def _validate_internal_pilot_plan(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["pilot_id"], "pilot_id")
    for name in ("measurement_plan", "operation_go", "canary_validation",
                 "canary_acceptance"):
        _artifact(value[name], name)
    validate_relative_path(value["canary_bundle_path"])
    ledger = _object(value["attempt_ledger"], "attempt_ledger")
    if set(ledger) != {"path", "device", "inode", "identity", "storage_control",
                       "custodian"}:
        raise ContractError("pilot attempt ledger identity is not exact")
    if (not isinstance(ledger["path"], str) or not ledger["path"].startswith("/") or
            "\x00" in ledger["path"]):
        raise ContractError("pilot attempt ledger path is not absolute")
    _integer(ledger["device"], "pilot attempt ledger device", 1)
    _integer(ledger["inode"], "pilot attempt ledger inode", 1)
    _artifact(ledger["identity"], "pilot attempt ledger identity")
    if "/" in ledger["identity"]["path"]:
        raise ContractError("pilot attempt ledger identity must be a root file")
    _enum(ledger["storage_control"], "pilot attempt ledger storage control",
          {"EXTERNAL_APPEND_ONLY"})
    _string(ledger["custodian"], "pilot attempt ledger custodian")
    windows = _array(value["approved_windows"], "approved_windows", True)
    identities: set[str] = set(); levels: list[int] = []
    for raw in windows:
        window = _object(raw, "approved window")
        if set(window) != {"window_id", "level", "private_bundle_path",
                           "level_authorization"}:
            raise ContractError("approved window fields are not exact")
        identity = _string(window["window_id"], "window_id")
        if identity in identities:
            raise ContractError("duplicate approved window")
        identities.add(identity)
        levels.append(_integer(window["level"], "level", 1))
        validate_relative_path(window["private_bundle_path"])
        _artifact(window["level_authorization"], "level_authorization")
    if levels != list(range(1, len(levels) + 1)):
        raise ContractError("approved windows must cover consecutive levels from one")
    paths = [item["private_bundle_path"] for item in windows]
    for index, left in enumerate(paths):
        left_parts = Path(left).parts
        for right in paths[index + 1:]:
            right_parts = Path(right).parts
            common = min(len(left_parts), len(right_parts))
            if left_parts[:common] == right_parts[:common]:
                raise ContractError("approved windows require distinct non-nested bundles")
    _string(value["retention_owner"], "retention_owner")
    if value["retention_due_at"] is not None:
        _timestamp(value["retention_due_at"], "retention_due_at")


def _validate_internal_pilot_window(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("pilot_id", "window_id", "run_id"):
        _string(value[name], name)
    _digest(value["plan_sha256"], "plan_sha256")
    _digest(value["attempt_record_sha256"], "attempt_record_sha256")
    _integer(value["level"], "level", 1)
    validate_relative_path(value["private_bundle_path"])
    for name in ("private_validation_receipt", "level_authorization",
                 "measurement_report", "retirement",
                 "dispatch_stop_proof", "fresh_off_proof"):
        _artifact(value[name], name)
    _timed_terminal(value, catalog)


def _validate_pilot_dispatch_stop(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("pilot_id", "window_id", "run_id"):
        _string(value[name], name)
    _timestamp(value["stopped_at"], "stopped_at")
    _enum(value["admission_state"], "admission_state", {"STOPPED"})


def _validate_pilot_fresh_off(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value)
    for name in ("pilot_id", "window_id", "run_id", "session_id"):
        _string(value[name], name)
    _timestamp(value["observed_at"], "observed_at")
    for name in ("pg_qi_enabled", "routing_state", "duckdb_force_execution"):
        _enum(value[name], name, {"off"})


def _validate_pilot_retention(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["pilot_id"], "pilot_id")
    _string(value["owner"], "retention owner")
    _timestamp(value["performed_at"], "performed_at")
    _identifier_array(value["artifacts_retained"], "artifacts_retained", True)
    _enum(value["result"], "retention result", {"PASS"})


def _validate_internal_pilot_closeout(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    _scope_ref(value); _string(value["pilot_id"], "pilot_id")
    _digest(value["plan_sha256"], "plan_sha256")
    windows = _array(value["windows"], "windows", True)
    for item in windows:
        _artifact(item, "window")
    findings = _array(value["hard_stop_findings"], "hard_stop_findings")
    finding_ids: set[str] = set()
    for raw in findings:
        finding = _object(raw, "hard-stop finding")
        if set(finding) != {"finding_id", "status", "evidence", "closure_approval"}:
            raise ContractError("hard-stop finding fields are not exact")
        identity = _string(finding["finding_id"], "finding_id")
        if identity in finding_ids:
            raise ContractError("duplicate hard-stop finding")
        finding_ids.add(identity)
        _enum(finding["status"], "hard-stop status", {"OPEN", "CLOSED"})
        _artifact(finding["evidence"], "hard-stop evidence")
        if finding["closure_approval"] is not None:
            _artifact(finding["closure_approval"], "hard-stop closure approval")
        if (finding["status"] == "CLOSED") != (finding["closure_approval"] is not None):
            raise ContractError("hard-stop status and closure approval disagree")
    records = _array(value["service_work_records"], "service_work_records", True)
    record_ids: set[str] = set()
    for raw in records:
        record = _object(raw, "service-work record")
        if set(record) != {"record_id", "category", "duration_minutes", "outcome"}:
            raise ContractError("service-work record fields are not exact")
        identity = _string(record["record_id"], "record_id")
        if identity in record_ids:
            raise ContractError("duplicate service-work record")
        record_ids.add(identity)
        _enum(record["category"], "service-work category",
              {"SETUP", "OPERATION", "DIAGNOSIS", "RECOVERY", "CLOSEOUT"})
        _integer(record["duration_minutes"], "duration_minutes", 0)
        _enum(record["outcome"], "service-work outcome",
              {"COMPLETED", "FAILED", "INCOMPLETE"})
    decisions = _object(value["decisions"], "decisions")
    expected = {"safety", "evidence_completeness", "exit", "workload_relevance",
                "measured_value", "service_work", "project_decision"}
    if set(decisions) != expected:
        raise ContractError("closeout decisions are incomplete or unknown")
    for name in ("safety", "evidence_completeness", "exit"):
        _enum(decisions[name], name, {"PASS", "FAIL", "INCOMPLETE"})
    _enum(decisions["workload_relevance"], "workload_relevance",
          {"RELEVANT", "NOT_RELEVANT", "INCONCLUSIVE"})
    _enum(decisions["measured_value"], "measured_value",
          {"POSITIVE", "NEGATIVE", "INCONCLUSIVE", "NOT_EVALUATED"})
    _enum(decisions["service_work"], "service_work",
          {"ACCEPTABLE", "UNACCEPTABLE", "INCONCLUSIVE"})
    _enum(decisions["project_decision"], "project_decision",
          {"CONTINUE", "TARGETED_CHANGE", "PAUSE", "STOP"})
    retention = _object(value["retention"], "retention")
    if set(retention) != {"status", "owner", "due_at", "evidence"}:
        raise ContractError("retention fields are not exact")
    _enum(retention["status"], "retention status", {"COMPLETE", "PENDING"})
    _string(retention["owner"], "retention owner")
    if retention["due_at"] is not None:
        _timestamp(retention["due_at"], "retention due_at")
    if retention["evidence"] is not None:
        _artifact(retention["evidence"], "retention evidence")
    if retention["status"] == "PENDING" and retention["due_at"] is None:
        raise ContractError("pending retention requires a due date")
    if (retention["status"] == "COMPLETE") != (retention["evidence"] is not None):
        raise ContractError("retention completion and evidence disagree")
    if value["share_release"] is not None:
        _artifact(value["share_release"], "share_release")
    shared = _array(value["shared_derived_paths"], "shared_derived_paths")
    shared_paths: list[str] = []
    for raw in shared:
        item = _object(raw, "shared-derived path")
        if set(item) != {"bundle_path", "private_validation_receipt"}:
            raise ContractError("shared-derived path fields are not exact")
        shared_paths.append(validate_relative_path(item["bundle_path"]))
        validate_relative_path(item["private_validation_receipt"])
    if len(shared_paths) != len(set(shared_paths)):
        raise ContractError("duplicate shared-derived path")
    if bool(shared) != (value["share_release"] is not None):
        raise ContractError("shared-derived paths require SHARE_RELEASE")
    _enum(value["shared_release_state"], "shared_release_state",
          {"NOT_REQUESTED", "RELEASED"})
    _timestamp(value["checked_at"], "checked_at")
    result = _enum(value["result"], "closeout result", {"PASS", "FAIL", "INCOMPLETE"})
    eligibility = _enum(value["eligibility"], "closeout eligibility",
                        {"ELIGIBLE_FOR_HUMAN_GRANT", "REFUSED"})
    limitations = set(_identifier_array(value["limitations"], "limitations", True))
    if limitations != INTERNAL_PILOT_LIMITATIONS:
        raise ContractError("internal pilot limitations are incomplete")
    _state(value["state"], catalog)
    expected_technical = {"PASS": "PASS", "FAIL": "FAIL",
                          "INCOMPLETE": "INCONCLUSIVE"}[result]
    if value["state"]["technical_result"] != expected_technical or \
            (result == "PASS") != aggregate_pass(value["state"]):
        raise ContractError("closeout result and aggregate state disagree")
    if (result == "PASS") != (eligibility == "ELIGIBLE_FOR_HUMAN_GRANT"):
        raise ContractError("closeout result and eligibility disagree")


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
