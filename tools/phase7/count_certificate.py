#!/usr/bin/env python3
"""Replay exact-site COUNT(*) evidence and prepare an inactive certificate record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.phase5.corpus_oracle import load_json
from tools.phase7.count_matrix import ARMS, CASES, DELETED, case_run_id, inspect, require, site_binding
from tools.provider_pins import DUCKDB_SHA as DUCKDB_SOURCE, PG_DUCKDB_SHA as PG_DUCKDB_SOURCE

VERSIONS = {16: "16.16", 17: "17.12", 18: "18.7", 19: "19.0"}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
RUNTIME_FIELDS = ("postgres", "pg_qi[.]so", "pg_duckdb[.]so", "libduckdb[.]so")
RUN_NAME = re.compile(r"count-matrix-pg(16|17|18|19)\.[A-Za-z0-9]{8}\Z")
MAX_LIFETIME = 90 * 24 * 60 * 60


def private_bytes(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and
                not before.st_mode & 0o077 and before.st_size <= maximum,
                f"not a bounded private evidence file: {path.name}")
        data = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
        require(len(data) == before.st_size and len(data) <= maximum and
                (before.st_ino, before.st_mtime_ns, before.st_size) ==
                (after.st_ino, after.st_mtime_ns, after.st_size),
                f"evidence changed while reading: {path.name}")
        return data


def identity(root: Path, major: int, revision: str) -> dict:
    before = private_bytes(root / "identity-before.txt", 8192)
    after = private_bytes(root / "identity-after.txt", 8192)
    require(before == after, "runtime identity changed during the run")
    fields = before.decode("ascii").splitlines()
    expected_site = site_binding(before)
    require(fields[:3] == [f"postgres_major={major}", f"postgres_version={VERSIONS[major]}",
                          f"pg_qi_revision={revision}"], "run does not have the expected exact version and revision")
    build_id = fields[3].removeprefix("postgres_build_id=")
    require(fields[3].startswith("postgres_build_id=") and HEX64.fullmatch(build_id) is not None and
            int(build_id, 16) != 0, "PostgreSQL build identity not recorded")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", fields[4]) is not None and
            0 < int(fields[4]) < 2**64, "invalid cluster identity")
    require(HEX64.fullmatch(fields[5]) is not None and int(fields[5], 16) != 0,
            "site identity not recorded")
    require(fields[6:8] == ["0", "0"], "runtime did not prove the serial envelope")
    observed_time = re.fullmatch(r"acquired_at_unix=([1-9][0-9]{0,18})", fields[8])
    require(observed_time is not None, "acquisition time is absent")
    binaries = {}
    for name, line in zip(RUNTIME_FIELDS, fields[10:], strict=True):
        parts = line.split("\t")
        require(len(parts) == 3 and parts[0] == f"runtime_{name}" and
                Path(parts[1]).is_absolute() and HEX64.fullmatch(parts[2]) is not None and
                int(parts[2], 16) != 0, "invalid binary identity")
        require(Path(parts[1]).name == name.replace("[.]", "."), "identity names the wrong binary")
        binaries[Path(parts[1]).name] = parts[2]
    return {"major": major, "postgres_version": VERSIONS[major], "pg_qi_revision": revision,
            "postgres_build_id": build_id, "cluster_identifier": fields[4], "site_build_id": fields[5],
            "acquired_at_unix": int(observed_time[1]), "acquisition_site_binding": expected_site,
            "binary_sha256": binaries, "identity_sha256": hashlib.sha256(before).hexdigest()}


def inventory(root: Path) -> dict[str, str]:
    names = ["identity-before.txt", "identity-after.txt", "matrix-result.json"]
    for case in CASES:
        items = ["subject.sql", "validated-subject.sql", "manifest.json", "result.json", "fixture.json",
                 "driver.log", "driver-exit.json"]
        items += [f"{arm}.{ext}" for arm in ARMS for ext in ["receipt.json", "spool"]]
        if case.expected_new_count is not None:
            items += ["mutation.sql", "missing-import.spool", "missing-import.receipt.json"]
        names += [f"{case.key}/{item}" for item in items]
    return {name: hashlib.sha256(private_bytes(root / name, 1024 * 1024)).hexdigest()
            for name in sorted(names)}


def verify(root: Path, major: int, revision: str) -> dict:
    match = RUN_NAME.fullmatch(root.name)
    require(match is not None and int(match[1]) == major, "unexpected run root")
    for path in (root, *(root / case.key for case in CASES)):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and
                info.st_uid == os.getuid(), "run root and cases must be owned private directories")
    bound = identity(root, major, revision)
    files = inventory(root)
    matrix = load_json(root / "matrix-result.json")
    require(matrix["document_type"] == "pq35_count_lab_matrix" and
            matrix["qualification"] == "LAB_ONLY_UNAPPROVED" and matrix["schema_version"] == 1 and
            matrix["identity_sha256"] == bound["identity_sha256"] and matrix["result"] == "PASS",
            "unexpected matrix format")
    completed = matrix["completed_at_unix"]
    require(type(completed) is int and bound["acquired_at_unix"] <= completed <=
            bound["acquired_at_unix"] + 3600, "matrix acquisition time is invalid")
    require([row["case"] for row in matrix["rows"]] == [case.key for case in CASES],
            "matrix is incomplete or out of order")
    for case, row in zip(CASES, matrix["rows"], strict=True):
        path = root / case.key
        require(private_bytes(path / "subject.sql", 4096) == case.subject, "source subject differs from case")
        if case.delta is not None:
            require(private_bytes(path / "mutation.sql", 4096) == case.delta, "mutation differs from case")
        manifest = load_json(path / "manifest.json")
        fixture = load_json(path / "fixture.json")
        require(fixture == row["fixture"] and type(fixture.get("baseline_count")) is int and
                fixture["baseline_count"] == case.expected_count, "invalid fixture baseline count")
        require(load_json(path / "result.json").get("site_binding") == bound["acquisition_site_binding"],
                "acquisition did not verify the same site in the driver")
        seal = case_run_id(root.name, bound["identity_sha256"], fixture, case)
        require(manifest["run_id"] == seal and manifest["subject_id"] == case.key and
                manifest["authorization_reference"] == "PQ-35-isolated-lab" and
                hashlib.sha256(case.subject).hexdigest() == manifest["sql_sha256"],
                "case receipt does not bind to this run")
        exit_status = load_json(path / "driver-exit.json")["status"]
        require(type(exit_status) is int and row["driver_exit_code"] == exit_status and
                row["test_result"] == "PASS" and row["qualification_case"] is (not case.only),
                "matrix did not record a complete passing test")
        measured = inspect(case, path, manifest, exit_status)
        require(all(row.get(key) == value for key, value in measured.items()),
                "matrix summary differs from the re-evaluated evidence")
        require(measured["shadow_safe"] == "unknown", "evidence was collected with an active certificate")
        if case.key == "dead-tuples":
            require(type(fixture.get("dead_tuples")) is int and fixture["dead_tuples"] >= DELETED,
                    "dead-tuple fixture was not observed")
        elif case.key == "all-visible":
            require(type(fixture.get("all_visible_pages")) is int and fixture["all_visible_pages"] > 0,
                    "all-visible fixture was not observed")
        elif case.key == "hot-update":
            require(type(fixture.get("hot_updates")) is int and fixture["hot_updates"] > 0,
                    "HOT fixture was not observed")
        elif case.key == "rolled-back":
            require(fixture.get("rollback_row_visible") is False, "rolled-back tuple visibility was not checked")
    require(inventory(root) == files, "evidence changed during verification")
    return {"schema_version": 1, "document_type": "pq35_count_evidence", "run_id": root.name,
            "completed_at_unix": completed,
            "scope": "count_star_serial_rr_ro", "positive_equivalence": sum(not c.only for c in CASES),
            "forced_refusal_controls": sum(c.only for c in CASES), "identity": bound, "files": files}


def record(receipt: dict, evidence_digest: str, issued: int, expires: int) -> str:
    require(receipt["completed_at_unix"] <= issued < expires < 2**64 and
            expires <= receipt["identity"]["acquired_at_unix"] + MAX_LIFETIME,
            "invalid certificate validity window")
    site = receipt["identity"]
    major, minor = map(int, site["postgres_version"].split("."))
    binaries = site["binary_sha256"]
    fields = (("v", 1), ("scope", receipt["scope"]), ("pg", major * 10_000 + minor),
              ("cluster", site["cluster_identifier"]), ("site", site["site_build_id"]),
              ("pg_build", site["postgres_build_id"]), ("qi_build", site["pg_qi_revision"]),
              ("provider_src", PG_DUCKDB_SOURCE), ("duckdb_src", DUCKDB_SOURCE),
              ("pg_bin", binaries["postgres"]), ("qi_bin", binaries["pg_qi.so"]),
              ("provider_bin", binaries["pg_duckdb.so"]), ("duckdb_bin", binaries["libduckdb.so"]),
              ("proof", evidence_digest), ("issued", issued), ("expires", expires))
    return ";".join(f"{key}={value}" for key, value in fields)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--major", type=int, choices=VERSIONS, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--expected-digest")
    parser.add_argument("--issued-at", type=int)
    parser.add_argument("--expires-at", type=int)
    args = parser.parse_args()
    try:
        require(HEX40.fullmatch(args.revision) is not None, "invalid expected pg_qi revision")
        receipt = verify(args.root.resolve(strict=True), args.major, args.revision)
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("ascii")
        digest = hashlib.sha256(encoded).hexdigest()
        require(args.expected_digest is None or args.expected_digest == digest, "sealed evidence digest mismatch")
        answer = {"digest": digest, "receipt": receipt}
        if args.issued_at is not None or args.expires_at is not None:
            require(args.issued_at is not None and args.expires_at is not None and
                    args.expected_digest == digest, "record preparation requires both dates and reviewed evidence digest")
            prepared = record(receipt, digest, args.issued_at, args.expires_at)
            answer.update(certificate_record=prepared,
                          approval_sha256=hashlib.sha256(prepared.encode("ascii")).hexdigest())
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"UNKNOWN: {error}", file=sys.stderr)
        return 1
    print(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
