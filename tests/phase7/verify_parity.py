#!/usr/bin/env python3
"""Strict receipt oracle for PQ-42 off/shadow parity evidence."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path


EXPECTED_SQLSTATES = {
    "runtime_division": "22012",
    "planning_constant": "22012",
    "runtime_timeout": "57014",
    "savepoint_error": "22012",
    "recovery": "00000",
    "plpgsql_recovery": "00000",
}

CASE_EXPECTED = {
    "ORDERED": ("unknown", "unsupported_shape", 1),
    "MULTISET": ("unknown", "unsupported_shape", 1),
    "AGGREGATE": ("unknown", "unsupported_shape", 1),
    "DUPLICATES_NULLS": ("unknown", "unsupported_shape", 1),
    "COUNT": ("bounded", "ok", 1),
    "PREPARED_CUSTOM": ("unknown", "unsupported_shape", 1),
    "PREPARED_CUSTOM_REUSE": ("unknown", "unsupported_shape", 1),
    "PREPARED_GENERIC": ("unknown", "unsupported_shape", 1),
    # A forced generic plan is planned once; reuse must retain that proved
    # decision rather than inventing a new planning observation.
    "PREPARED_GENERIC_REUSE": ("unknown", "unsupported_shape", 0),
}


def _lines(path: str | Path) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines()


def _drop_blocks(
    lines: list[str], markers: tuple[str, ...], following: int, *, exact: bool = False
) -> list[str]:
    result: list[str] = []
    skip = 0
    for line in lines:
        if skip:
            skip -= 1
        elif (line in markers if exact else line.startswith(markers)):
            skip = following
        else:
            result.append(line)
    return result


def verify_results(off_path: str | Path, shadow_path: str | Path) -> None:
    off, shadow = _lines(off_path), _lines(shadow_path)

    def multiset(lines: list[str]) -> Counter[str]:
        begin, end = lines.index("MULTISET_BEGIN"), lines.index("MULTISET_END")
        return Counter(lines[begin + 1 : end])

    if multiset(off) != multiset(shadow):
        raise ValueError("unordered result multiset differs")

    def normalized(lines: list[str]) -> list[str]:
        lines = _drop_blocks(lines, ("ACTUAL_MODE",), 3, exact=True)
        lines = _drop_blocks(lines, ("V2_BEFORE", "V2_AFTER"), 3, exact=True)
        lines = _drop_blocks(lines, ("V2_BEFORE_CASE_", "V2_AFTER_CASE_"), 3)
        begin, end = lines.index("MULTISET_BEGIN"), lines.index("MULTISET_END")
        return lines[:begin] + lines[end + 1 :]

    if normalized(off) != normalized(shadow):
        raise ValueError("ordered parity output differs")


def verify_errors(off_path: str | Path, shadow_path: str | Path) -> None:
    off, shadow = _lines(off_path), _lines(shadow_path)
    if _drop_blocks(off, ("ACTUAL_MODE",), 1, exact=True) != _drop_blocks(
        shadow, ("ACTUAL_MODE",), 1, exact=True
    ):
        raise ValueError("error receipt differs")
    actual: dict[str, str] = {}
    for line in off:
        fields = line.split()
        if len(fields) == 2 and fields[0] in EXPECTED_SQLSTATES:
            actual[fields[0]] = fields[1]
    if actual != EXPECTED_SQLSTATES:
        raise ValueError(f"SQLSTATE receipt differs: {actual!r}")
    if "COMMIT" not in off:
        raise ValueError("savepoint recovery did not commit")


def verify_case_snapshots(lines: list[str]) -> None:
    import sys

    tools = Path(__file__).resolve().parents[2] / "tools/phase7"
    sys.path.insert(0, str(tools))
    from decision_snapshot import parse_v2

    def snapshot(marker: str) -> dict[str, str]:
        start = lines.index(marker)
        encoded = next(
            line for line in lines[start + 1 : start + 5] if line.startswith("v=2;")
        )
        return parse_v2(encoded)

    for case, (p0, reason, delta) in CASE_EXPECTED.items():
        before = snapshot("V2_BEFORE_CASE_" + case)
        after = snapshot("V2_AFTER_CASE_" + case)
        if after["s"] != "complete" or after["mode"] != "shadow_p0":
            raise ValueError(f"{case}: incomplete or wrong-mode snapshot")
        if (after["cid"], after["g"]) != (before["cid"], before["g"]):
            raise ValueError(f"{case}: snapshot identity changed")
        if int(after["e"]) != int(before["e"]) + delta:
            raise ValueError(f"{case}: unexpected decision-event transition")
        if (after["p0"], after["p0_reason"]) != (p0, reason):
            raise ValueError(f"{case}: unexpected observer facts")
        if delta == 0 and after != before:
            raise ValueError(f"{case}: retained snapshot changed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("results", "errors"))
    parser.add_argument("off")
    parser.add_argument("shadow")
    args = parser.parse_args()
    (verify_results if args.kind == "results" else verify_errors)(args.off, args.shadow)


if __name__ == "__main__":
    main()
