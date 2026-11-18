#!/usr/bin/env python3
"""Fail-closed verifier for the PGQI-5 protocol spike evidence."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: object required")
    return value


def rows(path: Path):
    with path.open("rb") as stream:
        if stream.read(8) != b"PGQSP001":
            raise ValueError(f"{path.name}: unsupported spool version")
        encoded = stream.read(4)
        if len(encoded) != 4:
            raise ValueError(f"{path.name}: incomplete descriptor")
        (columns,) = struct.unpack(">I", encoded)
        if columns > 1024:
            raise ValueError(f"{path.name}: unreasonable column count")
        for _ in range(columns):
            encoded = stream.read(4)
            if len(encoded) != 4:
                raise ValueError(f"{path.name}: incomplete column name header")
            (size,) = struct.unpack(">I", encoded)
            name = stream.read(size)
            oid = stream.read(4)
            if not 0 < size <= 1024 or len(name) != size or len(oid) != 4:
                raise ValueError(f"{path.name}: incomplete column descriptor")
            name.decode("utf-8", "strict")
        while True:
            header = stream.read(4)
            if not header:
                return
            if len(header) != 4:
                raise ValueError(f"{path.name}: incomplete row header")
            (count,) = struct.unpack(">I", header)
            if count != columns:
                raise ValueError(f"{path.name}: row width differs from descriptor")
            row = []
            for _ in range(count):
                encoded = stream.read(4)
                if len(encoded) != 4:
                    raise ValueError(f"{path.name}: incomplete field header")
                (size,) = struct.unpack(">I", encoded)
                if size == 0xFFFFFFFF:
                    row.append(None)
                    continue
                data = stream.read(size)
                if len(data) != size:
                    raise ValueError(f"{path.name}: incomplete field payload")
                row.append(data.decode("utf-8"))
            yield row


def verify_literal(path: Path, count: int) -> None:
    seen = bytearray(count + 1)
    actual = 0
    for row in rows(path):
        if len(row) != 2 or row[1] != "1":
            raise ValueError(f"{path.name}: unexpected row")
        identity = int(row[0])
        if identity < 1 or identity > count or seen[identity]:
            raise ValueError(f"{path.name}: duplicate or out-of-range identity")
        seen[identity] = 1
        actual += 1
    if actual != count or 0 in seen[1:]:
        raise ValueError(f"{path.name}: incomplete multiset")


def verify_large(path: Path, count: int) -> None:
    seen = bytearray(count + 1)
    actual = 0
    for row in rows(path):
        if len(row) != 2 or row[1] != "x" * 64:
            raise ValueError(f"{path.name}: unexpected large row")
        identity = int(row[0])
        if identity < 1 or identity > count or seen[identity]:
            raise ValueError(f"{path.name}: duplicate or out-of-range identity")
        seen[identity] = 1
        actual += 1
    if actual != count or 0 in seen[1:]:
        raise ValueError(f"{path.name}: incomplete large multiset")


def snapshot(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "name\tvalue":
        raise ValueError(f"{path.name}: invalid header")
    pairs = [line.split("\t", 1) for line in lines[1:]]
    if any(len(pair) != 2 for pair in pairs):
        raise ValueError(f"{path.name}: invalid record")
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError(f"{path.name}: duplicate key")
    return result


def require_case(cases: dict[str, dict], name: str, state: str, reason: str,
                 rows_expected: int | None = None, sqlstate: str | None = None,
                 cancel: bool | None = None) -> None:
    record = cases[name]
    terminal = record["terminal"]
    if terminal.get("terminal_state") != state or terminal.get("reason") != reason:
        raise ValueError(f"{name}: incorrect terminal state/reason")
    if rows_expected is not None and terminal.get("rows") != rows_expected:
        raise ValueError(f"{name}: incorrect row count")
    if sqlstate is not None and terminal.get("sqlstate") != sqlstate:
        raise ValueError(f"{name}: incorrect SQLSTATE")
    if cancel is not None and (terminal.get("cancel_attempted") is not cancel or
                               terminal.get("cancel_succeeded") is not cancel or
                               terminal.get("cancel_timed_out") is not False):
        raise ValueError(f"{name}: incorrect cancel evidence")


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verify(root: Path) -> dict:
    cases = {p.stem: load_json(p) for p in root.glob("*.json")
             if p.name not in {"summary.json", "verification.json"}}
    cancel_blackhole = load_json(root / "cancel-blackhole.terminal")
    if (cancel_blackhole.get("terminal_state") != "FAILED" or
            cancel_blackhole.get("reason") != "query_deadline" or
            cancel_blackhole.get("cancel_attempted") is not True or
            cancel_blackhole.get("cancel_timed_out") is not True):
        raise ValueError("cancel blackhole is not bounded truthfully")
    blackhole_retirement = dict(line.split("\t", 1) for line in
        (root / "cancel-blackhole-retirement.tsv").read_text().splitlines())
    if blackhole_retirement != {"driver_retirement_state": "UNKNOWN",
                                "observer_cluster_start_unchanged": "true",
                                "observer_exact_backend_absent": "true"}:
        raise ValueError("cancel blackhole retirement evidence is invalid")
    for name in ("extended_literal", "prepared_literal", "simple_literal"):
        require_case(cases, name, "COMPLETE", "ok", 1000)
        verify_literal(root / f"{name}.spool", 1000)
    require_case(cases, "bounded_large", "COMPLETE", "ok", 1_200_000)
    verify_large(root / "bounded_large.spool", 1_200_000)
    if cases["bounded_large"]["terminal"]["bytes"] <= 64 * 1024 * 1024:
        raise ValueError("large result does not exceed the address-space budget")
    if cases["bounded_large"]["peak_rss_kib"] >= 32 * 1024:
        raise ValueError("large stream exceeded the peak RSS acceptance threshold")

    require_case(cases, "partial_error", "FAILED",
                 "server_error_after_partial_stream", 99, "22012")
    for name, reason, count in (
        ("row_limit", "max_rows", 10),
        ("byte_limit", "max_result_bytes", 36),
        ("byte_one_over", "max_result_bytes", 0),
        ("byte_two_fields", "max_result_bytes", 0),
        ("column_limit", "max_columns", 0),
        ("empty_column_limit", "max_columns", 0),
        ("message_limit", "max_messages", 10),
        ("max_field", "max_field_bytes", 0),
        ("spool_limit", "max_spool_bytes", 52),
    ):
        require_case(cases, name, "LIMIT_EXCEEDED", reason, count, cancel=True)
    require_case(cases, "query_deadline", "FAILED", "query_deadline", 0,
                 cancel=True)
    if cases["query_deadline"]["elapsed_ms"] > 1250:
        raise ValueError("query deadline exceeded its supervisor envelope")
    require_case(cases, "multiple_simple", "FAILED", "multiple_result_sets", 2)
    for name in ("byte_exact", "null_empty"):
        require_case(cases, name, "COMPLETE", "ok", 1)
    require_case(cases, "connect-failure", "FAILED", "connect_deadline_or_error", 0)
    if cases["connect-failure"]["elapsed_ms"] > 750:
        raise ValueError("stalled connect exceeded its supervisor envelope")
    hang = cases["supervisor-hang"]
    if (hang.get("supervisor_forced_action") != "deadline_sigkill" or
            hang.get("terminal", {}).get("terminal_state") != "SUPERVISOR_TERMINATED"):
        raise ValueError("bounded supervisor escalation was not proved")
    if hang["elapsed_ms"] > 750:
        raise ValueError("unresponsive child exceeded its supervisor envelope")

    expected_snapshot = {
        "pg_qi.shadow_contract_version": "1",
        "pg_qi.shadow_context": "proven_client",
        "pg_qi.shadow_policy_intent": "request_delegation",
        "pg_qi.shadow_reason": "delegation_verified",
        "pg_qi.shadow_force_origin": "qi_stack",
        "pg_qi.shadow_classifier_status": "candidate",
        "pg_qi.shadow_classifier_reason": "candidate",
        "pg_qi.shadow_observed_engine": "duckdb",
        "pg_qi.shadow_verification": "verified",
        "pg_qi.shadow_observation": "duckdb_scan_at_downstream_boundary",
        "pg_qi.shadow_request_applied": "on",
        "duckdb.force_execution": "off",
    }
    for name in ("extended_literal", "prepared_literal", "simple_literal"):
        observed = snapshot(root / f"{name}.snapshot.tsv")
        if any(observed.get(key) != value for key, value in expected_snapshot.items()):
            raise ValueError(f"{name}: verified snapshot mismatch")
        if int(observed["pg_qi.shadow_decision_id"]) <= int(
                observed["pg_qi.shadow_decision_id_before"]):
            raise ValueError(f"{name}: planning snapshot is not fresh")

    for mode in ("custom", "generic"):
        values = (root / f"parameter-{mode}.txt").read_text().splitlines()
        if values[-3:] != ["rejected", "unsupported_parameter", "off"]:
            raise ValueError(f"parameter-{mode}: conservative tuple mismatch")

    identity = (root / "lab-identity.tsv").read_text().splitlines()
    if not identity or not identity[0].startswith("16."):
        raise ValueError("lab identity is not PostgreSQL 16")
    retirement = dict(line.split("\t", 1) for line in
                      (root / "cancel-retirement.tsv").read_text().splitlines())
    if retirement != {"matching_active_sleep": "0"}:
        raise ValueError("cancel retirement observation is missing or nonzero")
    mutations = dict(line.split("\t", 1) for line in
                     (root / "mutation-tests.tsv").read_text().splitlines())
    if mutations != {"mutated_snapshot_refused": "PASS",
                     "no_false_summary_or_index": "PASS"}:
        raise ValueError("end-to-end false-PASS mutation proof is missing")

    summary = {
        "schema_version": 1,
        "result": "PASS",
        "postgresql_major": 16,
        "lab_profile": "disposable_synthetic",
        "cases": {name: cases[name]["terminal"]["terminal_state"]
                  for name in sorted(cases)},
        "parameter_controls": {"force_custom_plan": "PASS",
                               "force_generic_plan": "PASS"},
        "verified_positive_paths": ["extended", "named_prepared", "simple_lab_only"],
        "large_result_bytes": cases["bounded_large"]["terminal"]["bytes"],
        "large_result_peak_rss_kib": cases["bounded_large"]["peak_rss_kib"],
        "address_space_limit_kib": cases["bounded_large"]["address_space_limit_kib"],
        "server_active_sleep_absent_after_cancel": True,
        "notes": [
            "Simple protocol is qualified only for this synthetic spike.",
            "The active sleep absence check is not a general session-retirement proof.",
            "No site, customer or operation approval is implied.",
        ],
    }
    atomic_write(root / "summary.json",
                 (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode())
    entries = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.name not in {
                "SHA256SUMS", "SHA256SUMS.tmp", "verification.json"}:
            entries.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    atomic_write(root / "SHA256SUMS", "".join(entries).encode())
    return summary


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_spike.py RESULT_DIRECTORY")
    print(json.dumps(verify(Path(sys.argv[1]).resolve()), sort_keys=True))
