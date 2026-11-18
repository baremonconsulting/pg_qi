#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.phase5.control_runtime import (
    BackendIdentity, ControlError, DispatchStopped, OwnershipRegistry,
    LocalProcResourceFeed, PilotControlRuntime, ResourcePolicy, ResourceSample,
    ResourceWatchdog, StopController, load_strict_json,
    verify_journal_prefix,
)
from tools.phase5.pilot_runtime import AuditedSession, SequenceJournal


def policy(**changes: object) -> ResourcePolicy:
    value = {"schema_version": 1, "document_type": "resource_policy",
        "policy_id": "policy-1", "scope_sha256": "a" * 64, "feed_version": 1,
        "source_id": "local-proc-feed", "interval_ms": 100, "max_age_ms": 500,
        "stop_overshoot_ms": 100,
        "limits": {"rss_bytes": 1000, "cpu_milliseconds": 2000,
                   "evidence_bytes": 3000, "inflight_requests": 4},
        "units": {"rss_bytes": "bytes", "cpu_milliseconds": "milliseconds",
                  "evidence_bytes": "bytes", "inflight_requests": "count"}}
    value.update(changes)
    return ResourcePolicy.parse(value)


def sample(now: int, sequence: int = 1, **changes: object) -> ResourceSample:
    value = {"schema_version": 1, "document_type": "resource_sample",
        "scope_sha256": "a" * 64, "feed_version": 1, "source_id": "local-proc-feed",
        "sequence": sequence, "monotonic_ns": now,
        "values": {"rss_bytes": 10, "cpu_milliseconds": 20,
                   "evidence_bytes": 30, "inflight_requests": 0},
        "units": {"rss_bytes": "bytes", "cpu_milliseconds": "milliseconds",
                  "evidence_bytes": "bytes", "inflight_requests": "count"}}
    value.update(changes)
    return ResourceSample.parse(value)


