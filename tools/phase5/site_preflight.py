#!/usr/bin/env python3
"""Fail-closed PGQI-5 release qualification and connection preflight policy."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

from contract_rules import ContractError, canonical_bytes

DIGEST = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DNS_NAME = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$" )
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$")

PHASE4C_CASES = (
    "eligible_literal", "policy_off", "extension_disabled", "parameter_custom",
    "parameter_generic", "prepared_reuse", "prepared_invalidation", "planning_error",
    "join_rejected", "cast_rejected", "function_rejected", "temporary_relation_rejected",
    "unlogged_relation_rejected", "partitioned_relation_rejected", "foreign_relation_rejected",
    "rls_ordinary_role_rejected", "ordinary_role_control_denied", "rollback_reconnect",
)
CORE_CI_GLOBAL = ("phase5_protocol", "phase5_contracts", "phase5_evidence")
CORE_CI_PER_MAJOR = (
    "compile_production", "compile_test", "pg_regress", "production_shadow", "outer_hook",
    "phase4c", "integration_s0_s11",
)
CORE_CI_SELECTED = (
    *(f"pg{major}:ci:baseline_s0_s11" for major in (16, 17, 18)),
    *(f"pg{major}:ci:differential_corpus" for major in (16, 19)),
    "pg17:ci:reentrancy_failpoints", "pg17:ci:assertion_concurrency",
    "pg17:ci:parallel_worker_isolation",
)
REQUIRED_REPLAY_CELLS = frozenset(
    [f"pg{major}:4c:{case}" for major in (16, 17, 18, 19) for case in PHASE4C_CASES]
    + [f"pg{major}:ci:{job}" for major in (16, 17, 18, 19) for job in CORE_CI_PER_MAJOR]
    + list(CORE_CI_SELECTED)
    + [f"global:ci:{job}" for job in CORE_CI_GLOBAL]
)

FIXED_PREFLIGHT_QUERIES = (
    ("routing_mode", "SHOW pg_qi.routing_mode"),
    ("duckdb_force", "SHOW duckdb.force_execution"),
    ("server_version_num", "SELECT pg_catalog.current_setting('server_version_num')"),
    ("database_identity", "SELECT pg_catalog.current_database(), (SELECT oid::text FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database())"),
    ("role_identity", "SELECT SESSION_USER::text, CURRENT_USER::text"),
    ("postmaster_start", "SELECT pg_catalog.to_char(pg_catalog.pg_postmaster_start_time() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"),
    ("cluster_identity", "SELECT system_identifier::text FROM pg_catalog.pg_control_system()"),
    ("backend_identity", "SELECT pid::text, pg_catalog.to_char(backend_start AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') FROM pg_catalog.pg_stat_activity WHERE pid = pg_catalog.pg_backend_pid()"),
    ("extensions", "SELECT extname, extversion FROM pg_catalog.pg_extension WHERE extname IN ('pg_duckdb','pg_qi') ORDER BY extname"),
    ("pg_qi_enabled", "SHOW pg_qi.enabled"),
)


class QualificationError(ContractError):
    """Invalid or unsafe qualification input."""


class QualificationIncomplete(QualificationError):
    """Required evidence has not been run or retained."""


class QualificationFailed(QualificationError):
    """A measured qualification cell failed."""


def _exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise QualificationError(f"{name} fields are not exact")
    return value


def _text(value: Any, name: str, pattern: re.Pattern[str] = IDENTIFIER) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise QualificationError(f"invalid {name}")
    return value


def _digest(value: Any, name: str) -> str:
    return _text(value, name, DIGEST)


def _integer(value: Any, name: str, low: int = 0, high: int | None = None) -> int:
    if type(value) is not int or value < low or (high is not None and value > high):
        raise QualificationError(f"invalid {name}")
    return value


def _boolean(value: Any, name: str, expected: bool | None = None) -> bool:
    if not isinstance(value, bool) or (expected is not None and value is not expected):
        raise QualificationError(f"invalid {name}")
    return value


def _safe_scalar(value: Any, name: str, pattern: re.Pattern[str] = IDENTIFIER) -> str:
    text = _text(value, name, pattern)
    if any(token in text for token in (",", "\n", "\r", "\x00", "://", "=")):
        raise QualificationError(f"ambiguous {name}")
    return text


def _absolute_file(value: Any, name: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value or
            any(token in value for token in (",", "\n", "\r", "=")) or ".." in Path(value).parts):
        raise QualificationError(f"invalid {name}")
    return value


def _identity_block(value: Any, name: str, fields: tuple[str, ...]) -> dict[str, Any]:
    item = _exact(value, set(fields), name)
    for field in fields:
        if field.endswith("_sha256"):
            _digest(item[field], f"{name}.{field}")
    return item


def _start_id(cluster_id: str, postmaster_start_time: str) -> str:
    return hashlib.sha256(canonical_bytes({"cluster_id": cluster_id,
                                           "postmaster_start_time": postmaster_start_time})).hexdigest()


def _validate_endpoint(value: Any) -> tuple[dict[str, Any], dict[str, str]]:
    if not isinstance(value, dict) or value.get("kind") not in {"tls_tcp", "private_socket"}:
        raise QualificationError("unknown endpoint kind")
    if value["kind"] == "tls_tcp":
        endpoint = _exact(value, {"kind", "host", "hostaddr", "port", "database", "role",
                                  "sslmode", "sslrootcert_path", "sslrootcert_sha256", "gssencmode",
                                  "credential_channel", "passfile_disabled"}, "TLS endpoint")
        host = _safe_scalar(endpoint["host"], "host", DNS_NAME)
        try:
            address = str(ipaddress.ip_address(endpoint["hostaddr"]))
        except (ValueError, TypeError) as exc:
            raise QualificationError("invalid hostaddr") from exc
        port = str(_integer(endpoint["port"], "port", 1, 65535))
        database = _safe_scalar(endpoint["database"], "database")
        role = _safe_scalar(endpoint["role"], "role")
        if endpoint["sslmode"] != "verify-full" or endpoint["gssencmode"] != "disable":
            raise QualificationError("TLS or GSS policy is not fail-closed")
        _digest(endpoint["sslrootcert_sha256"], "sslrootcert_sha256")
        rootcert = _absolute_file(endpoint["sslrootcert_path"], "sslrootcert_path")
        if endpoint["credential_channel"] != "anonymous_fd":
            raise QualificationError("TLS credentials require anonymous_fd")
        _boolean(endpoint["passfile_disabled"], "passfile_disabled", True)
        return endpoint, {"host": host, "hostaddr": address, "port": port, "dbname": database,
                          "user": role, "sslmode": "verify-full", "gssencmode": "disable",
                          "sslrootcert": rootcert, "sslcert": "", "sslkey": "",
                          "sslcrl": "", "sslcrldir": "", "passfile": "isolated_empty_file",
                          "connect_timeout": "10"}
    endpoint = _exact(value, {"kind", "socket_directory", "socket_owner", "socket_owner_uid", "socket_mode",
                              "port", "database", "role", "credential_channel"}, "socket endpoint")
    directory = _absolute_file(endpoint["socket_directory"], "socket_directory")
    _safe_scalar(endpoint["socket_owner"], "socket_owner")
    owner_uid = str(_integer(endpoint["socket_owner_uid"], "socket_owner_uid", 1, 2147483647))
    mode = _integer(endpoint["socket_mode"], "socket_mode", 0, 0o777)
    if mode & 0o022:
        raise QualificationError("socket directory permits group/world writes")
    if endpoint["credential_channel"] != "peer":
        raise QualificationError("private socket requires peer credential channel")
    return endpoint, {"host": directory, "socket_owner_uid": owner_uid,
                      "port": str(_integer(endpoint["port"], "port", 1, 65535)),
                      "dbname": _safe_scalar(endpoint["database"], "database"),
                      "user": _safe_scalar(endpoint["role"], "role"), "passfile": "isolated_empty_file",
                      "connect_timeout": "10"}


def _validate_replay(value: Any, source_commit: str, build_sha256: str) -> None:
    replay = _exact(value, {"source_commit", "build_sha256", "cells"}, "candidate replay")
    if replay["source_commit"] != source_commit or replay["build_sha256"] != build_sha256:
        raise QualificationError("candidate replay identity mismatch")
    if not isinstance(replay["cells"], list):
        raise QualificationError("replay cells must be an array")
    seen: set[str] = set()
    outcomes: dict[str, str] = {}
    for raw in replay["cells"]:
        cell = _exact(raw, {"cell_id", "result", "evidence_sha256"}, "replay cell")
        identity = _text(cell["cell_id"], "cell_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$"))
        if identity in seen or identity not in REQUIRED_REPLAY_CELLS:
            raise QualificationError("duplicate or unknown replay cell")
        seen.add(identity); _digest(cell["evidence_sha256"], "evidence_sha256")
        if cell["result"] not in {"PASS", "FAIL", "NOT_RUN"}:
            raise QualificationError("invalid replay result")
        outcomes[identity] = cell["result"]
    if any(result == "FAIL" for result in outcomes.values()):
        raise QualificationFailed("candidate replay failed")
    missing = REQUIRED_REPLAY_CELLS - seen
    if missing or any(result == "NOT_RUN" for result in outcomes.values()):
        raise QualificationIncomplete("candidate replay is incomplete")


def validate_dossier(value: Any, trusted_scope_sha256: str, trusted_dossier_sha256: str,
                     trusted_start_sha256: str) -> dict[str, Any]:
    _digest(trusted_scope_sha256, "trusted_scope_sha256")
    _digest(trusted_dossier_sha256, "trusted_dossier_sha256")
    _digest(trusted_start_sha256, "trusted_start_sha256")
    dossier = _exact(value, {"schema_version", "document_type", "qualification_id",
                              "scope_sha256", "source", "build", "installed", "start",
                              "endpoint", "expected_online", "candidate_replay"}, "dossier")
    if type(dossier["schema_version"]) is not int or dossier["schema_version"] != 1:
        raise QualificationError("schema_version must be integer 1")
    if dossier["document_type"] != "site_qualification":
        raise QualificationError("invalid document_type")
    if hashlib.sha256(canonical_bytes(dossier)).hexdigest() != trusted_dossier_sha256:
        raise QualificationError("dossier is not externally trusted")
    _text(dossier["qualification_id"], "qualification_id")
    if _digest(dossier["scope_sha256"], "scope_sha256") != trusted_scope_sha256:
        raise QualificationError("scope is not externally trusted")

    source = _exact(dossier["source"], {"commit", "dirty", "tree_sha256"}, "source")
    _text(source["commit"], "source.commit", COMMIT)
    _boolean(source["dirty"], "source.dirty", False); _digest(source["tree_sha256"], "tree_sha256")
    build = _exact(dossier["build"], {"production", "pg_qi_testing", "postgresql_major",
                                      "postgresql_source_sha256", "pg_duckdb_source_commit",
                                      "duckdb_source_commit", "duckdb_linkage",
                                      "server_build_sha256", "pg_qi_sha256", "pg_duckdb_sha256",
                                      "duckdb_sha256", "configuration_sha256", "toolchain_sha256"}, "build")
    _boolean(build["production"], "build.production", True)
    _boolean(build["pg_qi_testing"], "build.pg_qi_testing", False)
    _integer(build["postgresql_major"], "postgresql_major", 16, 19)
    _digest(build["postgresql_source_sha256"], "build.postgresql_source_sha256")
    _text(build["pg_duckdb_source_commit"], "build.pg_duckdb_source_commit", COMMIT)
    _text(build["duckdb_source_commit"], "build.duckdb_source_commit", COMMIT)
    if build["duckdb_linkage"] != "static_in_pg_duckdb":
        raise QualificationError("unsupported DuckDB linkage mode")
    for field in set(build) - {"production", "pg_qi_testing", "postgresql_major",
                               "pg_duckdb_source_commit", "duckdb_source_commit", "duckdb_linkage"}:
        if field != "postgresql_source_sha256":
            _digest(build[field], f"build.{field}")
    build_sha256 = hashlib.sha256(canonical_bytes(build)).hexdigest()

    installed = _exact(dossier["installed"], {"postgres_path", "postgres_sha256", "pg_qi_path",
                                               "pg_qi_sha256", "pg_duckdb_path", "pg_duckdb_sha256",
                                               "duckdb_sha256", "configuration_sha256"}, "installed")
    for field in ("postgres_path", "pg_qi_path", "pg_duckdb_path"):
        _absolute_file(installed[field], f"installed.{field}")
    for field in ("postgres_sha256", "pg_qi_sha256", "pg_duckdb_sha256", "duckdb_sha256",
                  "configuration_sha256"):
        _digest(installed[field], f"installed.{field}")
    if (installed["postgres_sha256"] != build["server_build_sha256"] or
            installed["pg_qi_sha256"] != build["pg_qi_sha256"] or
            installed["pg_duckdb_sha256"] != build["pg_duckdb_sha256"] or
            installed["duckdb_sha256"] != build["duckdb_sha256"] or
            installed["configuration_sha256"] != build["configuration_sha256"]):
        raise QualificationError("installed identity differs from build")

    start = _exact(dossier["start"], {"attestation_sha256", "install_event_id", "start_id",
                                      "install_sequence", "start_sequence", "cluster_id", "boot_id",
                                      "postmaster_start_time", "postgres_sha256", "pg_qi_mapped_sha256",
                                      "pg_duckdb_mapped_sha256", "duckdb_dependency_sha256",
                                      "configuration_sha256",
                                      "shared_preload_libraries", "hook_inventory"}, "start attestation")
    claimed_attestation = _digest(start["attestation_sha256"], "attestation_sha256")
    attested_fields = dict(start); del attested_fields["attestation_sha256"]
    computed_attestation = hashlib.sha256(canonical_bytes(attested_fields)).hexdigest()
    if claimed_attestation != trusted_start_sha256 or computed_attestation != trusted_start_sha256:
        raise QualificationError("start attestation is not externally trusted")
    for field in ("install_event_id", "start_id", "cluster_id", "boot_id"):
        _text(start[field], f"start.{field}")
    if _integer(start["start_sequence"], "start_sequence", 1) <= _integer(start["install_sequence"], "install_sequence", 0):
        raise QualificationError("postmaster start does not follow installation")
    _text(start["postmaster_start_time"], "postmaster_start_time", UTC)
    if start["start_id"] != _start_id(start["cluster_id"], start["postmaster_start_time"]):
        raise QualificationError("start_id is not derived from observed start identity")
    for field in ("postgres_sha256", "pg_qi_mapped_sha256", "pg_duckdb_mapped_sha256",
                  "duckdb_dependency_sha256", "configuration_sha256"):
        _digest(start[field], f"start.{field}")
    if (start["postgres_sha256"] != installed["postgres_sha256"] or
            start["pg_qi_mapped_sha256"] != installed["pg_qi_sha256"] or
            start["pg_duckdb_mapped_sha256"] != installed["pg_duckdb_sha256"] or
            start["duckdb_dependency_sha256"] != installed["duckdb_sha256"] or
            start["configuration_sha256"] != installed["configuration_sha256"]):
        raise QualificationError("start identity differs from installed identity")
    if start["shared_preload_libraries"] != ["pg_duckdb", "pg_qi"]:
        raise QualificationError("unexpected preload order")
    if start["hook_inventory"] != ["pg_duckdb", "pg_qi"]:
        raise QualificationError("unexpected hook inventory")

    endpoint, parameters = _validate_endpoint(dossier["endpoint"])
    expected = _exact(dossier["expected_online"], {"server_version_num", "database_name",
                                                    "database_oid", "session_user", "current_user",
                                                    "postmaster_start_time", "start_id", "cluster_id",
                                                    "pg_qi_version", "pg_duckdb_version", "routing_mode",
                                                    "duckdb_force_execution", "pg_qi_enabled"}, "expected online")
    for field in ("server_version_num", "database_oid"):
        _text(expected[field], f"expected_online.{field}", re.compile(r"^[0-9]+$"))
    for field in set(expected) - {"server_version_num", "database_oid"}:
        _safe_scalar(expected[field], f"expected_online.{field}", re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+ -]{0,127}$"))
    if expected["routing_mode"] != "off" or expected["duckdb_force_execution"] != "off" or expected["pg_qi_enabled"] != "on":
        raise QualificationError("online safe defaults are not expected")
    if (expected["postmaster_start_time"] != start["postmaster_start_time"] or
            expected["start_id"] != start["start_id"] or expected["cluster_id"] != start["cluster_id"] or
            expected["database_name"] != endpoint["database"] or
            expected["session_user"] != endpoint["role"] or expected["current_user"] != endpoint["role"]):
        raise QualificationError("online expectation differs from approved target")
    if int(expected["server_version_num"]) // 10000 != build["postgresql_major"]:
        raise QualificationError("online server major differs from approved build")
    _validate_replay(dossier["candidate_replay"], source["commit"], build_sha256)
    return {"dossier": dossier, "scope_sha256": trusted_scope_sha256,
            "dossier_sha256": trusted_dossier_sha256,
            "start_attestation_sha256": trusted_start_sha256, "libpq_parameters": parameters,
            "query_manifest": [{"check_id": key, "sql": sql} for key, sql in FIXED_PREFLIGHT_QUERIES]}


def reject_ambient_environment(environment: dict[str, str]) -> None:
    if any(name.upper().startswith("PG") for name in environment):
        raise QualificationError("ambient PostgreSQL environment is forbidden")
    for name in ("OPENSSL_CONF", "OPENSSL_MODULES", "LD_PRELOAD", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if name in environment:
            raise QualificationError("ambient client runtime environment is forbidden")


def evaluate_observation(intent: dict[str, Any], observation: Any,
                         connection_id: str) -> dict[str, Any]:
    _text(connection_id, "connection_id")
    expected = intent["dossier"]["expected_online"]
    observed = _exact(observation, set(expected) | {"connection_id", "queries_executed",
                                                     "activation_attempts"}, "online observation")
    if observed["connection_id"] != connection_id:
        raise QualificationError("connection identity mismatch")
    if observed["queries_executed"] != [key for key, _ in FIXED_PREFLIGHT_QUERIES]:
        raise QualificationError("online query manifest mismatch")
    if observed["activation_attempts"] != 0:
        raise QualificationError("preflight attempted activation")
    for field, value in expected.items():
        if observed[field] != value:
            raise QualificationFailed(f"online identity mismatch: {field}")
    return {"schema_version": 1, "document_type": "connection_preflight",
            "qualification_id": intent["dossier"]["qualification_id"],
            "scope_sha256": intent["scope_sha256"], "connection_id": connection_id,
            "start_id": expected["start_id"], "result": "PASS", "activation_attempts": 0,
            "query_manifest_sha256": hashlib.sha256(canonical_bytes(
                {"queries": intent["query_manifest"]})).hexdigest()}


class Connector(Protocol):
    def connect_and_observe(self, intent: dict[str, Any]) -> tuple[str, dict[str, Any]]: ...


def _driver_observation(intent: dict[str, Any], raw: Any) -> tuple[str, dict[str, Any]]:
    envelope = _exact(raw, {"checks", "connection_nonce", "result"}, "driver result")
    if envelope["result"] != "PASS":
        raise QualificationFailed("connection preflight failed")
    checks = _exact(envelope["checks"], {key for key, _ in FIXED_PREFLIGHT_QUERIES},
                    "driver checks")
    nonce = _text(envelope["connection_nonce"], "connection_nonce",
                  re.compile(r"^[0-9a-f]{32}$"))

    def rows(name: str, columns: int, minimum: int = 1, maximum: int = 1) -> list[list[str]]:
        block = _exact(checks[name], {"rows"}, f"driver check {name}")
        value = block["rows"]
        if (not isinstance(value, list) or not minimum <= len(value) <= maximum or
                any(not isinstance(row, list) or len(row) != columns or
                    any(not isinstance(field, str) for field in row) for row in value)):
            raise QualificationError(f"invalid driver check {name}")
        return value

    extension_rows = rows("extensions", 2, 2, 2)
    extensions = {name: version for name, version in extension_rows}
    if set(extensions) != {"pg_duckdb", "pg_qi"}:
        raise QualificationFailed("required extensions are not installed")
    expected = intent["dossier"]["expected_online"]
    database = rows("database_identity", 2)[0]
    roles = rows("role_identity", 2)[0]
    cluster_id = rows("cluster_identity", 1)[0][0]
    backend = rows("backend_identity", 2)[0]
    _text(backend[0], "backend pid", re.compile(r"^[0-9]+$"))
    _text(backend[1], "backend start", UTC)
    postmaster_start = rows("postmaster_start", 1)[0][0]
    observed = {
        "server_version_num": rows("server_version_num", 1)[0][0],
        "database_name": database[0], "database_oid": database[1],
        "session_user": roles[0], "current_user": roles[1],
        "postmaster_start_time": postmaster_start,
        "start_id": _start_id(cluster_id, postmaster_start), "cluster_id": cluster_id,
        "pg_qi_version": extensions["pg_qi"], "pg_duckdb_version": extensions["pg_duckdb"],
        "routing_mode": rows("routing_mode", 1)[0][0],
        "duckdb_force_execution": rows("duckdb_force", 1)[0][0],
        "pg_qi_enabled": rows("pg_qi_enabled", 1)[0][0],
        "queries_executed": [key for key, _ in FIXED_PREFLIGHT_QUERIES],
        "activation_attempts": 0,
    }
    connection_id = hashlib.sha256(canonical_bytes(
        {"nonce": nonce, "backend_pid": backend[0], "backend_start": backend[1]})).hexdigest()
    observed["connection_id"] = connection_id
    return connection_id, observed


class SitePreflightDriver:
    """Execute the reviewed typed libpq adapter without a shell or ambient PG state."""

    def __init__(self, executable: Path, credential_fd: int | None, deadline_ms: int = 10000):
        self.executable = executable
        self.credential_fd = credential_fd
        self.deadline_ms = deadline_ms

    def connect_and_observe(self, intent: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        endpoint = intent["dossier"]["endpoint"]
        parameters = intent["libpq_parameters"]
        arguments = [str(self.executable), "--transport",
                     "tls" if endpoint["kind"] == "tls_tcp" else "socket",
                     "--host", parameters["host"]]
        if endpoint["kind"] == "tls_tcp":
            if self.credential_fd is None:
                raise QualificationError("TLS credential descriptor is required")
            arguments += ["--hostaddr", parameters["hostaddr"], "--sslrootcert",
                          parameters["sslrootcert"], "--sslrootcert-sha256",
                          endpoint["sslrootcert_sha256"], "--credential-fd",
                          str(self.credential_fd)]
        elif self.credential_fd is not None:
            raise QualificationError("socket preflight forbids a credential descriptor")
        else:
            arguments += ["--socket-uid", parameters["socket_owner_uid"]]
        arguments += ["--port", parameters["port"], "--dbname", parameters["dbname"],
                      "--user", parameters["user"], "--deadline-ms", str(self.deadline_ms)]
        completed = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                                   pass_fds=(() if self.credential_fd is None else (self.credential_fd,)),
                                   timeout=self.deadline_ms / 1000 + 2, check=False)
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            raise QualificationFailed("connection preflight failed")
        try:
            raw = json.loads(completed.stdout.decode("utf-8", "strict"), object_pairs_hook=lambda pairs: _driver_pairs(pairs))
        except (UnicodeDecodeError, json.JSONDecodeError, QualificationError) as exc:
            raise QualificationFailed("connection preflight failed") from None
        return _driver_observation(intent, raw)


def _driver_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError("duplicate driver result key")
        result[key] = value
    return result


def qualify_connection(dossier: Any, trusted_scope_sha256: str, trusted_dossier_sha256: str,
                       trusted_start_sha256: str,
                       environment: dict[str, str], connector: Connector) -> dict[str, Any]:
    reject_ambient_environment(environment)
    intent = validate_dossier(dossier, trusted_scope_sha256, trusted_dossier_sha256,
                              trusted_start_sha256)
    try:
        connection_id, observation = connector.connect_and_observe(intent)
    except Exception:
        raise QualificationFailed("connection preflight failed") from None
    return evaluate_observation(intent, observation, connection_id)


def _load(path: Path) -> dict[str, Any]:
    absolute = path.absolute()
    parts = absolute.parts
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd); directory_fd = next_fd
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_fd)
    except OSError as exc:
        raise QualificationError("unsafe dossier path") from exc
    finally:
        os.close(directory_fd)
    def duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise QualificationError("duplicate dossier key")
            result[key] = item
        return result
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
            raise QualificationError("unsafe dossier file")
        remaining = before.st_size; chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                raise QualificationError("dossier changed during capture")
            chunks.append(chunk); remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise QualificationError("dossier grew during capture")
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise QualificationError("dossier changed during capture")
        value = json.loads(b"".join(chunks).decode("utf-8", "strict"), object_pairs_hook=duplicate,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               QualificationError("non-finite dossier number")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise QualificationError("invalid dossier JSON") from exc
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise QualificationError("dossier root must be object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dossier", required=True, type=Path)
    parser.add_argument("--trusted-scope-sha256", required=True)
    parser.add_argument("--trusted-dossier-sha256", required=True)
    parser.add_argument("--trusted-start-sha256", required=True)
    parser.add_argument("--execute-driver", type=Path)
    parser.add_argument("--credential-fd", type=int)
    args = parser.parse_args(argv)
    try:
        reject_ambient_environment(dict(os.environ))
        intent = validate_dossier(_load(args.dossier), args.trusted_scope_sha256,
                                  args.trusted_dossier_sha256,
                                  args.trusted_start_sha256)
        if args.execute_driver:
            receipt = qualify_connection(intent["dossier"], args.trusted_scope_sha256,
                                         args.trusted_dossier_sha256, args.trusted_start_sha256,
                                         {}, SitePreflightDriver(args.execute_driver,
                                                                 args.credential_fd))
        else:
            if args.credential_fd is not None:
                raise QualificationError("credential descriptor requires execution")
            receipt = {"result": "PASS", "qualification_id": intent["dossier"]["qualification_id"],
                       "query_manifest_sha256": hashlib.sha256(canonical_bytes(
                           {"queries": intent["query_manifest"]})).hexdigest()}
        print(json.dumps(receipt,
                         sort_keys=True, separators=(",", ":")))
        return 0
    except QualificationIncomplete:
        print('{"result":"INCOMPLETE"}', file=sys.stderr); return 3
    except QualificationFailed:
        print('{"result":"FAIL"}', file=sys.stderr); return 1
    except (QualificationError, ContractError, OSError):
        print('{"reason":"site_qualification_refused","result":"INVALID"}', file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
