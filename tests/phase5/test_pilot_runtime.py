#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import sys
from contextlib import contextmanager
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.phase5.pilot_runtime import (AuditedSession, PreparedRegistry,
                                        SequenceJournal, encode_frame, read_frame)


class TestAdmissionController:
    """Explicit unit-test gate; production callers cannot omit admission."""
    @contextmanager
    def admission(self):
        yield
    def intent_started(self, _sequence): pass
    def intent_finished(self, _sequence): pass
    def journal_failed(self): pass


class JournalTests(unittest.TestCase):
    def test_chain_is_durable_ordered_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            journal = SequenceJournal(root)
            first = journal.append("request", "STARTED", {"case_id": "A"})
            second = journal.append("planning", "COMPLETE", {"decision_before": "1"})
            journal.close()
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(second["previous_sha256"], first["record_sha256"])
            rows = [json.loads(line) for line in (root / "request.journal.jsonl").read_text().splitlines()]
            unsigned = dict(rows[0]); digest = unsigned.pop("record_sha256")
            encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False).encode("ascii")
            self.assertEqual(digest, hashlib.sha256(encoded).hexdigest())
            with self.assertRaises(FileExistsError):
                SequenceJournal(root)

    def test_symlink_and_permissive_root_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o755)
            with self.assertRaises(ValueError):
                SequenceJournal(root)
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name); real = parent / "real"; real.mkdir(mode=0o700)
            link = parent / "link"; link.symlink_to(real, target_is_directory=True)
            with self.assertRaises((OSError, ValueError)):
                SequenceJournal(link)

    def test_record_count_and_size_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            journal = SequenceJournal(root, max_records=1, max_record_bytes=1024)
            journal.append("request", "STARTED", {})
            with self.assertRaises(ValueError):
                journal.append("request", "STARTED", {})
            journal.close()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            journal = SequenceJournal(root, max_record_bytes=128)
            with self.assertRaises(ValueError):
                journal.append("request", "STARTED", {"value": "x" * 1000})
            journal.close()

    def test_sync_failure_permanently_closes_journal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            journal = SequenceJournal(root)
            with mock.patch("os.fsync", side_effect=OSError("injected sync failure")):
                with self.assertRaises(OSError):
                    journal.append("request", "STARTED", {})
            with self.assertRaises(ValueError):
                journal.append("request", "STARTED", {})


class RegistryTests(unittest.TestCase):
    def test_a_b_cached_a_and_deallocate_are_distinct(self) -> None:
        registry = PreparedRegistry()
        a = registry.prepare("a", b"SELECT 1", 1)
        b = registry.prepare("b", b"SELECT 2", 2)
        self.assertEqual(registry.active("a"), a)
        self.assertNotEqual(a.sql_sha256, b.sql_sha256)
        retired = registry.deallocate("a", 3)
        self.assertEqual(retired.deallocated_sequence, 3)
        with self.assertRaises(ValueError):
            registry.active("a")
        reparsed = registry.prepare("a", b"SELECT 3", 4)
        self.assertEqual(reparsed.generation, 2)
        self.assertNotEqual(reparsed.sql_sha256, retired.sql_sha256)
        self.assertFalse(reparsed.attributable)
        attributed = registry.replan("a", 5)
        self.assertTrue(attributed.attributable)
        self.assertEqual(attributed.replanned_sequence, 5)
        unattributed = registry.mark_unattributed("a")
        self.assertFalse(unattributed.attributable)
        self.assertEqual(unattributed.generation, attributed.generation)


