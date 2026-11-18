#!/usr/bin/env python3
"""Fail-closed stop, resource and retirement controls for a PGQI-5 pilot."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import signal
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    from .pilot_runtime import SequenceJournal
except ImportError:  # direct script/tool execution
    from pilot_runtime import SequenceJournal

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
BACKEND_START = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
STOP_REASONS = frozenset({
    "USER_STOP", "SIGNAL_INT", "SIGNAL_TERM", "RESOURCE_FEED_MISSING",
    "RESOURCE_FEED_STALE", "RESOURCE_FEED_INVALID", "RESOURCE_LIMIT",
    "JOURNAL_FAILURE", "SESSION_FAILURE", "EVIDENCE_LIMIT",
})
METRIC_UNITS = {"rss_bytes": "bytes", "cpu_milliseconds": "milliseconds",
                "evidence_bytes": "bytes", "inflight_requests": "count"}


class ControlError(RuntimeError):
    """Fixed-message, fail-closed control error."""


class DispatchStopped(ControlError):
    """Dispatch was refused before a request frame was emitted."""


def _exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ControlError(f"invalid_{name}")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ControlError(f"invalid_{name}")
    return value


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ControlError(f"invalid_{name}")
    return value


def _nonnegative(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlError(f"invalid_{name}")
    return value


def load_strict_json(path: Path, maximum_bytes: int = 1024 * 1024) -> dict[str, Any]:
    try:
        if path.resolve(strict=True) != path.absolute():
            raise ControlError("invalid_control_file")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, RuntimeError) as exc:
        raise ControlError("invalid_control_file") from exc
    try:
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1 or
                    info.st_size > maximum_bytes):
                raise ControlError("invalid_control_file")
            payload = os.read(descriptor, info.st_size + 1)
            if len(payload) != info.st_size:
                raise ControlError("control_file_changed")
        except OSError as exc:
            raise ControlError("invalid_control_file") from exc
    finally:
        os.close(descriptor)

    def duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ControlError("duplicate_json_key")
            result[key] = item
        return result

    try:
        def finite_float(token: str) -> float:
            value = float(token)
            if not math.isfinite(value):
                raise ControlError("nonfinite_json_number")
            return value
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=duplicate,
                           parse_constant=lambda _token: (_ for _ in ()).throw(
                               ControlError("nonfinite_json_number")),
                           parse_float=finite_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ControlError("invalid_control_json") from exc
    if not isinstance(value, dict):
        raise ControlError("invalid_control_json")
    return value


@dataclass(frozen=True)
class ResourcePolicy:
    policy_id: str
    scope_sha256: str
    feed_version: int
    source_id: str
    interval_ms: int
    max_age_ms: int
    stop_overshoot_ms: int
    limits: dict[str, int]
    units: dict[str, str]

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "ResourcePolicy":
        value = _exact(value, {"schema_version", "document_type", "policy_id",
            "scope_sha256", "feed_version", "source_id", "interval_ms", "max_age_ms",
            "stop_overshoot_ms", "limits", "units"}, "resource_policy")
        if value["schema_version"] != 1 or type(value["schema_version"]) is not int or \
                value["document_type"] != "resource_policy":
            raise ControlError("unsupported_resource_policy")
        policy_id = _identifier(value["policy_id"], "policy_id")
        source_id = _identifier(value["source_id"], "source_id")
        if not isinstance(value["scope_sha256"], str) or not DIGEST.fullmatch(value["scope_sha256"]):
            raise ControlError("invalid_scope_sha256")
        feed_version = _positive(value["feed_version"], "feed_version")
        interval_ms = _positive(value["interval_ms"], "interval_ms")
        max_age_ms = _positive(value["max_age_ms"], "max_age_ms")
        overshoot = _positive(value["stop_overshoot_ms"], "stop_overshoot_ms")
        if max_age_ms < interval_ms or overshoot > max_age_ms:
            raise ControlError("invalid_resource_timing")
        limits = _exact(value["limits"], set(METRIC_UNITS), "resource_limits")
        units = _exact(value["units"], set(METRIC_UNITS), "resource_units")
        parsed_limits = {name: _positive(limits[name], f"limit_{name}")
                         for name in METRIC_UNITS}
        if units != METRIC_UNITS:
            raise ControlError("invalid_resource_units")
        return cls(policy_id, value["scope_sha256"], feed_version, source_id,
                   interval_ms, max_age_ms, overshoot, parsed_limits, dict(units))


@dataclass(frozen=True)
class ResourceSample:
    scope_sha256: str
    feed_version: int
    source_id: str
    sequence: int
    monotonic_ns: int
    values: dict[str, int]
    units: dict[str, str]

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "ResourceSample":
        value = _exact(value, {"schema_version", "document_type", "scope_sha256",
            "feed_version", "source_id", "sequence", "monotonic_ns", "values", "units"},
            "resource_sample")
        if value["schema_version"] != 1 or type(value["schema_version"]) is not int or \
                value["document_type"] != "resource_sample":
            raise ControlError("unsupported_resource_sample")
        if not isinstance(value["scope_sha256"], str) or not DIGEST.fullmatch(value["scope_sha256"]):
            raise ControlError("invalid_sample_scope")
        source_id = _identifier(value["source_id"], "sample_source_id")
        feed_version = _positive(value["feed_version"], "sample_feed_version")
        sequence = _positive(value["sequence"], "sample_sequence")
        monotonic_ns = _nonnegative(value["monotonic_ns"], "sample_monotonic_ns")
        values = _exact(value["values"], set(METRIC_UNITS), "sample_values")
        units = _exact(value["units"], set(METRIC_UNITS), "sample_units")
        parsed = {name: _nonnegative(values[name], f"sample_{name}") for name in METRIC_UNITS}
        if units != METRIC_UNITS:
            raise ControlError("invalid_sample_units")
        return cls(value["scope_sha256"], feed_version, source_id, sequence,
                   monotonic_ns, parsed, dict(units))


class LocalProcResourceFeed:
    """The single approved local sampler for the pilot process."""

    def __init__(self, policy: ResourcePolicy,
                 monotonic_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._policy = policy
        self._clock = monotonic_ns
        self._sequence = 0

    def sample(self, evidence_root: Path, inflight_requests: int) -> ResourceSample:
        inflight = _nonnegative(inflight_requests, "inflight_requests")
        evidence = self._evidence_bytes(evidence_root)
        try:
            rss_bytes, cpu_ms = self._process_tree_usage(os.getpid())
        except (OSError, ValueError, IndexError) as exc:
            raise ControlError("local_resource_feed_unavailable") from exc
        self._sequence += 1
        return ResourceSample(self._policy.scope_sha256, self._policy.feed_version,
            self._policy.source_id, self._sequence, self._clock(),
            {"rss_bytes": rss_bytes, "cpu_milliseconds": cpu_ms,
             "evidence_bytes": evidence, "inflight_requests": inflight},
            dict(self._policy.units))

    @staticmethod
    def _evidence_bytes(root: Path) -> int:
        if root.resolve(strict=True) != root.absolute() or not root.is_dir():
            raise ControlError("invalid_evidence_root")
        total = 0; count = 0
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories
                               if not (Path(current) / name).is_symlink()]
            for name in files:
                info = (Path(current) / name).lstat()
                if stat.S_ISLNK(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ControlError("invalid_evidence_entry")
                total += info.st_size; count += 1
                if count > 100000:
                    raise ControlError("evidence_file_limit")
        return total

    @staticmethod
    def _process_tree_usage(root_pid: int) -> tuple[int, int]:
        pending = [root_pid]; seen: set[int] = set(); rss_pages = 0; cpu_ticks = 0
        while pending:
            pid = pending.pop()
            if pid in seen: continue
            seen.add(pid)
            try:
                stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
                rss_pages += int(stat_fields[21])
                cpu_ticks += sum(int(stat_fields[index]) for index in (11, 12, 13, 14))
                task_directories = list(Path(f"/proc/{pid}/task").iterdir())
                if not task_directories:
                    raise FileNotFoundError("task")
                for task in task_directories:
                    children = (task / "children").read_text(encoding="ascii").split()
                    pending.extend(int(child) for child in children)
            except FileNotFoundError:
                if pid == root_pid:
                    raise
                continue
        return (rss_pages * os.sysconf("SC_PAGE_SIZE"),
                cpu_ticks * 1000 // os.sysconf("SC_CLK_TCK"))


@dataclass(frozen=True)
class BackendIdentity:
    cluster_id: str
    start_id: str
    pid: int
    backend_start: str
    session_id: str

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "BackendIdentity":
        value = _exact(value, {"cluster_id", "start_id", "pid", "backend_start", "session_id"},
                       "backend_identity")
        cluster = _identifier(value["cluster_id"], "cluster_id")
        start = _identifier(value["start_id"], "start_id")
        session = _identifier(value["session_id"], "session_id")
        pid = _positive(value["pid"], "backend_pid")
        backend_start = value["backend_start"]
        if not isinstance(backend_start, str) or not BACKEND_START.fullmatch(backend_start):
            raise ControlError("invalid_backend_start")
        try:
            normalized_start = backend_start.replace("Z", "+00:00")
            if re.search(r"[+-][0-9]{2}\Z", normalized_start):
                normalized_start += ":00"
            parsed_start = datetime.fromisoformat(normalized_start)
            if parsed_start.tzinfo is None:
                raise ValueError("timezone")
            backend_start = parsed_start.astimezone(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z")
        except ValueError as exc:
            raise ControlError("invalid_backend_start") from exc
        return cls(cluster, start, pid, backend_start, session)

    def key(self) -> tuple[str, str, int, str]:
        return self.cluster_id, self.start_id, self.pid, self.backend_start


class OwnershipRegistry:
    """Exact backend ownership; deliberately has no broad kill operation."""

    def __init__(self, cluster_id: str, start_id: str) -> None:
        self.cluster_id = _identifier(cluster_id, "registry_cluster_id")
        self.start_id = _identifier(start_id, "registry_start_id")
        self._owned: dict[str, BackendIdentity] = {}

    def register(self, identity: BackendIdentity) -> None:
        if identity.cluster_id != self.cluster_id or identity.start_id != self.start_id or \
                identity.session_id in self._owned:
            raise ControlError("ownership_identity_mismatch")
        self._owned[identity.session_id] = identity

    def exact_target(self, observed: BackendIdentity) -> int | None:
        owned = self._owned.get(observed.session_id)
        return observed.pid if owned is not None and owned == observed else None

    def retirement(self, observed_cluster_id: str, observed_start_id: str,
                   active: list[BackendIdentity], observer_authorized: bool) -> dict[str, Any]:
        if not observer_authorized or observed_cluster_id != self.cluster_id or \
                observed_start_id != self.start_id:
            return self._result(False, "UNKNOWN", "observer_or_start_identity_unverified")
        if not self._owned:
            return self._result(False, "UNKNOWN", "no_owned_backend_registered")
        if any(identity.cluster_id != self.cluster_id or identity.start_id != self.start_id
               for identity in active):
            return self._result(False, "UNKNOWN", "active_identity_scope_mismatch")
        active_keys = {(identity.pid, identity.backend_start) for identity in active}
        absent = all((identity.pid, identity.backend_start) not in active_keys
                     for identity in self._owned.values())
        return self._result(absent, "CLEAN" if absent else "UNKNOWN",
                            "exact_backends_absent" if absent else "owned_backend_present")

    def _result(self, verified: bool, exit_state: str, reason: str) -> dict[str, Any]:
        return {"schema_version": 1, "document_type": "retirement_observation",
                "cluster_id": self.cluster_id, "start_id": self.start_id,
                "owned_sessions": sorted(self._owned),
                "backend_absence_verified": verified, "exit_state": exit_state,
                "reason": reason}


class StopController:
    """Central admission owner. The first stop reason is durable and irreversible."""

    def __init__(self, journal: SequenceJournal, policy: ResourcePolicy,
                 monotonic_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._journal = journal
        self._policy = policy
        self._clock = monotonic_ns
        self._lock = threading.RLock()
        self._stopped = False
        self._reason: str | None = None
        self._latest: ResourceSample | None = None
        self._inflight: set[int] = set()
        self._signal_event = threading.Event()
        self._pending_signal_reason: str | None = None
        self._signal_worker: threading.Thread | None = None

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def accept_sample(self, sample: ResourceSample) -> None:
        with self._lock:
            self._materialize_pending_signal_locked()
            if self._stopped:
                raise DispatchStopped("dispatch_stopped")
            valid_identity = (sample.scope_sha256 == self._policy.scope_sha256 and
                              sample.feed_version == self._policy.feed_version and
                              sample.source_id == self._policy.source_id and
                              sample.units == self._policy.units)
            valid_sequence = (self._latest is None or
                              (sample.sequence > self._latest.sequence and
                               sample.monotonic_ns >= self._latest.monotonic_ns and
                               sample.values["cpu_milliseconds"] >=
                               self._latest.values["cpu_milliseconds"]))
            now = self._clock()
            valid_time = sample.monotonic_ns <= now
            if not valid_identity or not valid_sequence or not valid_time:
                self._latch_locked("RESOURCE_FEED_INVALID")
                raise DispatchStopped("dispatch_stopped")
            age_ms = (now - sample.monotonic_ns) // 1_000_000
            if age_ms > self._policy.max_age_ms:
                self._latch_locked("RESOURCE_FEED_STALE")
                raise DispatchStopped("dispatch_stopped")
            self._append_or_stop("resource", "COMPLETE", {"event": "resource_sample",
                "policy_id": self._policy.policy_id,
                "scope_sha256": self._policy.scope_sha256,
                "feed_version": self._policy.feed_version,
                "source_id": sample.source_id, "sample_sequence": sample.sequence,
                "sample_age_ms": age_ms, "values": sample.values, "units": sample.units})
            self._latest = sample
            if any(sample.values[name] > limit for name, limit in self._policy.limits.items()):
                self._latch_locked("RESOURCE_LIMIT")
                raise DispatchStopped("dispatch_stopped")

    @contextlib.contextmanager
    def admission(self) -> Iterator[None]:
        with self._lock:
            self._materialize_pending_signal_locked()
            if self._stopped:
                raise DispatchStopped("dispatch_stopped")
            if self._latest is None:
                self._latch_locked("RESOURCE_FEED_MISSING")
                raise DispatchStopped("dispatch_stopped")
            age_ms = (self._clock() - self._latest.monotonic_ns) // 1_000_000
            if age_ms > self._policy.max_age_ms:
                self._latch_locked("RESOURCE_FEED_STALE")
                raise DispatchStopped("dispatch_stopped")
            if len(self._inflight) >= self._policy.limits["inflight_requests"]:
                self._latch_locked("RESOURCE_LIMIT")
                raise DispatchStopped("dispatch_stopped")
            yield

    def intent_started(self, sequence: int) -> None:
        with self._lock:
            self._inflight.add(sequence)

    def intent_finished(self, sequence: int) -> None:
        with self._lock:
            self._inflight.discard(sequence)

    def stop(self, reason: str) -> bool:
        with self._lock:
            if reason not in STOP_REASONS:
                raise ControlError("invalid_stop_reason")
            if self._stopped:
                return False
            self._latch_locked(reason)
            return True

    def journal_failed(self) -> None:
        """Latch an in-memory hard stop when durability itself is unavailable."""
        with self._lock:
            self._stopped = True
            self._reason = "JOURNAL_FAILURE"

    def check_feed(self) -> None:
        with self._lock:
            self._materialize_pending_signal_locked()
            if self._stopped:
                return
            if self._latest is None:
                self._latch_locked("RESOURCE_FEED_MISSING")
            elif (self._clock() - self._latest.monotonic_ns) // 1_000_000 > self._policy.max_age_ms:
                self._latch_locked("RESOURCE_FEED_STALE")

    def _append_or_stop(self, phase: str, state: str, payload: dict[str, Any]) -> None:
        try:
            self._journal.append(phase, state, payload)
        except Exception as exc:
            self._stopped = True
            self._reason = "JOURNAL_FAILURE"
            raise ControlError("journal_failure") from exc

    def _latch_locked(self, reason: str) -> None:
        if self._stopped:
            return
        payload = {"event": "stop_latched", "reason": reason,
                   "inflight_intent_sequences": sorted(self._inflight)}
        try:
            self._journal.append("control", "INCOMPLETE", payload)
        except Exception as exc:
            self._reason = "JOURNAL_FAILURE"
            self._stopped = True
            raise ControlError("journal_failure") from exc
        self._reason = reason
        self._stopped = True

    def _materialize_pending_signal_locked(self) -> None:
        if not self._stopped and self._pending_signal_reason is not None:
            self._latch_locked(self._pending_signal_reason)

    def install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        if self._signal_worker is None:
            self._signal_worker = threading.Thread(target=self._persist_signal_stop,
                name="pgqi-signal-stop", daemon=True)
            self._signal_worker.start()
        for number, reason in ((signal.SIGINT, "SIGNAL_INT"), (signal.SIGTERM, "SIGNAL_TERM")):
            previous[number] = signal.getsignal(number)
            signal.signal(number, lambda _signum, _frame, reason=reason:
                          self._queue_signal_stop(reason))
        return previous

    def _queue_signal_stop(self, reason: str) -> None:
        if self._pending_signal_reason is None:
            self._pending_signal_reason = reason
            self._signal_event.set()

    def _persist_signal_stop(self) -> None:
        self._signal_event.wait()
        reason = self._pending_signal_reason
        if reason is not None:
            try:
                self.stop(reason)
            except ControlError:
                pass

    def restore_signal_handlers(self, previous: dict[int, Any]) -> None:
        for number, handler in previous.items():
            signal.signal(number, handler)
        self._signal_event.set()
        if self._signal_worker is not None:
            self._signal_worker.join(1)


class ResourceFeedMonitor:
    """Poll the mandatory local feed age within its declared stop budget."""

    def __init__(self, controller: StopController, policy: ResourcePolicy) -> None:
        self._controller = controller
        self._period = min(policy.interval_ms, policy.stop_overshoot_ms) / 1000.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pgqi-resource-feed-monitor",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(max(1.0, self._period * 2))
        if self._thread.is_alive():
            raise ControlError("resource_feed_monitor_shutdown")

    def _run(self) -> None:
        while not self._stop.wait(self._period):
            try:
                self._controller.check_feed()
            except ControlError:
                # The controller already latched JOURNAL_FAILURE in memory.
                return
            if self._controller.stopped:
                return


class PilotControlRuntime:
    """Compose sampler, feed-age monitor and controller for an audited pilot."""

    def __init__(self, journal: SequenceJournal, policy: ResourcePolicy,
                 evidence_root: Path) -> None:
        self.controller = StopController(journal, policy)
        self._feed = LocalProcResourceFeed(policy)
        self._feed_monitor = ResourceFeedMonitor(self.controller, policy)
        self._evidence_root = evidence_root
        self._period = policy.interval_ms / 1000.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pgqi-resource-sampler",
                                        daemon=True)
        self._previous_signals: dict[int, Any] | None = None

    def start(self) -> None:
        self.controller.accept_sample(self._feed.sample(
            self._evidence_root, self.controller.inflight_count))
        self._previous_signals = self.controller.install_signal_handlers()
        self._feed_monitor.start(); self._thread.start()

    def close(self) -> None:
        self._stop.set(); self._thread.join(max(1.0, self._period * 2))
        if self._thread.is_alive():
            raise ControlError("resource_sampler_shutdown")
        self._feed_monitor.close()
        if self._previous_signals is not None:
            self.controller.restore_signal_handlers(self._previous_signals)
            self._previous_signals = None

    def _run(self) -> None:
        while not self._stop.wait(self._period):
            try:
                self.controller.accept_sample(self._feed.sample(
                    self._evidence_root, self.controller.inflight_count))
            except (ControlError, DispatchStopped):
                return


def verify_journal_prefix(path: Path) -> dict[str, Any]:
    """Recover a durable prefix; interruption can never become PASS."""
    previous = "0" * 64
    expected = 1
    records = 0
    try:
        with path.open("rb") as stream:
            while True:
                raw = stream.readline(65537)
                if not raw:
                    break
                if len(raw) > 65536:
                    return {"result": "INCOMPLETE", "reason": "oversize_journal_record",
                            "records": records}
                if not raw.endswith(b"\n"):
                    return {"result": "INCOMPLETE", "reason": "torn_journal", "records": records}
                try:
                    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                        value: dict[str, Any] = {}
                        for key, item in pairs:
                            if key in value:
                                raise ValueError("duplicate")
                            value[key] = item
                        return value
                    record = json.loads(raw, object_pairs_hook=no_duplicates,
                        parse_constant=lambda _token: (_ for _ in ()).throw(ValueError("nonfinite")))
                    if not isinstance(record, dict):
                        raise ValueError("root")
                except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
                    return {"result": "INCOMPLETE", "reason": "invalid_journal", "records": records}
                digest = record.pop("record_sha256", None)
                try:
                    calculated = hashlib.sha256(json.dumps(record, sort_keys=True,
                        separators=(",", ":"), ensure_ascii=True,
                        allow_nan=False).encode("ascii")).hexdigest()
                except (TypeError, ValueError, RecursionError, UnicodeError):
                    return {"result": "INCOMPLETE", "reason": "invalid_journal", "records": records}
                if (record.get("sequence") != expected or record.get("previous_sha256") != previous or
                        not isinstance(digest, str) or calculated != digest):
                    return {"result": "INCOMPLETE", "reason": "invalid_journal", "records": records}
                previous = digest; expected += 1; records += 1
    except OSError:
        return {"result": "INCOMPLETE", "reason": "journal_unavailable", "records": records}
    return {"result": "INCOMPLETE", "reason": "recovered_prefix_requires_retirement_proof",
            "records": records, "last_sha256": previous}
