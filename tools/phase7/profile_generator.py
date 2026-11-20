#!/usr/bin/env python3
"""Generate a canonical PQ7-10 model/resource profile."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

PG_DUCKDB_SHA = "8756099e267c47f0b86c24de4cf995327f1f4279"
DUCKDB_SHA = "d54cbbe3df17e8e5ce68312f483b4155836112aa"
MAX_VALIDITY_SECONDS = 90 * 24 * 60 * 60


def bounded(name: str, minimum: int, maximum: int):
    def convert(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}"
            )
        return parsed
    return convert


def lower_hex(name: str, digits: int):
    def convert(value: str) -> str:
        if len(value) != digits or any(c not in "0123456789abcdef" for c in value):
            raise argparse.ArgumentTypeError(
                f"{name} must be exactly {digits} lowercase hexadecimal digits"
            )
        return value
    return convert


def crc32c(data: bytes) -> int:
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
    return (~value) & 0xFFFFFFFF


def profile(args: argparse.Namespace) -> str:
    if args.expires_at <= args.issued_at or \
            args.expires_at - args.issued_at > MAX_VALIDITY_SECONDS:
        raise SystemExit("validity interval must be positive and at most 90 days")
    fields = [
        ("v", 1),
        ("gen", args.generation),
        ("pg", args.postgres_version_num),
        ("pgbuild", args.postgres_build),
        ("qibuild", args.pg_qi_build),
        ("pgduckdb", args.pg_duckdb_sha),
        ("duckdb", args.duckdb_sha),
        ("site", args.site_build),
        ("contract", 2),
        ("rule", 1),
        ("model", 1),
        ("certificate", 1),
        ("iat", args.issued_at),
        ("exp", args.expires_at),
        ("memory", args.memory_bytes),
        ("temp", args.temp_bytes),
        ("threads", args.threads),
        ("concurrency", args.concurrency),
        ("fixed_us", args.fixed_overhead_us),
        ("scan_ns_kb", args.scan_ns_per_kb),
        ("conv_ns_row", args.conversion_ns_per_row),
        ("margin_ppm", args.margin_ppm),
    ]
    body = ";".join(f"{key}={value}" for key, value in fields)
    return f"{body};crc={crc32c(body.encode('ascii')):08x}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation", type=bounded("generation", 1, 2**64 - 1), required=True)
    parser.add_argument("--postgres-version-num", type=bounded("PostgreSQL version", 1, 2**32 - 1), required=True)
    parser.add_argument("--postgres-build", type=lower_hex("PostgreSQL build", 64), required=True)
    parser.add_argument("--pg-qi-build", type=lower_hex("pg_qi build", 40), required=True)
    parser.add_argument("--site-build", type=lower_hex("site build", 64), required=True)
    parser.add_argument("--issued-at", type=bounded("issued-at", 0, 2**63 - 1), required=True)
    parser.add_argument("--expires-at", type=bounded("expires-at", 0, 2**63 - 1), required=True)
    parser.add_argument("--pg-duckdb-sha", type=lower_hex("pg_duckdb SHA", 40), default=PG_DUCKDB_SHA)
    parser.add_argument("--duckdb-sha", type=lower_hex("DuckDB SHA", 40), default=DUCKDB_SHA)
    parser.add_argument("--memory-bytes", type=bounded("memory bytes", 1, 1 << 50), default=1 << 30)
    parser.add_argument("--temp-bytes", type=bounded("temp bytes", 0, 1 << 50), default=1 << 30)
    parser.add_argument("--threads", type=bounded("threads", 1, 1024), default=4)
    parser.add_argument("--concurrency", type=bounded("concurrency", 1, 65535), default=2)
    parser.add_argument("--fixed-overhead-us", type=bounded("fixed overhead", 0, 10**12), default=1000)
    parser.add_argument("--scan-ns-per-kb", type=bounded("scan cost", 0, 10**12), default=100)
    parser.add_argument("--conversion-ns-per-row", type=bounded("conversion cost", 0, 10**12), default=10)
    parser.add_argument("--margin-ppm", type=bounded("margin", 1, 1000000), default=100000)
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    value = profile(args)
    if args.output is None:
        print(value)
        return
    destination = Path(args.output)
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    temporary.write_text(f"pg_qi.model_profile = '{value}'\n", encoding="ascii")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    temporary.replace(destination)
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


if __name__ == "__main__":
    main()
