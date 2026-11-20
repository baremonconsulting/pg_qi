#!/usr/bin/env python3
"""Strict, fail-closed readers for pg_qi V1 and V2 decision snapshots."""

from __future__ import annotations

import re
from typing import Mapping


class SnapshotError(ValueError):
    """The supplied snapshot is not valid evidence."""


U64_MAX = (1 << 64) - 1
CID = re.compile(r"[0-9a-f]{32}\Z")

V1_FIELDS = {
    "pg_qi.shadow_contract_version",
    "pg_qi.shadow_decision_id",
    "pg_qi.shadow_context",
    "pg_qi.shadow_policy_intent",
    "pg_qi.shadow_reason",
    "pg_qi.shadow_force_origin",
    "pg_qi.shadow_classifier_status",
    "pg_qi.shadow_classifier_reason",
    "pg_qi.shadow_observed_engine",
    "pg_qi.shadow_verification",
    "pg_qi.shadow_observation",
    "pg_qi.shadow_request_applied",
}

V2_COMPLETE_FIELDS = {
    "v", "sv", "s", "cid", "g", "e", "mode", "enabled", "context",
    "eligible", "safe", "beneficial", "barrier", "recommendation",
    "reason", "rule", "model", "certificate", "policy", "force",
    "engine", "verification", "observation", "p0", "p0_reason",
}
V2_COMPLETE_ORDER = (
    "v", "sv", "s", "cid", "g", "e", "mode", "enabled", "context",
    "eligible", "safe", "beneficial", "barrier", "recommendation",
    "reason", "rule", "model", "certificate", "policy", "force",
    "engine", "verification", "observation", "p0", "p0_reason",
)
V2_NONE_ORDER = ("v", "sv", "s", "cid", "g", "e")

KNOWN_REASONS = {0, 100, 200, 300, 400, 500, 600, 700, 800, 900}


def _u64(value: str, name: str, minimum: int = 0) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise SnapshotError(f"{name} is not an unsigned integer")
    if len(value) > 1 and value.startswith("0"):
        raise SnapshotError(f"{name} is not canonical")
    result = int(value)
    if result < minimum or result > U64_MAX:
        raise SnapshotError(f"{name} is out of range")
    return result


