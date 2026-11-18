#!/usr/bin/env python3
"""Offline PGQI-5 evidence validator and shared-derived exporter."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

try:
    from . import contract_rules as _contract_rules
    from .corpus_oracle import (OracleError, compare_spools, validate_baseline,
                                validate_qualification)
except ImportError:
    import contract_rules as _contract_rules
    from corpus_oracle import (OracleError, compare_spools, validate_baseline,
                               validate_qualification)

ContractError = _contract_rules.ContractError
aggregate_pass = _contract_rules.aggregate_pass
canonical_bytes = _contract_rules.canonical_bytes
canonical_sha256 = _contract_rules.canonical_sha256
strict_load_bytes = _contract_rules.strict_load_bytes
validate_contract = _contract_rules.validate_contract

PRIVATE_REQUIRED_CONTRACTS = {
    "approval_receipt", "preflight", "workload", "release", "request", "snapshot",
    "resource", "retirement", "recovery",
}
REQUIRED_SITE_CELLS = {
    "target_identity", "installed_binary", "configuration", "dependencies",
    "privilege_boundary", "resource_probe", "evidence_store", "recovery_rehearsal",
}
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
SECRET_PATTERNS = (
    re.compile(r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]"),
    re.compile(r"(?i)(?:postgres(?:ql)?|https?|ssh)://"),
    re.compile(r"(?i)\b(?:host|hostaddr|user|dbname)\s*="),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
RESULT_ORDER = {"PASS": 0, "INCOMPLETE": 1, "FAIL": 2, "INVALID": 3}
MAX_FILES = 4096
MAX_DIRECTORIES = 4096
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024


class EvidenceError(ContractError):
    """Fail-closed evidence or export error."""


def _exact_object(value: Any, required: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise EvidenceError(f"{name} fields are not exact")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
        raise EvidenceError(f"invalid {name}")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise EvidenceError(f"invalid {name}")
    return value


def _enum(value: Any, allowed: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise EvidenceError(f"invalid {name}")
    return value


def _load_plain(path: Path, bundle_root: Path, maximum_bytes: int = 1048576) -> dict[str, Any]:
    _duplicate = _contract_rules._duplicate; _finite_float = _contract_rules._finite_float
    _reject_surrogates = _contract_rules._reject_surrogates; _depth = _contract_rules._depth

    relative = path.relative_to(bundle_root)
    descriptor = _open_readonly(bundle_root, relative.as_posix())
    chunks: list[bytes] = []
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise EvidenceError("plain document is not a bounded regular file")
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                raise EvidenceError("plain document truncated during capture")
            chunks.append(chunk); remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise EvidenceError("plain document grew during capture")
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise EvidenceError("plain document changed during capture")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    try:
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_duplicate,
                           parse_constant=lambda token: (_ for _ in ()).throw(
                               EvidenceError(f"non-finite number: {token}")),
                           parse_float=_finite_float)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceError("invalid plain JSON") from exc
    if not isinstance(value, dict) or _depth(value) > 32:
        raise EvidenceError("invalid plain document root or depth")
    _reject_surrogates(value)
    return value


def _open_readonly(root: Path, relative: str) -> int:
    return _contract_rules._open_bundle_file(root, relative)


def _tree_entries(root: Path) -> tuple[set[str], set[str]]:
    if root.is_symlink() or not root.is_dir():
        raise EvidenceError("bundle root must be a real directory")
    files: set[str] = set()
    directories: set[str] = set()
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise EvidenceError("symlink directory in bundle")
        for name in dirnames:
            path = current_path / name
            if path.is_symlink():
                raise EvidenceError("symlink directory in bundle")
            directories.add(path.relative_to(root).as_posix())
            if len(directories) > MAX_DIRECTORIES or len(path.relative_to(root).parts) > 32:
                raise EvidenceError("bundle directory bound exceeded")
        for name in filenames:
            path = current_path / name
            if path.is_symlink():
                raise EvidenceError("symlink file in bundle")
            files.add(path.relative_to(root).as_posix())
            if len(files) > MAX_FILES:
                raise EvidenceError("bundle file-count bound exceeded")
    return files, directories


def _capture_files(root: Path, files: set[str]) -> dict[str, bytes]:
    captured: dict[str, bytes] = {}
    total = 0
    for relative in sorted(files):
        descriptor = _open_readonly(root, relative)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ARTIFACT_BYTES:
                raise EvidenceError("artifact bound exceeded")
            total += before.st_size
            if total > MAX_BUNDLE_BYTES:
                raise EvidenceError("bundle byte bound exceeded")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    raise EvidenceError("artifact truncated during capture")
                chunks.append(chunk); remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise EvidenceError("artifact grew during capture")
            after = os.fstat(descriptor)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise EvidenceError("artifact changed during capture")
            captured[relative] = b"".join(chunks)
        finally:
            os.close(descriptor)
    return captured


def _verify_captured(artifact: dict[str, Any], captured: dict[str, bytes]) -> None:
    payload = captured.get(artifact["path"])
    if (payload is None or len(payload) != artifact["bytes"] or
            hashlib.sha256(payload).hexdigest() != artifact["sha256"]):
        raise EvidenceError("captured artifact identity mismatch")


def _plain_bytes(payload: bytes) -> dict[str, Any]:
    _duplicate = _contract_rules._duplicate; _finite_float = _contract_rules._finite_float
    _reject_surrogates = _contract_rules._reject_surrogates; _depth = _contract_rules._depth
    try:
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_duplicate,
                           parse_constant=lambda token: (_ for _ in ()).throw(
                               EvidenceError(f"non-finite number: {token}")),
                           parse_float=_finite_float)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceError("invalid plain JSON") from exc
    if not isinstance(value, dict) or _depth(value) > 32:
        raise EvidenceError("invalid plain document root or depth")
    _reject_surrogates(value)
    return value


def _worse(left: str, right: str) -> str:
    return left if RESULT_ORDER[left] >= RESULT_ORDER[right] else right


def _state_result(value: dict[str, Any]) -> str:
    technical = value["technical_result"]
    if technical == "FAIL":
        return "FAIL"
    if (technical != "PASS" or value["evidence_completeness"] != "COMPLETE" or
            value["exit_state"] != "CLEAN"):
        return "INCOMPLETE"
    return "PASS"


def _assert_no_credentials(payload: bytes, name: str) -> None:
    if (zipfile.is_zipfile(io.BytesIO(payload)) or
            payload.startswith((b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00",
                                b"7z\xbc\xaf'\x1c", b"Rar!\x1a\x07", b"\x28\xb5\x2f\xfd")) or
            (len(payload) > 262 and payload[257:262] == b"ustar")):
        raise EvidenceError("archive containers are not accepted as evidence artifacts")
    text = payload.decode("latin-1")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise EvidenceError(f"credential-like content in {name}")
    if any(pattern.search(name) for pattern in SECRET_PATTERNS):
        raise EvidenceError("credential-like bundle path")


def _oracle_artifact(value: Any, name: str, indexed: set[str],
                     captured: dict[str, bytes]) -> dict[str, Any]:
    artifact = _exact_object(value, {"path", "sha256", "bytes"}, name)
    if not isinstance(artifact["path"], str) or artifact["path"] not in indexed:
        raise EvidenceError(f"{name} is not indexed")
    _digest(artifact["sha256"], f"{name}.sha256")
    if isinstance(artifact["bytes"], bool) or not isinstance(artifact["bytes"], int) or \
            artifact["bytes"] < 0:
        raise EvidenceError(f"invalid {name}.bytes")
    _verify_captured(artifact, captured)
    return artifact


def _validate_oracle(value: dict[str, Any], workload_ids: set[str],
                     indexed: set[str], captured: dict[str, bytes], root: Path,
                     baseline_cases: dict[str, dict[str, Any]],
                     requests_by_query: dict[str, dict[str, Any]],
                     scratch_root: Path, limits: dict[str, int]) -> tuple[str, dict[str, str]]:
    _exact_object(value, {"schema_version", "document_type", "oracle_id", "cases"}, "oracle")
    if value["schema_version"] != 1 or type(value["schema_version"]) is not int:
        raise EvidenceError("oracle schema_version must be integer 1")
    _enum(value["document_type"], {"oracle"}, "document_type")
    _identifier(value["oracle_id"], "oracle_id")
    if not isinstance(value["cases"], list):
        raise EvidenceError("oracle cases must be an array")
    cases: dict[str, str] = {}
    outcome = "PASS"
    for raw in value["cases"]:
        case = _exact_object(raw, {"query_id", "baseline_spool", "observed_spool",
                                  "result", "raw_retained", "sqlstate"}, "oracle case")
        query_id = _identifier(case["query_id"], "query_id")
        if query_id in cases:
            raise EvidenceError("duplicate oracle query_id")
        baseline_spool = _oracle_artifact(case["baseline_spool"], "baseline_spool",
                                          indexed, captured)
        observed_spool = _oracle_artifact(case["observed_spool"], "observed_spool",
                                          indexed, captured)
        frozen_baseline = baseline_cases.get(query_id)
        if (not frozen_baseline or baseline_spool["path"] != frozen_baseline["spool_path"] or
                baseline_spool["sha256"] != frozen_baseline["spool_sha256"] or
                baseline_spool["bytes"] != frozen_baseline["spool_bytes"]):
            raise EvidenceError("oracle baseline differs from baseline manifest")
        if observed_spool["path"] == baseline_spool["path"]:
            raise EvidenceError("observed spool cannot alias the baseline artifact")
        result = _enum(case["result"], {"PASS", "FAIL", "NOT_RUN"}, "oracle result")
        request = requests_by_query.get(query_id)
        request_bound = bool(request and request["execution_status"] == "SUCCEEDED" and
                             request.get("observed_spool") == observed_spool)
        if (request and request.get("observed_spool") is not None and
                request.get("observed_spool") != observed_spool):
            raise EvidenceError("oracle observation contradicts request spool binding")
        if result != "NOT_RUN" and not request_bound:
            outcome = _worse(outcome, "INCOMPLETE")
        if not isinstance(case["raw_retained"], bool):
            raise EvidenceError("raw_retained must be boolean")
        if case["sqlstate"] is not None and (not isinstance(case["sqlstate"], str) or
                                               not SQLSTATE.fullmatch(case["sqlstate"])):
            raise EvidenceError("invalid oracle SQLSTATE")
        if result == "PASS" and case["sqlstate"] is not None:
            raise EvidenceError("successful oracle case carries SQLSTATE")
        cases[query_id] = result
        outcome = _worse(outcome, "INCOMPLETE" if result == "NOT_RUN" else result)
        if case["raw_retained"] and request_bound:
            if result != "NOT_RUN":
                try:
                    computed = compare_spools(root / baseline_spool["path"],
                                              root / observed_spool["path"], scratch_root,
                                              **limits)
                except OracleError as exc:
                    raise EvidenceError(f"invalid typed oracle evidence: {exc}") from exc
                if computed["baseline_sha256"] != baseline_spool["sha256"] or \
                        computed["observed_sha256"] != observed_spool["sha256"]:
                    raise EvidenceError("oracle spool changed during comparison")
                if computed["result"] != result:
                    raise EvidenceError("declared oracle result contradicts complete comparison")
        else:
            outcome = _worse(outcome, "INCOMPLETE")
    if set(cases) != workload_ids:
        outcome = _worse(outcome, "INCOMPLETE")
    return outcome, cases


def _validate_site_cells(value: dict[str, Any], indexed_hashes: set[str]) -> tuple[str, dict[str, str]]:
    _exact_object(value, {"schema_version", "document_type", "cells"}, "site cells")
    if value["schema_version"] != 1 or type(value["schema_version"]) is not int:
        raise EvidenceError("site cells schema_version must be integer 1")
    _enum(value["document_type"], {"site_cells"}, "document_type")
    if not isinstance(value["cells"], list):
        raise EvidenceError("site cells must be an array")
    cells: dict[str, str] = {}
    outcome = "PASS"
    for raw in value["cells"]:
        cell = _exact_object(raw, {"cell_id", "result", "evidence_sha256"}, "site cell")
        identity = _identifier(cell["cell_id"], "cell_id")
        if identity not in REQUIRED_SITE_CELLS:
            raise EvidenceError("unknown site cell")
        if identity in cells:
            raise EvidenceError("duplicate site cell")
        result = _enum(cell["result"], {"PASS", "FAIL", "NOT_RUN"}, "site cell result")
        _digest(cell["evidence_sha256"], "evidence_sha256")
        if cell["evidence_sha256"] not in indexed_hashes:
            raise EvidenceError("site-cell evidence is not indexed")
        cells[identity] = result
        outcome = _worse(outcome, "INCOMPLETE" if result == "NOT_RUN" else result)
    if set(cells) != REQUIRED_SITE_CELLS:
        outcome = _worse(outcome, "INCOMPLETE")
    return outcome, cells


def _validate_workload_definition(value: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    try:
        coverage = validate_qualification(value)
    except OracleError as exc:
        raise EvidenceError(f"invalid corpus qualification: {exc}") from exc
    cases: dict[str, dict[str, Any]] = {}
    for case in value["execution_corpus"]:
        identity = _identifier(case["query_id"], "query_id")
        if identity in cases:
            raise EvidenceError("duplicate workload definition query_id")
        _digest(case["sql_sha256"], "sql_sha256")
        cases[identity] = case
    if not cases:
        raise EvidenceError("execution corpus must be non-empty")
    return cases, coverage


def _nearest_rank(values: list[int], percentile: int) -> int:
    ordered = sorted(values)
    if not ordered:
        raise EvidenceError("empty measurement percentile")
    return ordered[max(0, math.ceil(percentile / 100 * len(ordered)) - 1)]


def _validate_measurement_evidence(report: dict[str, Any], plan: dict[str, Any],
                                   scope_digest: str, indexed: set[str],
                                   captured: dict[str, bytes],
                                   expected_coverage: dict[str, Any],
                                   indexed_artifacts: list[dict[str, Any]]) -> set[str]:
    if report["scope_sha256"] != scope_digest or \
            report["plan_sha256"] != canonical_sha256(plan):
        raise EvidenceError("measurement report identity differs from frozen scope")
    sample_artifact = report["sample_file"]
    if sample_artifact["path"] not in indexed:
        raise EvidenceError("measurement sample stream is not indexed")
    _verify_captured(sample_artifact, captured)
    payload = captured[sample_artifact["path"]]
    samples: list[dict[str, Any]] = []
    for line in payload.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise EvidenceError("measurement sample stream lacks final record delimiter")
        try:
            sample = strict_load_bytes(line)
        except ContractError as exc:
            raise EvidenceError(f"invalid measurement sample: {exc}") from exc
        if sample.get("contract_type") != "measurement_sample":
            raise EvidenceError("measurement sample stream contains another contract type")
        samples.append(sample)
    if not samples or len(samples) != report["raw_sample_count"]:
        raise EvidenceError("measurement raw sample count mismatch")
    identities = [sample["sample_id"] for sample in samples]
    contract_ids = [sample["contract_id"] for sample in samples]
    if len(identities) != len(set(identities)) or len(contract_ids) != len(set(contract_ids)):
        raise EvidenceError("duplicate measurement sample identity")
    level = next((item for item in plan["concurrency_levels"]
                  if item["level"] == report["level"]), None)
    if level is None:
        raise EvidenceError("measurement report level is outside frozen plan")
    scenarios = {item["scenario_id"]: item for item in plan["required_scenarios"]}
    blocks = {index: item for index, item in enumerate(plan["measurement_blocks"], 1)}
    expected_sequence: list[tuple[str, int, int, str, str, int]] = []
    planned_scenarios = plan["required_scenarios"]
    for block_index, block in blocks.items():
        for order_index, mode in enumerate(block["order"], 1):
            for _ in range(plan["warmup_repetitions"]):
                for scenario in planned_scenarios:
                    if scenario["cache_state"] == "first_use":
                        continue
                    for slot in range(1, level["sessions"] + 1):
                        expected_sequence.append(("WARMUP", block_index, order_index,
                                                  mode, scenario["scenario_id"], slot))
            shift = ((block_index - 1) * 3 + order_index - 1) % len(planned_scenarios)
            ordered = planned_scenarios[shift:] + planned_scenarios[:shift]
            for position, scenario in enumerate(ordered):
                expected_sequence.append(("MEASURE", block_index, order_index, mode,
                                          scenario["scenario_id"],
                                          position % level["sessions"] + 1))
    observed_sequence = [(item["phase"], item["block_index"], item["order_index"],
                          item["mode"], item["scenario_id"], item["session_slot"])
                         for item in samples]
    if report["technical_result"] == "PASS" and observed_sequence != expected_sequence:
        raise EvidenceError("measurement sample sequence differs from frozen execution order")
    if report["technical_result"] != "PASS" and \
            observed_sequence != expected_sequence[:len(observed_sequence)]:
        raise EvidenceError("partial measurement is not a prefix of frozen execution order")
    warmup_keys: set[tuple[str, str, int, int, int]] = set()
    warmup_counts: dict[tuple[str, str, int, int], int] = {}
    measured_keys: set[tuple[str, str, int]] = set()
    for sample in samples:
        if (sample["scope_sha256"] != scope_digest or
                sample["plan_sha256"] != report["plan_sha256"] or
                sample["run_id"] != report["run_id"] or
                sample["level"] != report["level"] or
                sample["scenario_id"] not in scenarios or
                sample["protocol"] != scenarios[sample["scenario_id"]]["protocol"] or
                sample["cache_state"] != scenarios[sample["scenario_id"]]["cache_state"] or
                sample["session_slot"] > level["sessions"]):
            raise EvidenceError("measurement sample lies outside frozen report identity")
        if sample["phase"] == "WARMUP":
            if scenarios[sample["scenario_id"]]["cache_state"] == "first_use":
                raise EvidenceError("first-use scenario cannot have a warmup sample")
            block = blocks.get(sample["block_index"])
            if block is None or sample["order_index"] > 3 or \
                    block["order"][sample["order_index"] - 1] != sample["mode"]:
                raise EvidenceError("measurement warmup order differs from frozen plan")
            prefix = (sample["scenario_id"], sample["mode"], sample["session_slot"],
                      sample["block_index"])
            repetition = warmup_counts.get(prefix, 0)
            key = (*prefix, repetition)
            if key in warmup_keys or repetition >= plan["warmup_repetitions"]:
                raise EvidenceError("measurement warmup multiplicity exceeds frozen plan")
            warmup_keys.add(key)
            warmup_counts[prefix] = repetition + 1
        else:
            block = blocks.get(sample["block_index"])
            if block is None or sample["order_index"] > 3 or \
                    block["order"][sample["order_index"] - 1] != sample["mode"]:
                raise EvidenceError("measurement sample order differs from frozen plan")
            key = (sample["scenario_id"], sample["mode"], sample["block_index"])
            if key in measured_keys:
                raise EvidenceError("duplicate measured scenario/mode/block")
            ordered_ids = list(scenarios)
            shift = ((sample["block_index"] - 1) * 3 +
                     sample["order_index"] - 1) % len(ordered_ids)
            ordered_ids = ordered_ids[shift:] + ordered_ids[:shift]
            expected_slot = ordered_ids.index(sample["scenario_id"]) % level["sessions"] + 1
            if sample["session_slot"] != expected_slot:
                raise EvidenceError("measurement scenario/slot assignment differs from plan")
            measured_keys.add(key)
    measured = [sample for sample in samples if sample["phase"] == "MEASURE"]
    warmups = [sample for sample in samples if sample["phase"] == "WARMUP"]
    if len(measured) != report["sample_count"] or len(warmups) != report["warmup_sample_count"]:
        raise EvidenceError("measurement phase counts mismatch")
    group_map = {(item["scenario_id"], item["mode"]): item for item in report["groups"]}
    expected_groups = {(scenario, mode) for scenario in scenarios
                       for mode in ("disabled", "off", "allowlist")}
    if set(group_map) != expected_groups or len(group_map) != len(report["groups"]):
        raise EvidenceError("measurement report group closure mismatch")
    minimum = plan["statistics"]["minimum_n"]
    for key, group in group_map.items():
        subset = [item for item in measured
                  if (item["scenario_id"], item["mode"]) == key]
        counts = {state.lower(): sum(item["terminal_state"] == state for item in subset)
                  for state in ("PASS", "FAIL", "TIMEOUT", "INCOMPLETE")}
        values = [item["request_interval"]["duration_ns"] for item in subset
                  if item["terminal_state"] == "PASS"]
        postprocess = [item["postprocess_duration_ns"] for item in subset]
        block_medians = []
        for block_id in sorted({item["block_index"] for item in subset}):
            block_values = [item["request_interval"]["duration_ns"] for item in subset
                            if item["block_index"] == block_id and
                            item["terminal_state"] == "PASS"]
            if block_values:
                block_medians.append(_nearest_rank(block_values, 50))
        expected = {"n": len(subset), **counts,
            "p50_ns": _nearest_rank(values, 50) if len(values) >= minimum["p50"] else None,
            "postprocess_p50_ns": _nearest_rank(postprocess, 50) if postprocess else None,
            "p95_ns": _nearest_rank(values, 95) if len(values) >= minimum["p95"] else None,
            "p99_ns": _nearest_rank(values, 99) if len(values) >= minimum["p99"] else None,
            "p95_status": "REPORTED" if len(values) >= minimum["p95"] else
                f"SUPPRESSED(N={len(values)},MIN={minimum['p95']})",
            "p99_status": "REPORTED" if len(values) >= minimum["p99"] else
                f"SUPPRESSED(N={len(values)},MIN={minimum['p99']})",
            "block_median_min_ns": min(block_medians) if block_medians else None,
            "block_median_max_ns": max(block_medians) if block_medians else None}
        if any(group[name] != value for name, value in expected.items()):
            raise EvidenceError("measurement report statistics differ from raw samples")
    by_group = {(item["scenario_id"], item["mode"]): item for item in report["groups"]}
    expected_contrasts = []
    for scenario in plan["required_scenarios"]:
        disabled = by_group[(scenario["scenario_id"], "disabled")]["p50_ns"]
        off = by_group[(scenario["scenario_id"], "off")]["p50_ns"]
        allow = by_group[(scenario["scenario_id"], "allowlist")]["p50_ns"]
        expected_contrasts.append({"scenario_id": scenario["scenario_id"],
            "extension_overhead_p50_ns": (off - disabled
                if off is not None and disabled is not None else None),
            "routing_effect_p50_ns": (allow - off
                if allow is not None and off is not None else None)})
    if report["contrasts"] != expected_contrasts:
        raise EvidenceError("measurement report contrasts differ from raw samples")
    failures = any(item["terminal_state"] != "PASS" or item["oracle_result"] != "PASS"
                   for item in samples)
    warm_cache_unproven = any(
        item["phase"] == "MEASURE" and item["cache_state"] == "warm_cached" and
        item["reason_family"] != "cached" for item in samples)
    warm_scenarios = sum(item["cache_state"] != "first_use"
                         for item in plan["required_scenarios"])
    expected_warmups = (warm_scenarios * 3 * plan["warmup_repetitions"] *
                        level["sessions"] * len(blocks))
    expected_measured = len(scenarios) * 3 * len(blocks)
    if report["technical_result"] == "PASS" and (failures or warm_cache_unproven or
            len(warmups) != expected_warmups or len(measured) != expected_measured):
        raise EvidenceError("measurement technical PASS contradicts raw completeness")
    rules = plan["value_rules"]
    weights: dict[str, dict[str, int]] = {}
    weight_paths: set[str] = set()
    for name in ("frequency", "baseline"):
        digest = rules[f"{name}_weights_sha256"]
        if digest is None:
            continue
        matches = [item for item in indexed_artifacts if item["sha256"] == digest]
        if len(matches) > 1:
            raise EvidenceError("measurement value-weight identity is ambiguous")
        if matches:
            artifact = matches[0]; _verify_captured(artifact, captured)
            value = _plain_bytes(captured[artifact["path"]])
            if not value or any(not SAFE_NAME.fullmatch(key) or
                    isinstance(weight, bool) or not isinstance(weight, int) or weight < 0
                    for key, weight in value.items()):
                raise EvidenceError("measurement value weights are invalid")
            weights[name] = value
            weight_paths.add(artifact["path"])
    planned_natural = {item["natural_query_id"] for item in plan["required_scenarios"]
                       if item["natural_query_id"] is not None}
    passed_natural: set[str] = set()
    expected_per_scenario = 3 * len(blocks)
    for natural in planned_natural:
        scenario_ids = [item["scenario_id"] for item in plan["required_scenarios"]
                        if item["natural_query_id"] == natural]
        subset = [item for item in measured if item["scenario_id"] in scenario_ids]
        if (len(subset) == expected_per_scenario * len(scenario_ids) and
                all(item["terminal_state"] == "PASS" and item["oracle_result"] == "PASS"
                    for item in subset)):
            passed_natural.add(natural)
    denominator = len(planned_natural) + expected_coverage["missing_natural_items"]
    actual_coverage = dict(expected_coverage)
    actual_coverage["measured_natural_share"] = (
        len(passed_natural) / denominator if denominator else None)
    actual_coverage["missing_natural_items"] = denominator - len(passed_natural)
    if passed_natural != planned_natural:
        actual_coverage["frequency_coverage"] = (sum(weights["frequency"].get(key, 0)
            for key in passed_natural) / sum(weights["frequency"].values())
            if weights.get("frequency") and sum(weights["frequency"].values()) else None)
        actual_coverage["baseline_measured_share"] = (sum(weights["baseline"].get(key, 0)
            for key in passed_natural) / sum(weights["baseline"].values())
            if weights.get("baseline") and sum(weights["baseline"].values()) else None)
    if report["coverage"] != actual_coverage:
        raise EvidenceError("measurement report coverage differs from measured samples")
    expected_value = "INCONCLUSIVE"
    complete_coverage = all(report["coverage"][name] == 1 for name in
        ("frequency_coverage", "baseline_measured_share", "measured_natural_share"))
    if (rules["threshold_kind"] == "approved_relative_improvement" and
            report["technical_result"] == "PASS" and complete_coverage and
            set(weights) == {"frequency", "baseline"}):
        block_improvements: list[int] = []
        for block in range(1, len(plan["measurement_blocks"]) + 1):
            weighted_base = weighted_delta = 0
            for scenario in plan["required_scenarios"]:
                natural = scenario["natural_query_id"]
                if natural is None or natural not in weights["frequency"] or \
                        natural not in weights["baseline"]:
                    break
                pair = {item["mode"]: item["request_interval"]["duration_ns"]
                        for item in measured if item["block_index"] == block and
                        item["scenario_id"] == scenario["scenario_id"] and
                        item["terminal_state"] == "PASS" and
                        item["mode"] in {"off", "allowlist"}}
                if set(pair) != {"off", "allowlist"} or pair["off"] <= 0:
                    break
                weight = weights["frequency"][natural]
                weighted_base += pair["off"] * weight
                weighted_delta += (pair["off"] - pair["allowlist"]) * weight
            else:
                if weighted_base > 0:
                    block_improvements.append(weighted_delta * 10000 // weighted_base)
                    continue
            break
        if len(block_improvements) >= rules["minimum_paired_blocks"]:
            conservative_bps = _nearest_rank(block_improvements, 5)
            expected_value = ("POSITIVE" if conservative_bps >=
                rules["minimum_relative_improvement_bps"] else "NEGATIVE")
    if report["value_outcome"] != expected_value:
        raise EvidenceError("measurement value outcome is not reproducible")
    return {sample_artifact["path"], *weight_paths}


def validate_measurement_level(root: Path, index: dict[str, Any], report: dict[str, Any],
                               plan: dict[str, Any], scope_sha256: str) -> None:
    """Recompute an escalation candidate's raw measurement evidence."""
    files, _ = _tree_entries(root)
    captured = _capture_files(root, files)
    indexed = {item["path"] for item in index["payloads"]}
    for item in index["payloads"]:
        _verify_captured(item, captured)
    referenced = _validate_measurement_evidence(
        report, plan, scope_sha256, indexed, captured, report["coverage"],
        index["payloads"])
    expected = {report["sample_file"]["path"]}
    for digest in (plan["value_rules"]["frequency_weights_sha256"],
                   plan["value_rules"]["baseline_weights_sha256"]):
        if digest is not None:
            expected.update(item["path"] for item in index["payloads"]
                            if item["sha256"] == digest)
    if referenced != expected:
        raise EvidenceError("measurement escalation evidence closure mismatch")


