#!/usr/bin/env python3
"""Fail-closed three-arm differential verdict for PQ-31."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.phase5.corpus_oracle import (OracleError, SUPPORTED_TYPES, TypedSpool,
                                        UnsupportedEvidence, compare_spools, load_json,
                                        write_json_exclusive)
from tools.phase7.decision_snapshot import SnapshotError, parse_v2
from tools.phase7.witness_record import WitnessError, WitnessRecord, parse_witness


ARMS = ("native", "forced", "shadow")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
PG_SNAPSHOT = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*):(?:|(?:0|[1-9][0-9]*)(?:,(?:0|[1-9][0-9]*))*)\Z")


class DifferentialError(ValueError):
    """The differential evidence contract is malformed."""


def _exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise DifferentialError(f"{name} fields are not exact")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise DifferentialError(f"invalid {name}")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise DifferentialError(f"invalid {name}")
    return value


def _regular_private(path: Path, maximum: int) -> os.stat_result:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (not path.is_absolute() or not __import__("stat").S_ISREG(info.st_mode) or
            info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > maximum):
        raise DifferentialError("spool is not an owned bounded private file")
    return info


def validate_manifest(value: dict[str, Any]) -> dict[str, Any]:
    fields = {"schema_version", "document_type", "run_id", "subject_id",
              "sql_sha256", "ordered", "authorization_reference", "arms"}
    if isinstance(value, dict) and value.get("schema_version") == 2:
        fields.add("bindings_sha256")
    _exact(value, fields, "manifest")
    if type(value["schema_version"]) is not int or value["schema_version"] not in (1, 2) or \
            value["document_type"] != "pq31_differential_run":
        raise DifferentialError("unsupported manifest")
    _identifier(value["run_id"], "run_id")
    _identifier(value["subject_id"], "subject_id")
    _identifier(value["authorization_reference"], "authorization_reference")
    _digest(value["sql_sha256"], "sql_sha256")
    if value["schema_version"] == 2:
        _digest(value["bindings_sha256"], "bindings_sha256")
    if type(value["ordered"]) is not bool or value["arms"] != list(ARMS):
        raise DifferentialError("invalid manifest arm or order contract")
    return value


def validate_receipt(value: dict[str, Any], expected_arm: str) -> dict[str, Any]:
    fields = {"schema_version", "document_type", "run_id", "subject_id", "arm",
              "sql_sha256", "snapshot", "exporter", "backend", "v2_before", "v2_after",
              "witness_before", "witness_after", "spool", "acquisition"}
    if isinstance(value, dict) and value.get("schema_version") == 2:
        fields.add("bindings_sha256")
    _exact(value, fields, f"{expected_arm} receipt")
    if type(value["schema_version"]) is not int or value["schema_version"] not in (1, 2) or \
            value["document_type"] != "pq31_arm_receipt" or \
            value["arm"] != expected_arm:
        raise DifferentialError("receipt identity is invalid")
    for name in ("run_id", "subject_id"):
        _identifier(value[name], name)
    _digest(value["sql_sha256"], "sql_sha256")
    if value["schema_version"] == 2:
        _digest(value["bindings_sha256"], "bindings_sha256")
    snapshot = _exact(value["snapshot"], {"token_sha256", "imported",
        "import_command_complete", "exporter_snapshot", "arm_snapshot"}, "snapshot")
    _digest(snapshot["token_sha256"], "snapshot token digest")
    if type(snapshot["imported"]) is not bool or type(snapshot["import_command_complete"]) is not bool:
        raise DifferentialError("snapshot flags are invalid")
    for name in ("exporter_snapshot", "arm_snapshot"):
        if not isinstance(snapshot[name], str) or len(snapshot[name]) > 256 or \
                not PG_SNAPSHOT.fullmatch(snapshot[name]):
            raise DifferentialError(f"invalid {name}")
    identity_fields = {"pid", "database", "user", "database_oid", "user_oid"}
    exporter = _exact(value["exporter"], identity_fields, "exporter")
    backend = _exact(value["backend"], identity_fields, "backend")
    for identity in (exporter, backend):
        if type(identity["pid"]) is not int or identity["pid"] <= 0:
            raise DifferentialError("invalid backend pid")
        if type(identity["database_oid"]) is not int or identity["database_oid"] <= 0 or \
                type(identity["user_oid"]) is not int or identity["user_oid"] <= 0:
            raise DifferentialError("invalid backend object identity")
    if type(backend["pid"]) is not int or backend["pid"] <= 0:
        raise DifferentialError("invalid backend pid")
    _identifier(backend["database"], "database")
    _identifier(backend["user"], "user")
    for name in ("v2_before", "v2_after", "witness_before", "witness_after"):
        if not isinstance(value[name], str):
            raise DifferentialError(f"{name} is not a string")
    v2_before = parse_v2(value["v2_before"])
    v2_after = parse_v2(value["v2_after"])
    non_evidence_states = {"none", "denied", "withheld", "unavailable", "exhausted", "invalid"}
    if v2_before.get("s") in non_evidence_states and v2_after == v2_before:
        value["_v2_advanced"] = False
    elif (v2_before.get("s") != "complete" or v2_after.get("s") != "complete" or
            v2_before.get("cid") != v2_after.get("cid") or
            v2_before.get("g") != v2_after.get("g") or
            int(v2_after["e"]) not in {int(v2_before["e"]), int(v2_before["e"]) + 1}):
        raise DifferentialError("V2 event binding transition is invalid")
    else:
        value["_v2_advanced"] = int(v2_after["e"]) == int(v2_before["e"]) + 1
    expected_v2 = {
        "native": ("off", "off"),
        "forced": ("off", "external"),
        "shadow": ("shadow_p0", "off"),
    }[expected_arm]
    if value["_v2_advanced"] and (v2_after.get("mode"), v2_after.get("force")) != expected_v2:
        raise DifferentialError("V2 arm mode or force origin mismatch")
    if value["_v2_advanced"] and v2_after.get("enabled") != "on":
        raise DifferentialError("pg_qi is not enabled for the arm")
    value["_shadow_inert"] = not value["_v2_advanced"] or expected_arm != "shadow" or (
        v2_after.get("barrier") == "closed" and v2_after.get("policy") == "pass_through")
    before = parse_witness(value["witness_before"])
    after = parse_witness(value["witness_after"])
    value["_witness_sufficient"] = not after.truncated
    if after.sequence not in {before.sequence, before.sequence + 1}:
        raise DifferentialError("witness sequence transition is invalid")
    value["_witness_advanced"] = after.sequence == before.sequence + 1
    spool = _exact(value["spool"], {"path", "sha256", "bytes", "rows",
        "terminal_status", "sqlstate"}, "spool")
    if not isinstance(spool["path"], str):
        raise DifferentialError("invalid spool path")
    path = Path(spool["path"])
    info = _regular_private(path, 64 * 1024 * 1024)
    _digest(spool["sha256"], "spool sha256")
    if hashlib.sha256(path.read_bytes()).hexdigest() != spool["sha256"] or \
            spool["bytes"] != info.st_size or type(spool["rows"]) is not int or spool["rows"] < 0:
        raise DifferentialError("spool binding mismatch")
    if not isinstance(spool["terminal_status"], str) or \
            spool["terminal_status"] not in {"complete", "error"}:
        raise DifferentialError("invalid spool terminal status")
    if not isinstance(spool["sqlstate"], str) or (spool["terminal_status"] == "complete" and
            spool["sqlstate"] != "") or (spool["terminal_status"] == "error" and
            not re.fullmatch(r"[0-9A-Z]{5}", spool["sqlstate"])):
        raise DifferentialError("invalid spool SQLSTATE")
    with TypedSpool(path, 64 * 1024 * 1024, 64, 1024 * 1024, 10_000_000,
                    allow_unsupported_types=True) as parsed:
        actual_rows = sum(1 for _ in parsed.rows())
        if (actual_rows != spool["rows"] or parsed.terminal_status != spool["terminal_status"] or
                parsed.sqlstate != spool["sqlstate"]):
            raise DifferentialError("spool terminal receipt mismatch")
        value["_unsupported"] = (parsed.has_unsupported_types or
            (value["_witness_advanced"] and
             any(column.oid not in SUPPORTED_TYPES for column in after.columns)))
        if value["_witness_advanced"] and value["_witness_sufficient"] and not (
                parsed.terminal_status == "error" and not parsed.columns):
            descriptor = tuple((column.oid, column.typmod) for column in parsed.columns)
            witnessed = tuple((column.oid, column.typmod) for column in after.columns)
            if descriptor != witnessed:
                raise DifferentialError("libpq and witness descriptor mismatch")
    if value["_witness_advanced"] and ((spool["terminal_status"] == "complete" and
            after.status != "success") or (spool["terminal_status"] == "error" and
            after.status != "error")):
        raise DifferentialError("spool and witness terminal status disagree")
    acquisition = _exact(value["acquisition"], {"complete", "reason"}, "acquisition")
    if type(acquisition["complete"]) is not bool or not isinstance(acquisition["reason"], str):
        raise DifferentialError("invalid acquisition state")
    value["_witness"] = after
    value["_path"] = path
    return value


def _inconclusive(reason: str) -> dict[str, Any]:
    return {"result": "INCONCLUSIVE", "reason": reason}


def evaluate_qualified_pair(left_name: str, left: dict[str, Any], right_name: str,
                            right: dict[str, Any], manifest: dict[str, Any],
                            scratch_root: Path) -> dict[str, Any] | None:
    """Return a proven pairwise FAIL, or None when equal or not fully qualified."""
    expected = {"native": "postgres", "forced": "duckdb", "shadow": "postgres"}
    pair = (left, right)
    snapshots = (left["snapshot"], right["snapshot"])
    qualified = (all(item["_v2_advanced"] and item["_witness_advanced"] and
                     item["_witness_sufficient"] and
                     item["acquisition"]["complete"] and not item["_unsupported"]
                     for item in pair) and
                 left["_witness"].engine == expected[left_name] and
                 right["_witness"].engine == expected[right_name] and
                 all(item["imported"] and item["import_command_complete"]
                     for item in snapshots) and
                 snapshots[0]["token_sha256"] == snapshots[1]["token_sha256"] and
                 snapshots[0]["exporter_snapshot"] == snapshots[1]["exporter_snapshot"] and
                 all(item["arm_snapshot"] == item["exporter_snapshot"] for item in snapshots) and
                 all(item["spool"]["sqlstate"] != "57014" and
                     not (item["spool"]["terminal_status"] == "error" and
                          item["spool"]["rows"] > 0) for item in pair))
    if not qualified:
        return None
    descriptors = {tuple((column.oid, column.typmod)
                          for column in item["_witness"].columns) for item in pair}
    if len(descriptors) != 1:
        return {"result": "FAIL", "reason": "descriptor_mismatch"}
    collations = {tuple(column.collation for column in item["_witness"].columns)
                  for item in pair}
    if len(collations) != 1:
        return {"result": "FAIL", "reason": "collation_mismatch"}
    terminals = {item["spool"]["terminal_status"] for item in pair}
    if len(terminals) != 1:
        return {"result": "FAIL", "reason": "success_error_mismatch"}
    if terminals == {"error"}:
        if left["spool"]["sqlstate"] != right["spool"]["sqlstate"]:
            return {"result": "FAIL", "reason": "sqlstate_mismatch"}
        return None
    comparison = compare_spools(left["_path"], right["_path"], scratch_root,
                                ordered=manifest["ordered"],
                                max_field_bytes=1024 * 1024)
    if comparison["result"] != "PASS":
        return {"result": "FAIL", "reason": comparison["reason"],
                "comparisons": [{"arms": [left_name, right_name], **comparison}]}
    return None


def execution_subject_token(manifest: dict[str, Any]) -> str:
    parts = [manifest["run_id"], manifest["subject_id"], manifest["sql_sha256"]]
    if manifest["schema_version"] == 2:
        parts.append(manifest["bindings_sha256"])
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def validate_cross_bindings(manifest: dict[str, Any],
                            receipts: dict[str, dict[str, Any]]) -> None:
    for arm, receipt in receipts.items():
        for name in ("schema_version", "run_id", "subject_id", "sql_sha256", "bindings_sha256"):
            if receipt.get(name) != manifest.get(name):
                raise DifferentialError(f"{arm} {name} binding mismatch")
    subject_token = execution_subject_token(manifest)
    exporters = [receipt["exporter"] for receipt in receipts.values()]
    if any(item != exporters[0] for item in exporters[1:]):
        raise DifferentialError("exporter identity mismatch")
    backends = [receipt["backend"] for receipt in receipts.values()]
    if len({item["pid"] for item in backends}) != len(backends) or \
            exporters[0]["pid"] in {item["pid"] for item in backends}:
        raise DifferentialError("backend sessions are not isolated")
    for receipt in receipts.values():
        backend = receipt["backend"]
        exporter = receipt["exporter"]
        if any(backend[name] != exporter[name] for name in
               ("database", "user", "database_oid", "user_oid")):
            raise DifferentialError("backend principal differs from exporter")
        witness = receipt["_witness"]
        if receipt["_witness_advanced"] and (witness.token != subject_token or
                witness.pid != backend["pid"] or
                witness.database_oid != backend["database_oid"] or
                witness.user_oid != backend["user_oid"]):
            raise DifferentialError("witness subject or backend binding mismatch")


def validate_no_import_control(manifest: dict[str, Any],
                               subjects: dict[str, dict[str, Any]],
                               raw_control: dict[str, Any]) -> None:
    """Bind the native negative control to this run but a different snapshot."""
    bound = {arm: validate_receipt(copy.deepcopy(subjects[arm]), arm) for arm in ARMS}
    control = validate_receipt(copy.deepcopy(raw_control), "native")
    validate_cross_bindings(manifest, {**bound, "no_import_control": control})
    snapshot = control["snapshot"]
    expected = bound["native"]["snapshot"]
    if (snapshot["imported"] or snapshot["import_command_complete"] or
            not expected["imported"] or not expected["import_command_complete"] or
            snapshot["token_sha256"] != expected["token_sha256"] or
            snapshot["exporter_snapshot"] != expected["exporter_snapshot"] or
            snapshot["arm_snapshot"] == expected["arm_snapshot"]):
        raise DifferentialError("negative control did not prove a different, non-imported snapshot")
    if (not control["acquisition"]["complete"] or not control["_v2_advanced"] or
            not control["_witness_advanced"] or not control["_witness_sufficient"] or
            control["_witness"].engine != "postgres" or
            control["spool"]["terminal_status"] != "complete"):
        raise DifferentialError("negative control native execution was not proved")


def evaluate(manifest: dict[str, Any], raw_receipts: dict[str, dict[str, Any]],
             scratch_root: Path) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    if set(raw_receipts) != set(ARMS):
        raise DifferentialError("receipt arms are not exact")
    receipts = {arm: validate_receipt(copy.deepcopy(raw_receipts[arm]), arm) for arm in ARMS}
    validate_cross_bindings(manifest, receipts)

    if not receipts["shadow"]["_shadow_inert"]:
        return {"result": "FAIL", "reason": "shadow_policy_interference"}
    # Engine/shadow violations are independently meaningful and do not depend on
    # snapshot parity. Unknown or declined execution remains inconclusive.
    native_engine = receipts["native"]["_witness"].engine
    forced_engine = receipts["forced"]["_witness"].engine
    shadow_engine = receipts["shadow"]["_witness"].engine
    if ((receipts["native"]["_witness_advanced"] and native_engine == "duckdb") or
            (receipts["shadow"]["_witness_advanced"] and shadow_engine == "duckdb")):
        return {"result": "FAIL", "reason": "native_or_shadow_interference"}

    for left, right in (("native", "forced"), ("native", "shadow"),
                        ("forced", "shadow")):
        pair_failure = evaluate_qualified_pair(left, receipts[left], right,
                                               receipts[right], manifest, scratch_root)
        if pair_failure is not None:
            return pair_failure
    if any(not receipt["_witness_sufficient"] for receipt in receipts.values()):
        return _inconclusive("witness_metadata_truncated")
    if any(not receipts[arm]["_v2_advanced"] for arm in ("native", "shadow")):
        return _inconclusive("decision_not_advanced")
    if any(not receipts[arm]["_witness_advanced"] for arm in ("native", "shadow")):
        return _inconclusive("witness_not_advanced")
    if native_engine != "postgres" or shadow_engine != "postgres":
        return _inconclusive("native_or_shadow_engine_unproved")
    if any(not receipts[arm]["acquisition"]["complete"] for arm in ("native", "shadow")):
        return _inconclusive("acquisition_incomplete")
    if any(receipts[arm]["spool"]["sqlstate"] == "57014" for arm in ("native", "shadow")):
        return _inconclusive("cancelled")
    if any(receipts[arm]["spool"]["terminal_status"] == "error" and
           receipts[arm]["spool"]["rows"] > 0 for arm in ("native", "shadow")):
        return _inconclusive("partial_output")

    snapshots = [receipt["snapshot"] for receipt in receipts.values()]

    if not receipts["forced"]["_v2_advanced"]:
        return _inconclusive("decision_not_advanced")
    if not receipts["forced"]["_witness_advanced"]:
        return _inconclusive("witness_not_advanced")
    if any(receipt["_unsupported"] for receipt in receipts.values()):
        return _inconclusive("unsupported_result_type")
    if forced_engine == "postgres":
        return _inconclusive("force_declined")
    if forced_engine != "duckdb":
        return _inconclusive("forced_engine_unproved")
    if any(not receipt["acquisition"]["complete"] for receipt in receipts.values()):
        return _inconclusive("acquisition_incomplete")
    if any(receipt["spool"]["sqlstate"] == "57014" for receipt in receipts.values()):
        return _inconclusive("cancelled")
    if any(receipt["spool"]["terminal_status"] == "error" and
           receipt["spool"]["rows"] > 0 for receipt in receipts.values()):
        return _inconclusive("partial_output")
    if any(not item["imported"] or not item["import_command_complete"] for item in snapshots):
        return _inconclusive("snapshot_import_unproved")
    token_digests = {item["token_sha256"] for item in snapshots}
    exporter_snapshots = {item["exporter_snapshot"] for item in snapshots}
    arm_snapshots = {item["arm_snapshot"] for item in snapshots}
    if len(token_digests) != 1 or len(exporter_snapshots) != 1 or \
            arm_snapshots != exporter_snapshots:
        return _inconclusive("snapshot_parity_unproved")

    witness_descriptors = {
        tuple((column.oid, column.typmod) for column in receipt["_witness"].columns)
        for receipt in receipts.values()
    }
    if len(witness_descriptors) != 1:
        return {"result": "FAIL", "reason": "descriptor_mismatch"}
    witness_collations = {
        tuple(column.collation for column in receipt["_witness"].columns)
        for receipt in receipts.values()
    }
    if len(witness_collations) != 1:
        return {"result": "FAIL", "reason": "collation_mismatch"}

    terminals = {receipt["spool"]["terminal_status"] for receipt in receipts.values()}
    if terminals == {"error"}:
        states = {receipt["spool"]["sqlstate"] for receipt in receipts.values()}
        return ({"result": "ERROR_PARITY", "reason": "matching_sqlstate",
                 "sqlstate": next(iter(states))} if len(states) == 1 else
                {"result": "FAIL", "reason": "sqlstate_mismatch"})
    if terminals != {"complete"}:
        return {"result": "FAIL", "reason": "success_error_mismatch"}

    parsed: dict[str, tuple[WitnessRecord, TypedSpool]] = {}
    try:
        for arm, receipt in receipts.items():
            spool = TypedSpool(receipt["_path"], 64 * 1024 * 1024, 64,
                               1024 * 1024, 10_000_000)
            spool.__enter__()
            rows = list(spool.rows())
            witness = receipt["_witness"]
            if (spool.terminal_status != "complete" or len(rows) != receipt["spool"]["rows"] or
                    spool.sqlstate != receipt["spool"]["sqlstate"]):
                raise DifferentialError("spool completion mismatch")
            descriptor = tuple((column.oid, column.typmod) for column in spool.columns)
            witnessed = tuple((column.oid, column.typmod) for column in witness.columns)
            if descriptor != witnessed:
                raise DifferentialError("libpq and witness descriptor mismatch")
            parsed[arm] = (witness, spool)
        collations = {tuple(column.collation for column in item[0].columns)
                      for item in parsed.values()}
        if len(collations) != 1:
            return {"result": "FAIL", "reason": "collation_mismatch"}
    finally:
        for _, spool in parsed.values():
            spool.__exit__()

    comparisons = []
    for observed in ("forced", "shadow"):
        result = compare_spools(receipts["native"]["_path"], receipts[observed]["_path"],
                                scratch_root, ordered=manifest["ordered"],
                                max_field_bytes=1024 * 1024)
        comparisons.append({"arms": ["native", observed], **result})
    failure = next((item for item in comparisons if item["result"] != "PASS"), None)
    if failure:
        return {"result": "FAIL", "reason": failure["reason"],
                "comparisons": comparisons}
    return {"result": "PASS", "reason": "three_arm_equivalent",
            "comparisons": comparisons}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--manifest", required=True, type=Path)
    result.add_argument("--native", required=True, type=Path)
    result.add_argument("--forced", required=True, type=Path)
    result.add_argument("--shadow", required=True, type=Path)
    result.add_argument("--scratch-root", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = {"schema_version": 1, "document_type": "pq31_differential_result",
                  **evaluate(load_json(args.manifest), {
                      "native": load_json(args.native), "forced": load_json(args.forced),
                      "shadow": load_json(args.shadow)}, args.scratch_root)}
    except (OSError, ValueError, SnapshotError, WitnessError, OracleError) as exc:
        report = {"schema_version": 1, "document_type": "pq31_differential_result",
                  "result": "INVALID", "reason": str(exc)}
    write_json_exclusive(args.output, report)
    return 0 if report["result"] in {"PASS", "ERROR_PARITY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
