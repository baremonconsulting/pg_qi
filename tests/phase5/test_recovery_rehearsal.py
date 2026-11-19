#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr
import io
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.phase5.contract_rules import (INCIDENT_STEPS, R1_STEPS, R2_STEPS,
    REHEARSAL_ACTIONS, canonical_bytes, canonical_sha256, validate_contract)
from tools.phase5.recovery_rehearsal import (RehearsalError, build_recovery_record,
                                             main, validate_rehearsal_bundle)

ZERO = "0" * 64


def state(result: str = "PASS") -> dict:
    return {"lifecycle": "VALIDATED", "technical_result": result,
        "evidence_completeness": "COMPLETE", "exit_state": "CLEAN",
        "value_outcome": "NOT_EVALUATED", "project_decision": "UNDECIDED"}


def write_json(path: Path, value: dict) -> None:
    path.write_bytes(canonical_bytes(value)); path.chmod(0o600)


def artifact(root: Path, name: str) -> dict:
    payload = (root / name).read_bytes()
    return {"path": name, "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload)}


def contract(kind: str, identity: str, **fields: object) -> dict:
    return {"schema_version": 1, "contract_type": kind,
            "contract_id": identity, **fields}


def identity(session: str, start: str = "start-1", pid: int = 111,
             backend_start: str = "2026-11-18T10:00:00Z") -> dict:
    return {"cluster_id": "copy-cluster", "start_id": start, "pid": pid,
        "backend_start": backend_start, "session_id": session}


def build_bundle(root: Path) -> tuple[dict, dict, dict, dict, dict]:
    root.chmod(0o700)
    mandate = contract("rehearsal_mandate", "mandate-1", scope_sha256=ZERO,
        rehearsal_id="rehearsal-1", target_copy_identity_sha256="c" * 64,
        target_cluster_id="copy-cluster", target_initial_start_id="start-1",
        source_environment_identity_sha256="a" * 64,
        recovery_plan_sha256="b" * 64, runbook_sha256="d" * 64,
        artifact_identity_scheme="PGQI_STACK_SHA256_V1",
        candidate_artifact_sha256="e" * 64, candidate_configuration_sha256="f" * 64,
        approved_artifact_sha256="a" * 64, approved_configuration_sha256="b" * 64,
        approved_restore_source_sha256=None,
        pool_profile="DIRECT",
        valid_from="2026-11-18T09:00:00Z", valid_through="2026-11-18T23:00:00Z",
        primary_operator="operator-primary", second_operator="operator-second",
        implementer="implementer", approvals=["technical-owner", "operation-owner", "environment-owner"],
        allowed_strategies=["EXACT_BINARY_CONFIG_DOWNGRADE"],
        allowed_actions=REHEARSAL_ACTIONS, recoverable_copy_verified=True,
        lifecycle_executor="HUMAN_OPERATOR_ONLY")
    write_json(root / "mandate.json", mandate)
    proofs = {
        "positive.json": {"document_type": "r1_positive_probe", "rehearsal_id": "rehearsal-1",
            "session_id": "session-1", "observed_engine": "duckdb",
            "observed_at": "2026-11-18T10:00:10Z", "result": "PASS"},
        "off.json": {"document_type": "r1_off_proof", "rehearsal_id": "rehearsal-1",
            "mode": "off", "new_owned_request_admitted": False,
            "cached_plan_persistence_observed": True,
            "observed_at": "2026-11-18T10:00:20Z", "result": "PASS"},
        "pool.json": {"document_type": "r1_pool_proof", "rehearsal_id": "rehearsal-1",
            "pool_profile": "DIRECT", "pool_action": "NOT_APPLICABLE",
            "observation_complete": True,
            "recycle_completed_at": "2026-11-18T10:00:25Z",
            "observed_backend_starts": [],
            "observed_at": "2026-11-18T10:00:30Z", "result": "PASS"},
        "exit.json": {"document_type": "r1_backend_exit", "rehearsal_id": "rehearsal-1",
            "observer_authorized": True, "observation_complete": True,
            "cluster_id": "copy-cluster", "start_id": "start-1",
            "owned_sessions": ["session-1"], "active_owned_sessions": [],
            "observed_at": "2026-11-18T10:00:40Z", "result": "PASS"},
        "r1-control.json": {"document_type": "fresh_safe_control", "rehearsal_id": "rehearsal-1",
            "identity": identity("fresh-r1", pid=222,
                backend_start="2026-11-18T10:00:45Z"),
            "connected_at": "2026-11-18T10:00:50Z",
            "mode": "off", "force_execution": "off", "observed_engine": "postgres", "result": "PASS"},
        "r2-control.json": {"document_type": "fresh_safe_control", "rehearsal_id": "rehearsal-1",
            "identity": identity("fresh-r2", "start-2", 333,
                "2026-11-18T10:02:45Z"), "connected_at": "2026-11-18T10:02:50Z",
            "mode": "off", "force_execution": "off", "observed_engine": "postgres", "result": "PASS"},
        "r2-identity.json": {"document_type": "r2_identity_proof",
            "rehearsal_id": "rehearsal-1", "cluster_id": "copy-cluster",
            "artifact_identity_scheme": "PGQI_STACK_SHA256_V1",
            "start_id": "start-2", "artifact_sha256": "a" * 64,
            "configuration_sha256": "b" * 64,
            "observed_at": "2026-11-18T10:02:40Z", "result": "PASS"},
        "r2-source.json": {"document_type": "r2_source_identity_proof",
            "rehearsal_id": "rehearsal-1", "cluster_id": "copy-cluster",
            "artifact_identity_scheme": "PGQI_STACK_SHA256_V1",
            "start_id": "start-1", "artifact_sha256": "e" * 64,
            "configuration_sha256": "f" * 64,
            "observed_at": "2026-11-18T10:02:10Z", "result": "PASS"},
        "incident-exit.json": {"document_type": "incident_backend_exit",
            "rehearsal_id": "rehearsal-1", "identity": identity("poisoned", "start-2", 444,
                "2026-11-18T10:03:10Z"),
            "observer_authorized": True, "observation_complete": True,
            "observation_cluster_id": "copy-cluster", "observation_start_id": "start-2",
            "backend_present": False,
            "bystander_identity": identity("bystander", "start-2", 555,
                "2026-11-18T10:03:05Z"),
            "bystander_observed_before": True, "bystander_present": True,
            "isolation_started_at": "2026-11-18T10:03:30Z",
            "observed_at": "2026-11-18T10:03:50Z",
            "result": "PASS"}}
    for name, proof in proofs.items(): write_json(root / name, proof)
    mandate_sha = canonical_sha256(mandate)
    r1 = contract("recovery_r1", "r1-1", scope_sha256=ZERO,
        rehearsal_id="rehearsal-1", mandate_sha256=mandate_sha, run_id="run-1",
        steps=R1_STEPS, pool_profile="DIRECT", pool_action="NOT_APPLICABLE",
        pool_proof=artifact(root, "pool.json"),
        owned_sessions=["session-1"], positive_probe=artifact(root, "positive.json"),
        off_proof=artifact(root, "off.json"), backend_exit_proof=artifact(root, "exit.json"),
        fresh_control=artifact(root, "r1-control.json"), started_at="2026-11-18T10:00:00Z",
        finished_at="2026-11-18T10:01:00Z", duration_ms=60000, result="PASS", state=state())
    write_json(root / "r1-terminal.json", r1)
    r2 = contract("recovery_r2", "r2-1", scope_sha256=ZERO,
        rehearsal_id="rehearsal-1", mandate_sha256=mandate_sha,
        strategy="EXACT_BINARY_CONFIG_DOWNGRADE", r1_terminal_sha256=canonical_sha256(r1),
        steps=R2_STEPS, source_artifact_sha256="e" * 64,
        source_configuration_sha256="f" * 64, result_artifact_sha256="a" * 64,
        result_configuration_sha256="b" * 64, restore_source_sha256=None,
        before_cluster_id="copy-cluster", before_start_id="start-1",
        after_cluster_id="copy-cluster", after_start_id="start-2",
        same_data_continuity=True, source_identity_proof=artifact(root, "r2-source.json"),
        identity_proof=artifact(root, "r2-identity.json"),
        fresh_control=artifact(root, "r2-control.json"),
        started_at="2026-11-18T10:02:00Z", finished_at="2026-11-18T10:03:00Z",
        duration_ms=60000, result="PASS", state=state())
    write_json(root / "r2-terminal.json", r2)
    walkthrough = contract("operator_walkthrough", "walkthrough-1", scope_sha256=ZERO,
        rehearsal_id="rehearsal-1", mandate_sha256=mandate_sha,
        primary_operator="operator-primary", second_operator="operator-second",
        implementer="implementer", runbook_sha256="d" * 64,
        r1_terminal_sha256=canonical_sha256(r1), r2_terminal_sha256=canonical_sha256(r2),
        actions_performed=["R1_SESSION_RETIREMENT", "INCIDENT_HANDOFF"],
        signed_at="2026-11-18T10:05:00Z", result="PASS", state=state())
    write_json(root / "walkthrough.json", walkthrough)
    incident = contract("recovery_incident", "incident-1", scope_sha256=ZERO,
        rehearsal_id="rehearsal-1", mandate_sha256=mandate_sha, incident_id="incident-1",
        steps=INCIDENT_STEPS, poisoned_backend=identity("poisoned", "start-2", 444,
            "2026-11-18T10:03:10Z"),
        backend_exit_proof=artifact(root, "incident-exit.json"),
        performed_at="2026-11-18T10:04:00Z",
        invalidated_go_sha256="9" * 64, invalidated_go_nonce="prior-operation-go",
        current_go_invalidated=True, restart_requires_new_go=True,
        result="PASS", state=state())
    write_json(root / "incident.json", incident)
    return mandate, r1, r2, walkthrough, incident


def relink_after_r1(root: Path, r1: dict) -> None:
    r2 = json.loads((root / "r2-terminal.json").read_text())
    r2["r1_terminal_sha256"] = canonical_sha256(r1)
    write_json(root / "r2-terminal.json", r2)
    walkthrough = json.loads((root / "walkthrough.json").read_text())
    walkthrough["r1_terminal_sha256"] = canonical_sha256(r1)
    walkthrough["r2_terminal_sha256"] = canonical_sha256(r2)
    write_json(root / "walkthrough.json", walkthrough)


class RecoveryRehearsalTests(unittest.TestCase):
    NOW = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)

    def test_complete_bundle_is_only_eligible_for_human_grant(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); _mandate, _r1, r2, walkthrough, _incident = build_bundle(root)
            validation = validate_rehearsal_bundle(root, self.NOW)
            self.assertEqual((validation["result"], validation["decision"]),
                             ("PASS", "ELIGIBLE_FOR_HUMAN_GRANT"))
            recovery = build_recovery_record(validation, r2, walkthrough)
            validate_contract(recovery)
            self.assertNotIn("GRANTED", json.dumps(recovery))

    def test_client_loss_or_reconnect_cannot_prove_server_exit(self) -> None:
        for mutation in ("observer", "incomplete", "active"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_bundle(root)
                proof = json.loads((root / "exit.json").read_text())
                if mutation == "observer": proof["observer_authorized"] = False
                elif mutation == "incomplete": proof["observation_complete"] = False
                else: proof["active_owned_sessions"] = ["session-1"]
                write_json(root / "exit.json", proof)
                r1 = json.loads((root / "r1-terminal.json").read_text())
                r1["backend_exit_proof"] = artifact(root, "exit.json")
                write_json(root / "r1-terminal.json", r1)
                relink_after_r1(root, r1)
                with self.assertRaisesRegex(RehearsalError, "retirement_not_proven"):
                    validate_rehearsal_bundle(root, self.NOW)

    def test_changed_terminal_or_nonfresh_control_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_bundle(root)
            control = json.loads((root / "r1-control.json").read_text())
            control["identity"]["session_id"] = "session-1"
            write_json(root / "r1-control.json", control)
            r1 = json.loads((root / "r1-terminal.json").read_text())
            r1["fresh_control"] = artifact(root, "r1-control.json")
            write_json(root / "r1-terminal.json", r1)
            relink_after_r1(root, r1)
            with self.assertRaisesRegex(RehearsalError, "fresh_default_off"):
                validate_rehearsal_bundle(root, self.NOW)

    def test_expired_mandate_and_missing_incident_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_bundle(root)
            with self.assertRaisesRegex(RehearsalError, "not_current"):
                validate_rehearsal_bundle(root,
                    datetime(2026, 11, 19, tzinfo=timezone.utc))
            (root / "incident.json").unlink()
            with self.assertRaisesRegex(RehearsalError, "invalid_recovery_incident"):
                validate_rehearsal_bundle(root, self.NOW)

    def test_contracts_refuse_role_alias_restore_as_downgrade_and_sla_fields(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); mandate, _r1, r2, _walk, _incident = build_bundle(root)
            bad = deepcopy(mandate); bad["second_operator"] = bad["primary_operator"]
            with self.assertRaisesRegex(ValueError, "independent"): validate_contract(bad)
            bad = deepcopy(r2); bad["restore_source_sha256"] = "9" * 64
            with self.assertRaisesRegex(ValueError, "restore semantics"): validate_contract(bad)
            bad = deepcopy(r2); bad["rto_ms"] = 100
            with self.assertRaisesRegex(ValueError, "unknown controlling"): validate_contract(bad)

    def test_r1_requires_ordered_pool_and_control_proofs_inside_terminal(self) -> None:
        for target, field, value, reason in (
                ("pool.json", "pool_profile", "SESSION_POOL", "r1_server_retirement"),
                ("off.json", "observed_at", "2026-11-18T10:00:05Z", "r1_server_retirement"),
                ("r1-control.json", "connected_at", "2026-11-18T10:01:01Z",
                 "fresh_default_off"),
                ("r1-control.json", "identity", identity("fresh-r1", pid=222,
                 backend_start="2026-11-18T10:00:30Z"), "fresh_default_off")):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_bundle(root)
                proof = json.loads((root / target).read_text()); proof[field] = value
                write_json(root / target, proof)
                r1 = json.loads((root / "r1-terminal.json").read_text())
                artifact_field = {"pool.json": "pool_proof", "off.json": "off_proof",
                                  "r1-control.json": "fresh_control"}[target]
                r1[artifact_field] = artifact(root, target)
                write_json(root / "r1-terminal.json", r1); relink_after_r1(root, r1)
                with self.assertRaisesRegex(RehearsalError, reason):
                    validate_rehearsal_bundle(root, self.NOW)

    def test_r2_identity_and_incident_isolation_are_observed_not_asserted(self) -> None:
        for target, field, value, reason in (
                ("r2-identity.json", "artifact_sha256", "f" * 64, "approved_identity"),
                ("incident-exit.json", "bystander_present", False, "incident_exit"),
                ("incident-exit.json", "observation_start_id", "start-3", "incident_exit"),
                ("incident-exit.json", "observed_at", "2026-11-18T10:03:00Z",
                 "incident_exit"),
                ("incident-exit.json", "bystander_identity",
                 identity("different-label", "start-2", 444,
                          "2026-11-18T10:03:10Z"), "incident_exit")):
            with self.subTest(target=target, field=field), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_bundle(root)
                proof = json.loads((root / target).read_text()); proof[field] = value
                write_json(root / target, proof)
                if target == "r2-identity.json":
                    r2 = json.loads((root / "r2-terminal.json").read_text())
                    r2["identity_proof"] = artifact(root, target)
                    write_json(root / "r2-terminal.json", r2)
                    walk = json.loads((root / "walkthrough.json").read_text())
                    walk["r2_terminal_sha256"] = canonical_sha256(r2)
                    write_json(root / "walkthrough.json", walk)
                else:
                    incident = json.loads((root / "incident.json").read_text())
                    incident["backend_exit_proof"] = artifact(root, target)
                    write_json(root / "incident.json", incident)
                with self.assertRaisesRegex(RehearsalError, reason):
                    validate_rehearsal_bundle(root, self.NOW)

    def test_pooled_profile_requires_recycle_after_routing_off(self) -> None:
        for recycle_at, passes in (("2026-11-18T10:00:25Z", True),
                                   ("2026-11-18T10:00:15Z", False)):
            with self.subTest(recycle_at=recycle_at), tempfile.TemporaryDirectory() as name:
                root = Path(name); build_bundle(root)
                mandate = json.loads((root / "mandate.json").read_text())
                mandate["pool_profile"] = "SESSION_POOL"
                write_json(root / "mandate.json", mandate)
                mandate_sha = canonical_sha256(mandate)
                pool = json.loads((root / "pool.json").read_text())
                pool.update(pool_profile="SESSION_POOL", pool_action="RECYCLED",
                            recycle_completed_at=recycle_at,
                            observed_backend_starts=["2026-11-18T10:00:27Z"])
                write_json(root / "pool.json", pool)
                r1 = json.loads((root / "r1-terminal.json").read_text())
                r1.update(mandate_sha256=mandate_sha, pool_profile="SESSION_POOL",
                          pool_action="RECYCLED", pool_proof=artifact(root, "pool.json"))
                write_json(root / "r1-terminal.json", r1)
                r2 = json.loads((root / "r2-terminal.json").read_text())
                r2.update(mandate_sha256=mandate_sha,
                          r1_terminal_sha256=canonical_sha256(r1))
                write_json(root / "r2-terminal.json", r2)
                walk = json.loads((root / "walkthrough.json").read_text())
                walk.update(mandate_sha256=mandate_sha,
                            r1_terminal_sha256=canonical_sha256(r1),
                            r2_terminal_sha256=canonical_sha256(r2))
                write_json(root / "walkthrough.json", walk)
                incident = json.loads((root / "incident.json").read_text())
                incident["mandate_sha256"] = mandate_sha
                write_json(root / "incident.json", incident)
                if passes:
                    self.assertEqual(validate_rehearsal_bundle(root, self.NOW)["result"], "PASS")
                else:
                    with self.assertRaisesRegex(RehearsalError, "r1_server_retirement"):
                        validate_rehearsal_bundle(root, self.NOW)

    def test_validator_has_no_lifecycle_or_network_capability(self) -> None:
        path = Path(__file__).resolve().parents[2] / "tools/phase5/recovery_rehearsal.py"
        tree = ast.parse(path.read_text())
        imports = {alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in node.names}
        self.assertTrue(imports.isdisjoint({"subprocess", "socket", "signal", "psycopg", "asyncpg"}))
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertTrue(calls.isdisjoint({"kill", "terminate", "cancel", "system", "popen"}))

    def test_cli_writes_exclusive_private_validation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); build_bundle(root)
            output_dir = root / "output"; output_dir.mkdir(mode=0o700)
            output = output_dir / "validation.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr), \
                    mock.patch("tools.phase5.recovery_rehearsal.datetime") as clock:
                clock.now.return_value = self.NOW
                clock.fromisoformat.side_effect = datetime.fromisoformat
                clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
                code = main(["--bundle", str(root), "--output", str(output)])
            self.assertEqual(code, 0); self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertIn("ELIGIBLE_FOR_HUMAN_GRANT", stdout.getvalue())
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(["--bundle", str(root), "--output", str(output)]), 1)


if __name__ == "__main__":
    unittest.main()