def validate_private(root: Path, trusted_scope_sha256: str) -> dict[str, Any]:
    _digest(trusted_scope_sha256, "trusted_scope_sha256")
    files, _ = _tree_entries(root)
    captured = _capture_files(root, files)
    for relative, payload in captured.items():
        _assert_no_credentials(payload, relative)
    required_root = {"scope.json", "index.json", "terminal.json"}
    if not required_root <= files:
        raise EvidenceError("private bundle lacks root contract files")
    scope = strict_load_bytes(captured["scope.json"])
    index = strict_load_bytes(captured["index.json"])
    terminal = strict_load_bytes(captured["terminal.json"])
    if scope["contract_type"] != "scope" or index["contract_type"] != "index" or terminal["contract_type"] != "terminal":
        raise EvidenceError("root contract types are incorrect")
    scope_digest = canonical_sha256(scope)
    if scope_digest != trusted_scope_sha256:
        raise EvidenceError("scope digest is not trusted")
    if index["scope_sha256"] != scope_digest or terminal["scope_sha256"] != scope_digest:
        raise EvidenceError("root scope linkage mismatch")
    if terminal["index_sha256"] != canonical_sha256(index):
        raise EvidenceError("terminal index linkage mismatch")
    if terminal["aggregate_pass"] != aggregate_pass(terminal["state"]):
        raise EvidenceError("terminal aggregate result mismatch")

    indexed = {item["path"] for item in index["payloads"]}
    indexed_hashes = {item["sha256"] for item in index["payloads"]}
    allowed = required_root | indexed
    if files != allowed:
        raise EvidenceError("unexpected or unindexed file in private bundle")
    for item in index["payloads"]:
        _verify_captured(item, captured)
    for item in scope["scope_inputs"].values():
        _verify_captured(item, captured)

    workload_definition_path = scope["scope_inputs"]["workload_definition"]["path"]
    qualification = _plain_bytes(captured[workload_definition_path])
    definition_cases, coverage = _validate_workload_definition(qualification)
    measurement_plan_path = scope["scope_inputs"]["measurement_plan"]["path"]
    try:
        measurement_plan = strict_load_bytes(captured[measurement_plan_path])
    except ContractError as exc:
        raise EvidenceError(f"invalid frozen measurement plan: {exc}") from exc
    if measurement_plan.get("contract_type") != "measurement_plan":
        raise EvidenceError("scope measurement_plan input has wrong contract type")
    natural_ids = {item["query_id"] for item in qualification["natural_inventory"]}
    planned_queries: set[str] = set()
    for scenario in measurement_plan["required_scenarios"]:
        frozen_case = definition_cases.get(scenario["query_id"])
        if (frozen_case is None or scenario["oracle_case_id"] != scenario["query_id"] or
                scenario["natural_query_id"] != frozen_case["natural_query_id"] or
                (scenario["natural_query_id"] is not None and
                 scenario["natural_query_id"] not in natural_ids)):
            raise EvidenceError("measurement scenario differs from frozen corpus or oracle")
        planned_queries.add(scenario["query_id"])
    if planned_queries != set(definition_cases):
        raise EvidenceError("measurement scenarios do not cover the frozen execution corpus")
    qualification_frequency = {item["query_id"]: item["frequency_weight"]
                               for item in qualification["natural_inventory"]}
    qualification_baseline = {item["query_id"]: item["baseline_time_weight"]
                              for item in qualification["natural_inventory"]}
    if measurement_plan["value_rules"]["threshold_kind"] == \
            "approved_relative_improvement":
        if (any(value is None for value in qualification_frequency.values()) or
                any(value is None for value in qualification_baseline.values()) or
                hashlib.sha256(canonical_bytes(qualification_frequency)).hexdigest() !=
                measurement_plan["value_rules"]["frequency_weights_sha256"] or
                hashlib.sha256(canonical_bytes(qualification_baseline)).hexdigest() !=
                measurement_plan["value_rules"]["baseline_weights_sha256"]):
            raise EvidenceError("measurement value weights differ from frozen qualification")
    if (measurement_plan["closure_inputs"]["workload_definition_sha256"] !=
            scope["scope_inputs"]["workload_definition"]["sha256"] or
            measurement_plan["closure_inputs"]["qualification_sha256"] !=
            scope["scope_inputs"]["workload_definition"]["sha256"] or
            measurement_plan["closure_inputs"]["baseline_manifest_sha256"] !=
            scope["scope_inputs"]["baseline_manifest"]["sha256"] or
            measurement_plan["closure_inputs"]["resource_policy_sha256"] !=
            scope["scope_inputs"]["limits"]["sha256"] or
            measurement_plan["closure_inputs"]["release_sha256"] !=
            scope["scope_inputs"]["release_definition"]["sha256"] or
            measurement_plan["closure_inputs"]["target_sha256"] !=
            scope["scope_inputs"]["target"]["sha256"] or
            measurement_plan["closure_inputs"]["dataset_sha256"] !=
            scope["scope_inputs"]["dataset"]["sha256"] or
            measurement_plan["closure_inputs"]["toolkit_sha256"] !=
            scope["scope_inputs"]["toolkit"]["sha256"]):
        raise EvidenceError("measurement plan closure differs from frozen scope inputs")
    try:
        frozen_baseline = validate_baseline(_plain_bytes(captured[
            scope["scope_inputs"]["baseline_manifest"]["path"]]))
    except OracleError as exc:
        raise EvidenceError(f"invalid frozen baseline manifest: {exc}") from exc
    frozen_dataset = _plain_bytes(captured[scope["scope_inputs"]["dataset"]["path"]])
    if (set(frozen_dataset) != {"schema_version", "document_type", "data_sha256"} or
            frozen_dataset["schema_version"] != 1 or
            frozen_dataset["document_type"] != "dataset_identity"):
        raise EvidenceError("invalid frozen dataset identity")
    _digest(frozen_dataset["data_sha256"], "frozen dataset data_sha256")
    if frozen_dataset["data_sha256"] != frozen_baseline["data_sha256"]:
        raise EvidenceError("frozen dataset and baseline identities differ")

    contracts: dict[str, list[dict[str, Any]]] = {}
    oracle: dict[str, Any] | None = None
    baseline_manifest: dict[str, Any] | None = None
    site_cells: dict[str, Any] | None = None
    deferred_jsonl: set[str] = set()
    weight_digests = {digest for digest in (
        measurement_plan["value_rules"]["frequency_weights_sha256"],
        measurement_plan["value_rules"]["baseline_weights_sha256"]) if digest is not None}
    deferred_weights = {item["path"] for item in index["payloads"]
                        if item["sha256"] in weight_digests}
    for relative in sorted(indexed):
        if relative in {entry["path"] for entry in scope["scope_inputs"].values()}:
            continue
        if relative.startswith("raw/"):
            continue
        if relative.endswith(".jsonl"):
            deferred_jsonl.add(relative)
            continue
        if relative in deferred_weights:
            continue
        plain = _plain_bytes(captured[relative])
        if "contract_type" in plain:
            contract = strict_load_bytes(captured[relative])
            contracts.setdefault(contract["contract_type"], []).append(contract)
        elif plain.get("document_type") == "oracle":
            if oracle is not None:
                raise EvidenceError("multiple oracle documents")
            oracle = plain
        elif plain.get("document_type") == "baseline_manifest":
            if baseline_manifest is not None:
                raise EvidenceError("multiple baseline manifests")
            try:
                baseline_manifest = validate_baseline(plain)
            except OracleError as exc:
                raise EvidenceError(f"invalid baseline manifest: {exc}") from exc
        elif plain.get("document_type") == "site_cells":
            if site_cells is not None:
                raise EvidenceError("multiple site-cell documents")
            site_cells = plain
        else:
            raise EvidenceError("unknown indexed payload schema")
    referenced_deferred: set[str] = set()
    expected_measurement_coverage = {
        "structural_eligibility": coverage["confirmed_ratio"],
        "frequency_coverage": coverage["frequency_ratio"],
        "baseline_measured_share": coverage["baseline_time_ratio"],
        "measured_natural_share": coverage["measured_natural_ratio"],
        "missing_natural_items": (coverage["denominator"] -
                                  coverage["natural_items_linked_to_execution"])}
    for report in contracts.get("measurement_report", []):
        referenced_deferred.update(_validate_measurement_evidence(
            report, measurement_plan, scope_digest, indexed, captured,
            expected_measurement_coverage,
            index["payloads"]))
    if deferred_jsonl | deferred_weights != referenced_deferred:
        raise EvidenceError("unreferenced or misplaced measurement evidence stream")
    missing_types = PRIVATE_REQUIRED_CONTRACTS - set(contracts)
    result = ("INCOMPLETE" if missing_types or oracle is None or
              baseline_manifest is None or site_cells is None else "PASS")
    for report in contracts.get("measurement_report", []):
        result = _worse(result, "PASS" if report["technical_result"] == "PASS" else
                        "FAIL" if report["technical_result"] == "FAIL" else "INCOMPLETE")
    singleton = {"approval_receipt", "preflight", "workload", "release", "retirement", "recovery"}
    if any(len(contracts.get(kind, [])) != 1 for kind in singleton if kind in contracts):
        raise EvidenceError("singleton contract family count mismatch")
    contract_ids = [document["contract_id"] for family in contracts.values() for document in family]
    if len(contract_ids) != len(set(contract_ids)):
        raise EvidenceError("duplicate contract identity")

    workload_ids: set[str] = set()
    for workload in contracts.get("workload", []):
        if workload["inventory_sha256"] != hashlib.sha256(
                captured[workload_definition_path]).hexdigest():
            raise EvidenceError("workload inventory digest is not the frozen qualification")
        for entry in workload["entries"]:
            workload_ids.add(entry["query_id"])
            frozen = definition_cases.get(entry["query_id"])
            if (not frozen or frozen["sql_sha256"] != entry["sql"]["sha256"] or
                    frozen["executed_sql_origin"] != entry["executed_sql_origin"] or
                    frozen["dataset_origin"] != entry["dataset_origin"]):
                raise EvidenceError("workload differs from frozen definition")
            if any(entry[name] != "AUTHORISED" for name in
                   ("inventory_authority", "executed_sql_authority", "dataset_authority")):
                raise EvidenceError("workload input authority is not authorised")
            if entry["execution_authority"] != "OPERATION_GO":
                raise EvidenceError("workload execution authority is not OPERATION_GO")
    if workload_ids != set(definition_cases):
        raise EvidenceError("workload definition and execution corpus differ")
    baseline_cases = ({case["query_id"]: case for case in baseline_manifest["cases"]}
                      if baseline_manifest is not None else {})
    if set(baseline_cases) != workload_ids and baseline_manifest is not None:
        raise EvidenceError("baseline manifest and execution corpus differ")
    if baseline_manifest is not None:
        if baseline_manifest != frozen_baseline:
            raise EvidenceError("executed baseline differs from frozen baseline manifest")
        approved = qualification["baseline_authority"]
        for name in ("server_build_sha256", "schema_sha256", "data_sha256",
                     "configuration_sha256", "provider_identity"):
            if baseline_manifest[name] != approved[name]:
                raise EvidenceError("baseline manifest differs from approved baseline identity")
        if baseline_manifest["cases"] != approved["cases"]:
            raise EvidenceError("baseline cases differ from scope-approved artifact identities")
    requests_by_query: dict[str, dict[str, Any]] = {}
    for request in contracts.get("request", []):
        if request["query_id"] in requests_by_query:
            raise EvidenceError("duplicate request query identity")
        if "observed_spool" in request:
            if request["observed_spool"]["path"] not in indexed:
                raise EvidenceError("request observed spool is not indexed")
            _verify_captured(request["observed_spool"], captured)
        requests_by_query[request["query_id"]] = request
    scratch_root = Path(qualification["storage_policy"]["scratch_root"])
    if oracle is not None:
        oracle_result, oracle_cases = _validate_oracle(
            oracle, workload_ids, indexed, captured, root, baseline_cases,
            requests_by_query, scratch_root, qualification["oracle_limits"])
        result = _worse(result, oracle_result)
    else:
        oracle_cases = {}
    if site_cells is not None:
        cells_result, cells = _validate_site_cells(site_cells, indexed_hashes)
        result = _worse(result, cells_result)
    else:
        cells = {}

    for family in contracts.values():
        for contract in family:
            if contract.get("scope_sha256") != scope_digest:
                raise EvidenceError("payload scope linkage mismatch")
    for workload in contracts.get("workload", []):
        for entry in workload["entries"]:
            if entry["sql"]["path"] not in indexed:
                raise EvidenceError("workload SQL artifact is not indexed")
            _verify_captured(entry["sql"], captured)
    if contracts.get("approval_receipt") and contracts.get("preflight"):
        approval = contracts["approval_receipt"][0]
        preflight = contracts["preflight"][0]
        if (approval["receipt_kind"] != "OPERATION_GO" or
                approval["scope_sha256"] != scope_digest or
                approval["preflight_sha256"] != canonical_sha256(preflight)):
            raise EvidenceError("approval receipt preflight linkage mismatch")
    if contracts.get("request") and contracts.get("snapshot"):
        _contract_rules.validate_cached_sequence(contracts["request"], contracts["snapshot"])
        if any(request["query_id"] not in workload_ids for request in contracts["request"]):
            raise EvidenceError("request query_id is outside workload")
    requests = contracts.get("request", [])
    run_ids = {request["run_id"] for request in requests}
    if requests and len(run_ids) != 1:
        raise EvidenceError("private bundle must contain one run identity")
    request_ids = [request["request_id"] for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise EvidenceError("duplicate request identity")
    request_queries = [request["query_id"] for request in requests]
    if len(request_queries) != len(set(request_queries)):
        raise EvidenceError("duplicate request query identity")
    if set(request_queries) != workload_ids:
        result = _worse(result, "INCOMPLETE")
    request_sessions = {request["session_id"] for request in requests}
    if requests and any(resource["run_id"] not in run_ids for resource in contracts.get("resource", [])):
        raise EvidenceError("resource belongs to another run")
    if requests and any(retirement["run_id"] not in run_ids for retirement in contracts.get("retirement", [])):
        raise EvidenceError("retirement belongs to another run")
    if requests and any(set(retirement["owned_sessions"]) != request_sessions
           for retirement in contracts.get("retirement", [])):
        raise EvidenceError("retirement does not own the request sessions")
    for request in contracts.get("request", []):
        status = request["execution_status"]
        if status == "SUCCEEDED" and "sqlstate" in request:
            raise EvidenceError("successful request carries SQLSTATE")
        if status in {"FAILED", "CANCELLED"}:
            result = _worse(result, "FAIL")
        elif status != "SUCCEEDED":
            result = _worse(result, "INCOMPLETE")
    for name in ("preflight", "retirement", "recovery"):
        for document in contracts.get(name, []):
            result = _worse(result, _state_result(document["state"]))
    for recovery in contracts.get("recovery", []):
        result = _worse(result, "FAIL" if recovery["rehearsal_result"] == "FAIL" else
                        "INCOMPLETE" if recovery["rehearsal_result"] == "NOT_RUN" else "PASS")
    for retirement in contracts.get("retirement", []):
        if not all(retirement[name] for name in
                   ("backend_absence_verified", "cached_plans_retired", "default_off_reconnect")):
            result = _worse(result, "FAIL")
    for release in contracts.get("release", []):
        if release["source_dirty"]:
            result = _worse(result, "INCOMPLETE")
    if terminal["state"]["technical_result"] == "FAIL":
        result = _worse(result, "FAIL")
    elif terminal["state"]["technical_result"] in {"NOT_RUN", "ERROR", "INCONCLUSIVE"}:
        result = _worse(result, "INCOMPLETE")
    if result == "PASS" and not terminal["aggregate_pass"]:
        raise EvidenceError("private evidence passes but terminal does not")
    return {"profile": "private_full", "result": result, "scope": scope,
            "scope_sha256": scope_digest, "index": index, "terminal": terminal,
            "oracle_cases": oracle_cases, "site_cells": cells,
            "qualification_coverage": coverage,
            "frequency_weights": ({item["query_id"]: item["frequency_weight"]
                for item in qualification["natural_inventory"]}
                if all(item["frequency_weight"] is not None
                       for item in qualification["natural_inventory"]) else None),
            "baseline_weights": ({item["query_id"]: item["baseline_time_weight"]
                for item in qualification["natural_inventory"]}
                if all(item["baseline_time_weight"] is not None
                       for item in qualification["natural_inventory"]) else None),
            "natural_coverage": coverage}


def _validation_receipt(validated: dict[str, Any], identity: str) -> dict[str, Any]:
    index = validated["index"]; terminal = validated["terminal"]
    checks = (["structure", "hashes", "graph", "oracle", "site_cells"]
              if validated["profile"] == "private_full" else
              ["structure", "hashes", "allow_list", "private_receipt_link"])
    if validated["profile"] == "private_full":
        core = _derived_core_documents(validated)
        checks.extend([
            f"derived-results:{canonical_sha256(core['results.json'])}",
            f"derived-cells:{canonical_sha256(core['site-summary.json'])}",
            f"derived-terminal-state:{canonical_sha256(_shared_terminal_state(validated))}",
        ])
    receipt = {"schema_version": 1, "contract_type": "validation",
               "contract_id": identity, "scope_sha256": validated["scope_sha256"],
               "index_sha256": canonical_sha256(index),
               "terminal_sha256": canonical_sha256(terminal),
               "profile": validated["profile"], "result": validated["result"],
               "checks": checks}
    validate_contract(receipt)
    return receipt


def _write_exclusive(path: Path, payload: bytes, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    owned = os.fstat(descriptor)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise EvidenceError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.link(temporary, path, follow_symlinks=False)
        published = path.stat(follow_symlinks=False)
        if (published.st_dev, published.st_ino) != (owned.st_dev, owned.st_ino):
            raise EvidenceError("published receipt identity mismatch")
    finally:
        os.close(descriptor)
        try:
            current = temporary.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                temporary.unlink()
        except FileNotFoundError:
            pass


def _write_exclusive_at(directory_fd: int, name: str, payload: bytes,
                        mode: int = 0o600) -> None:
    _identifier(name, "export file name")
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode, dir_fd=directory_fd)
    try:
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise EvidenceError("short write")
                view = view[written:]
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor); descriptor = -1
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_validation_receipt(validated: dict[str, Any], output: Path, identity: str) -> None:
    _identifier(identity, "receipt identity")
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise EvidenceError("unsafe receipt parent")
    _write_exclusive(output, canonical_bytes(_validation_receipt(validated, identity)))
    _fsync_directory(output.parent)