class FramedSessionTests(unittest.TestCase):
    def test_audited_driver_has_no_lifecycle_or_shell_dispatch(self) -> None:
        source = (ROOT / "tools/phase5/audited_session_driver.c").read_text()
        for forbidden in ("system(", "popen(", "execve(", "pg_ctl", "initdb",
                          "ALTER SYSTEM", "CREATE ROLE", "DROP ROLE", "ssh "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_bounded_protocol_refuses_unknown_opcode(self) -> None:
        with self.assertRaises(ValueError):
            encode_frame(99, {})

    def test_intent_precedes_correlated_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            fixture = Path(__file__).parent / "fixtures/framed-driver.py"
            driver = Path(sys.executable).resolve()
            journal = SequenceJournal(root)
            session = AuditedSession(driver, journal, [str(fixture)], {"case_a": "a" * 64},
                                     hashlib.sha256(driver.read_bytes()).hexdigest(),
                                     TestAdmissionController())
            response = session.dispatch("case_a")
            session.close(); journal.close()
            self.assertEqual(response["state"], "COMPLETE")
            rows = [json.loads(line) for line in
                    (root / "request.journal.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["payload"]["event"], "dispatch_intent")
            self.assertEqual(rows[1]["payload"]["intent_sequence"], 1)
            self.assertEqual([row["phase"] for row in rows[1:]],
                             ["planning", "execution", "snapshot", "request"])
            self.assertEqual(rows[-1]["state"], "COMPLETE")

    def test_session_loss_is_incomplete_and_never_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            fixture = Path(__file__).parent / "fixtures/framed-driver.py"
            driver = Path(sys.executable).resolve()
            journal = SequenceJournal(root)
            session = AuditedSession(driver, journal, [str(fixture), "disconnect"], {"case_a": "a" * 64},
                                     hashlib.sha256(driver.read_bytes()).hexdigest(),
                                     TestAdmissionController())
            with self.assertRaisesRegex(RuntimeError, "will not be replayed"):
                session.dispatch("case_a")
            with self.assertRaises(ValueError):
                session.dispatch("case_a")
            journal.close()
            rows = [json.loads(line) for line in
                    (root / "request.journal.jsonl").read_text().splitlines()]
            self.assertEqual([r["state"] for r in rows], ["STARTED", "INCOMPLETE"])
            self.assertEqual(rows[-1]["payload"]["event"], "session_lost_after_dispatch")

    def test_mismatched_response_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            fixture = Path(__file__).parent / "fixtures/framed-driver.py"
            driver = Path(sys.executable).resolve()
            journal = SequenceJournal(root)
            session = AuditedSession(driver, journal, [str(fixture), "mismatch"], {"case_a": "a" * 64},
                                     hashlib.sha256(driver.read_bytes()).hexdigest(),
                                     TestAdmissionController())
            with self.assertRaises(RuntimeError):
                session.dispatch("case_a")
            journal.close()

    def test_unapproved_case_is_refused_before_process_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            fixture = Path(__file__).parent / "fixtures/framed-driver.py"
            driver = Path(sys.executable).resolve()
            journal = SequenceJournal(root)
            session = AuditedSession(driver, journal, [str(fixture)],
                                     {"safe": "a" * 64},
                                     hashlib.sha256(driver.read_bytes()).hexdigest(),
                                     TestAdmissionController())
            with self.assertRaises(ValueError):
                session.dispatch("pg_terminate_backend")
            session.close(); journal.close()
            self.assertEqual((root / "request.journal.jsonl").read_bytes(), b"")

    def test_prepared_lifecycle_reprepare_generation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); os.chmod(root, 0o700)
            fixture = Path(__file__).parent / "fixtures/framed-driver.py"
            journal = SequenceJournal(root)
            session = AuditedSession(Path(sys.executable).resolve(), journal,
                [str(fixture)], {"case_a": "a" * 64, "case_b": "b" * 64},
                hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
                TestAdmissionController())
            session.prepare("a", "case_a")
            session.prepare("b", "case_b")
            session.execute_prepared("a")
            session.deallocate("a")
            session.prepare("a", "case_a")
            self.assertEqual(session._registry.active("a").generation, 2)
            session.close(); journal.close()


if __name__ == "__main__":
    unittest.main()
