#!/usr/bin/env python3
"""Fail-closed local state and journal primitives for the PGQI-5 pilot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import selectors
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
TERMINAL = frozenset({"COMPLETE", "FAILED", "REFUSED", "INCOMPLETE",
                      "NOT_RUN", "UNAVAILABLE"})
PHASES = frozenset({"request", "planning", "execution", "snapshot"})
FRAME_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
OP_EXECUTE = 1
OP_CLOSE = 2
OP_PREPARE = 3
OP_EXECUTE_PREPARED = 4
OP_DEALLOCATE = 5


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _exclusive_file(root: Path, name: str):
    if not IDENTIFIER.fullmatch(name.replace(".", "_")):
        raise ValueError("invalid output name")
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

    def __init__(self, root: Path, name: str = "request.journal.jsonl") -> None:
        self._stream, self._root_fd = _exclusive_file(root, name)
        self._sequence = 0
        self._previous = "0" * 64
        self._closed = False

    def append(self, phase: str, state: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed or phase not in PHASES or state not in TERMINAL | {"STARTED"}:
            raise ValueError("invalid journal transition")
        self._sequence += 1
        record = {"schema_version": 1, "sequence": self._sequence,
                  "phase": phase, "state": state, "previous_sha256": self._previous,
                  "payload": payload}
        digest = hashlib.sha256(_canonical(record)).hexdigest()
        record["record_sha256"] = digest
        data = _canonical(record) + b"\n"
        if self._stream.write(data) != len(data):
            raise OSError("short journal write")
        os.fsync(self._stream.fileno())
        self._previous = digest
        return record

    def close(self) -> None:
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

    def active(self, name: str) -> PreparedPlan:
        try:
            return self._plans[name]
        except KeyError as exc:
            raise ValueError("prepared plan is not active") from exc

    def deallocate(self, name: str, sequence: int) -> PreparedPlan:
        plan = self.active(name)
        retired = PreparedPlan(plan.name, plan.generation, plan.sql_sha256,
                               plan.created_sequence, plan.replanned_sequence, sequence)
        del self._plans[name]
        return retired

    def replan(self, name: str, sequence: int) -> PreparedPlan:
        plan = self.active(name)
        generation = plan.generation + 1
        updated = PreparedPlan(plan.name, generation, plan.sql_sha256,
                               plan.created_sequence, sequence, None)
        self._plans[name] = updated
        self._generations[name] = generation
        return updated


def encode_frame(opcode: int, payload: dict[str, Any]) -> bytes:
    """Encode one bounded, versioned local protocol frame."""
    if opcode not in (OP_EXECUTE, OP_CLOSE, OP_PREPARE,
                      OP_EXECUTE_PREPARED, OP_DEALLOCATE):
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
            OP_PREPARE, OP_EXECUTE_PREPARED, OP_DEALLOCATE):
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
            OP_PREPARE, OP_EXECUTE_PREPARED, OP_DEALLOCATE):
        raise ValueError("unsupported frame")
    decoded = json.loads(body[2:].decode("ascii"))
    if not isinstance(decoded, dict):
        raise ValueError("frame payload must be an object")
    return body[1], decoded


class AuditedSession:
    """Single child/session dispatcher; deliberately has no retry or reconnect."""

    def __init__(self, driver: Path, journal: SequenceJournal,
                 fixed_args: list[str], approved_cases: dict[str, str],
                 response_timeout_seconds: int = 30) -> None:
        if not driver.is_file() or driver.is_symlink():
            raise ValueError("audited driver must be a real file")
        self._journal = journal
        self._approved_cases = dict(approved_cases)
        self._response_timeout = response_timeout_seconds
        self._registry = PreparedRegistry()
        self._closed = False
        self._proc = subprocess.Popen(
            [str(driver), *fixed_args], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
            start_new_session=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})

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
        plan = self._registry.prepare_digest(prepared, digest, 0)
        try:
            response = self._exchange(OP_PREPARE, {"prepared": prepared,
                "case_id": case_id, "case_sha256": digest,
                "generation": plan.generation}, {"event": "prepare_intent",
                "prepared": prepared, "case_id": case_id,
                "generation": plan.generation})
            if response["state"] != "COMPLETE":
                self._registry.deallocate(prepared, 0)
            return response
        except Exception:
            self._registry.deallocate(prepared, 0)
            raise

    def execute_prepared(self, prepared: str) -> dict[str, Any]:
        plan = self._registry.active(prepared)
        response = self._exchange(OP_EXECUTE_PREPARED, {"prepared": prepared,
            "generation": plan.generation}, {"event": "execute_prepared_intent",
            "prepared": prepared, "generation": plan.generation})
        if response["state"] == "COMPLETE" and response["reason"] == "replanned":
            self._registry.replan(prepared, 0)
        return response

    def deallocate(self, prepared: str) -> dict[str, Any]:
        plan = self._registry.active(prepared)
        response = self._exchange(OP_DEALLOCATE, {"prepared": prepared,
            "generation": plan.generation}, {"event": "deallocate_intent",
            "prepared": prepared, "generation": plan.generation})
        if response["state"] == "COMPLETE":
            self._registry.deallocate(prepared, 0)
        return response

    def _exchange(self, opcode_expected: int, payload: dict[str, Any],
                  intent_payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise ValueError("audited session is closed")
        intent = self._journal.append("request", "STARTED", intent_payload)
        request = encode_frame(opcode_expected,
            {**payload, "sequence": intent["sequence"]})
        try:
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._proc.stdin.write(request); self._proc.stdin.flush()
            opcode, response = read_frame_deadline(self._proc.stdout,
                                                   self._response_timeout)
            if opcode != opcode_expected or response.get("sequence") != intent["sequence"]:
                raise ValueError("response correlation mismatch")
            required = {"sequence", "state", "planning_state", "execution_state",
                        "snapshot_state", "reason"}
            if set(response) != required or not all(isinstance(response[key], str)
                    for key in required - {"sequence"}) or response["state"] not in TERMINAL or \
                    response["planning_state"] not in TERMINAL or \
                    response["execution_state"] not in TERMINAL or \
                    response["snapshot_state"] not in TERMINAL:
                raise ValueError("invalid driver terminal schema")
        except (BrokenPipeError, EOFError, OSError, TimeoutError, ValueError) as exc:
            self._closed = True
            self._journal.append("execution", "INCOMPLETE", {
                "event": "session_lost_after_dispatch", "intent_sequence": intent["sequence"],
                "error_family": type(exc).__name__})
            self._retire_child()
            raise RuntimeError("audited session lost; request will not be replayed") from exc
        for phase in ("planning", "execution", "snapshot"):
            self._journal.append(phase, response[f"{phase}_state"], {
                "event": "driver_terminal", "intent_sequence": intent["sequence"],
                "reason": response["reason"]})
        return response

    def _retire_child(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill(); self._proc.wait(timeout=1)
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
        self._proc.stdin.close(); self._proc.wait(timeout=5)
        self._retire_child()
        self._closed = True
