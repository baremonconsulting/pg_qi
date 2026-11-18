#!/usr/bin/env python3
"""Offline refusal and supervisor-boundary tests for the phase-5 spike."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import importlib.util
import struct

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tools/phase5/protocol-driver"
SUPERVISOR = ROOT / "tools/phase5/supervisor.py"
SPEC = importlib.util.spec_from_file_location("verify_spike", ROOT / "tests/phase5/verify_spike.py")
VERIFY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VERIFY)


def encode_rows(path: Path, values: list[tuple[int, int]], truncate: bool = False) -> None:
    data = bytearray(b"PGQSP001") + struct.pack(">I", 2)
    for name in (b"id", b"value"):
        data += struct.pack(">I", len(name)) + name + struct.pack(">I", 23)
    for identity, value in values:
        data += struct.pack(">I", 2)
        for field in (str(identity), str(value)):
            encoded = field.encode()
            data += struct.pack(">I", len(encoded)) + encoded
    path.write_bytes(bytes(data[:-1] if truncate else data))


def driver_args(root: Path, sql: Path) -> list[str]:
    return [str(DRIVER), "--conninfo", "host=/definitely/missing",
            "--sql-file", str(sql), "--spool", str(root / "out.spool"),
            "--snapshot", str(root / "out.snapshot"), "--protocol", "extended",
            "--session-mode", "off", "--deadline-ms", "10", "--cancel-deadline-ms", "10",
            "--max-rows", "10",
            "--max-bytes", "100", "--max-columns", "2", "--max-field-bytes", "50",
            "--max-spool-bytes", "200", "--max-messages", "20"]


class RefusalTests(unittest.TestCase):
    def test_missing_value_is_usage_not_crash(self) -> None:
        result = subprocess.run([str(DRIVER), "--conninfo"], check=False)
        self.assertEqual(result.returncode, 64)

    def test_signed_and_overflow_numbers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); sql = root / "q.sql"; sql.write_text("SELECT 1")
            for value in ("-1", "+1", "0", "999999999999999999999999"):
                args = driver_args(root, sql)
                args[args.index("--max-rows") + 1] = value
                self.assertEqual(subprocess.run(args, check=False).returncode, 64)

    def test_embedded_nul_is_refused_before_connect(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); sql = root / "q.sql"; sql.write_bytes(b"SELECT 1\0; SELECT 2")
            result = subprocess.run(driver_args(root, sql), check=False,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["reason"], "sql_file")

    def test_existing_or_aliased_output_is_not_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); sql = root / "q.sql"; sql.write_text("SELECT 1")
            output = root / "out.spool"; output.write_text("sentinel")
            result = subprocess.run(driver_args(root, sql), check=False,
                                    capture_output=True, text=True)
            self.assertEqual(output.read_text(), "sentinel")
            self.assertEqual(json.loads(result.stdout)["reason"], "spool_open")


class SupervisorTests(unittest.TestCase):
    def supervisor_command(self, root: Path, driver: Path) -> list[str]:
        sql = root / "q.sql"
        if not sql.exists():
            sql.write_text("SELECT 1")
        return ["python3", str(SUPERVISOR), "--driver", str(driver),
                "--run-root", str(root), "--conninfo", "unused",
                "--sql-file", str(sql), "--spool", str(root / "x.spool"),
                "--snapshot", str(root / "x.snapshot"), "--protocol", "extended",
                "--session-mode", "off", "--deadline-ms", "100",
                "--cancel-deadline-ms", "100",
                "--cancel-grace-ms", "100", "--max-address-space-kib", "65536",
                "--max-output-bytes", "4096", "--max-rows", "10",
                "--max-bytes", "100", "--max-columns", "2",
                "--max-field-bytes", "50", "--max-spool-bytes", "200",
                "--max-messages", "20"]

    def test_noisy_child_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            command = self.supervisor_command(
                root, ROOT / "tests/phase5/fixtures/noisy-driver.sh")
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual(report["terminal"]["terminal_state"], "OUTPUT_LIMIT_EXCEEDED")
            self.assertGreater(report["stdout_bytes"], 4096)
            self.assertLessEqual(report["stdout_captured_bytes"], 4096)

    def test_multiple_or_scalar_terminal_is_rejected(self) -> None:
        for fixture in ("double-terminal.sh", "scalar-terminal.sh",
                        "incomplete-terminal.sh", "invalid-utf8-terminal.py"):
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as name:
                root = Path(name); os.chmod(root, 0o700)
                result = subprocess.run(self.supervisor_command(
                    root, ROOT / "tests/phase5/fixtures" / fixture), check=False,
                    capture_output=True, text=True)
                report = json.loads(result.stdout)
                self.assertEqual(report["terminal"]["terminal_state"],
                                 "INVALID_DRIVER_OUTPUT")

    def test_inherited_pipe_cannot_turn_complete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            result = subprocess.run(self.supervisor_command(
                root, ROOT / "tests/phase5/fixtures/inherited-pipe.sh"), check=False,
                capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual(report["terminal"]["terminal_state"],
                             "SUPERVISOR_TERMINATED")

    def test_closed_streams_do_not_trigger_false_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            command = self.supervisor_command(
                root, ROOT / "tests/phase5/fixtures/close-streams-driver.sh")
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertIsNone(report["supervisor_forced_action"])
            self.assertEqual(report["terminal"]["terminal_state"], "MISSING_TERMINAL")


class OracleMutationTests(unittest.TestCase):
    def test_multiset_allows_reordering_but_refuses_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "rows.spool"
            encode_rows(path, [(2, 1), (1, 1), (3, 1)])
            VERIFY.verify_literal(path, 3)
            encode_rows(path, [(1, 1), (1, 1), (3, 1)])
            with self.assertRaises(ValueError):
                VERIFY.verify_literal(path, 3)

    def test_incomplete_framing_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "rows.spool"
            encode_rows(path, [(1, 1)], truncate=True)
            with self.assertRaises(ValueError):
                list(VERIFY.rows(path))


if __name__ == "__main__":
    unittest.main()
