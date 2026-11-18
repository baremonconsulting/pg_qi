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
    response = {"sequence": payload["sequence"], "state": "COMPLETE",
        "planning_state": "COMPLETE", "execution_state": "COMPLETE",
        "snapshot_state": "COMPLETE", "reason": "ok",
        "cancel_attempted": False, "cancel_succeeded": False,
        "cancel_timed_out": False}
    if mode == "mismatch": response["sequence"] += 1
    encoded = bytes((1, opcode)) + json.dumps(response, sort_keys=True,
        separators=(",", ":")).encode("ascii")
    sys.stdout.buffer.write(struct.pack(">I", len(encoded)) + encoded)
    sys.stdout.buffer.flush()