def _one_of(value: str, name: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise SnapshotError(f"{name} is invalid")
    return value


def parse_v1(values: Mapping[str, str]) -> dict[str, str | int]:
    if set(values) != V1_FIELDS:
        raise SnapshotError("V1 fields are not exact")
    if any(not isinstance(value, str) for value in values.values()):
        raise SnapshotError("V1 values must be strings")
    if (sum(len(value.encode("utf-8")) for value in values.values()) > 4096 or
            any(len(value.encode("utf-8")) > 256 for value in values.values())):
        raise SnapshotError("V1 values are oversized")
    if values["pg_qi.shadow_contract_version"] != "1":
        raise SnapshotError("unsupported V1 contract version")
    result: dict[str, str | int] = dict(values)
    result["pg_qi.shadow_decision_id"] = _u64(
        values["pg_qi.shadow_decision_id"], "V1 decision id")
    if result["pg_qi.shadow_decision_id"] > 2_147_483_647:
        raise SnapshotError("V1 decision id is outside the producer domain")
    _one_of(values["pg_qi.shadow_context"], "V1 context",
            {"unproved_outermost", "nested", "proven_client"})
    _one_of(values["pg_qi.shadow_policy_intent"], "V1 policy",
            {"pass_through", "request_delegation"})
    _one_of(values["pg_qi.shadow_reason"], "V1 reason", {
        "no_completed_plan", "control_disabled", "context_nested",
        "force_external", "classifier_rejected", "policy_missing",
        "context_unproved", "state_poisoned", "decision_only",
        "delegation_requested", "delegation_declined", "delegation_verified"})
    _one_of(values["pg_qi.shadow_force_origin"], "V1 force",
            {"off", "external", "qi_stack"})
    _one_of(values["pg_qi.shadow_classifier_status"], "V1 classifier",
            {"not_evaluated", "candidate", "rejected"})
    _one_of(values["pg_qi.shadow_classifier_reason"], "V1 classifier reason", {
        "candidate", "prototype_rejected", "security_context", "table_sample",
        "inheritance", "unsupported_relation", "unsupported_join",
        "unsupported_aggregate", "unsupported_grouping", "unsupported_ordering",
        "unsupported_parameter", "unsupported_function", "unsupported_cast",
        "unsupported_expression", "invalid_var"})
    _one_of(values["pg_qi.shadow_observed_engine"], "V1 engine",
            {"unknown", "duckdb"})
    _one_of(values["pg_qi.shadow_verification"], "V1 verification",
            {"unverified", "verified"})
    _one_of(values["pg_qi.shadow_observation"], "V1 observation", {
        "none", "no_authoritative_match", "duckdb_scan_at_downstream_boundary"})
    _one_of(values["pg_qi.shadow_request_applied"], "V1 request",
            {"on", "off"})
    policy = values["pg_qi.shadow_policy_intent"]
    reason = values["pg_qi.shadow_reason"]
    force = values["pg_qi.shadow_force_origin"]
    classifier = values["pg_qi.shadow_classifier_status"]
    classifier_reason = values["pg_qi.shadow_classifier_reason"]
    engine = values["pg_qi.shadow_observed_engine"]
    verification = values["pg_qi.shadow_verification"]
    observation = values["pg_qi.shadow_observation"]
    request = values["pg_qi.shadow_request_applied"]
    if ((classifier == "candidate" and classifier_reason != "candidate") or
            (classifier == "rejected" and classifier_reason == "candidate")):
        raise SnapshotError("V1 classifier fields contradict each other")
    verified = (engine == "duckdb" and verification == "verified" and
                observation == "duckdb_scan_at_downstream_boundary")
    if verification == "verified" and not verified:
        raise SnapshotError("V1 verified observation is contradictory")
    if observation == "duckdb_scan_at_downstream_boundary" and engine != "duckdb":
        raise SnapshotError("V1 engine and observation contradict each other")
    delegation_reasons = {
        "delegation_requested", "delegation_declined", "delegation_verified"}
    if reason in delegation_reasons and not (
            values["pg_qi.shadow_context"] == "proven_client" and
            classifier == "candidate" and classifier_reason == "candidate" and
            policy == "request_delegation" and force == "qi_stack" and
            request == "on"):
        raise SnapshotError("V1 delegation outcome is contradictory")
    if reason == "delegation_verified" and not verified:
        raise SnapshotError("V1 verified delegation lacks verified evidence")
    if reason == "delegation_declined" and not (
            engine == "unknown" and verification == "unverified" and
            observation == "no_authoritative_match"):
        raise SnapshotError("V1 declined delegation has positive evidence")
    if reason in {"classifier_rejected", "policy_missing", "decision_only"}:
        expected_classifier = "rejected" if reason == "classifier_rejected" else "candidate"
        if not (values["pg_qi.shadow_context"] == "proven_client" and
                classifier == expected_classifier and policy == "pass_through" and
                request == "off"):
            raise SnapshotError("V1 classifier outcome is contradictory")
    if reason == "context_unproved" and not (
            values["pg_qi.shadow_context"] == "unproved_outermost" and
            classifier == "not_evaluated" and policy == "pass_through" and
            request == "off"):
        raise SnapshotError("V1 unproved context outcome is contradictory")
    if reason == "context_nested" and not (
            values["pg_qi.shadow_context"] == "nested" and
            classifier == "not_evaluated" and policy == "pass_through" and
            request == "off"):
        raise SnapshotError("V1 nested context outcome is contradictory")
    if reason == "force_external" and not (
            force == "external" and classifier == "not_evaluated" and
            policy == "pass_through" and request == "off"):
        raise SnapshotError("V1 external-force outcome is contradictory")
    if reason == "no_completed_plan" and result["pg_qi.shadow_decision_id"] != 0:
        raise SnapshotError("V1 incomplete outcome has a decision id")
    if request == "on" and not (
            policy == "request_delegation" and force == "qi_stack" and
            reason in {"delegation_requested", "delegation_declined",
                       "delegation_verified"}):
        raise SnapshotError("V1 applied request is contradictory")
    if request == "off" and policy == "request_delegation":
        raise SnapshotError("V1 delegation policy lacks an applied request")
    return result


def _parse_pairs(text: str) -> dict[str, str]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 4096:
        raise SnapshotError("V2 snapshot is not a bounded string")
    try:
        text.encode("ascii")
    except UnicodeEncodeError as error:
        raise SnapshotError("V2 snapshot is not ASCII") from error
    result: dict[str, str] = {}
    for item in text.split(";"):
        if not item or "=" not in item:
            raise SnapshotError("V2 token is malformed")
        key, value = item.split("=", 1)
        if not key or not value or key in result:
            raise SnapshotError("V2 key is empty, duplicated, or valueless")
        result[key] = value
    return result


def _validate_decision(values: Mapping[str, str]) -> None:
    gate_names = {"not_evaluated", "pass", "veto", "unknown"}
    gates = [
        _one_of(values[name], name, gate_names)
        for name in ("eligible", "safe", "beneficial")
    ]
    barrier = _one_of(values["barrier"], "barrier", {"closed", "open"})
    recommendation = _one_of(
        values["recommendation"], "recommendation",
        {"would_pass_through", "would_delegate", "insufficient_evidence"})
    reason = _u64(values["reason"], "reason")
    if reason > 2_147_483_647:
        raise SnapshotError("reason is outside the V2 integer domain")
    versions = tuple(_u64(values[name], name) for name in
                     ("rule", "model", "certificate"))
    if any(version > 2_147_483_647 for version in versions):
        raise SnapshotError("V2 version is outside the integer domain")
    any_veto = "veto" in gates
    all_pass = all(gate == "pass" for gate in gates)
    versions_exact = versions == (1, 1, 1)

    if reason not in KNOWN_REASONS:
        if recommendation != "insufficient_evidence" or any_veto or (
                all_pass and versions_exact and barrier == "open"):
            raise SnapshotError("unknown reason contradicts the V2 contract")
        return

    if gates[0] == "veto": expected = ("would_pass_through", 100)
    elif gates[1] == "veto": expected = ("would_pass_through", 200)
    elif gates[2] == "veto": expected = ("would_pass_through", 300)
    elif gates[0] == "unknown": expected = ("insufficient_evidence", 400)
    elif gates[1] == "unknown": expected = ("insufficient_evidence", 500)
    elif gates[2] == "unknown": expected = ("insufficient_evidence", 600)
    elif not all_pass: expected = ("insufficient_evidence", 0)
    elif not versions_exact: expected = ("insufficient_evidence", 800)
    elif barrier != "open": expected = ("insufficient_evidence", 700)
    else: expected = ("would_delegate", 900)
    if (recommendation, reason) != expected:
        raise SnapshotError("V2 recommendation and reason contradict the gates")


def parse_v2(text: str) -> dict[str, str | int]:
    values = _parse_pairs(text)
    if values.get("v") != "2" or values.get("sv") != "1":
        raise SnapshotError("unsupported V2 contract or envelope version")
    state = values.get("s")
    if state in {"denied", "withheld", "unavailable", "exhausted", "invalid"}:
        if set(values) != {"v", "sv", "s"}:
            raise SnapshotError("non-evidence V2 state has extra fields")
        return values
    if state == "none":
        if (set(values) != set(V2_NONE_ORDER) or
                tuple(values) != V2_NONE_ORDER):
            raise SnapshotError("empty V2 envelope fields are not exact")
        if not CID.fullmatch(values["cid"]):
            raise SnapshotError("invalid V2 incarnation")
        _u64(values["g"], "generation", 1)
        if _u64(values["e"], "event sequence") != 0:
            raise SnapshotError("empty V2 envelope has an event")
        return values
    if (state != "complete" or set(values) != V2_COMPLETE_FIELDS or
            tuple(values) != V2_COMPLETE_ORDER):
        raise SnapshotError("complete V2 fields are not exact")
    if not CID.fullmatch(values["cid"]):
        raise SnapshotError("invalid V2 incarnation")
    _u64(values["g"], "generation", 1)
    _u64(values["e"], "event sequence", 1)
    _one_of(values["mode"], "mode", {"off", "allowlist", "shadow_p0"})
    _one_of(values["enabled"], "enabled", {"on", "off"})
    if values["context"] != "proven_client":
        raise SnapshotError("V2 event context is not proven")
    _one_of(values["policy"], "policy",
            {"pass_through", "request_delegation"})
    _one_of(values["force"], "force", {"off", "external", "qi_stack"})
    _one_of(values["engine"], "engine", {"unknown", "duckdb"})
    _one_of(values["verification"], "verification",
            {"unverified", "verified"})
    _one_of(values["observation"], "observation", {
        "none", "no_authoritative_match", "duckdb_scan_at_downstream_boundary"})
    _one_of(values["p0"], "p0", {"unknown", "bounded"})
    _one_of(values["p0_reason"], "p0 reason",
            {"ok", "no_query", "limit", "catalog_miss",
             "unsupported_shape", "unsupported_node",
             "unsupported_relation", "catalog_mismatch"})
    if values["verification"] == "verified" and not (
            values["engine"] == "duckdb" and
            values["observation"] == "duckdb_scan_at_downstream_boundary"):
        raise SnapshotError("V2 verified observation is contradictory")
    if (values["observation"] == "duckdb_scan_at_downstream_boundary" and
            values["engine"] != "duckdb"):
        raise SnapshotError("V2 engine and observation contradict each other")
    if ((values["p0"] == "bounded") != (values["p0_reason"] == "ok")):
        raise SnapshotError("V2 P0 state and reason contradict each other")
    if values["policy"] == "request_delegation" and not (
            values["enabled"] == "on" and values["mode"] == "allowlist" and
            values["force"] == "qi_stack"):
        raise SnapshotError("V2 delegation request is contradictory")
    if values["force"] == "qi_stack" and values["policy"] != "request_delegation":
        raise SnapshotError("V2 owned force lacks a delegation request")
    _validate_decision(values)
    return values


def parse_snapshot(*, version: int, v1: Mapping[str, str] | None = None,
                   v2: str | None = None) -> dict[str, str | int]:
    """Dispatch explicitly. A present malformed V2 never falls back to V1."""
    if version == 1 and v1 is not None and v2 is None:
        return parse_v1(v1)
    if version == 2 and v2 is not None and v1 is None:
        return parse_v2(v2)
    raise SnapshotError("snapshot version and payload are not exact")