def _assert_public_value(value: Any, forbidden_tokens: tuple[str, ...]) -> None:
    if isinstance(value, str):
        if any(token and token.casefold() in value.casefold() for token in forbidden_tokens):
            raise EvidenceError("forbidden canary in shared output")
        if any(pattern.search(value) for pattern in SECRET_PATTERNS):
            raise EvidenceError("secret-like value in shared output")
        if len(value.encode("utf-8")) > 512:
            raise EvidenceError("oversized shared string")
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_public_value(key, forbidden_tokens); _assert_public_value(item, forbidden_tokens)
    elif isinstance(value, list):
        for item in value:
            _assert_public_value(item, forbidden_tokens)


def _derived_core_documents(validated: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results = [{"case_id": "case-" + hashlib.sha256(query_id.encode("utf-8")).hexdigest()[:24],
                "result": result}
               for query_id, result in sorted(validated["oracle_cases"].items())]
    cells = [{"cell_id": identity,
              "result": validated["site_cells"].get(identity, "NOT_RUN")}
             for identity in sorted(REQUIRED_SITE_CELLS)]
    return {
        "results.json": {"schema_version": 1, "document_type": "derived_results",
                         "results": results},
        "site-summary.json": {"schema_version": 1, "document_type": "derived_site_cells",
                              "cells": cells},
    }


def _derived_documents(validated: dict[str, Any], private_receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    documents = _derived_core_documents(validated)
    documents["manifest.json"] = {"schema_version": 1, "document_type": "shared_manifest",
                                  "profile": "shared_derived",
                                  "source_scope_sha256": validated["scope_sha256"],
                                  "private_validation_sha256": canonical_sha256(private_receipt),
                                  "raw_reproducibility": "NOT_CLAIMED"}
    return documents


def _shared_terminal_state(validated: dict[str, Any]) -> dict[str, str]:
    result = validated["result"]
    state = dict(validated["terminal"]["state"])
    if result == "PASS":
        return state
    state["value_outcome"] = "INCONCLUSIVE"
    state["project_decision"] = "PAUSE"
    if result == "FAIL":
        state["technical_result"] = "FAIL"
    else:
        if state["technical_result"] == "PASS":
            state["technical_result"] = "INCONCLUSIVE"
        state["evidence_completeness"] = "PARTIAL"
    return state


def _receipt_binds_derived(receipt: dict[str, Any], documents: dict[str, dict[str, Any]]) -> bool:
    expected = {"derived-results": canonical_sha256(documents["results.json"]),
                "derived-cells": canonical_sha256(documents["site-summary.json"])}
    observed: dict[str, str] = {}
    for check in receipt["checks"]:
        if ":" in check:
            key, digest = check.split(":", 1)
            if key in expected:
                observed[key] = digest
    return observed == expected


def export_shared(private_root: Path, private_receipt_path: Path, output: Path,
                  forbidden_tokens: tuple[str, ...]) -> dict[str, Any]:
    receipt = _load_plain(private_receipt_path, private_receipt_path.parent)
    validate_contract(receipt)
    if receipt["contract_type"] != "validation" or receipt["profile"] != "private_full":
        raise EvidenceError("private validation receipt type mismatch")
    validated = validate_private(private_root, receipt["scope_sha256"])
    if (receipt["contract_type"] != "validation" or receipt["profile"] != "private_full" or
            receipt["scope_sha256"] != validated["scope_sha256"] or
            receipt["index_sha256"] != canonical_sha256(validated["index"]) or
            receipt["terminal_sha256"] != canonical_sha256(validated["terminal"]) or
            receipt["result"] != validated["result"]):
        raise EvidenceError("private validation receipt linkage mismatch")
    documents = _derived_documents(validated, receipt)
    if not _receipt_binds_derived(receipt, documents):
        raise EvidenceError("private receipt does not bind derived values")
    terminal_state = _shared_terminal_state(validated)
    if f"derived-terminal-state:{canonical_sha256(terminal_state)}" not in receipt["checks"]:
        raise EvidenceError("private receipt does not bind derived terminal state")
    for document in documents.values():
        _assert_public_value(document, forbidden_tokens)
    if output.exists() or output.is_symlink():
        raise EvidenceError("export destination must not exist")
    if not SAFE_NAME.fullmatch(output.name) or output.parent.is_symlink() or not output.parent.is_dir():
        raise EvidenceError("unsafe export destination")
    output.mkdir(mode=0o700, parents=False)
    directory_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    owned = os.fstat(directory_fd)
    created_names: list[str] = []
    try:
        document_bytes = {name: canonical_bytes(document) for name, document in documents.items()}
        for name, payload in document_bytes.items():
            _write_exclusive_at(directory_fd, name, payload); created_names.append(name)
        payloads = [{"path": name, "sha256": hashlib.sha256(document_bytes[name]).hexdigest(),
                     "bytes": len(document_bytes[name])} for name in sorted(documents)]
        index = {"schema_version": 1, "contract_type": "index", "contract_id": "shared-index",
                 "scope_sha256": validated["scope_sha256"], "payloads": payloads,
                 "payload_flush_fsync_complete": True, "index_sequence": 1}
        validate_contract(index)
        _write_exclusive_at(directory_fd, "index.json", canonical_bytes(index)); created_names.append("index.json")
        terminal = {"schema_version": 1, "contract_type": "terminal",
                    "contract_id": "shared-terminal", "scope_sha256": validated["scope_sha256"],
                    "index_sha256": canonical_sha256(index), "state": terminal_state,
                    "aggregate_pass": aggregate_pass(terminal_state)}
        validate_contract(terminal)
        _write_exclusive_at(directory_fd, "terminal.json", canonical_bytes(terminal)); created_names.append("terminal.json")
        os.fsync(directory_fd)
        _fsync_directory(output.parent)
        for name, document in (*documents.items(), ("index.json", index), ("terminal.json", terminal)):
            _assert_public_value(name, forbidden_tokens); _assert_public_value(document, forbidden_tokens)
        return {"profile": "shared_derived", "result": validated["result"],
                "scope_sha256": validated["scope_sha256"], "index": index,
                "terminal": terminal, "private_validation_sha256": canonical_sha256(receipt)}
    except Exception:
        for name in reversed(created_names):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.fsync(directory_fd)
        try:
            current = os.stat(output, follow_symlinks=False)
            if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                output.rmdir()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)


def validate_shared(root: Path, private_receipt_path: Path) -> dict[str, Any]:
    files, directories = _tree_entries(root)
    expected = {"manifest.json", "results.json", "site-summary.json", "index.json", "terminal.json"}
    if files != expected or directories:
        raise EvidenceError("shared bundle allow-list mismatch")
    captured = _capture_files(root, files)
    for relative, payload in captured.items():
        _assert_no_credentials(payload, relative)
    manifest = _plain_bytes(captured["manifest.json"])
    results = _plain_bytes(captured["results.json"])
    cells = _plain_bytes(captured["site-summary.json"])
    _exact_object(manifest, {"schema_version", "document_type", "profile",
                             "source_scope_sha256", "private_validation_sha256",
                             "raw_reproducibility"}, "shared manifest")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or
            manifest["document_type"] != "shared_manifest" or
            manifest["profile"] != "shared_derived" or
            manifest["raw_reproducibility"] != "NOT_CLAIMED"):
        raise EvidenceError("invalid shared manifest")
    _digest(manifest["source_scope_sha256"], "source_scope_sha256")
    _digest(manifest["private_validation_sha256"], "private_validation_sha256")
    _exact_object(results, {"schema_version", "document_type", "results"}, "derived results")
    _exact_object(cells, {"schema_version", "document_type", "cells"}, "derived cells")
    if (type(results["schema_version"]) is not int or results["schema_version"] != 1 or
            results["document_type"] != "derived_results" or
            not isinstance(results["results"], list)):
        raise EvidenceError("invalid derived results")
    if (type(cells["schema_version"]) is not int or cells["schema_version"] != 1 or
            cells["document_type"] != "derived_site_cells" or
            not isinstance(cells["cells"], list)):
        raise EvidenceError("invalid derived site cells")
    result_ids: set[str] = set()
    for item in results["results"]:
        item = _exact_object(item, {"case_id", "result"}, "derived result")
        _identifier(item["case_id"], "case_id")
        _enum(item["result"], {"PASS", "FAIL", "NOT_RUN"}, "derived result")
        if item["case_id"] in result_ids:
            raise EvidenceError("duplicate derived case_id")
        result_ids.add(item["case_id"])
    cell_ids: set[str] = set()
    for item in cells["cells"]:
        item = _exact_object(item, {"cell_id", "result"}, "derived cell")
        _identifier(item["cell_id"], "cell_id")
        _enum(item["result"], {"PASS", "FAIL", "NOT_RUN"}, "derived cell result")
        if item["cell_id"] in cell_ids:
            raise EvidenceError("duplicate derived cell_id")
        cell_ids.add(item["cell_id"])
    if cell_ids != REQUIRED_SITE_CELLS:
        raise EvidenceError("derived site-cell set is incomplete")
    for document in (manifest, results, cells):
        _assert_public_value(document, ())
    receipt = _load_plain(private_receipt_path, private_receipt_path.parent)
    validate_contract(receipt)
    if (receipt["contract_type"] != "validation" or receipt["profile"] != "private_full" or
            canonical_sha256(receipt) != manifest["private_validation_sha256"] or
            receipt["scope_sha256"] != manifest["source_scope_sha256"]):
        raise EvidenceError("shared bundle private-receipt linkage mismatch")
    expected_checks = {
        "derived-results": canonical_sha256(results),
        "derived-cells": canonical_sha256(cells),
    }
    observed_checks: dict[str, str] = {}
    for check in receipt["checks"]:
        if ":" in check:
            key, digest = check.split(":", 1)
            if key in expected_checks:
                observed_checks[key] = digest
    if observed_checks != expected_checks:
        raise EvidenceError("shared values are not bound by private receipt")
    index = strict_load_bytes(captured["index.json"])
    terminal = strict_load_bytes(captured["terminal.json"])
    if index["contract_type"] != "index" or terminal["contract_type"] != "terminal":
        raise EvidenceError("shared seal contract types are incorrect")
    if f"derived-terminal-state:{canonical_sha256(terminal['state'])}" not in receipt["checks"]:
        raise EvidenceError("shared terminal state is not bound by private receipt")
    if index["scope_sha256"] != manifest["source_scope_sha256"]:
        raise EvidenceError("shared index scope mismatch")
    if {item["path"] for item in index["payloads"]} != {"manifest.json", "results.json", "site-summary.json"}:
        raise EvidenceError("shared index allow-list mismatch")
    for artifact in index["payloads"]:
        _verify_captured(artifact, captured)
    if (terminal["scope_sha256"] != manifest["source_scope_sha256"] or
            terminal["index_sha256"] != canonical_sha256(index)):
        raise EvidenceError("shared terminal linkage mismatch")
    result = receipt["result"]
    if (_state_result(terminal["state"]) != result or
            terminal["aggregate_pass"] != (result == "PASS")):
        raise EvidenceError("shared terminal contradicts private receipt")
    return {"profile": "shared_derived", "result": result,
            "scope_sha256": manifest["source_scope_sha256"], "index": index,
            "terminal": terminal}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--profile", required=True, choices=("private_full", "shared_derived"))
    validate.add_argument("--bundle", required=True, type=Path)
    validate.add_argument("--private-receipt", type=Path)
    validate.add_argument("--trusted-scope-sha256")
    validate.add_argument("--receipt", required=True, type=Path)
    validate.add_argument("--receipt-id", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--private-bundle", required=True, type=Path)
    export.add_argument("--private-receipt", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--forbidden-token", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            if arguments.profile == "private_full":
                if arguments.trusted_scope_sha256 is None:
                    raise EvidenceError("private validation requires --trusted-scope-sha256")
                validated = validate_private(arguments.bundle, arguments.trusted_scope_sha256)
            else:
                if arguments.private_receipt is None:
                    raise EvidenceError("shared validation requires --private-receipt")
                validated = validate_shared(arguments.bundle, arguments.private_receipt)
            write_validation_receipt(validated, arguments.receipt, arguments.receipt_id)
            print(json.dumps({"profile": validated["profile"], "result": validated["result"]},
                             sort_keys=True, separators=(",", ":")))
            return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3}[validated["result"]]
        exported = export_shared(arguments.private_bundle, arguments.private_receipt,
                                 arguments.output, tuple(arguments.forbidden_token))
        print(json.dumps({"profile": exported["profile"], "result": exported["result"]},
                         sort_keys=True, separators=(",", ":")))
        return 0 if exported["result"] == "PASS" else 1
    except (ContractError, EvidenceError, OSError):
        print(json.dumps({"result": "INVALID", "reason": "evidence_validation_failed"}, sort_keys=True,
                         separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
