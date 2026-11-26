#!/usr/bin/env python3
"""Run typed differential checks and qualify bound routing in a lab."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.phase5.corpus_oracle import (MAGIC_V2, OracleError, TypedSpool,
                                        compare_spools, load_json, write_json_exclusive)
from tools.phase7.differential_oracle import (ARMS, HEX64, PG_SNAPSHOT, DifferentialError,
                                              compare_receipt_results, evaluate,
                                              evaluate_qualified_pair, execution_subject_token,
                                              validate_cross_bindings,
                                              validate_manifest, validate_no_import_control,
                                              validate_receipt)
from tools.phase7.decision_snapshot import SnapshotError, parse_v2
from tools.phase7.witness_record import WitnessError, parse_witness


class RunError(RuntimeError):
    pass


def private_directory(path: Path) -> None:
    info = path.stat()
    if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise RunError("run root must be an owned absolute 0700 directory")


def one_line(process: subprocess.Popen[str], name: str) -> str:
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line or not line.endswith("\n") or "\x00" in line:
        raise RunError(f"{name} did not produce one complete line")
    return line[:-1]


def parse_export(line: str) -> tuple[str, str, int, str, str, int, int]:
    fields = line.split("\t")
    if (len(fields) != 8 or fields[0] != "EXPORT" or
            not re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{8}-[0-9]+", fields[1]) or
            not PG_SNAPSHOT.fullmatch(fields[2]) or not fields[4] or not fields[5]):
        raise DifferentialError("invalid exporter output")
    try:
        pid, database_oid, user_oid = int(fields[3]), int(fields[6]), int(fields[7])
        if pid <= 0 or database_oid <= 0 or user_oid <= 0:
            raise ValueError
    except ValueError as exc:
        raise DifferentialError("invalid exporter identity") from exc
    return fields[1], fields[2], pid, fields[4], fields[5], database_oid, user_oid


def parse_arm(line: str) -> dict[str, object]:
    fields = line.split("\t")
    if len(fields) != 16 or fields[0] != "ARM":
        raise DifferentialError(f"invalid arm output field count: {len(fields)}: {line[:1024]!r}")
    if fields[1] not in {"complete", "incomplete"} or not fields[2] or len(fields[2]) > 128:
        raise DifferentialError("invalid arm acquisition state")
    if fields[3] and not re.fullmatch(r"[0-9A-Z]{5}", fields[3]):
        raise DifferentialError("invalid arm SQLSTATE")
    if fields[15] not in {"complete", "error", "incomplete"} or \
            (fields[1] == "complete") != (fields[15] in {"complete", "error"}):
        raise DifferentialError("invalid arm terminal state")
    if ((fields[15] == "complete" and fields[3]) or
            (fields[15] == "error" and not fields[3])):
        raise DifferentialError("arm SQLSTATE contradicts terminal state")
    try:
        rows = int(fields[4])
        if rows < 0:
            raise ValueError
        if fields[6] and fields[9] and fields[10]:
            pid = int(fields[6]); database_oid = int(fields[9]); user_oid = int(fields[10])
            if pid <= 0 or database_oid <= 0 or user_oid <= 0 or not fields[7] or not fields[8]:
                raise ValueError
        else:
            if any(fields[index] for index in range(5, 11)):
                raise ValueError
            pid = database_oid = user_oid = 0
    except ValueError as exc:
        raise DifferentialError("invalid arm identity or row count") from exc
    if fields[5] and not PG_SNAPSHOT.fullmatch(fields[5]):
        raise DifferentialError("invalid arm snapshot")
    return {"complete": fields[1] == "complete", "reason": fields[2],
            "sqlstate": fields[3], "rows": rows, "arm_snapshot": fields[5],
            "pid": pid, "database": fields[7], "user": fields[8],
            "database_oid": database_oid, "user_oid": user_oid,
            "v2_before": fields[11], "v2_after": fields[12],
            "witness_before": fields[13], "witness_after": fields[14],
            "terminal_status": fields[15]}


def arm_receipt(manifest: dict, arm: str, result: dict, spool: Path,
                exporter: dict, snapshot: str, token_digest: str, *, imported: bool) -> dict:
    bindings = {"bindings_sha256": manifest["bindings_sha256"]} if manifest["schema_version"] == 2 else {}
    return {"schema_version": manifest["schema_version"], "document_type": "pq31_arm_receipt",
            "run_id": manifest["run_id"], "subject_id": manifest["subject_id"],
            "arm": arm, "sql_sha256": manifest["sql_sha256"], "exporter": exporter, **bindings,
            "snapshot": {"token_sha256": token_digest, "imported": imported,
                         "import_command_complete": imported, "exporter_snapshot": snapshot,
                         "arm_snapshot": result["arm_snapshot"]},
            "backend": {key: result[key] for key in
                        ("pid", "database", "user", "database_oid", "user_oid")},
            **{key: result[key] for key in
               ("v2_before", "v2_after", "witness_before", "witness_after")},
            "spool": {"path": str(spool), "sha256": hashlib.sha256(spool.read_bytes()).hexdigest(),
                      "bytes": spool.stat().st_size, "rows": result["rows"],
                      "terminal_status": result["terminal_status"], "sqlstate": result["sqlstate"]},
            "acquisition": {"complete": result["complete"], "reason": result["reason"]}}


def serialize_bindings(path: Path, expected_digest: str) -> bytearray:
    with path.open("rb") as source:
        raw = source.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise RunError("bindings file does not match manifest")
    try:
        parameters = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunError("invalid bindings JSON") from exc
    if not isinstance(parameters, list) or not 1 <= len(parameters) <= 64:
        raise RunError("bindings must contain 1 to 64 parameters")
    binary = bytearray(struct.pack("!I", len(parameters)))
    for parameter in parameters:
        if not isinstance(parameter, dict) or set(parameter) != {"oid", "value"}:
            raise RunError("invalid binding fields")
        oid, value = parameter["oid"], parameter["value"]
        if (type(oid) is not int or not 0 <= oid < 2**32 or
                (value is not None and not isinstance(value, str))):
            raise RunError("invalid binding type or value")
        if value is None:
            binary.extend(struct.pack("!II", oid, 2**32 - 1))
            continue
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RunError("invalid UTF-8 binding") from exc
        if b"\x00" in encoded or len(encoded) > 1024 * 1024:
            raise RunError("binding exceeds the text parameter contract")
        binary.extend(struct.pack("!II", oid, len(encoded)))
        binary.extend(encoded)
    return binary


def measured_bounds(samples: list[dict], saving_ms: int, saving_percent: int) -> dict:
    postgres_low = min(sample["native_ns"] for sample in samples) // 1_000_000
    duckdb_high = (max(sample["bound_ns"] for sample in samples) + 999_999) // 1_000_000
    saving = postgres_low - duckdb_high
    clears_floors = (saving > 0 and saving >= saving_ms and
                     saving * 100 >= postgres_low * saving_percent)
    return {"postgres_lower_ms": postgres_low, "duckdb_upper_ms": duckdb_high,
            "min_saving_ms": saving_ms, "min_saving_percent": saving_percent,
            "beneficial": "pass" if clears_floors else "veto"}


def qualify_bound(args: argparse.Namespace, manifest: dict, command: list[str],
                  exporter: dict, snapshot: str, token_digest: str) -> dict:
    report = {"schema_version": 1, "document_type": "pq54_bound_qualification",
              "eligible": "unknown", "safe": "unknown", "beneficial": "unknown",
              "result": "INCONCLUSIVE", "samples": []}
    if args.site_binding:
        report["site_binding"] = args.site_binding
    receipts = {}

    def acquire(label: str, arm: str, trial_key: str = "") -> tuple[dict, int, tuple[int, int], str]:
        spool = args.run_root / f"{label}.spool"
        process = subprocess.run(
            [*command, "--mode", "bound-arm", "--arm", arm, "--spool", str(spool)],
            input=trial_key + "\n", capture_output=True, text=True, check=False)
        lines = process.stdout.splitlines()
        if len(lines) != 1:
            raise RunError(f"{label} did not produce one result")
        fields = lines[0].rsplit("\t", 3)
        if len(fields) != 4:
            raise DifferentialError("bound arm timing fields are missing")
        result = parse_arm(fields[0])
        if process.returncode != 0 or not result["complete"]:
            raise RunError(f"{label} did not complete ({result['reason']})")
        raw = arm_receipt(manifest, arm, result, spool, exporter, snapshot,
                          token_digest, imported=True)
        write_json_exclusive(args.run_root / f"{label}.receipt.json", raw)
        receipt = validate_receipt(copy.deepcopy(raw), arm)
        receipts[label] = receipt
        validate_cross_bindings(manifest, receipts)
        if receipt["spool"]["terminal_status"] != "complete":
            raise RunError(f"{label} returned SQLSTATE {receipt['spool']['sqlstate']}")
        if not (receipt["_v2_advanced"] and receipt["_witness_advanced"] and
                receipt["_witness_sufficient"] and not receipt["_unsupported"] and
                receipt["snapshot"]["arm_snapshot"] == snapshot):
            raise RunError(f"{label} lacks a complete execution, decision or snapshot")
        if not re.fullmatch(r"[1-9][0-9]*", fields[1]) or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]):
            raise DifferentialError("invalid bound arm timing or saving floors")
        milliseconds, percent = map(int, fields[2].split(":"))
        if milliseconds > 2**31 - 1 or percent > 100:
            raise DifferentialError("saving floors are outside the policy range")
        return receipt, int(fields[1]), (milliseconds, percent), fields[3]

    probe, _, floors, key = acquire("probe", "bound_probe")
    decision = parse_v2(probe["v2_after"])
    if probe["_witness"].engine != "postgres" or decision["policy"] != "pass_through":
        return {**report, "reason": "native_probe_unproved"}
    if decision["eligible"] == "veto":
        return {**report, "result": "FAIL", "eligible": "veto", "reason": "ineligible_binding"}
    if decision["eligible"] != "unknown" or not HEX64.fullmatch(key):
        return {**report, "reason": "qualification_key_unavailable"}

    for index in range(args.bound_pairs):
        order = ("native", "bound") if index % 2 == 0 else ("bound", "native")
        pair = {}
        durations = {}
        for arm in order:
            receipt, duration, observed_floors, observed_key = acquire(
                f"pair-{index + 1}-{arm}", arm, key if arm == "bound" else "")
            if observed_floors != floors:
                return {**report, "reason": "saving_floors_changed"}
            if arm == "native" and receipt["_witness"].engine != "postgres":
                return {**report, "reason": "native_execution_unproved"}
            if arm == "bound":
                decision = parse_v2(receipt["v2_after"])
                if observed_key and observed_key != key:
                    return {**report, "reason": "qualification_key_changed"}
                if (observed_key != key or decision["force"] != "qi_stack" or
                        decision["policy"] != "request_delegation"):
                    return {**report, "reason": "bound_rule_not_applied"}
                if receipt["_witness"].engine != "duckdb":
                    if receipt["_witness"].engine == "postgres" and decision["eligible"] == "veto":
                        return {**report, "result": "FAIL", "eligible": "veto",
                                "reason": "provider_declined"}
                    return {**report, "reason": "bound_execution_unproved"}
                if (decision["eligible"], decision["safe"], decision["beneficial"]) != ("pass", "pass", "pass"):
                    return {**report, "reason": "bound_decision_unproved"}
            pair[arm] = receipt
            durations[f"{arm}_ns"] = duration
        failure = compare_receipt_results("native", pair["native"], "bound", pair["bound"],
                                          manifest, args.run_root)
        if failure is not None:
            return {**report, **failure, "eligible": "pass", "safe": "veto"}
        report["samples"].append({"order": list(order), **durations})

    bounds = measured_bounds(report["samples"], *floors)
    report.update(bounds, eligible="pass", safe="pass")
    if bounds["beneficial"] == "pass":
        return {**report, "result": "PASS", "reason": "measured_saving", "bound_key": key}
    return {**report, "result": "FAIL", "reason": "saving_below_floor"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--driver", required=True, type=Path)
    result.add_argument("--conninfo", required=True)
    result.add_argument("--manifest", required=True, type=Path)
    result.add_argument("--sql-file", required=True, type=Path)
    result.add_argument("--bindings-file", type=Path)
    result.add_argument("--bound-pairs", type=int, help="measure this many native and bound pairs")
    result.add_argument("--run-root", required=True, type=Path)
    result.add_argument("--concurrent-sql-file", type=Path)
    result.add_argument("--site-binding", help="require this site on every isolated-lab connection")
    result.add_argument("--deadline-ms", type=int, default=30000)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    document_type = ("pq54_bound_qualification" if args.bound_pairs is not None
                     else "pq31_differential_result")
    site_args = ["--site-binding", args.site_binding] if args.site_binding else []
    exporter: subprocess.Popen[str] | None = None
    proven_failure: dict[str, object] | None = None
    try:
        private_directory(args.run_root)
        manifest = validate_manifest(load_json(args.manifest))
        sql = args.sql_file.read_bytes()
        if b"\x00" in sql or hashlib.sha256(sql).hexdigest() != manifest["sql_sha256"]:
            raise RunError("SQL file does not match manifest")
        if (args.bindings_file is not None) != (manifest["schema_version"] == 2):
            raise RunError("bound runs require a version 2 manifest and a bindings file")
        if args.bound_pairs is not None and (args.bound_pairs < 2 or
                args.bindings_file is None or args.concurrent_sql_file is not None):
            raise RunError("paired qualification needs at least two pairs, bindings and a fixed snapshot")
        binding_args = []
        if args.bindings_file is not None:
            encoded = serialize_bindings(args.bindings_file, manifest["bindings_sha256"])
            path = args.run_root / "validated-bindings"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
            binding_args = ["--bindings-file", str(path)]
        subject_copy = args.run_root / "validated-subject.sql"
        descriptor = os.open(subject_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW, 0o600)
        try:
            offset = 0
            while offset < len(sql):
                written = os.write(descriptor, sql[offset:])
                if written <= 0:
                    raise RunError("validated SQL copy was not written completely")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        exporter = subprocess.Popen(
            [str(args.driver), "--mode", "exporter", "--conninfo", args.conninfo,
             "--deadline-ms", str(args.deadline_ms), *site_args], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        token, exported_snapshot, exporter_pid, exporter_database, exporter_user, \
            exporter_database_oid, exporter_user_oid = parse_export(
            one_line(exporter, "exporter"))
        token_digest = hashlib.sha256(token.encode()).hexdigest()
        subject_token = execution_subject_token(manifest)
        exporter_identity = {"pid": exporter_pid, "database": exporter_database,
            "user": exporter_user, "database_oid": exporter_database_oid,
            "user_oid": exporter_user_oid}
        arm_command = [str(args.driver), "--conninfo", args.conninfo, "--snapshot", token,
                       "--sql-file", str(subject_copy), "--subject-token", subject_token,
                       "--deadline-ms", str(args.deadline_ms), *site_args, *binding_args]
        if args.bound_pairs is not None:
            report = qualify_bound(args, manifest, arm_command, exporter_identity,
                                   exported_snapshot, token_digest)
            write_json_exclusive(args.run_root / "result.json", report)
            return 0 if report["result"] == "PASS" else 1
        if args.concurrent_sql_file is not None:
            mutation = subprocess.run(
                [str(args.driver), "--mode", "command", "--conninfo", args.conninfo,
                 "--sql-file", str(args.concurrent_sql_file),
                 "--deadline-ms", str(args.deadline_ms), *site_args],
                capture_output=True, text=True, check=False)
            if mutation.returncode != 0 or mutation.stdout != "COMMAND\tcomplete\n":
                raise RunError("concurrent snapshot control failed")
        receipts: dict[str, dict] = {}
        validated_receipts: dict[str, dict] = {}
        completed_results: dict[str, dict[str, object]] = {}
        incomplete_arms: list[dict[str, object]] = []
        seen_backend_pids = {exporter_pid}
        for arm in ARMS:
            spool = args.run_root / f"{arm}.spool"
            process = subprocess.run(
                [*arm_command, "--mode", "arm", "--arm", arm, "--spool", str(spool)],
                capture_output=True, text=True, check=False)
            lines = process.stdout.splitlines()
            if not lines:
                if spool.exists():
                    spool_descriptor = os.open(spool, os.O_RDONLY | os.O_NOFOLLOW |
                                               os.O_NONBLOCK)
                    try:
                        prefix = os.read(spool_descriptor, len(MAGIC_V2))
                    finally:
                        os.close(spool_descriptor)
                    if prefix and not MAGIC_V2.startswith(prefix):
                        raise DifferentialError("abandoned spool has invalid framing")
                    if prefix == MAGIC_V2:
                        try:
                            with TypedSpool(spool, 64 * 1024 * 1024, 64,
                                            1024 * 1024, 10_000_000,
                                            allow_unsupported_types=True) as abandoned:
                                sum(1 for _ in abandoned.rows())
                        except OracleError as exc:
                            if not (str(exc).startswith("truncated ") or
                                    str(exc) == "missing v2 trailer"):
                                raise
                incomplete_arms.append({"arm": arm, "reason": "driver_no_result"})
                continue
            if len(lines) != 1:
                raise DifferentialError(f"{arm} produced multiple result lines")
            arm_result = parse_arm(lines[0])
            if arm_result["complete"] and process.returncode != 0:
                raise DifferentialError("complete arm has nonzero driver status")
            spool_available = spool.exists()
            if not spool_available and arm_result["complete"]:
                raise DifferentialError(f"{arm} complete result has no spool")
            if arm_result["pid"]:
                if arm_result["pid"] in seen_backend_pids:
                    raise DifferentialError("arm backend sessions are not isolated")
                seen_backend_pids.add(int(arm_result["pid"]))
            if not arm_result["complete"]:
                proven_interference = False
                shadow_policy_interference = False
                proven_difference: dict[str, object] | None = None
                parsed_witness_before = (parse_witness(str(arm_result["witness_before"]))
                    if arm_result["witness_before"] else None)
                parsed_witness_after = (parse_witness(str(arm_result["witness_after"]))
                    if arm_result["witness_after"] else None)
                parsed_v2_before = (parse_v2(str(arm_result["v2_before"]))
                    if arm_result["v2_before"] else None)
                parsed_v2_after = (parse_v2(str(arm_result["v2_after"]))
                    if arm_result["v2_after"] else None)
                if arm_result["pid"]:
                    if (arm_result["database"] != exporter_database or
                            arm_result["user"] != exporter_user or
                            arm_result["database_oid"] != exporter_database_oid or
                            arm_result["user_oid"] != exporter_user_oid or
                            arm_result["pid"] == exporter_pid):
                        raise DifferentialError("incomplete arm identity mismatch")
                if parsed_witness_before is not None and parsed_witness_after is not None:
                    if parsed_witness_after.sequence not in {
                            parsed_witness_before.sequence,
                            parsed_witness_before.sequence + 1}:
                        raise DifferentialError("incomplete witness transition is invalid")
                    if parsed_witness_after.sequence == parsed_witness_before.sequence + 1:
                        if (not arm_result["pid"] or
                                parsed_witness_after.token != subject_token or
                                parsed_witness_after.pid != arm_result["pid"] or
                                parsed_witness_after.database_oid != arm_result["database_oid"] or
                                parsed_witness_after.user_oid != arm_result["user_oid"]):
                            raise DifferentialError("incomplete witness binding mismatch")
                if parsed_v2_before is not None and parsed_v2_after is not None:
                    non_evidence = {"none", "denied", "withheld", "unavailable",
                                    "exhausted", "invalid"}
                    valid_v2_transition = (
                        parsed_v2_before.get("s") in non_evidence and
                        parsed_v2_after == parsed_v2_before) or (
                        parsed_v2_before.get("s") == "complete" and
                        parsed_v2_after.get("s") == "complete" and
                        parsed_v2_before.get("cid") == parsed_v2_after.get("cid") and
                        parsed_v2_before.get("g") == parsed_v2_after.get("g") and
                        int(parsed_v2_after["e"]) in {int(parsed_v2_before["e"]),
                                                      int(parsed_v2_before["e"]) + 1})
                    if not valid_v2_transition:
                        raise DifferentialError("incomplete V2 transition is invalid")
                    if (parsed_v2_before.get("s") == "complete" and
                            parsed_v2_after.get("s") == "complete" and
                            int(parsed_v2_after["e"]) == int(parsed_v2_before["e"]) + 1):
                        expected_mode, expected_force = {
                            "native": ("off", "off"),
                            "forced": ("off", "external"),
                            "shadow": ("shadow_p0", "off"),
                        }[arm]
                        if ((parsed_v2_after.get("mode"), parsed_v2_after.get("force")) !=
                                (expected_mode, expected_force) or
                                parsed_v2_after.get("enabled") != "on"):
                            raise DifferentialError("incomplete V2 arm configuration mismatch")
                prefix = b""
                if spool_available:
                    spool_descriptor = os.open(spool, os.O_RDONLY | os.O_NOFOLLOW |
                                               os.O_NONBLOCK)
                    try:
                        prefix = os.read(spool_descriptor, len(MAGIC_V2))
                    finally:
                        os.close(spool_descriptor)
                if prefix and not MAGIC_V2.startswith(prefix):
                    raise DifferentialError("incomplete spool has invalid framing")
                parsed_partial = None
                parsed_partial_rows = None
                if prefix == MAGIC_V2:
                    try:
                        with TypedSpool(spool, 64 * 1024 * 1024, 64,
                                        1024 * 1024, 10_000_000,
                                        allow_unsupported_types=True) as partial:
                            parsed_partial_rows = sum(1 for _ in partial.rows())
                            parsed_partial = partial
                    except OracleError as exc:
                        if not (str(exc).startswith("truncated ") or
                                str(exc) == "missing v2 trailer"):
                            raise
                if parsed_partial is not None:
                    if parsed_partial_rows != arm_result["rows"]:
                        raise DifferentialError("incomplete arm row count contradicts spool")
                    if (parsed_witness_before is not None and
                            parsed_witness_after is not None and
                            parsed_witness_after.sequence == parsed_witness_before.sequence + 1):
                        expected_status = ("success" if parsed_partial.terminal_status == "complete"
                                           else "error")
                        if parsed_witness_after.status != expected_status:
                            raise DifferentialError("incomplete spool and witness status disagree")
                        if (not parsed_witness_after.truncated and not (
                                parsed_partial.terminal_status == "error" and
                                not parsed_partial.columns)):
                            descriptor_columns = tuple((column.oid, column.typmod)
                                for column in parsed_partial.columns)
                            witness_columns = tuple((column.oid, column.typmod)
                                for column in parsed_witness_after.columns)
                            if descriptor_columns != witness_columns:
                                raise DifferentialError(
                                    "incomplete spool descriptor contradicts witness")
                    if (parsed_partial.terminal_status == "error" and
                            parsed_partial.sqlstate != arm_result["sqlstate"]) or \
                            (parsed_partial.terminal_status == "complete" and
                             arm_result["sqlstate"]):
                        raise DifferentialError("incomplete arm SQLSTATE contradicts spool")
                if (arm in {"native", "shadow"} and arm_result["pid"] and
                        parsed_witness_before is not None and
                        parsed_witness_after is not None):
                    proven_interference = (
                        parsed_witness_after.sequence == parsed_witness_before.sequence + 1 and
                        parsed_witness_after.engine == "duckdb" and
                        parsed_witness_after.token == subject_token and
                        parsed_witness_after.pid == arm_result["pid"] and
                        parsed_witness_after.database_oid == arm_result["database_oid"] and
                        parsed_witness_after.user_oid == arm_result["user_oid"])
                if arm == "shadow" and parsed_v2_before is not None and parsed_v2_after is not None:
                    shadow_policy_interference = (parsed_v2_before.get("s") == "complete" and
                        parsed_v2_after.get("s") == "complete" and
                        parsed_v2_before.get("cid") == parsed_v2_after.get("cid") and
                        parsed_v2_before.get("g") == parsed_v2_after.get("g") and
                        int(parsed_v2_after["e"]) == int(parsed_v2_before["e"]) + 1 and
                        parsed_v2_after.get("mode") == "shadow_p0" and
                        parsed_v2_after.get("force") == "off" and
                        (parsed_v2_after.get("barrier") != "closed" or
                         parsed_v2_after.get("policy") != "pass_through"))
                if "native" in completed_results:
                    prior = completed_results["native"]
                    before = parse_witness(str(prior["witness_before"]))
                    after = parse_witness(str(prior["witness_after"]))
                    proven_interference = proven_interference or (
                        after.sequence == before.sequence + 1 and
                        after.engine == "duckdb" and after.token == subject_token and
                        after.pid == prior["pid"] and
                        after.database_oid == prior["database_oid"] and
                        after.user_oid == prior["user_oid"])
                proven_difference = proven_failure
                if shadow_policy_interference:
                    proven_failure = {"result": "FAIL",
                                      "reason": "shadow_policy_interference"}
                elif proven_interference:
                    proven_failure = {"result": "FAIL",
                                      "reason": "native_or_shadow_interference"}
                elif proven_difference is not None:
                    proven_failure = proven_difference
                incomplete_arms.append({"arm": arm, "reason": arm_result["reason"]
                    if spool_available else "spool_missing"})
                continue
            completed_results[arm] = arm_result
            receipt = arm_receipt(manifest, arm, arm_result, spool, exporter_identity,
                                  exported_snapshot, token_digest, imported=True)
            if receipt["backend"]["database"] != exporter_database or \
                    receipt["backend"]["user"] != exporter_user:
                raise RunError("exporter and arm principal differ")
            receipt_path = args.run_root / f"{arm}.receipt.json"
            write_json_exclusive(receipt_path, receipt)
            receipts[arm] = receipt
            validated_receipts[arm] = validate_receipt(copy.deepcopy(receipt), arm)
            validate_cross_bindings(manifest, validated_receipts)
            current = validated_receipts[arm]
            if (arm in {"native", "shadow"} and current["_witness_advanced"] and
                    current["_witness"].engine == "duckdb"):
                proven_failure = {"result": "FAIL",
                                  "reason": "native_or_shadow_interference"}
            if arm == "shadow" and not current["_shadow_inert"]:
                proven_failure = {"result": "FAIL",
                                  "reason": "shadow_policy_interference"}
            for previous_arm, previous in validated_receipts.items():
                if previous_arm == arm:
                    continue
                failure = evaluate_qualified_pair(previous_arm, previous, arm,
                                                  validated_receipts[arm], manifest,
                                                  args.run_root)
                if failure is not None:
                    proven_failure = failure
        if incomplete_arms:
            report = {"schema_version": 1,
                "document_type": "pq31_differential_result",
                "result": "FAIL" if proven_failure else "INCONCLUSIVE",
                "reason": str(proven_failure["reason"]) if proven_failure else
                          "acquisition_incomplete",
                "incomplete_arms": incomplete_arms,
                "missing_import_control": {"result": "NOT_RUN"}}
            write_json_exclusive(args.run_root / "result.json", report)
            return 1
        verdict = evaluate(manifest, receipts, args.run_root)
        control: dict[str, object] = {"result": "NOT_RUN"}
        if args.concurrent_sql_file is not None:
            negative = args.run_root / "missing-import.spool"
            process = subprocess.run(
                [*arm_command, "--mode", "arm", "--arm", "native",
                 "--snapshot-mode", "skip", "--spool", str(negative)], capture_output=True,
                text=True, check=False)
            lines = process.stdout.splitlines()
            if len(lines) != 1:
                raise RunError("missing-import control did not produce one result")
            negative_result = parse_arm(lines[0])
            if process.returncode != 0 or not negative_result["complete"] or \
                    negative_result["terminal_status"] != "complete":
                raise RunError("missing-import control acquisition failed")
            negative_receipt = arm_receipt(manifest, "native", negative_result, negative,
                                           exporter_identity, exported_snapshot, token_digest,
                                           imported=False)
            validate_no_import_control(manifest, receipts, negative_receipt)
            write_json_exclusive(args.run_root / "missing-import.receipt.json", negative_receipt)
            comparison = compare_spools(args.run_root / "native.spool", negative,
                                        args.run_root, ordered=manifest["ordered"],
                                        max_field_bytes=1024 * 1024)
            control = {"result": "PASS" if comparison["result"] == "FAIL" else "FAIL",
                       "reason": "missing_import_observed_new_commit" if
                       comparison["result"] == "FAIL" else
                       "missing_import_control_ineffective"}
            if control["result"] != "PASS" and verdict["result"] == "PASS":
                verdict = {"result": "INCONCLUSIVE",
                           "reason": "missing_import_control_ineffective"}
        report = {"schema_version": 1, "document_type": "pq31_differential_result",
                  **verdict, "missing_import_control": control}
        if args.site_binding:
            report["site_binding"] = args.site_binding
        write_json_exclusive(args.run_root / "result.json", report)
        return 0 if report["result"] in {"PASS", "ERROR_PARITY"} else 1
    except (DifferentialError, OracleError, SnapshotError, WitnessError) as exc:
        print(f"run-differential: {exc}", file=sys.stderr)
        try:
            result_path = args.run_root / "result.json"
            if args.run_root.is_absolute() and args.run_root.is_dir() and not result_path.exists():
                write_json_exclusive(result_path, {"schema_version": 1,
                    "document_type": document_type,
                    "result": "INVALID", "reason": str(exc)[:512],
                    "missing_import_control": {"result": "NOT_RUN"}})
        except OSError:
            pass
        return 2
    except (OSError, ValueError, RunError) as exc:
        print(f"run-differential: {exc}", file=sys.stderr)
        try:
            result_path = args.run_root / "result.json"
            if args.run_root.is_absolute() and args.run_root.is_dir() and not result_path.exists():
                write_json_exclusive(result_path, {"schema_version": 1,
                    "document_type": document_type,
                    "result": "FAIL" if proven_failure else "INCONCLUSIVE",
                    "reason": str(proven_failure["reason"]) if proven_failure else
                              "acquisition_failure",
                    "detail": str(exc)[:512],
                    "missing_import_control": {"result": "NOT_RUN"}})
        except OSError:
            pass
        return 2
    finally:
        if exporter is not None:
            if exporter.stdin is not None:
                try:
                    exporter.stdin.write("\n"); exporter.stdin.flush(); exporter.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                exporter.wait(timeout=10)
            except subprocess.TimeoutExpired:
                exporter.kill(); exporter.wait()


if __name__ == "__main__":
    raise SystemExit(main())
