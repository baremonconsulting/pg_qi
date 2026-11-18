#!/usr/bin/env python3
import json
import struct
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

def read_exact(count):
    value = sys.stdin.buffer.read(count)
    if len(value) != count: raise SystemExit(1)
    return value

while True:
    header = sys.stdin.buffer.read(4)
    if not header: break
    body = read_exact(struct.unpack(">I", header)[0])
    opcode = body[1]
    payload = json.loads(body[2:])
    if opcode == 2: break
    if mode == "disconnect": raise SystemExit(9)
    timed = opcode in (1, 4)
    response = {"sequence": payload["sequence"], "state": "COMPLETE",
        "planning_state": "COMPLETE", "execution_state": "COMPLETE",
        "snapshot_state": "COMPLETE", "reason": "ok",
        "cancel_attempted": False, "cancel_succeeded": False,
        "cancel_timed_out": False,
        "request_started_monotonic_ns": 100 if timed else 0,
        "request_finished_monotonic_ns": 200 if timed else 0,
        "request_duration_ns": 100 if timed else 0,
        "request_interval_state": "COMPLETE" if timed else "NOT_RUN",
        "request_interval_censored": False}
    if mode == "mismatch": response["sequence"] += 1
    encoded = bytes((1, opcode)) + json.dumps(response, sort_keys=True,
        separators=(",", ":")).encode("ascii")
    sys.stdout.buffer.write(struct.pack(">I", len(encoded)) + encoded)
    sys.stdout.buffer.flush()
