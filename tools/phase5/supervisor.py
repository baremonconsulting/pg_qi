#!/usr/bin/env python3
"""Bounded process supervisor for the PGQI-5 protocol spike."""

from __future__ import annotations

import argparse
import json
import os
import resource
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--driver", required=True, type=Path)
    p.add_argument("--run-root", required=True, type=Path)
    p.add_argument("--conninfo", required=True)
    p.add_argument("--sql-file", required=True, type=Path)
    p.add_argument("--spool", required=True, type=Path)
    p.add_argument("--snapshot", required=True, type=Path)
    p.add_argument("--protocol", required=True, choices=("extended", "simple"))
    p.add_argument("--session-mode", required=True,
                   choices=("disabled", "off", "lab_allowlist", "native_baseline"))
    p.add_argument("--prepared")
    p.add_argument("--deadline-ms", required=True, type=positive_int)
    p.add_argument("--cancel-grace-ms", required=True, type=positive_int)
    p.add_argument("--cancel-deadline-ms", required=True, type=positive_int)
    p.add_argument("--max-address-space-kib", required=True, type=positive_int)
    p.add_argument("--max-output-bytes", required=True, type=positive_int)
    for name in ("rows", "bytes", "columns", "field-bytes", "spool-bytes", "messages"):
        p.add_argument(f"--max-{name}", required=True, type=positive_int)
    return p


