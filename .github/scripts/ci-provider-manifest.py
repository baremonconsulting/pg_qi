#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tools.provider_pins import DUCKDB_SHA as EXPECTED_DUCKDB, PG_DUCKDB_SHA as EXPECTED_PG_DUCKDB

HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
EXPECTED = {
    "16": ("PostgreSQL 16.16", "c0534eb02de050ac1035ad7b3ad7c3f02980f153"),
    "17": ("PostgreSQL 17.12", "702b3dc86d35bcc08c8e23cffa3c4d9fe9d32c85"),
    "18": ("PostgreSQL 18.7", "f5c41f6681c910b3f69951bf6fb97c6d74605d25"),
    "19": ("PostgreSQL 19.0", "5c839a2a08b9b87a349be97371fac6c5a2873b10"),
}
FIELDS = {
    "schema_version", "postgresql_major", "postgresql_version",
    "postgresql_source_commit", "postgresql_configure",
        "postgresql_binary_sha256", "postgresql_source_binary_sha256", "postgresql_build_id",
        "pg_qi_source_commit", "pg_qi_binary_sha256",
        "pg_duckdb_source_commit", "pg_duckdb_binary_sha256", "pg_duckdb_source_binary_sha256",
        "duckdb_source_commit", "duckdb_binary_path", "duckdb_binary_sha256", "duckdb_source_binary_sha256",
    "production_testing_flag",
}


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def postgres_build_id(document: dict) -> str:
    payload = (
        f"major={document['postgresql_major']}\n"
        f"version={document['postgresql_version']}\n"
        f"source_commit={document['postgresql_source_commit']}\n"
        f"configure={document['postgresql_configure']}\n"
        f"postgres_sha256={document['postgresql_binary_sha256']}\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate(document: dict) -> None:
    if set(document) != FIELDS:
        raise ValueError(f"manifest fields differ: missing={FIELDS-set(document)}, extra={set(document)-FIELDS}")
    major = document["postgresql_major"]
    if major not in EXPECTED:
        raise ValueError("unsupported PostgreSQL major")
    version, source = EXPECTED[major]
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("invalid manifest schema version")
    if type(document["production_testing_flag"]) is not int or document["production_testing_flag"] != 0:
        raise ValueError("manifest does not describe a production build")
    if not isinstance(document["postgresql_configure"], str) or not document["postgresql_configure"]:
        raise ValueError("invalid PostgreSQL configure identity")
    if not isinstance(document["duckdb_binary_path"], str) or not pathlib.Path(document["duckdb_binary_path"]).is_absolute():
        raise ValueError("invalid DuckDB runtime-library path")
    if document["postgresql_version"] != version:
        raise ValueError("unexpected manifest schema or PostgreSQL version")
    if document["postgresql_source_commit"] != source:
        raise ValueError("PostgreSQL source pin mismatch")
    for field in ("postgresql_source_commit", "pg_qi_source_commit", "pg_duckdb_source_commit", "duckdb_source_commit"):
        if not isinstance(document[field], str) or not HEX40.fullmatch(document[field]):
            raise ValueError(f"invalid 40-hex identity: {field}")
    for field in ("postgresql_binary_sha256", "postgresql_source_binary_sha256", "postgresql_build_id", "pg_qi_binary_sha256", "pg_duckdb_binary_sha256", "pg_duckdb_source_binary_sha256", "duckdb_binary_sha256", "duckdb_source_binary_sha256"):
        if not isinstance(document[field], str) or not HEX64.fullmatch(document[field]):
            raise ValueError(f"invalid 64-hex identity: {field}")
    if document["postgresql_binary_sha256"] != document["postgresql_source_binary_sha256"]:
        raise ValueError("installed PostgreSQL differs from its source build output")
    if document["pg_duckdb_binary_sha256"] != document["pg_duckdb_source_binary_sha256"]:
        raise ValueError("installed pg_duckdb differs from its source build output")
    if document["duckdb_binary_sha256"] != document["duckdb_source_binary_sha256"]:
        raise ValueError("installed DuckDB differs from its source build output")
    if document["postgresql_build_id"] != postgres_build_id(document):
        raise ValueError("PostgreSQL build ID is not derived from the provider")
    if document["postgresql_build_id"] != os.environ.get("PG_QI_POSTGRES_BUILD_ID", document["postgresql_build_id"]):
        raise ValueError("compile-time PostgreSQL build ID mismatch")
    if document["pg_qi_source_commit"] != os.environ.get("PG_QI_BUILD_ID", document["pg_qi_source_commit"]):
        raise ValueError("compile-time pg_qi build ID mismatch")
    if document["pg_duckdb_source_commit"] != os.environ.get("PG_DUCKDB_SHA", EXPECTED_PG_DUCKDB):
        raise ValueError("pg_duckdb source pin mismatch")
    if document["duckdb_source_commit"] != os.environ.get("DUCKDB_SHA", EXPECTED_DUCKDB):
        raise ValueError("DuckDB source pin mismatch")


def generate(major: str, output: pathlib.Path) -> None:
    pg_config = pathlib.Path(os.environ.get("PG_CONFIG", f"/usr/lib/postgresql/{major}/bin/pg_config"))
    bindir = pathlib.Path(command(str(pg_config), "--bindir"))
    libdir = pathlib.Path(command(str(pg_config), "--pkglibdir"))
    runner_temp = pathlib.Path(os.environ["RUNNER_TEMP"])
    pg_source = runner_temp / f"postgresql-{major}-source"
    pg_build = runner_temp / f"postgresql-{major}-build"
    pg_duckdb = runner_temp / f"pg_duckdb-{major}"
    repo = pathlib.Path(__file__).resolve().parents[2]
    pg_qi_source = command("git", "-C", str(repo), "rev-parse", "HEAD")
    pg_qi_binary = libdir / "pg_qi.so"
    pg_duckdb_binary = libdir / "pg_duckdb.so"
    duckdb_binary = None
    for line in command("ldd", str(pg_duckdb_binary)).splitlines():
        fields = line.strip().split()
        if fields and fields[0] == "libduckdb.so" and len(fields) >= 3 and fields[1] == "=>":
            duckdb_binary = pathlib.Path(fields[2]).resolve()
            break
    if duckdb_binary is None or not duckdb_binary.is_file():
        raise ValueError("pg_duckdb does not resolve a separate libduckdb.so")
    compiled = pg_qi_binary.read_bytes()
    for identity in (pg_qi_source, os.environ["PG_QI_POSTGRES_BUILD_ID"]):
        if identity.encode("ascii") not in compiled:
            raise ValueError(f"compiled pg_qi does not contain build identity {identity}")
    document = {
        "schema_version": 1,
        "postgresql_major": major,
        "postgresql_version": command(str(pg_config), "--version"),
        "postgresql_source_commit": command("git", "-C", str(pg_source), "rev-parse", "HEAD"),
        "postgresql_configure": command(str(pg_config), "--configure"),
        "postgresql_binary_sha256": sha256(bindir / "postgres"),
        "postgresql_source_binary_sha256": sha256(pg_build / "src/backend/postgres"),
        "postgresql_build_id": os.environ["PG_QI_POSTGRES_BUILD_ID"],
        "pg_qi_source_commit": pg_qi_source,
        "pg_qi_binary_sha256": sha256(pg_qi_binary),
        "pg_duckdb_source_commit": command("git", "-C", str(pg_duckdb), "rev-parse", "HEAD"),
        "pg_duckdb_binary_sha256": sha256(pg_duckdb_binary),
        "pg_duckdb_source_binary_sha256": sha256(pg_duckdb / "pg_duckdb.so"),
        "duckdb_source_commit": command("git", "-C", str(pg_duckdb / "third_party/duckdb"), "rev-parse", "HEAD"),
        "duckdb_binary_path": str(duckdb_binary),
        "duckdb_binary_sha256": sha256(duckdb_binary),
        "duckdb_source_binary_sha256": sha256(
            pg_duckdb / "third_party/duckdb/build/release/src/libduckdb.so"),
        "production_testing_flag": 0,
    }
    validate(document)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("generate")
    create.add_argument("major", choices=EXPECTED)
    create.add_argument("output", type=pathlib.Path)
    check = sub.add_parser("validate")
    check.add_argument("manifest", type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "generate":
            generate(args.major, args.output)
        else:
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError(f"duplicate JSON field: {key}")
                    value[key] = item
                return value
            validate(json.loads(args.manifest.read_text(encoding="utf-8"),
                                object_pairs_hook=unique_object))
    except (KeyError, OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as error:
        print(f"provider manifest error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