class ResourceAndStopTests(unittest.TestCase):
    def test_approved_local_proc_feed_is_versioned_ordered_and_unit_bound(self) -> None:
        configured = policy()
        feed = LocalProcResourceFeed(configured, lambda: 123456)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root / "evidence").write_bytes(b"1234567")
            first = feed.sample(root, inflight_requests=1)
            second = feed.sample(root, inflight_requests=0)
            self.assertEqual((first.source_id, first.feed_version, first.sequence),
                             (configured.source_id, configured.feed_version, 1))
            self.assertEqual(second.sequence, 2)
            self.assertEqual(first.monotonic_ns, 123456)
            self.assertEqual(first.units, configured.units)
            self.assertGreaterEqual(first.values["evidence_bytes"], 7)

    def test_local_feed_includes_descendant_process_memory(self) -> None:
        configured = policy(limits={"rss_bytes": 2**50, "cpu_milliseconds": 2**50,
            "evidence_bytes": 2**50, "inflight_requests": 4})
        feed = LocalProcResourceFeed(configured)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); before = feed.sample(root, 0).values["rss_bytes"]
            child = subprocess.Popen([sys.executable, "-c",
                "x=bytearray(32*1024*1024); print('READY',flush=True); input()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "READY")  # type: ignore[union-attr]
                during = feed.sample(root, 0).values["rss_bytes"]
                self.assertGreater(during, before + 16 * 1024 * 1024)
            finally:
                child.terminate(); child.wait(timeout=5)
                if child.stdin is not None: child.stdin.close()
                if child.stdout is not None: child.stdout.close()

    def test_composed_runtime_feeds_the_real_controller(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); journal = SequenceJournal(root)
            configured = policy(interval_ms=20, max_age_ms=100, stop_overshoot_ms=20,
                limits={"rss_bytes": 2**50, "cpu_milliseconds": 2**50,
                        "evidence_bytes": 2**50, "inflight_requests": 4})
            runtime = PilotControlRuntime(journal, configured, root); runtime.start()
            with runtime.controller.admission(): pass
            __import__("time").sleep(0.05); runtime.close(); journal.close()
            rows = [json.loads(line) for line in
                    (root / "request.journal.jsonl").read_text().splitlines()]
            self.assertGreaterEqual(sum(row["phase"] == "resource" for row in rows), 2)

    def test_audited_session_obeys_atomic_admission_gate(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); now = 1_000_000_000
            journal = SequenceJournal(root); controller = StopController(journal, policy(), lambda: now)
            controller.accept_sample(sample(now))
            driver = Path(sys.executable).resolve()
            fixture = Path(__file__).parent / "fixtures/framed-driver.py"
            session = AuditedSession(driver, journal, [str(fixture)], {"safe": "a" * 64},
                hashlib.sha256(driver.read_bytes()).hexdigest(), admission_controller=controller)
            self.assertEqual(session.dispatch("safe")["state"], "COMPLETE")
            controller.stop("USER_STOP")
            with self.assertRaises(DispatchStopped): session.dispatch("safe")
            session.close(); journal.close()
            rows = [json.loads(line) for line in
                    (root / "request.journal.jsonl").read_text().splitlines()]
            self.assertEqual(sum(row["payload"].get("event") == "dispatch_intent"
                                 for row in rows), 1)

    def test_missing_stale_invalid_and_limit_feed_latch_stop(self) -> None:
        for kind in ("missing", "stale", "identity", "scope", "version", "units",
                     "sequence", "older_time", "cpu_backwards", "future", "limit"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as name:
                root = Path(name); root.chmod(0o700); now = [1_000_000_000]
                journal = SequenceJournal(root)
                controller = StopController(journal, policy(), lambda: now[0])
                if kind == "missing":
                    with self.assertRaises(DispatchStopped):
                        with controller.admission(): pass
                elif kind == "stale":
                    controller.accept_sample(sample(now[0])); now[0] += 600_000_000
                    with self.assertRaises(DispatchStopped):
                        with controller.admission(): pass
                elif kind == "identity":
                    with self.assertRaises(DispatchStopped):
                        controller.accept_sample(sample(now[0], source_id="foreign"))
                elif kind == "scope":
                    with self.assertRaises(DispatchStopped):
                        controller.accept_sample(sample(now[0], scope_sha256="b" * 64))
                elif kind == "version":
                    with self.assertRaises(DispatchStopped):
                        controller.accept_sample(sample(now[0], feed_version=2))
                elif kind == "units":
                    bad_units = {"rss_bytes": "kilobytes", "cpu_milliseconds": "milliseconds",
                                 "evidence_bytes": "bytes", "inflight_requests": "count"}
                    with self.assertRaises(ControlError):
                        sample(now[0], units=bad_units)
                elif kind == "sequence":
                    controller.accept_sample(sample(now[0]))
                    with self.assertRaises(DispatchStopped):
                        controller.accept_sample(sample(now[0], sequence=1))
                elif kind == "older_time":
                    controller.accept_sample(sample(now[0], sequence=1))
                    with self.assertRaises(DispatchStopped):
                        controller.accept_sample(sample(now[0] - 1, sequence=2))
                elif kind == "cpu_backwards":
                    controller.accept_sample(sample(now[0], sequence=1))
                    values = {"rss_bytes": 10, "cpu_milliseconds": 19,
                              "evidence_bytes": 30, "inflight_requests": 0}
                    with self.assertRaises(DispatchStopped):
                        controller.accept_sample(sample(now[0], sequence=2, values=values))
                elif kind == "future":
                    with self.assertRaises(DispatchStopped):
                        controller.accept_sample(sample(now[0] + 1))
                else:
                    values = {"rss_bytes": 1001, "cpu_milliseconds": 20,
                              "evidence_bytes": 30, "inflight_requests": 0}
                    with self.assertRaises(DispatchStopped):
                        controller.accept_sample(sample(now[0], values=values))
                if kind != "units": self.assertTrue(controller.stopped)
                journal.close()
                rows = [json.loads(line) for line in
                        (root / "request.journal.jsonl").read_text().splitlines()]
                if kind != "units":
                    self.assertEqual(rows[-1]["payload"]["event"], "stop_latched")
                    self.assertNotIn("foreign", json.dumps(rows[-1]))

    def test_each_resource_metric_has_exact_limit_boundary(self) -> None:
        configured = policy()
        for metric, limit in configured.limits.items():
            with self.subTest(metric=metric), tempfile.TemporaryDirectory() as name:
                root = Path(name); root.chmod(0o700); now = 1_000_000_000
                journal = SequenceJournal(root); controller = StopController(journal, configured,
                    lambda: now)
                equal = {key: 0 for key in configured.limits}; equal[metric] = limit
                controller.accept_sample(sample(now, values=equal))
                above = dict(equal); above[metric] = limit + 1
                with self.assertRaises(DispatchStopped):
                    controller.accept_sample(sample(now, sequence=2, values=above))
                self.assertEqual(controller.reason, "RESOURCE_LIMIT")
                journal.close()

    def test_stop_is_ordered_after_admitted_intent_and_blocks_later_intent(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); now = 1_000_000_000
            journal = SequenceJournal(root); controller = StopController(journal, policy(), lambda: now)
            controller.accept_sample(sample(now))
            admitted = threading.Event(); release = threading.Event()
            def dispatch_boundary() -> None:
                with controller.admission():
                    intent = journal.append("request", "STARTED", {"event": "dispatch_intent"})
                    controller.intent_started(intent["sequence"]); admitted.set(); release.wait(2)
            worker = threading.Thread(target=dispatch_boundary); worker.start(); admitted.wait(2)
            stopper = threading.Thread(target=lambda: controller.stop("USER_STOP")); stopper.start()
            self.assertTrue(stopper.is_alive())
            release.set(); worker.join(2); stopper.join(2)
            self.assertTrue(controller.stopped)
            with self.assertRaises(DispatchStopped):
                with controller.admission(): pass
            journal.close()
            rows = [json.loads(line) for line in
                    (root / "request.journal.jsonl").read_text().splitlines()]
            self.assertEqual(rows[-1]["payload"]["reason"], "USER_STOP")
            self.assertEqual(rows[-1]["payload"]["inflight_intent_sequences"], [2])
            self.assertFalse(any(row["payload"].get("event") == "dispatch_intent"
                                 for row in rows[rows[-1]["sequence"]:]))

    def test_local_inflight_limit_is_not_dependent_on_feed_lag(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); now = 1_000_000_000
            configured = policy(limits={"rss_bytes": 1000, "cpu_milliseconds": 2000,
                "evidence_bytes": 3000, "inflight_requests": 1})
            journal = SequenceJournal(root)
            controller = StopController(journal, configured, lambda: now)
            controller.accept_sample(sample(now)); controller.intent_started(2)
            with self.assertRaises(DispatchStopped):
                with controller.admission(): pass
            self.assertEqual(controller.reason, "RESOURCE_LIMIT")
            journal.close()

    def test_concurrent_journal_writers_preserve_one_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); journal = SequenceJournal(root)
            threads = [threading.Thread(target=lambda offset=offset: [
                journal.append("resource", "COMPLETE", {"writer": offset, "item": item})
                for item in range(20)]) for offset in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5)
            journal.close()
            result = verify_journal_prefix(root / "request.journal.jsonl")
            self.assertEqual(result["records"], 80)

    def test_journal_failure_is_irreversible_hard_stop(self) -> None:
        class BrokenJournal:
            def append(self, *_args: object, **_kwargs: object) -> None:
                raise OSError("secret free text must not escape")
        controller = StopController(BrokenJournal(), policy(), lambda: 1_000_000_000)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ControlError, "journal_failure"):
            controller.accept_sample(sample(1_000_000_000))
        self.assertEqual(controller.reason, "JOURNAL_FAILURE")

    def test_signal_handler_uses_fixed_reason(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); journal = SequenceJournal(root)
            controller = StopController(journal, policy(), lambda: 1_000_000_000)
            previous = controller.install_signal_handlers()
            try:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler); handler(signal.SIGTERM, None)
                for _ in range(100):
                    if controller.reason is not None: break
                    __import__("time").sleep(0.001)
                self.assertEqual(controller.reason, "SIGNAL_TERM")
            finally:
                for number, old in previous.items(): signal.signal(number, old)
                journal.close()

    def test_watchdog_stops_missing_feed_within_budget(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); journal = SequenceJournal(root)
            configured = policy(interval_ms=20, max_age_ms=40, stop_overshoot_ms=20)
            controller = StopController(journal, configured)
            watchdog = ResourceWatchdog(controller, configured)
            started = __import__("time").monotonic(); watchdog.start()
            while not controller.stopped and __import__("time").monotonic() - started < 0.5:
                __import__("time").sleep(0.005)
            elapsed_ms = (__import__("time").monotonic() - started) * 1000
            watchdog.close(); journal.close()
            self.assertEqual(controller.reason, "RESOURCE_FEED_MISSING")
            self.assertLessEqual(elapsed_ms, configured.stop_overshoot_ms + 40)


