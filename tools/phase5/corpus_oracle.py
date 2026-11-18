#!/usr/bin/env python3
"""Bounded typed-result oracle and corpus qualification for PGQI-5."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import re
import shutil
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

MAGIC = b"PGQSP001"
SUPPORTED_TYPES = {20: ("int8", -(2**63), 2**63 - 1),
                   21: ("int2", -(2**15), 2**15 - 1),
                   23: ("int4", -(2**31), 2**31 - 1)}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
INTEGER = re.compile(rb"(?:0|-[1-9][0-9]*|[1-9][0-9]*)")
U32 = struct.Struct(">I")
I64 = struct.Struct(">q")


class OracleError(ValueError):
    """Fail-closed corpus or result evidence error."""


def _duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OracleError(f"duplicate key: {key}")
        result[key] = value
    return result


def load_json(path: Path, maximum: int = 1024 * 1024) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise OracleError("JSON input is not a bounded regular file")
        payload = os.read(descriptor, info.st_size + 1)
        if len(payload) != info.st_size:
            raise OracleError("JSON input changed during read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_duplicate,
                           parse_constant=lambda token: (_ for _ in ()).throw(
                               OracleError(f"non-finite number: {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OracleError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise OracleError("JSON root must be an object")
    return value


def _exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise OracleError(f"{name} fields are not exact")
    return value


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise OracleError(f"invalid {name}")
    return value


def _authority(value: Any, name: str) -> None:
    item = _exact(value, {"state", "reference", "authorized_before_acquisition"}, name)
    if item["state"] != "AUTHORISED" or item["authorized_before_acquisition"] is not True:
        raise OracleError(f"{name} is not authorised before acquisition")
    _id(item["reference"], f"{name}.reference")


def _weight(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OracleError(f"invalid {name}")
    return value


def validate_qualification(value: dict[str, Any]) -> dict[str, Any]:
    _exact(value, {"schema_version", "document_type", "qualification_id",
                   "authorities", "storage_policy", "baseline_authority", "natural_inventory",
                   "oracle_limits", "execution_corpus", "synthetic_controls"}, "qualification")
    if value["schema_version"] != 1 or type(value["schema_version"]) is not int or \
            value["document_type"] != "corpus_qualification":
        raise OracleError("unsupported qualification schema")
    _id(value["qualification_id"], "qualification_id")
    authorities = _exact(value["authorities"], {"inventory", "executed_sql", "dataset"},
                         "authorities")
    for name, item in authorities.items():
        _authority(item, f"authorities.{name}")
    storage = _exact(value["storage_policy"], {"authorization_reference", "raw_root",
        "spool_root", "scratch_root", "required_mode", "retention_class",
        "retention_authority", "authorized_before_acquisition"}, "storage_policy")
    for name in ("authorization_reference", "retention_class", "retention_authority"):
        _id(storage[name], f"storage_policy.{name}")
    for name in ("raw_root", "spool_root", "scratch_root"):
        if not isinstance(storage[name], str) or not storage[name].startswith("/"):
            raise OracleError(f"invalid storage_policy.{name}")
    if storage["required_mode"] != "0700" or storage["authorized_before_acquisition"] is not True:
        raise OracleError("storage is not authorised before acquisition")
    baseline = _exact(value["baseline_authority"], {"authorization_reference",
        "authorized_before_acquisition", "clean_instance_attested", "server_build_sha256",
        "schema_sha256", "data_sha256", "configuration_sha256", "provider_identity", "cases"},
        "baseline_authority")
    _id(baseline["authorization_reference"], "baseline_authority.authorization_reference")
    if baseline["authorized_before_acquisition"] is not True or \
            baseline["clean_instance_attested"] is not True:
        raise OracleError("baseline is not authorised as a clean instance")
    for name in ("server_build_sha256", "schema_sha256", "data_sha256", "configuration_sha256"):
        if not isinstance(baseline[name], str) or not DIGEST.fullmatch(baseline[name]):
            raise OracleError(f"invalid baseline_authority.{name}")
    if baseline["provider_identity"] != "POSTGRESQL_NATIVE_NO_DELEGATING_PROVIDER":
        raise OracleError("baseline provider identity is not native PostgreSQL")
    if not isinstance(baseline["cases"], list) or not baseline["cases"]:
        raise OracleError("approved baseline cases must be non-empty")
    approved_case_ids: set[str] = set()
    for raw in baseline["cases"]:
        case = _exact(raw, {"query_id", "spool_path", "spool_sha256", "spool_bytes"},
                      "approved baseline case")
        query_id = _id(case["query_id"], "baseline query_id")
        if query_id in approved_case_ids:
            raise OracleError("duplicate approved baseline query_id")
        approved_case_ids.add(query_id)
        if not isinstance(case["spool_path"], str) or not case["spool_path"].startswith("raw/") or \
                ".." in Path(case["spool_path"]).parts:
            raise OracleError("invalid approved baseline spool_path")
        if not isinstance(case["spool_sha256"], str) or not DIGEST.fullmatch(case["spool_sha256"]):
            raise OracleError("invalid approved baseline spool_sha256")
        if isinstance(case["spool_bytes"], bool) or not isinstance(case["spool_bytes"], int) or \
                case["spool_bytes"] < len(MAGIC) + 4:
            raise OracleError("invalid approved baseline spool_bytes")
    limits = _exact(value["oracle_limits"], {"max_spool_bytes", "max_rows", "max_columns",
        "max_field_bytes", "max_rows_in_memory", "max_open_runs"}, "oracle_limits")
    for name, limit in limits.items():
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise OracleError(f"invalid oracle_limits.{name}")
    if limits["max_open_runs"] < 2:
        raise OracleError("oracle max_open_runs must be at least two")

    natural = value["natural_inventory"]
    execution = value["execution_corpus"]
    controls = value["synthetic_controls"]
    if not isinstance(natural, list) or not isinstance(execution, list) or not isinstance(controls, list):
        raise OracleError("corpus collections must be arrays")
    natural_ids: set[str] = set()
    natural_origins: dict[str, str] = {}
    confirmed = unknown = 0
    frequency_total = frequency_confirmed = 0
    frequency_known = True
    for raw in natural:
        item = _exact(raw, {"query_id", "inventory_origin", "inventory_source_ref",
            "eligibility", "reason_family", "frequency_weight", "baseline_time_weight"},
            "natural inventory item")
        query_id = _id(item["query_id"], "query_id")
        if query_id in natural_ids:
            raise OracleError("duplicate natural query_id")
        natural_ids.add(query_id)
        if item["inventory_origin"] not in {"natural_internal", "natural_customer"}:
            raise OracleError("natural inventory has non-natural origin")
        natural_origins[query_id] = item["inventory_origin"]
        _id(item["inventory_source_ref"], "inventory_source_ref")
        eligibility = item["eligibility"]
        if eligibility not in {"ELIGIBLE", "UNSUPPORTED", "NOT_EXECUTABLE", "UNKNOWN"}:
            raise OracleError("invalid eligibility")
        _id(item["reason_family"], "reason_family")
        frequency = _weight(item["frequency_weight"], "frequency_weight")
        _weight(item["baseline_time_weight"], "baseline_time_weight")
        confirmed += eligibility == "ELIGIBLE"
        unknown += eligibility == "UNKNOWN"
        if frequency is None:
            frequency_known = False
        else:
            frequency_total += frequency
            if eligibility == "ELIGIBLE":
                frequency_confirmed += frequency

    execution_ids: set[str] = set()
    linked_natural: set[str] = set()
    for raw in execution:
        item = _exact(raw, {"query_id", "natural_query_id", "executed_sql_origin",
            "executed_sql_ref", "sql_sha256", "dataset_origin", "dataset_ref", "concurrent_writes"},
            "execution corpus item")
        query_id = _id(item["query_id"], "query_id")
        if query_id in execution_ids:
            raise OracleError("duplicate execution query_id")
        execution_ids.add(query_id)
        parent = item["natural_query_id"]
        if parent is not None:
            parent = _id(parent, "natural_query_id")
            if parent not in natural_ids:
                raise OracleError("execution corpus has unknown natural parent")
        if item["executed_sql_origin"] not in {"natural_internal", "natural_customer",
                                                "derived_reproduction"}:
            raise OracleError("invalid executed_sql_origin")
        if item["executed_sql_origin"] in {"natural_internal", "natural_customer"}:
            if parent is None or natural_origins[parent] != item["executed_sql_origin"]:
                raise OracleError("natural executed SQL lacks a matching natural parent")
            linked_natural.add(parent)
        if item["dataset_origin"] not in {"natural_internal", "natural_customer",
                                           "derived_reproduction", "synthetic_control"}:
            raise OracleError("invalid dataset_origin")
        _id(item["executed_sql_ref"], "executed_sql_ref")
        if not isinstance(item["sql_sha256"], str) or not DIGEST.fullmatch(item["sql_sha256"]):
            raise OracleError("invalid sql_sha256")
        _id(item["dataset_ref"], "dataset_ref")
        if item["concurrent_writes"] is not False:
            raise OracleError("concurrent writes are outside v1")

    control_ids: set[str] = set()
    for raw in controls:
        item = _exact(raw, {"query_id", "control_kind", "executed_sql_ref", "dataset_ref"},
                      "synthetic control")
        query_id = _id(item["query_id"], "query_id")
        if query_id in control_ids or query_id in execution_ids or query_id in natural_ids:
            raise OracleError("duplicate control query_id")
        control_ids.add(query_id)
        _id(item["control_kind"], "control_kind")
        _id(item["executed_sql_ref"], "executed_sql_ref")
        _id(item["dataset_ref"], "dataset_ref")

    denominator = len(natural_ids)
    coverage = {"denominator": denominator, "confirmed_eligible": confirmed,
                "upper_eligible": confirmed + unknown,
                "confirmed_ratio": None if not denominator else confirmed / denominator,
                "upper_ratio": None if not denominator else (confirmed + unknown) / denominator,
                "frequency_ratio": (frequency_confirmed / frequency_total
                                    if frequency_known and frequency_total else None),
                "execution_items": len(execution_ids), "synthetic_controls": len(control_ids),
                "natural_items_linked_to_execution": len(linked_natural)}
    return coverage


def validate_baseline(value: dict[str, Any]) -> dict[str, Any]:
    _exact(value, {"schema_version", "document_type", "baseline_id", "server_build_sha256",
        "schema_sha256", "data_sha256", "configuration_sha256", "provider_identity",
        "provider_absence_verified", "static_data", "concurrent_writes", "cases"},
        "baseline manifest")
    if value["schema_version"] != 1 or type(value["schema_version"]) is not int or \
            value["document_type"] != "baseline_manifest":
        raise OracleError("unsupported baseline schema")
    _id(value["baseline_id"], "baseline_id")
    for name in ("server_build_sha256", "schema_sha256", "data_sha256", "configuration_sha256"):
        if not isinstance(value[name], str) or not DIGEST.fullmatch(value[name]):
            raise OracleError(f"invalid {name}")
    if value["provider_identity"] != "POSTGRESQL_NATIVE_NO_DELEGATING_PROVIDER" or \
            value["provider_absence_verified"] is not True:
        raise OracleError("baseline does not prove provider absence")
    if value["static_data"] is not True or value["concurrent_writes"] is not False:
        raise OracleError("baseline data is not static")
    if not isinstance(value["cases"], list) or not value["cases"]:
        raise OracleError("baseline cases must be non-empty")
    seen: set[str] = set()
    for raw in value["cases"]:
        case = _exact(raw, {"query_id", "spool_path", "spool_sha256", "spool_bytes"},
                      "baseline case")
        query_id = _id(case["query_id"], "query_id")
        if query_id in seen:
            raise OracleError("duplicate baseline query_id")
        seen.add(query_id)
        if not isinstance(case["spool_path"], str) or not case["spool_path"].startswith("raw/") or \
                ".." in Path(case["spool_path"]).parts:
            raise OracleError("invalid baseline spool_path")
        if not isinstance(case["spool_sha256"], str) or not DIGEST.fullmatch(case["spool_sha256"]):
            raise OracleError("invalid baseline spool_sha256")
        if isinstance(case["spool_bytes"], bool) or not isinstance(case["spool_bytes"], int) or \
                case["spool_bytes"] < len(MAGIC) + 4:
            raise OracleError("invalid baseline spool_bytes")
    return value


def _read_exact(stream: BinaryIO, count: int, what: str) -> bytes:
    payload = stream.read(count)
    if len(payload) != count:
        raise OracleError(f"truncated {what}")
    return payload


@dataclass(frozen=True)
class Column:
    name: str
    oid: int


class TypedSpool:
    def __init__(self, path: Path, max_spool_bytes: int, max_columns: int,
                 max_field_bytes: int, max_rows: int) -> None:
        self.path = path
        self.max_spool_bytes = max_spool_bytes
        self.max_columns = max_columns
        self.max_field_bytes = max_field_bytes
        self.max_rows = max_rows
        self.stream: BinaryIO | None = None
        self.columns: tuple[Column, ...] = ()
        self.sha256 = ""
        self.identity: tuple[int, int, int, int] | None = None

    def __enter__(self) -> "TypedSpool":
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > self.max_spool_bytes):
            os.close(descriptor); raise OracleError("spool bound exceeded")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        try:
            digest = hashlib.sha256()
            offset = 0
            while offset < info.st_size:
                chunk = os.pread(descriptor, min(65536, info.st_size - offset), offset)
                if not chunk:
                    raise OracleError("spool changed during hashing")
                digest.update(chunk); offset += len(chunk)
            self.sha256 = digest.hexdigest()
            if _read_exact(stream, len(MAGIC), "spool magic") != MAGIC:
                raise OracleError("unsupported spool version")
            count = U32.unpack(_read_exact(stream, 4, "column count"))[0]
            if count > self.max_columns:
                raise OracleError("column bound exceeded")
            columns: list[Column] = []
            names: set[str] = set()
            for _ in range(count):
                size = U32.unpack(_read_exact(stream, 4, "column name length"))[0]
                if not 0 < size <= 1024:
                    raise OracleError("invalid column name length")
                try:
                    name = _read_exact(stream, size, "column name").decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise OracleError("invalid column name UTF-8") from exc
                oid = U32.unpack(_read_exact(stream, 4, "column type"))[0]
                if oid not in SUPPORTED_TYPES:
                    raise OracleError("unsupported result type")
                if name in names:
                    raise OracleError("duplicate column name")
                names.add(name); columns.append(Column(name, oid))
            self.columns = tuple(columns)
            self.identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            self.stream = stream
            return self
        except Exception:
            stream.close()
            raise

    def __exit__(self, *_: object) -> None:
        if self.stream is not None:
            self.stream.close(); self.stream = None

    def rows(self) -> Iterator[bytes]:
        if self.stream is None:
            raise OracleError("spool is not open")
        count = 0
        while True:
            encoded = self.stream.read(4)
            if not encoded:
                current = os.fstat(self.stream.fileno())
                if self.identity != (current.st_dev, current.st_ino,
                                     current.st_size, current.st_mtime_ns):
                    raise OracleError("spool changed during comparison")
                return
            if len(encoded) != 4:
                raise OracleError("truncated row field count")
            fields = U32.unpack(encoded)[0]
            if fields != len(self.columns):
                raise OracleError("row width differs from descriptor")
            canonical = bytearray()
            for column in self.columns:
                size = U32.unpack(_read_exact(self.stream, 4, "field length"))[0]
                if size == 0xFFFFFFFF:
                    canonical += b"\x00"
                    continue
                if size > self.max_field_bytes:
                    raise OracleError("field bound exceeded")
                payload = _read_exact(self.stream, size, "field payload")
                if not INTEGER.fullmatch(payload):
                    raise OracleError("non-canonical integer payload")
                value = int(payload)
                _, minimum, maximum = SUPPORTED_TYPES[column.oid]
                if value < minimum or value > maximum:
                    raise OracleError("integer value out of range")
                canonical += b"\x01" + I64.pack(value)
            count += 1
            if count > self.max_rows:
                raise OracleError("row bound exceeded")
            yield bytes(canonical)


def _write_record(stream: BinaryIO, record: bytes) -> None:
    stream.write(U32.pack(len(record))); stream.write(record)


def _run_records(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while True:
            header = stream.read(4)
            if not header:
                return
            if len(header) != 4:
                raise OracleError("truncated sort run")
            yield _read_exact(stream, U32.unpack(header)[0], "sort record")


def _merge_group(inputs: list[Path], output: Path) -> None:
    iterators = [iter(_run_records(path)) for path in inputs]
    heap: list[tuple[bytes, int]] = []
    for index, iterator in enumerate(iterators):
        try:
            heap.append((next(iterator), index))
        except StopIteration:
            pass
    heapq.heapify(heap)
    with output.open("xb") as stream:
        while heap:
            record, index = heapq.heappop(heap)
            _write_record(stream, record)
            try:
                heapq.heappush(heap, (next(iterators[index]), index))
            except StopIteration:
                pass
        stream.flush(); os.fsync(stream.fileno())


def _sort_spool(spool: TypedSpool, directory: Path, prefix: str,
                max_rows_in_memory: int, max_open_runs: int) -> tuple[Path, int, int]:
    runs: list[Path] = []
    chunk: list[bytes] = []
    rows = 0
    for record in spool.rows():
        chunk.append(record); rows += 1
        if len(chunk) == max_rows_in_memory:
            path = directory / f"{prefix}-{len(runs):08d}.run"
            with path.open("xb") as stream:
                for item in sorted(chunk): _write_record(stream, item)
                stream.flush(); os.fsync(stream.fileno())
            runs.append(path); chunk.clear()
    if chunk or not runs:
        path = directory / f"{prefix}-{len(runs):08d}.run"
        with path.open("xb") as stream:
            for item in sorted(chunk): _write_record(stream, item)
            stream.flush(); os.fsync(stream.fileno())
        runs.append(path)
    initial_runs = len(runs)
    generation = 0
    while len(runs) > 1:
        next_runs: list[Path] = []
        for offset in range(0, len(runs), max_open_runs):
            group = runs[offset:offset + max_open_runs]
            output = directory / f"{prefix}-merge-{generation:04d}-{len(next_runs):08d}.run"
            _merge_group(group, output); next_runs.append(output)
        for path in runs: path.unlink()
        runs = next_runs; generation += 1
    return runs[0], rows, initial_runs


def compare_spools(baseline: Path, observed: Path, scratch_root: Path, *,
                   max_spool_bytes: int = 64 * 1024 * 1024,
                   max_rows: int = 10_000_000, max_columns: int = 64,
                   max_field_bytes: int = 64, max_rows_in_memory: int = 4096,
                   max_open_runs: int = 32) -> dict[str, Any]:
    if min(max_spool_bytes, max_rows, max_columns, max_field_bytes,
           max_rows_in_memory, max_open_runs) <= 0 or max_open_runs < 2:
        raise OracleError("comparison limits must be positive and fan-in at least two")
    root_fd = os.open(scratch_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_info = os.fstat(root_fd)
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid() or \
            stat.S_IMODE(root_info.st_mode) != 0o700:
        os.close(root_fd)
        raise OracleError("scratch root must be an owned 0700 directory")
    temporary_name = f"pgqi-oracle-{os.getpid()}-{os.urandom(12).hex()}"
    os.mkdir(temporary_name, mode=0o700, dir_fd=root_fd)
    temporary = Path(f"/proc/self/fd/{root_fd}/{temporary_name}")
    try:
        with TypedSpool(baseline, max_spool_bytes, max_columns, max_field_bytes, max_rows) as left, \
                TypedSpool(observed, max_spool_bytes, max_columns, max_field_bytes, max_rows) as right:
            if left.columns != right.columns:
                left_rows = sum(1 for _ in left.rows())
                right_rows = sum(1 for _ in right.rows())
                return {"result": "FAIL", "reason": "descriptor_mismatch",
                        "baseline_sha256": left.sha256, "observed_sha256": right.sha256,
                        "baseline_rows": left_rows, "observed_rows": right_rows,
                        "baseline_runs": 0, "observed_runs": 0,
                        "columns": [{"name": column.name, "oid": column.oid,
                                     "type": SUPPORTED_TYPES[column.oid][0]}
                                    for column in left.columns]}
            left_run, left_rows, left_runs = _sort_spool(
                left, temporary, "baseline", max_rows_in_memory, max_open_runs)
            right_run, right_rows, right_runs = _sort_spool(
                right, temporary, "observed", max_rows_in_memory, max_open_runs)
            mismatch = next(((a, b) for a, b in __import__("itertools").zip_longest(
                _run_records(left_run), _run_records(right_run)) if a != b), None)
            result = "PASS" if mismatch is None else "FAIL"
            reason = "complete_multiset_equal" if result == "PASS" else "multiset_mismatch"
            return {"result": result, "reason": reason,
                    "baseline_sha256": left.sha256, "observed_sha256": right.sha256,
                    "baseline_rows": left_rows, "observed_rows": right_rows,
                    "baseline_runs": left_runs, "observed_runs": right_runs,
                    "columns": [{"name": column.name, "oid": column.oid,
                                 "type": SUPPORTED_TYPES[column.oid][0]}
                                for column in left.columns]}
    finally:
        shutil.rmtree(temporary)
        os.close(root_fd)


def write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0: raise OracleError("short output write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(parent)
    finally: os.close(parent)


def _positive(value: str) -> int:
    result = int(value)
    if result <= 0: raise argparse.ArgumentTypeError("must be positive")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    qualify = commands.add_parser("qualify")
    qualify.add_argument("--manifest", required=True, type=Path)
    qualify.add_argument("--output", required=True, type=Path)
    baseline = commands.add_parser("validate-baseline")
    baseline.add_argument("--manifest", required=True, type=Path)
    baseline.add_argument("--output", required=True, type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", required=True, type=Path)
    compare.add_argument("--observed", required=True, type=Path)
    compare.add_argument("--scratch-root", required=True, type=Path)
    compare.add_argument("--output", required=True, type=Path)
    compare.add_argument("--max-spool-bytes", type=_positive, default=64 * 1024 * 1024)
    compare.add_argument("--max-rows", type=_positive, default=10_000_000)
    compare.add_argument("--max-columns", type=_positive, default=64)
    compare.add_argument("--max-field-bytes", type=_positive, default=64)
    compare.add_argument("--max-rows-in-memory", type=_positive, default=4096)
    compare.add_argument("--max-open-runs", type=_positive, default=32)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "qualify":
            report = {"schema_version": 1, "document_type": "qualification_result",
                      "result": "PASS", "coverage": validate_qualification(load_json(args.manifest))}
        elif args.command == "validate-baseline":
            manifest = validate_baseline(load_json(args.manifest))
            report = {"schema_version": 1, "document_type": "baseline_validation",
                      "baseline_id": manifest["baseline_id"], "result": "PASS"}
        else:
            report = {"schema_version": 1, "document_type": "oracle_comparison", **compare_spools(
                args.baseline, args.observed, args.scratch_root,
                max_spool_bytes=args.max_spool_bytes, max_rows=args.max_rows,
                max_columns=args.max_columns, max_field_bytes=args.max_field_bytes,
                max_rows_in_memory=args.max_rows_in_memory,
                max_open_runs=args.max_open_runs)}
        write_json_exclusive(args.output, report)
        return 0 if report["result"] == "PASS" else 1
    except (OSError, ValueError) as exc:
        print(f"corpus-oracle: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
