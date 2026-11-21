#!/usr/bin/env python3

import os
import pathlib
import sys
import math
import re


def fail(message: str) -> None:
    raise SystemExit(f"overhead gate: {message}")


if len(sys.argv) != 3:
    fail("usage: verify-overhead.py RUN_ROOT EXPECTED_SAMPLES")
root = pathlib.Path(sys.argv[1])
expected = int(sys.argv[2])
if expected < 1:
    fail("expected sample count must be positive")

expected_modes = ("disabled", "off", "shadow_p0", "allowlist")
def physical_lines(path):
    payload = path.read_bytes()
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        fail(f"{path.name} must end with exactly one newline")
    if any(byte not in (9, 10) and not 32 <= byte <= 126 for byte in payload):
        fail(f"{path.name} contains a forbidden control byte")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        fail(f"{path.name} is not UTF-8")
    return text[:-1].split("\n")

summary_lines = physical_lines(root / "summary.tsv")
if not summary_lines or summary_lines[0] != "mode\tp50_us\tp95_us\tp99_us":
    fail("invalid summary header")
summary = {}
for line in summary_lines[1:]:
    fields = line.split("\t")
    if len(fields) != 4 or fields[0] not in expected_modes or fields[0] in summary:
        fail("summary modes or columns are invalid")
    if any(not re.fullmatch(r"0|[1-9][0-9]*", value) for value in fields[1:]):
        fail("summary contains a malformed percentile")
    summary[fields[0]] = dict(zip(("p50_us", "p95_us", "p99_us"), map(int, fields[1:])))
if tuple(summary) != expected_modes:
    fail("summary must contain exactly the expected modes in order")
for mode in expected_modes:
    if mode not in summary:
        fail(f"missing summary mode {mode}")
    lines = physical_lines(root / f"{mode}.tsv")
    if not lines or lines[0] != "sample\tlatency_us":
        fail(f"{mode} has an invalid sample header")
    timings = []
    identifiers = []
    metadata = {}
    snapshots = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if fields[0] == "info":
            if len(fields) != 3 or fields[1] in metadata:
                fail(f"{mode} contains malformed metadata")
            metadata[fields[1]] = fields[2]
            continue
        if fields[0] == "snapshot":
            if len(fields) != 4 or fields[1] != mode or fields[2] in snapshots:
                fail(f"{mode} contains malformed snapshot metadata")
            snapshots[fields[2]] = fields[3]
            continue
        if len(fields) != 2 or not re.fullmatch(r"[1-9][0-9]*", fields[0]) or \
                not re.fullmatch(r"0|[1-9][0-9]*", fields[1]):
            fail(f"{mode} contains a malformed sample row")
        sample, latency = map(int, fields)
        if sample < 1 or latency < 0:
            fail(f"{mode} contains an out-of-range sample")
        identifiers.append(sample)
        timings.append(latency)
    if identifiers != list(range(1, expected + 1)):
        fail(f"{mode} sample identifiers are incomplete or out of order")
    required = {"mode": mode, "samples": str(expected)}
    if any(metadata.get(key) != value for key, value in required.items()):
        fail(f"{mode} benchmark metadata is inconsistent")
    if not re.fullmatch(r"0|[1-9][0-9]*", metadata.get("warmup", "")):
        fail(f"{mode} warmup metadata is invalid")
    expected_keys = {"mode", "warmup", "samples"}
    if mode == "shadow_p0":
        expected_keys |= {"shadow_decision_before", "shadow_decision_after"}
        try:
            before_text = metadata["shadow_decision_before"]
            after_text = metadata["shadow_decision_after"]
            if not re.fullmatch(r"0|[1-9][0-9]*", before_text) or not re.fullmatch(r"0|[1-9][0-9]*", after_text):
                fail("shadow_p0 decision metadata is invalid")
            decision_delta = int(after_text) - int(before_text)
        except (KeyError, ValueError):
            fail("shadow_p0 decision metadata is invalid")
        if decision_delta != expected:
            fail("shadow_p0 decision delta does not match sample count")
    if set(metadata) != expected_keys:
        fail(f"{mode} metadata fields are not exact")
    expected_snapshot_keys = {
        "pg_qi.shadow_contract_version", "pg_qi.shadow_decision_id",
        "pg_qi.shadow_context", "pg_qi.shadow_policy_intent", "pg_qi.shadow_reason",
        "pg_qi.shadow_force_origin", "pg_qi.shadow_classifier_status",
        "pg_qi.shadow_classifier_reason", "pg_qi.shadow_observed_engine",
        "pg_qi.shadow_verification", "pg_qi.shadow_request_applied",
        "pg_qi.shadow_p0_facts_state", "pg_qi.shadow_p0_facts_reason",
        "duckdb.force_execution",
    }
    if set(snapshots) != expected_snapshot_keys:
        fail(f"{mode} snapshot fields are not exact")
    if mode == "shadow_p0" and (snapshots["pg_qi.shadow_p0_facts_state"] != "unknown" or
                                snapshots["pg_qi.shadow_p0_facts_reason"] != "unsupported_shape"):
        fail("shadow_p0 observer facts do not match the measured query")
    ordered = sorted(timings)
    observed = {
        key: ordered[math.ceil(expected * percentile / 100) - 1]
        for key, percentile in (("p50_us", 50), ("p95_us", 95), ("p99_us", 99))
    }
    if summary[mode] != observed:
        fail(f"{mode} summary does not match validated raw samples")

ratio = float(os.environ.get("PG_QI_OVERHEAD_MAX_RATIO", "4.0"))
slack = int(os.environ.get("PG_QI_OVERHEAD_SLACK_US", "200"))
if not math.isfinite(ratio) or ratio < 1 or slack < 0:
    fail("invalid bound configuration")
for percentile in ("p50_us", "p95_us"):
    baseline = summary["off"][percentile]
    observed = summary["shadow_p0"][percentile]
    limit = int(baseline * ratio + slack)
    if observed > limit:
        fail(f"shadow_p0 {percentile} {observed} exceeds bound {limit} (off={baseline})")
    print(f"PASS {percentile} off={baseline} shadow_p0={observed} limit={limit}")
print("p99 is evidence-only and is not a shared-runner acceptance threshold")
