#!/usr/bin/env python3
"""Collect the exact COUNT(*) differential matrix on a disposable lab cluster."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.phase5.corpus_oracle import OracleError, TypedSpool, load_json, write_json_exclusive
from tools.phase7.decision_snapshot import parse_v2
from tools.phase7.differential_oracle import ARMS, DifferentialError, evaluate, validate_no_import_control
from tools.phase7.decision_snapshot import SnapshotError
from tools.phase7.witness_record import WitnessError
from tools.phase7.witness_record import parse_witness

NROWS = 131_072
DELETED = 1_024
SCHEMA = "pq35_matrix"
ENGINES = {"native": "postgres", "forced": "duckdb", "shadow": "postgres"}


@dataclass(frozen=True)
class Case:
    key: str
    expected_count: int
    expected_new_count: int | None = None
    only: bool = False

    @property
    def table(self) -> str:
        return f"{SCHEMA}.{self.key.replace('-', '_')}"

    @property
    def subject(self) -> bytes:
        return f"SELECT pg_catalog.count(*) FROM {'ONLY ' if self.only else ''}{self.table}\n".encode("ascii")

    @property
    def delta(self) -> bytes | None:
        if self.expected_new_count is None:
            return None
        table = self.table
        return (f"BEGIN; UPDATE {table} SET payload = 42 WHERE id = 1; "
                f"DELETE FROM {table} WHERE id = 2; INSERT INTO {table} VALUES "
                f"({NROWS+1}, 1), ({NROWS+2}, 2), ({NROWS+3}, NULL); COMMIT;\n").encode("ascii")


CASES = (
    Case("empty", 0),
    Case("single-null", 1),
    Case("all-null", 25),
    Case("mixed", NROWS),
    Case("dead-tuples", NROWS - DELETED),
    Case("all-visible", NROWS - DELETED),
    Case("hot-update", NROWS),
    Case("rolled-back", NROWS),
    Case("new-commit", NROWS, NROWS + 2),
    Case("only-refusal", NROWS, only=True),
)


def require(predicate: bool, problem: str) -> None:
    if not predicate:
        raise ValueError(problem)


def site_binding(identity: bytes) -> str:
    fields = identity.decode("ascii").splitlines()
    require(len(fields) == 14, "runtime evidence has incorrect schema")
    version = re.fullmatch(r"postgres_version=(16|17|18|19)\.([0-9]{1,2})", fields[1])
    boot = re.fullmatch(r"postmaster_start_us=([1-9][0-9]{0,18})", fields[9])
    require(version is not None and boot is not None and
            re.fullmatch(r"[1-9][0-9]{0,19}", fields[4]) is not None and
            re.fullmatch(r"[0-9a-f]{64}", fields[5]) is not None, "invalid site binding evidence")
    return f"{fields[4]}:{fields[5]}:{int(version[1]) * 10_000 + int(version[2])}:{boot[1]}"


def case_run_id(run_name: str, identity_digest: str, fixture: dict, case: Case) -> str:
    encoded = json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode("ascii")
    seal = hashlib.sha256(bytes.fromhex(identity_digest) + encoded).hexdigest()
    return f"{run_name}.{seal}.{case.key}"


def psql(binary: Path, conninfo: str, sql: str) -> str:
    result = subprocess.run([str(binary), "-XqAt", "-v", "ON_ERROR_STOP=1", "-d", conninfo],
                            input=sql.rstrip() + "\n", text=True, capture_output=True,
                            timeout=45, check=False)
    if result.returncode != 0:
        raise ValueError(f"fixture command failed: {result.stderr.strip()[:512]}")
    return result.stdout.strip()


def prepare(binary: Path, conninfo: str, case: Case) -> dict[str, object]:
    table = case.table
    statements = [f"CREATE TABLE {table} (id int PRIMARY KEY, payload int) WITH (fillfactor=55);"]
    if case.key == "single-null":
        statements.append(f"INSERT INTO {table} VALUES (1, NULL);")
    elif case.key == "all-null":
        statements.append(f"INSERT INTO {table} SELECT i, NULL FROM generate_series(1, 25) AS i;")
    elif case.key != "empty":
        statements.append(f"INSERT INTO {table} SELECT i, NULLIF(i % 7, 0) "
                          f"FROM generate_series(1, {NROWS}) AS i;")
    if case.key in {"dead-tuples", "all-visible"}:
        statements.append(f"DELETE FROM {table} WHERE id <= {DELETED};")
    if case.key == "all-visible":
        statements.append(f"VACUUM (FREEZE, ANALYZE) {table};")
    else:
        statements.append(f"ANALYZE {table};")
    if case.key == "hot-update":
        statements.append(f"UPDATE {table} SET payload = 42 WHERE id = 1;")
    if case.key == "rolled-back":
        statements.extend(["BEGIN;", f"INSERT INTO {table} VALUES ({NROWS + 1}, 1);", "ROLLBACK;"])
    statements.append("SELECT pg_catalog.pg_stat_force_next_flush();")
    psql(binary, conninfo, "\n".join(statements))
    checks: dict[str, object] = {}
    if case.key == "dead-tuples":
        observed = int(psql(binary, conninfo, f"SELECT n_dead_tup FROM pg_catalog.pg_stat_user_tables "
                            f"WHERE relid = '{table}'::regclass;"))
        require(observed >= DELETED, "deleted tuples not observed before vacuum")
        checks["dead_tuples"] = observed
    if case.key == "all-visible":
        observed = int(psql(binary, conninfo, f"SELECT relallvisible FROM pg_catalog.pg_class "
                            f"WHERE oid = '{table}'::regclass;"))
        require(observed > 0, "fixture has no all-visible pages")
        checks["all_visible_pages"] = observed
    if case.key == "hot-update":
        observed = int(psql(binary, conninfo, f"SELECT n_tup_hot_upd FROM pg_catalog.pg_stat_user_tables "
                            f"WHERE relid = '{table}'::regclass;"))
        require(observed > 0, "fixture update did not create a HOT tuple")
        checks["hot_updates"] = observed
    if case.key == "rolled-back":
        value = psql(binary, conninfo, f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id = {NROWS + 1});")
        require(value == "f", "rolled-back row is visible")
        checks["rollback_row_visible"] = False
    checks["baseline_count"] = int(psql(binary, conninfo,
        "SET duckdb.force_execution = off; SET pg_qi.routing_mode = off;\n" +
        case.subject.decode("ascii") + ";"))
    require(checks["baseline_count"] == case.expected_count, "fixture baseline count did not match")
    return checks


def read_int8_count(path: Path) -> int:
    with TypedSpool(path, 4096, 1, 64, 1) as spool:
        require(spool.version == 2 and len(spool.columns) == 1, "wrong count descriptor width/version")
        column = spool.columns[0]
        require((column.name, column.oid, column.typmod, column.format) == ("count", 20, -1, 0),
                "wrong count descriptor")
        rows = list(spool.rows())
        require(len(rows) == 1 and len(rows[0]) == 9 and rows[0][0] == 1 and
                spool.terminal_status == "complete" and spool.command_completions == 1,
                "expected exactly one complete non-null int8 result")
        return struct.unpack(">q", rows[0][1:])[0]


def inspect(case: Case, path: Path, manifest: dict, driver_status: int) -> dict:
    require((path / "validated-subject.sql").read_bytes() == case.subject, "subject differs from case")
    raw = {arm: load_json(path / f"{arm}.receipt.json") for arm in ARMS}
    for arm, receipt in raw.items():
        require(Path(receipt["spool"]["path"]) == path / f"{arm}.spool", "receipt is outside its case")
    measured = evaluate(manifest, raw, path)
    reported = load_json(path / "result.json")
    state = str(measured["result"])
    reason = str(measured["reason"])
    require((state, reason) == (reported.get("result"), reported.get("reason")),
            "replayed oracle verdict differs from the recorded result")
    expected = ("INCONCLUSIVE", "force_declined") if case.only else ("PASS", "three_arm_equivalent")
    require((state, reason) == expected, f"unexpected oracle verdict: {state}/{reason}")
    require(driver_status == (1 if case.only else 0), "runner exit does not match the oracle")
    observed_engines = {arm: parse_witness(raw[arm]["witness_after"]).engine for arm in ARMS}
    require(observed_engines == (dict.fromkeys(ARMS, "postgres") if case.only else ENGINES),
            "witness engine differs from the case contract")
    counts = {arm: read_int8_count(path / f"{arm}.spool") for arm in ARMS}
    require(set(counts.values()) == {case.expected_count}, "unexpected count in at least one arm")
    shadow = parse_v2(raw["shadow"]["v2_after"])
    require(shadow.get("p0") == "bounded" and shadow.get("barrier") == "closed" and
            shadow.get("policy") == "pass_through", "shadow did not observe the intended bounded form")
    result = {"oracle_result": state, "oracle_reason": reason, "engines": observed_engines,
              "counts": counts, "shadow_safe": shadow.get("safe")}
    if case.expected_new_count is not None:
        control = load_json(path / "missing-import.receipt.json")
        require(Path(control["spool"]["path"]) == path / "missing-import.spool",
                "negative control points outside this run")
        validate_no_import_control(manifest, raw, control)
        missing = read_int8_count(path / "missing-import.spool")
        require(missing == case.expected_new_count, "missing-import control did not see the new count")
        result["missing_import_count"] = missing
    return result


def run(args: argparse.Namespace) -> int:
    out = args.output.resolve(strict=True)
    require(args.isolated_lab and out.is_dir(), "a disposable lab and an existing private output directory are required")
    require(0 == out.stat().st_mode & 0o077, "output directory is not private")
    with (out / "identity-before.txt").open("rb") as handle:
        identity = handle.read(8193)
    require(0 < len(identity) <= 8192, "runtime identity evidence is absent or oversized")
    identity_digest = hashlib.sha256(identity).hexdigest()
    expected_site = site_binding(identity)
    psql(args.psql, args.conninfo, f"CREATE EXTENSION pg_duckdb; CREATE SCHEMA {SCHEMA};")
    results: list[dict] = []
    for case in CASES:
        path = out / case.key
        path.mkdir(mode=0o700)
        current: dict = {"case": case.key, "qualification_case": not case.only,
                         "expected_count": case.expected_count}
        try:
            current["fixture"] = prepare(args.psql, args.conninfo, case)
            write_json_exclusive(path / "fixture.json", current["fixture"])
            (path / "subject.sql").write_bytes(case.subject)
            manifest = {"schema_version": 1, "document_type": "pq31_differential_run",
                        "run_id": case_run_id(out.name, identity_digest, current["fixture"], case),
                        "subject_id": case.key,
                        "sql_sha256": hashlib.sha256(case.subject).hexdigest(), "ordered": False,
                        "authorization_reference": "PQ-35-isolated-lab", "arms": list(ARMS)}
            write_json_exclusive(path / "manifest.json", manifest)
            command = [sys.executable, str(Path(__file__).with_name("run_differential.py")),
                       "--driver", str(args.driver), "--conninfo", args.conninfo,
                       "--manifest", str(path / "manifest.json"), "--sql-file", str(path / "subject.sql"),
                       "--run-root", str(path), "--deadline-ms", "30000", "--site-binding", expected_site]
            if case.delta is not None:
                (path / "mutation.sql").write_bytes(case.delta)
                command += ["--concurrent-sql-file", str(path / "mutation.sql")]
            process = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
            (path / "driver.log").write_text(process.stdout + process.stderr, encoding="utf-8")
            write_json_exclusive(path / "driver-exit.json", {"status": process.returncode})
            current["driver_exit_code"] = process.returncode
            if (path / "result.json").is_file():
                current["reported_oracle"] = load_json(path / "result.json").get("result")
            current.update(inspect(case, path, manifest, process.returncode))
            current["test_result"] = "PASS"
        except (DifferentialError, OracleError, SnapshotError, WitnessError, ValueError,
                OSError, subprocess.TimeoutExpired, KeyError) as error:
            current.update(test_result="FAIL", detail=str(error)[:512])
        results.append(current)
        print(json.dumps(current, sort_keys=True), flush=True)
    ok = all(case["test_result"] == "PASS" for case in results)
    write_json_exclusive(out / "matrix-result.json", {"schema_version": 1,
        "document_type": "pq35_count_lab_matrix", "qualification": "LAB_ONLY_UNAPPROVED",
        "identity_sha256": identity_digest, "completed_at_unix": int(time.time()),
        "result": "PASS" if ok else "FAIL", "rows": results})
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-lab", action="store_true")
    parser.add_argument("--psql", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--conninfo", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
