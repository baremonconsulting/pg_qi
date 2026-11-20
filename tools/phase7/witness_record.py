#!/usr/bin/env python3
"""Strict parser for the bounded PQ-31 test-only execution witness."""

from __future__ import annotations

import re
from dataclasses import dataclass


class WitnessError(ValueError):
    """The supplied witness is malformed or internally contradictory."""


FIELDS = ("v", "seq", "status", "engine", "token", "pid", "db", "user",
          "meta", "columns", "truncated",
          "custom_nodes", "custom_completed", "duckdb_present")
UINT = re.compile(r"(?:0|[1-9][0-9]*)\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
META = re.compile(r"([0-9]+):(-?(?:0|[1-9][0-9]*)):([0-9]+)\Z")


@dataclass(frozen=True)
class WitnessColumn:
    oid: int
    typmod: int
    collation: int


@dataclass(frozen=True)
class WitnessRecord:
    sequence: int
    status: str
    engine: str
    token: str
    pid: int
    database_oid: int
    user_oid: int
    truncated: bool
    columns: tuple[WitnessColumn, ...]
    custom_nodes: int
    custom_completed: int
    duckdb_present: bool


def _uint(value: str, name: str, maximum: int = 2_147_483_647) -> int:
    if not UINT.fullmatch(value):
        raise WitnessError(f"{name} is not a canonical unsigned integer")
    result = int(value)
    if result > maximum:
        raise WitnessError(f"{name} is out of range")
    return result


def parse_witness(text: str) -> WitnessRecord:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 8192:
        raise WitnessError("witness is not a bounded string")
    try:
        text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise WitnessError("witness is not ASCII") from exc
    pairs: list[tuple[str, str]] = []
    for token in text.split(";"):
        if "=" not in token:
            raise WitnessError("malformed witness token")
        key, value = token.split("=", 1)
        if not key:
            raise WitnessError("empty witness key")
        pairs.append((key, value))
    if tuple(key for key, _ in pairs) != FIELDS:
        raise WitnessError("witness fields or order are not exact")
    values = dict(pairs)
    if values["v"] != "1":
        raise WitnessError("unsupported witness version")
    sequence = _uint(values["seq"], "sequence")
    if values["status"] not in {"none", "success", "error"}:
        raise WitnessError("invalid witness status")
    if values["engine"] not in {"postgres", "duckdb", "mixed", "unknown"}:
        raise WitnessError("invalid witness engine")
    if not HEX64.fullmatch(values["token"]):
        raise WitnessError("invalid witness subject token")
    pid = _uint(values["pid"], "backend pid")
    database_oid = _uint(values["db"], "database oid", 0xFFFFFFFF)
    user_oid = _uint(values["user"], "user oid", 0xFFFFFFFF)
    if values["truncated"] not in {"t", "f"}:
        raise WitnessError("invalid witness truncation flag")
    truncated = values["truncated"] == "t"
    column_count = _uint(values["columns"], "column count", 64)
    custom_nodes = _uint(values["custom_nodes"], "custom node count", 4096)
    custom_completed = _uint(values["custom_completed"], "completed custom count", 4096)
    if custom_completed > custom_nodes:
        raise WitnessError("completed custom count exceeds custom nodes")
    if values["duckdb_present"] not in {"t", "f"}:
        raise WitnessError("invalid DuckDB presence flag")
    duckdb_present = values["duckdb_present"] == "t"

    columns: list[WitnessColumn] = []
    if values["meta"]:
        for item in values["meta"].split(","):
            match = META.fullmatch(item)
            if not match:
                raise WitnessError("malformed witness metadata")
            oid, typmod, collation = (int(part) for part in match.groups())
            if not 0 < oid <= 0xFFFFFFFF or not -(2**31) <= typmod < 2**31 or \
                    not 0 <= collation <= 0xFFFFFFFF:
                raise WitnessError("witness metadata is out of range")
            columns.append(WitnessColumn(oid, typmod, collation))
    if len(columns) != column_count:
        raise WitnessError("witness column count does not match metadata")

    status = values["status"]
    engine = values["engine"]
    if status == "none":
        if sequence != 0 or engine != "unknown" or pid != 0 or database_oid != 0 or \
                user_oid != 0 or truncated or columns or custom_nodes or custom_completed or duckdb_present:
            raise WitnessError("empty witness record is contradictory")
    elif sequence == 0:
        raise WitnessError("completed witness has zero sequence")
    if engine == "postgres" and (custom_nodes != 0 or custom_completed != 0 or
                                  duckdb_present):
        raise WitnessError("PostgreSQL witness contains a custom provider")
    if engine == "duckdb" and (not duckdb_present or custom_completed == 0):
        raise WitnessError("DuckDB witness lacks completed execution")
    if engine == "mixed" and (not duckdb_present or custom_completed < 2):
        raise WitnessError("mixed witness lacks two completed providers")

    return WitnessRecord(sequence, status, engine, values["token"], pid,
                         database_oid, user_oid, truncated, tuple(columns), custom_nodes,
                         custom_completed, duckdb_present)