def child_limits(max_address_space_kib: int) -> None:
    os.umask(0o077)
    byte_limit = max_address_space_kib * 1024
    resource.setrlimit(resource.RLIMIT_AS, (byte_limit, byte_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main() -> int:
    args = parser().parse_args()
    run_root = args.run_root.resolve(strict=True)
    root_stat = args.run_root.lstat()
    if (args.run_root.is_symlink() or root_stat.st_uid != os.getuid() or
            (root_stat.st_mode & 0o777) != 0o700):
        raise SystemExit("run root must be a caller-owned real directory with mode 0700")
    outputs = (args.spool, args.snapshot)
    if len({p.resolve(strict=False) for p in outputs}) != len(outputs):
        raise SystemExit("output paths must be distinct")
    for output in outputs:
        if output.resolve(strict=False).parent != run_root or output.exists() or output.is_symlink():
            raise SystemExit("outputs must be new direct children of run root")
    if args.sql_file.resolve(strict=True) in {p.resolve(strict=False) for p in outputs}:
        raise SystemExit("SQL input cannot alias an output")
    command = [
        str(args.driver), "--conninfo", args.conninfo,
        "--sql-file", str(args.sql_file), "--spool", str(args.spool),
        "--snapshot", str(args.snapshot),
        "--protocol", args.protocol, "--deadline-ms", str(args.deadline_ms),
        "--cancel-deadline-ms", str(args.cancel_deadline_ms),
        "--session-mode", args.session_mode,
        "--max-rows", str(args.max_rows), "--max-bytes", str(args.max_bytes),
        "--max-columns", str(args.max_columns),
        "--max-field-bytes", str(args.max_field_bytes),
        "--max-spool-bytes", str(args.max_spool_bytes),
        "--max-messages", str(args.max_messages),
    ]
    if args.prepared:
        command += ["--prepared", args.prepared]

    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=lambda: child_limits(args.max_address_space_kib),
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    selector = selectors.DefaultSelector()
    streams = {proc.stdout: "stdout", proc.stderr: "stderr"}
    for stream, label in streams.items():
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    captured = bytearray()
    counts = {"stdout": 0, "stderr": 0}
    forced = None
    hard_deadline = time.monotonic() + (
        5 * args.deadline_ms + args.cancel_deadline_ms + args.cancel_grace_ms) / 1000
    while (selector.get_map() or proc.poll() is None) and time.monotonic() < hard_deadline:
        ready = selector.select(timeout=0.02) if selector.get_map() else ()
        for key, _ in ready:
            chunk = os.read(key.fileobj.fileno(), 4096)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            counts[key.data] += len(chunk)
            if key.data == "stdout" and len(captured) < args.max_output_bytes:
                captured.extend(chunk[:args.max_output_bytes - len(captured)])
            if counts[key.data] > args.max_output_bytes and proc.poll() is None:
                forced = "output_limit_sigkill"
                os.killpg(proc.pid, signal.SIGKILL)
        if proc.poll() is not None and not selector.get_map():
            break
    drain_deadline = bool(selector.get_map() and time.monotonic() >= hard_deadline)
    if proc.poll() is None:
        forced = "deadline_sigterm"
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=args.cancel_grace_ms / 1000)
        except subprocess.TimeoutExpired:
            forced = "deadline_sigkill"
            os.killpg(proc.pid, signal.SIGKILL)
    elif drain_deadline:
        forced = "output_drain_deadline"
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=args.cancel_grace_ms / 1000)
    for stream in streams:
        stream.close()
    selector.close()

    output_exceeded = counts["stdout"] > args.max_output_bytes or counts["stderr"] > args.max_output_bytes
    decode_error = False
    try:
        stdout = bytes(captured).decode("utf-8", "strict")
    except UnicodeDecodeError:
        stdout = ""
        decode_error = True

    lines = stdout.splitlines()
    terminal = {"terminal_state": "MISSING_TERMINAL", "reason": "no_driver_record"}
    states = {"COMPLETE", "FAILED", "LIMIT_EXCEEDED", "REFUSED"}
    reasons = {
        "ok", "invalid_arguments", "prepared_requires_extended", "sql_file",
        "spool_open", "connect_deadline_or_error", "session_setup", "send_error",
        "single_row_mode", "query_deadline", "connection_error", "max_messages",
        "max_columns", "max_field_bytes", "max_result_bytes", "max_rows",
        "max_spool_bytes", "spool_write", "not_single_row", "multiple_result_sets",
        "missing_result_completion", "result_metadata_changed", "server_error",
        "server_error_after_partial_stream", "unexpected_result_status",
        "snapshot_write", "spool_sync",
    }
    counters = ("rows", "bytes", "spool_bytes", "messages")
    if decode_error:
        terminal = {"terminal_state": "INVALID_DRIVER_OUTPUT",
                    "reason": "terminal_utf8"}
    elif len(lines) == 1:
        try:
            candidate = json.loads(lines[0])
            valid_counters = (isinstance(candidate, dict) and all(
                isinstance(candidate.get(key), int) and
                not isinstance(candidate.get(key), bool) and candidate[key] >= 0
                for key in counters))
            if (valid_counters and candidate.get("terminal_state") in states and
                    candidate.get("reason") in reasons and
                    isinstance(candidate.get("sqlstate"), str) and
                    len(candidate["sqlstate"]) in (0, 5) and
                    isinstance(candidate.get("cancel_attempted"), bool) and
                    isinstance(candidate.get("cancel_succeeded"), bool) and
                    isinstance(candidate.get("cancel_timed_out"), bool)):
                terminal = candidate
            else:
                terminal = {"terminal_state": "INVALID_DRIVER_OUTPUT",
                            "reason": "terminal_schema"}
        except json.JSONDecodeError:
            terminal = {"terminal_state": "INVALID_DRIVER_OUTPUT",
                        "reason": "terminal_json"}
    elif lines:
        terminal = {"terminal_state": "INVALID_DRIVER_OUTPUT",
                    "reason": "terminal_record_count"}
    if forced:
        terminal = {"terminal_state": "SUPERVISOR_TERMINATED", "reason": forced}
    if output_exceeded:
        terminal = {"terminal_state": "OUTPUT_LIMIT_EXCEEDED", "reason": "driver_output_limit"}
    peak_rss_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    report = {
        "schema_version": 1,
        "protocol": args.protocol,
        "prepared_lifecycle": bool(args.prepared),
        "driver_exit": proc.returncode,
        "supervisor_forced_action": forced,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "peak_rss_kib": peak_rss_kib,
        "address_space_limit_kib": args.max_address_space_kib,
        "terminal": terminal,
        "stdout_bytes": counts["stdout"],
        "stdout_captured_bytes": len(captured),
        "stderr_bytes": counts["stderr"],
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if terminal.get("terminal_state") == "COMPLETE" and proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
