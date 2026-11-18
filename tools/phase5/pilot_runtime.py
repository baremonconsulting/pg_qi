#!/usr/bin/env python3
"""Fail-closed local state and journal primitives for the PGQI-5 pilot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import stat
import struct
import subprocess
import selectors
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
TERMINAL = frozenset({"COMPLETE", "FAILED", "REFUSED", "INCOMPLETE",
                      "NOT_RUN", "UNAVAILABLE"})
PHASES = frozenset({"request", "planning", "execution", "snapshot",
                    "resource", "control", "retirement"})
FRAME_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
OP_EXECUTE = 1
OP_CLOSE = 2
OP_PREPARE = 3
OP_EXECUTE_PREPARED = 4
OP_DEALLOCATE = 5
OP_BEGIN = 6


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _exclusive_file(root: Path, name: str):
    if not IDENTIFIER.fullmatch(name.replace(".", "_")):
        raise ValueError("invalid output name")
    if root.resolve(strict=True) != root.absolute():
        raise ValueError("output root must not contain symbolic links")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(root_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("output root must be caller-owned mode 0700")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=root_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("output must be a singly-linked regular file")
        os.fsync(root_fd)
        return os.fdopen(fd, "xb", buffering=0), root_fd
    except Exception:
        os.close(root_fd)
        raise


class SequenceJournal:
    """Append-only hash-chained JSONL written by one central sequence owner."""

    def __init__(self, root: Path, name: str = "request.journal.jsonl",
                 max_records: int = 100000, max_record_bytes: int = 65536) -> None:
        self._stream, self._root_fd = _exclusive_file(root, name)
        self._sequence = 0
        self._previous = "0" * 64
        self._closed = False
        self._max_records = max_records
        self._max_record_bytes = max_record_bytes
        self._lock = threading.Lock()

    def append(self, phase: str, state: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._append_locked(phase, state, payload)

    def _append_locked(self, phase: str, state: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed or phase not in PHASES or state not in TERMINAL | {"STARTED"}:
            raise ValueError("invalid journal transition")
        if self._sequence >= self._max_records:
            raise ValueError("journal record limit reached")
        next_sequence = self._sequence + 1
        record = {"schema_version": 1, "sequence": next_sequence,
                  "phase": phase, "state": state, "previous_sha256": self._previous,
                  "payload": payload}
        digest = hashlib.sha256(_canonical(record)).hexdigest()
        record["record_sha256"] = digest
        data = _canonical(record) + b"\n"
        if len(data) > self._max_record_bytes:
            raise ValueError("journal record exceeds limit")
        try:
            if self._stream.write(data) != len(data):
                raise OSError("short journal write")
            os.fsync(self._stream.fileno())
        except Exception:
            self._closed = True
            try:
                self._stream.close()
            finally:
                os.close(self._root_fd)
            raise
        self._sequence = next_sequence
        self._previous = digest
        return record

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._stream.close()
                os.fsync(self._root_fd)
                os.close(self._root_fd)
                self._closed = True


@dataclass(frozen=True)
class PreparedPlan:
    name: str
    generation: int
    sql_sha256: str
    created_sequence: int
    replanned_sequence: int | None = None
    deallocated_sequence: int | None = None
    attributable: bool = False


class PreparedRegistry:
    """Tracks attribution without inferring server plan state."""

    def __init__(self) -> None:
        self._plans: dict[str, PreparedPlan] = {}
        self._generations: dict[str, int] = {}

    def prepare(self, name: str, sql: bytes, sequence: int) -> PreparedPlan:
        return self.prepare_digest(name, hashlib.sha256(sql).hexdigest(), sequence)

    def prepare_digest(self, name: str, sql_sha256: str, sequence: int) -> PreparedPlan:
        if not IDENTIFIER.fullmatch(name) or name in self._plans:
            raise ValueError("prepared name is invalid or already active")
        if not re.fullmatch(r"[0-9a-f]{64}", sql_sha256):
            raise ValueError("invalid SQL digest")
        generation = self._generations.get(name, 0) + 1
        plan = PreparedPlan(name, generation,
                            sql_sha256, sequence)
        self._plans[name] = plan
        self._generations[name] = generation
        return plan

    def next_generation(self, name: str) -> int:
        if name in self._plans:
            raise ValueError("prepared name is already active")
        return self._generations.get(name, 0) + 1

    def active(self, name: str) -> PreparedPlan:
        try:
            return self._plans[name]
        except KeyError as exc:
            raise ValueError("prepared plan is not active") from exc

    def deallocate(self, name: str, sequence: int) -> PreparedPlan:
        plan = self.active(name)
        retired = PreparedPlan(plan.name, plan.generation, plan.sql_sha256,
                               plan.created_sequence, plan.replanned_sequence, sequence,
                               plan.attributable)
        del self._plans[name]
        return retired

    def replan(self, name: str, sequence: int) -> PreparedPlan:
        plan = self.active(name)
        generation = plan.generation + 1
        updated = PreparedPlan(plan.name, generation, plan.sql_sha256,
                               plan.created_sequence, sequence, None, True)
        self._plans[name] = updated
        self._generations[name] = generation
        return updated

    def mark_unattributed(self, name: str) -> PreparedPlan:
        plan = self.active(name)
        updated = PreparedPlan(plan.name, plan.generation, plan.sql_sha256,
                               plan.created_sequence, plan.replanned_sequence,
                               plan.deallocated_sequence, False)
        self._plans[name] = updated
        return updated


def encode_frame(opcode: int, payload: dict[str, Any]) -> bytes:
    """Encode one bounded, versioned local protocol frame."""
    if opcode not in (OP_EXECUTE, OP_CLOSE, OP_PREPARE,
                      OP_EXECUTE_PREPARED, OP_DEALLOCATE, OP_BEGIN):
        raise ValueError("unsupported opcode")
    body = bytes((FRAME_VERSION, opcode)) + _canonical(payload)
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError("frame exceeds limit")
    return struct.pack(">I", len(body)) + body


def read_frame(stream) -> tuple[int, dict[str, Any]]:
    header = stream.read(4)
    if len(header) != 4:
        raise EOFError("missing frame header")
    length = struct.unpack(">I", header)[0]
    if length < 2 or length > MAX_FRAME_BYTES:
        raise ValueError("invalid frame length")
    body = stream.read(length)
    if len(body) != length:
        raise EOFError("truncated frame")
    if body[0] != FRAME_VERSION or body[1] not in (OP_EXECUTE, OP_CLOSE,
            OP_PREPARE, OP_EXECUTE_PREPARED, OP_DEALLOCATE, OP_BEGIN):
        raise ValueError("unsupported frame")
    decoded = json.loads(body[2:].decode("ascii"))
    if not isinstance(decoded, dict):
        raise ValueError("frame payload must be an object")
    return body[1], decoded


def read_frame_deadline(stream, timeout_seconds: int) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    def exact(count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("driver response deadline")
            selector = selectors.DefaultSelector()
            selector.register(stream, selectors.EVENT_READ)
            ready = selector.select(remaining)
            selector.close()
            if not ready:
                raise TimeoutError("driver response deadline")
            chunk = os.read(stream.fileno(), count - len(data))
            if not chunk:
                raise EOFError("truncated frame")
            data.extend(chunk)
        return bytes(data)
    header = exact(4)
    length = struct.unpack(">I", header)[0]
    if length < 2 or length > MAX_FRAME_BYTES:
        raise ValueError("invalid frame length")
    body = exact(length)
    if body[0] != FRAME_VERSION or body[1] not in (OP_EXECUTE, OP_CLOSE,
            OP_PREPARE, OP_EXECUTE_PREPARED, OP_DEALLOCATE, OP_BEGIN):
        raise ValueError("unsupported frame")
    try:
        decoded = json.loads(body[2:].decode("ascii"))
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid frame JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("frame payload must be an object")
    return body[1], decoded


class AuditedSession:
    """Single child/session dispatcher; deliberately has no retry or reconnect."""

    def __init__(self, driver: Path, journal: SequenceJournal,
                 fixed_args: list[str], approved_cases: dict[str, str],
                 expected_driver_sha256: str,
                 admission_controller: Any,
                 response_timeout_seconds: int = 30,
                 max_address_space_kib: int = 262144) -> None:
        driver_fd = os.open(driver, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(driver_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or
                info.st_mode & 0o022 or not re.fullmatch(r"[0-9a-f]{64}", expected_driver_sha256)):
            os.close(driver_fd); raise ValueError("audited driver identity is unsafe")
        digest = hashlib.sha256()
        while chunk := os.read(driver_fd, 65536):
            digest.update(chunk)
        if digest.hexdigest() != expected_driver_sha256:
            os.close(driver_fd); raise ValueError("audited driver digest mismatch")
        self._journal = journal
        self._approved_cases = dict(approved_cases)
        self._response_timeout = response_timeout_seconds
        self._registry = PreparedRegistry()
        self._closed = False
        if admission_controller is None:
            os.close(driver_fd)
            raise ValueError("admission controller is required")
        self._admission = admission_controller
        child_args = list(fixed_args)
        inherited = [driver_fd]
        conninfo_read = None
        if "--conninfo" in child_args:
            index = child_args.index("--conninfo")
            if index + 1 >= len(child_args):
                os.close(driver_fd); raise ValueError("missing conninfo")
            conninfo = child_args[index + 1].encode("utf-8")
            if not conninfo or len(conninfo) > 8192 or b"\0" in conninfo:
                os.close(driver_fd); raise ValueError("invalid conninfo")
            conninfo_read, conninfo_write = os.pipe()
            os.write(conninfo_write, conninfo)
            os.close(conninfo_write)
            child_args[index:index + 2] = ["--conninfo-fd", str(conninfo_read)]
            inherited.append(conninfo_read)
        def child_boundary() -> None:
            os.umask(0o077)
            limit = max_address_space_kib * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            try:
                import ctypes
                ctypes.CDLL(None).prctl(1, 15, 0, 0, 0)
            except Exception:
                pass
        self._proc = subprocess.Popen(
            [f"/proc/self/fd/{driver_fd}", *child_args], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
            pass_fds=tuple(inherited), preexec_fn=child_boundary,
            start_new_session=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        if conninfo_read is not None:
            os.close(conninfo_read)
        os.close(driver_fd)

    def dispatch(self, case_id: str) -> dict[str, Any]:
        case_digest = self._approved_cases.get(case_id, "")
        if self._closed or not IDENTIFIER.fullmatch(case_id) or not re.fullmatch(r"[0-9a-f]{64}", case_digest):
            raise ValueError("invalid or closed audited request")
        return self._exchange(OP_EXECUTE, {
            "case_id": case_id, "case_sha256": case_digest}, {
            "event": "dispatch_intent", "case_id": case_id,
            "case_sha256": case_digest})

    def prepare(self, prepared: str, case_id: str) -> dict[str, Any]:
        digest = self._approved_cases.get(case_id, "")
        if (not IDENTIFIER.fullmatch(prepared) or not IDENTIFIER.fullmatch(case_id)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("invalid prepared request")
        generation = self._registry.next_generation(prepared)
        response = self._exchange(OP_PREPARE, {"prepared": prepared,
                "case_id": case_id, "case_sha256": digest,
                "generation": generation}, {"event": "prepare_intent",
                "prepared": prepared, "case_id": case_id,
                "generation": generation})
        if response["state"] == "COMPLETE":
            self._registry.prepare_digest(prepared, digest, response["sequence"])
        return response

    def execute_prepared(self, prepared: str) -> dict[str, Any]:
        plan = self._registry.active(prepared)
        response = self._exchange(OP_EXECUTE_PREPARED, {"prepared": prepared,
            "generation": plan.generation}, {"event": "execute_prepared_intent",
            "prepared": prepared, "generation": plan.generation,
            "planning_sequence": ((plan.replanned_sequence or plan.created_sequence)
                                  if plan.attributable else None)})
        if response["reason"] in ("planned", "planned_execution_error"):
            self._registry.replan(prepared, response["sequence"])
        elif response["reason"] in ("prepared_plan_activity_unattributed",
                                    "prepared_error_with_plan_activity"):
            self._registry.mark_unattributed(prepared)
        return response

    def deallocate(self, prepared: str) -> dict[str, Any]:
        plan = self._registry.active(prepared)
        response = self._exchange(OP_DEALLOCATE, {"prepared": prepared,
            "generation": plan.generation}, {"event": "deallocate_intent",
            "prepared": prepared, "generation": plan.generation})
        if response["state"] == "COMPLETE":
            self._registry.deallocate(prepared, response["sequence"])
        return response

    def begin(self) -> dict[str, Any]:
        return self._exchange(OP_BEGIN, {}, {"event": "begin_intent"})

    def _exchange(self, opcode_expected: int, payload: dict[str, Any],
                  intent_payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise ValueError("audited session is closed")
        guard = self._admission.admission()
        intent = None
        try:
            with guard:
                try:
                    intent = self._journal.append("request", "STARTED", intent_payload)
                except Exception:
                    self._closed = True
                    if self._admission is not None:
                        self._admission.journal_failed()
                    self._retire_child()
                    raise
                if self._admission is not None:
                    self._admission.intent_started(intent["sequence"])
                request = encode_frame(opcode_expected,
                    {**payload, "sequence": intent["sequence"]})
                assert self._proc.stdin is not None
                self._proc.stdin.write(request); self._proc.stdin.flush()
            assert self._proc.stdout is not None
            opcode, response = read_frame_deadline(self._proc.stdout,
                                                   self._response_timeout)
            if opcode != opcode_expected or response.get("sequence") != intent["sequence"]:
                raise ValueError("response correlation mismatch")
            required = {"sequence", "state", "planning_state", "execution_state",
                        "snapshot_state", "reason", "cancel_attempted",
                        "cancel_succeeded", "cancel_timed_out",
                        "request_started_monotonic_ns", "request_finished_monotonic_ns",
                        "request_duration_ns", "request_interval_state",
                        "request_interval_censored"}
            if set(response) != required or not all(isinstance(response[key], str)
                    for key in {"state", "planning_state", "execution_state",
                                "snapshot_state", "reason"}) or \
                    not all(isinstance(response[key], bool) for key in
                            {"cancel_attempted", "cancel_succeeded", "cancel_timed_out",
                             "request_interval_censored"}) or \
                    not all(type(response[key]) is int and response[key] >= 0 for key in
                            {"request_started_monotonic_ns",
                             "request_finished_monotonic_ns", "request_duration_ns"}) or \
                    response["state"] not in TERMINAL or \
                    response["planning_state"] not in TERMINAL or \
                    response["execution_state"] not in TERMINAL or \
                    response["snapshot_state"] not in TERMINAL or \
                    response["request_interval_state"] not in \
                            {"NOT_RUN", "COMPLETE", "ERROR", "CENSORED_TIMEOUT"}:
                raise ValueError("invalid driver terminal schema")
            started = response["request_started_monotonic_ns"]
            finished = response["request_finished_monotonic_ns"]
            duration = response["request_duration_ns"]
            interval_state = response["request_interval_state"]
            if ((interval_state == "NOT_RUN" and
                    ((started, finished, duration) != (0, 0, 0) or
                     response["request_interval_censored"])) or
                    (interval_state != "NOT_RUN" and
                     (not started or finished < started or duration != finished - started)) or
                    response["request_interval_censored"] !=
                        (interval_state == "CENSORED_TIMEOUT")):
                raise ValueError("invalid driver request interval")
        except BaseException as exc:
            if intent is None:
                raise
            self._closed = True
            try:
                self._journal.append("execution", "INCOMPLETE", {
                    "event": "session_lost_after_dispatch", "intent_sequence": intent["sequence"],
                    "error_family": type(exc).__name__})
            except Exception:
                if self._admission is not None:
                    self._admission.journal_failed()
            finally:
                if self._admission is not None:
                    self._admission.intent_finished(intent["sequence"])
                self._retire_child()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError("audited session lost; request will not be replayed") from exc
        try:
            for phase in ("planning", "execution", "snapshot"):
                self._journal.append(phase, response[f"{phase}_state"], {
                    "event": "driver_terminal", "intent_sequence": intent["sequence"],
                    "reason": response["reason"],
                    "cancel_attempted": response["cancel_attempted"],
                    "cancel_succeeded": response["cancel_succeeded"],
                    "cancel_timed_out": response["cancel_timed_out"]})
            self._journal.append("request", response["state"], {
                "event": "request_terminal", "intent_sequence": intent["sequence"],
                "reason": response["reason"],
                "cancel_attempted": response["cancel_attempted"],
                "cancel_succeeded": response["cancel_succeeded"],
                "cancel_timed_out": response["cancel_timed_out"],
                "request_interval": {
                    "started_monotonic_ns": response["request_started_monotonic_ns"],
                    "finished_monotonic_ns": response["request_finished_monotonic_ns"],
                    "duration_ns": response["request_duration_ns"],
                    "state": response["request_interval_state"],
                    "censored": response["request_interval_censored"]}})
        except BaseException:
            self._closed = True
            if self._admission is not None:
                self._admission.journal_failed()
            self._retire_child()
            raise
        finally:
            if self._admission is not None:
                self._admission.intent_finished(intent["sequence"])
        if response["state"] != "COMPLETE":
            self._closed = True
            self._retire_child()
        return response

    def _retire_child(self) -> None:
        if self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    self._proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (self._proc.stdin, self._proc.stdout):
            if stream is not None and not stream.closed:
                stream.close()

    def close(self) -> None:
        if self._closed:
            return
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(encode_frame(OP_CLOSE, {})); self._proc.stdin.flush()
        except BrokenPipeError:
            pass
        self._proc.stdin.close()
        try:
            self._proc.wait(timeout=5)
        finally:
            self._retire_child()
            self._closed = True
