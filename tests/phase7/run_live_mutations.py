#!/usr/bin/env python3
"""Inject PQ-31 defects into live three-arm evidence and prove detection."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.phase5.corpus_oracle import MAGIC_V2, TRAILER_MARKER
from tools.phase7.differential_oracle import evaluate

U32 = struct.Struct(">I")


def read_spool(path: Path) -> tuple[list[list], list[list[bytes | None]]]:
    data = path.read_bytes(); offset = 0
    assert data[:8] == MAGIC_V2; offset = 8
    count = U32.unpack_from(data, offset)[0]; offset += 4
    columns = []
    for _ in range(count):
        length = U32.unpack_from(data, offset)[0]; offset += 4
        name = data[offset:offset + length].decode(); offset += length
        oid = U32.unpack_from(data, offset)[0]; offset += 4
        typmod, result_format = struct.unpack_from(">iI", data, offset); offset += 8
        columns.append([name, oid, typmod, result_format])
    rows = []
    while True:
        fields = U32.unpack_from(data, offset)[0]; offset += 4
        if fields == TRAILER_MARKER:
            break
        row = []
        for _ in range(fields):
            length = U32.unpack_from(data, offset)[0]; offset += 4
            if length == 0xFFFFFFFF:
                row.append(None)
            else:
                row.append(data[offset:offset + length]); offset += length
        rows.append(row)
    return columns, rows


def write_spool(path: Path, columns: list[list], rows: list[list[bytes | None]],
                sqlstate: str = "") -> None:
    payload = bytearray(MAGIC_V2) + U32.pack(len(columns))
    for name, oid, typmod, result_format in columns:
        encoded = name.encode()
        payload += U32.pack(len(encoded)) + encoded + U32.pack(oid)
        payload += struct.pack(">iI", typmod, result_format)
    descriptor = hashlib.sha256(payload).digest()
    for row in rows:
        payload += U32.pack(len(row))
        for value in row:
            payload += U32.pack(0xFFFFFFFF if value is None else len(value))
            if value is not None:
                payload += value
    state = (sqlstate.encode() if sqlstate else b"\0" * 5) + b"\0" * 3
    payload += U32.pack(TRAILER_MARKER) + U32.pack(bool(sqlstate)) + state
    payload += struct.pack(">QI", len(rows), 0 if sqlstate else 1) + descriptor
    path.write_bytes(payload); path.chmod(0o600)


def bind_spool(receipt: dict, path: Path, rows: int, sqlstate: str = "") -> None:
    receipt["spool"] = {"path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size, "rows": rows,
        "terminal_status": "error" if sqlstate else "complete",
        "sqlstate": sqlstate}


def replace_meta(record: str, index: int, *, oid: int | None = None,
                 typmod: int | None = None, collation: int | None = None) -> str:
    tokens = record.split(";")
    position = next(i for i, token in enumerate(tokens) if token.startswith("meta="))
    columns = tokens[position][5:].split(",")
    parts = [int(value) for value in columns[index].split(":")]
    if oid is not None: parts[0] = oid
    if typmod is not None: parts[1] = typmod
    if collation is not None: parts[2] = collation
    columns[index] = ":".join(str(value) for value in parts)
    tokens[position] = "meta=" + ",".join(columns)
    return ";".join(tokens)


def verdict(manifest: dict, receipts: dict, root: Path) -> tuple[str, str]:
    result = evaluate(copy.deepcopy(manifest), copy.deepcopy(receipts), root)
    return result["result"], result["reason"]


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: run_live_mutations.py MANIFEST RUN_ROOT")
    manifest = json.loads(Path(sys.argv[1]).read_text())
    run = Path(sys.argv[2])
    receipts = {arm: json.loads((run / f"{arm}.receipt.json").read_text())
                for arm in ("native", "forced", "shadow")}
    mutation_root = run / "mutations"; mutation_root.mkdir(mode=0o700)
    columns, rows = read_spool(run / "forced.spool")
    cases: list[tuple[str, str, str]] = []

    def mutated(name: str, new_columns: list[list], new_rows: list[list[bytes | None]],
                witness_transform=lambda value: value) -> dict:
        current = copy.deepcopy(receipts)
        path = mutation_root / f"{name}.spool"
        write_spool(path, new_columns, new_rows)
        bind_spool(current["forced"], path, len(new_rows))
        current["forced"]["witness_after"] = witness_transform(
            current["forced"]["witness_after"])
        return current

    changed_rows = copy.deepcopy(rows); changed_rows[0][1] = b"changed"
    cases.append(("value", *verdict(manifest, mutated("value", columns, changed_rows), run)))

    oid_columns = copy.deepcopy(columns); oid_columns[0][1] = 20
    cases.append(("oid", *verdict(manifest, mutated(
        "oid", oid_columns, rows, lambda value: replace_meta(value, 0, oid=20)), run)))

    typmod_columns = copy.deepcopy(columns); typmod_columns[1][2] = 12
    cases.append(("typmod", *verdict(manifest, mutated(
        "typmod", typmod_columns, rows,
        lambda value: replace_meta(value, 1, typmod=12)), run)))

    cases.append(("collation", *verdict(manifest, mutated(
        "collation", columns, rows,
        lambda value: replace_meta(value, 1, collation=101)), run)))

    null_rows = copy.deepcopy(rows); null_rows[0][1] = None
    cases.append(("null", *verdict(manifest, mutated("null", columns, null_rows), run)))
    duplicate_manifest = copy.deepcopy(manifest); duplicate_manifest["ordered"] = False
    duplicate_rows = copy.deepcopy(rows); duplicate_rows.append(copy.deepcopy(rows[0]))
    cases.append(("duplicate", *verdict(duplicate_manifest, mutated(
        "duplicate", columns, duplicate_rows), run)))
    cases.append(("ordered-row", *verdict(manifest, mutated(
        "ordered-row", columns, list(reversed(rows))), run)))
    unordered_manifest = copy.deepcopy(manifest); unordered_manifest["ordered"] = False
    cases.append(("unordered-row", *verdict(unordered_manifest, mutated(
        "unordered-row", columns, list(reversed(rows))), run)))

    error_receipts = copy.deepcopy(receipts)
    for arm in ("native", "forced", "shadow"):
        arm_columns, _ = read_spool(run / f"{arm}.spool")
        path = mutation_root / f"{arm}-error.spool"
        write_spool(path, arm_columns, [], "22012")
        bind_spool(error_receipts[arm], path, 0, "22012")
        error_receipts[arm]["witness_after"] = error_receipts[arm][
            "witness_after"].replace("status=success", "status=error")
    parity = verdict(manifest, error_receipts, run)
    if parity != ("ERROR_PARITY", "matching_sqlstate"):
        raise AssertionError(("error-parity", parity))
    forced_columns, _ = read_spool(run / "forced.spool")
    forced_error = mutation_root / "forced-sqlstate.spool"
    write_spool(forced_error, forced_columns, [], "22P02")
    bind_spool(error_receipts["forced"], forced_error, 0, "22P02")
    cases.append(("sqlstate", *verdict(manifest, error_receipts, run)))

    expected = {"value": "FAIL", "oid": "FAIL", "typmod": "FAIL",
                "collation": "FAIL", "null": "FAIL", "duplicate": "FAIL",
                "ordered-row": "FAIL", "unordered-row": "PASS", "sqlstate": "FAIL"}
    for name, result, reason in cases:
        if result != expected[name]:
            raise AssertionError((name, result, reason))
    report = {"schema_version": 1, "document_type": "pq31_live_mutations",
              "result": "PASS", "cases": [
                  {"name": name, "result": result, "reason": reason}
                  for name, result, reason in cases]}
    output = mutation_root / "result.json"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, (json.dumps(report, sort_keys=True,
                                         separators=(",", ":")) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