class OwnershipTests(unittest.TestCase):
    def identity(self, **changes: object) -> BackendIdentity:
        value = {"cluster_id": "cluster-1", "start_id": "start-1", "pid": 42,
                 "backend_start": "2026-11-18T00:00:00Z", "session_id": "session-1"}
        value.update(changes); return BackendIdentity.parse(value)

    def test_only_full_identity_is_owned_and_restart_is_not_absence_proof(self) -> None:
        registry = OwnershipRegistry("cluster-1", "start-1"); owned = self.identity()
        registry.register(owned)
        self.assertEqual(registry.exact_target(owned), 42)
        self.assertIsNone(registry.exact_target(self.identity(pid=43)))
        self.assertFalse(registry.retirement("cluster-1", "start-2", [], True)
                         ["backend_absence_verified"])
        self.assertFalse(registry.retirement("cluster-1", "start-1", [owned], True)
                         ["backend_absence_verified"])
        result = registry.retirement("cluster-1", "start-1", [], True)
        self.assertTrue(result["backend_absence_verified"]); self.assertEqual(result["exit_state"], "CLEAN")

    def test_empty_registry_and_real_postgresql_timestamp_are_fail_closed(self) -> None:
        registry = OwnershipRegistry("cluster-1", "start-1")
        self.assertEqual(registry.retirement("cluster-1", "start-1", [], True)["exit_state"],
                         "UNKNOWN")
        parsed = self.identity(backend_start="2026-11-18 00:00:00.123456+00")
        self.assertEqual(parsed.pid, 42)
        registry.register(self.identity(backend_start="2026-11-18T00:00:00.123456Z"))
        result = registry.retirement("cluster-1", "start-1", [parsed], True)
        self.assertEqual(result["exit_state"], "UNKNOWN")
        self.assertFalse(result["backend_absence_verified"])
        foreign_scope = self.identity(start_id="start-X")
        result = registry.retirement("cluster-1", "start-1", [foreign_scope], True)
        self.assertEqual(result["reason"], "active_identity_scope_mismatch")
        self.assertFalse(result["backend_absence_verified"])


class RecoveryAndInputTests(unittest.TestCase):
    def test_torn_or_valid_prefix_is_never_pass(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700); journal = SequenceJournal(root)
            journal.append("request", "STARTED", {"event": "dispatch_intent"}); journal.close()
            path = root / "request.journal.jsonl"
            self.assertEqual(verify_journal_prefix(path)["result"], "INCOMPLETE")
            path.write_bytes(path.read_bytes()[:-1])
            self.assertEqual(verify_journal_prefix(path)["reason"], "torn_journal")

    def test_strict_private_control_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "policy.json"
            path.write_text(json.dumps({"value": 1})); path.chmod(0o600)
            self.assertEqual(load_strict_json(path), {"value": 1})
            path.chmod(0o644)
            with self.assertRaises(ControlError): load_strict_json(path)

    def test_control_file_rejects_links_duplicates_nonfinite_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path = root / "policy.json"
            for payload in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e9999}'):
                path.write_bytes(payload); path.chmod(0o600)
                with self.assertRaises(ControlError): load_strict_json(path)
            path.write_bytes(b"{}" * 20); path.chmod(0o600)
            with self.assertRaises(ControlError): load_strict_json(path, maximum_bytes=4)
            hardlink = root / "hardlink"; os.link(path, hardlink)
            with self.assertRaises(ControlError): load_strict_json(path)
            hardlink.unlink(); target = root / "target"; path.rename(target)
            path.symlink_to(target.name)
            with self.assertRaises(ControlError): load_strict_json(path)

    def test_sigkill_recovers_only_an_incomplete_durable_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); root.chmod(0o700)
            script = """import pathlib, sys, time
from tools.phase5.pilot_runtime import SequenceJournal
j=SequenceJournal(pathlib.Path(sys.argv[1])); j.append('request','STARTED',{'event':'dispatch_intent'})
print('DURABLE', flush=True); time.sleep(30)
"""
            child = subprocess.Popen([sys.executable, "-c", script, str(root)],
                cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            self.assertEqual(child.stdout.readline().strip(), "DURABLE")  # type: ignore[union-attr]
            child.kill(); child.wait(timeout=5)
            if child.stdout is not None: child.stdout.close()
            if child.stderr is not None: child.stderr.close()
            self.assertEqual(child.returncode, -signal.SIGKILL)
            result = verify_journal_prefix(root / "request.journal.jsonl")
            self.assertEqual(result["result"], "INCOMPLETE")
            self.assertEqual(result["records"], 1)

    def test_malformed_journal_is_incomplete_not_an_exception(self) -> None:
        for payload in (b"[1]\n", b'{"x":NaN}\n', b'{"x":1,"x":2}\n'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as name:
                path = Path(name) / "journal"; path.write_bytes(payload)
                self.assertEqual(verify_journal_prefix(path)["result"], "INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
