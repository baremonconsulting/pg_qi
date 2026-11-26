import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
MANIFEST_TOOL = REPO / ".github/scripts/ci-provider-manifest.py"
OVERHEAD_TOOL = REPO / "scripts/phase4c/verify-overhead.py"
BOUNDARY_TOOL = REPO / ".github/scripts/ci-production-boundary.sh"
HASH_TOOL = REPO / ".github/scripts/ci-sha256.sh"
MAPPED_TOOL = REPO / ".github/scripts/ci-mapped-file.py"
PARITY_TOOL = REPO / "tests/phase7/verify_parity.py"
spec = importlib.util.spec_from_file_location("provider_manifest", MANIFEST_TOOL)
provider_manifest = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(provider_manifest)
parity_spec = importlib.util.spec_from_file_location("verify_parity", PARITY_TOOL)
verify_parity = importlib.util.module_from_spec(parity_spec)
assert parity_spec.loader is not None
parity_spec.loader.exec_module(verify_parity)


class ProviderManifestMutationTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        os.environ.pop("PG_QI_POSTGRES_BUILD_ID", None)
        os.environ.pop("PG_QI_BUILD_ID", None)

    def tearDown(self):
        self.environment.stop()

    def base(self):
        value = {
            "schema_version": 1,
            "postgresql_major": "17",
            "postgresql_version": "PostgreSQL 17.12",
            "postgresql_source_commit": "702b3dc86d35bcc08c8e23cffa3c4d9fe9d32c85",
            "postgresql_configure": "'--prefix=/usr/lib/postgresql/17'",
            "postgresql_binary_sha256": "1" * 64,
            "postgresql_source_binary_sha256": "1" * 64,
            "postgresql_build_id": "",
            "pg_qi_source_commit": "3" * 40,
            "pg_qi_binary_sha256": "4" * 64,
            "pg_duckdb_source_commit": provider_manifest.EXPECTED_PG_DUCKDB,
            "pg_duckdb_binary_sha256": "6" * 64,
            "pg_duckdb_source_binary_sha256": "6" * 64,
            "duckdb_source_commit": provider_manifest.EXPECTED_DUCKDB,
            "duckdb_binary_path": "/usr/lib/libduckdb.so",
            "duckdb_binary_sha256": "7" * 64,
            "duckdb_source_binary_sha256": "7" * 64,
            "production_testing_flag": 0,
        }
        value["postgresql_build_id"] = provider_manifest.postgres_build_id(value)
        return value

    def test_valid_manifest(self):
        provider_manifest.validate(self.base())

    def test_missing_identity_field_is_rejected(self):
        value = self.base()
        del value["postgresql_binary_sha256"]
        with self.assertRaises(ValueError):
            provider_manifest.validate(value)

    def test_wrong_source_pin_is_rejected(self):
        value = self.base()
        value["postgresql_source_commit"] = "8" * 40
        with self.assertRaises(ValueError):
            provider_manifest.validate(value)

    def test_test_build_is_rejected(self):
        value = self.base()
        value["production_testing_flag"] = 1
        with self.assertRaises(ValueError):
            provider_manifest.validate(value)

    def test_json_boolean_types_are_rejected(self):
        value = self.base()
        value["schema_version"] = True
        with self.assertRaises(ValueError):
            provider_manifest.validate(value)

    def test_duplicate_json_field_is_rejected_by_cli(self):
        value = json.dumps(self.base())
        duplicate = '{"production_testing_flag":1,' + value[1:]
        result = subprocess.run(
            [sys.executable, str(MANIFEST_TOOL), "validate", "/dev/stdin"],
            input=duplicate, text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate JSON field", result.stderr)

    def test_compile_time_build_id_mismatch_is_rejected(self):
        value = self.base()
        with mock.patch.dict(os.environ, {"PG_QI_POSTGRES_BUILD_ID": "9" * 64}):
            with self.assertRaises(ValueError):
                provider_manifest.validate(value)

    def test_substituted_provider_binaries_are_rejected(self):
        pairs = (
            ("postgresql_binary_sha256", "postgresql_source_binary_sha256"),
            ("pg_duckdb_binary_sha256", "pg_duckdb_source_binary_sha256"),
            ("duckdb_binary_sha256", "duckdb_source_binary_sha256"),
        )
        for installed, _source in pairs:
            with self.subTest(installed=installed):
                value = self.base()
                value[installed] = "a" * 64
                if installed == "postgresql_binary_sha256":
                    value["postgresql_build_id"] = provider_manifest.postgres_build_id(value)
                with self.assertRaises(ValueError):
                    provider_manifest.validate(value)


class OverheadGateMutationTests(unittest.TestCase):
    def write_run(self, root: Path, shadow=(120, 250)) -> None:
        rows = {
            "disabled": (90, 120),
            "off": (100, 150),
            "shadow_p0": shadow,
            "allowlist": (110, 170),
        }
        with (root / "summary.tsv").open("w", encoding="utf-8") as stream:
            stream.write("mode\tp50_us\tp95_us\tp99_us\n")
            for mode, timings in rows.items():
                values = sorted(timings)
                stream.write(f"{mode}\t{values[0]}\t{values[1]}\t{values[1]}\n")
                snapshot_keys = (
                    "pg_qi.shadow_contract_version", "pg_qi.shadow_decision_id",
                    "pg_qi.shadow_context", "pg_qi.shadow_policy_intent", "pg_qi.shadow_reason",
                    "pg_qi.shadow_force_origin", "pg_qi.shadow_classifier_status",
                    "pg_qi.shadow_classifier_reason", "pg_qi.shadow_observed_engine",
                    "pg_qi.shadow_verification", "pg_qi.shadow_request_applied",
                    "pg_qi.shadow_p0_facts_state", "pg_qi.shadow_p0_facts_reason",
                    "duckdb.force_execution",
                )
                snapshot = "".join(
                    f"snapshot\t{mode}\t{key}\t"
                    f"{('unsupported_shape' if key.endswith('facts_reason') and mode == 'shadow_p0' else 'unknown' if key.endswith('facts_state') and mode == 'shadow_p0' else 'test')}\n"
                    for key in snapshot_keys)
                (root / f"{mode}.tsv").write_text(
                    f"sample\tlatency_us\n1\t{timings[0]}\n2\t{timings[1]}\n"
                    + ("info\tshadow_decision_before\t10\ninfo\tshadow_decision_after\t12\n"
                       if mode == "shadow_p0" else "")
                    + f"info\tmode\t{mode}\ninfo\twarmup\t1\ninfo\tsamples\t2\n"
                    + snapshot,
                    encoding="utf-8"
                )

    def run_gate(self, root: Path):
        env = os.environ.copy()
        env.update(PG_QI_OVERHEAD_MAX_RATIO="2", PG_QI_OVERHEAD_SLACK_US="10")
        return subprocess.run(
            [sys.executable, str(OVERHEAD_TOOL), str(root), "2"],
            text=True, capture_output=True, env=env, check=False
        )

    def test_bound_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_run(root)
            self.assertEqual(self.run_gate(root).returncode, 0)

    def test_raw_p50_bound_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_run(root, (211, 211))
            result = self.run_gate(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exceeds bound", result.stderr)

    def test_raw_p95_bound_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_run(root, (120, 311))
            result = self.run_gate(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exceeds bound", result.stderr)

    def test_forged_summary_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_run(root)
            summary = root / "summary.tsv"
            summary.write_text(summary.read_text().replace("shadow_p0\t120\t250\t250",
                                                           "shadow_p0\t120\t120\t120"))
            self.assertIn("summary does not match", self.run_gate(root).stderr)

    def test_duplicate_sample_identifier_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_run(root)
            path = root / "shadow_p0.tsv"
            path.write_text(path.read_text().replace("2\t250", "1\t250"))
            self.assertIn("identifiers", self.run_gate(root).stderr)

    def test_malformed_raw_row_and_duplicate_summary_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_run(root)
            path = root / "shadow_p0.tsv"
            path.write_text(path.read_text().replace("2\t250", "2\t2_50\textra"))
            self.assertIn("malformed", self.run_gate(root).stderr)
            self.write_run(root)
            summary = root / "summary.tsv"
            summary.write_text(summary.read_text() + "shadow_p0\t120\t250\t250\n")
            self.assertIn("summary modes or columns", self.run_gate(root).stderr)


class ProductionBoundaryMutationTests(unittest.TestCase):
    def test_exported_symbol_and_installed_witness_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); lib = root / "lib"; share = root / "share"
            lib.mkdir(); (share / "extension").mkdir(parents=True)
            source = root / "leak.c"; built = root / "pg_qi.so"; installed = lib / "pg_qi.so"
            source.write_text("void pg_qi_execution_witness_leak(void) {}\n", encoding="utf-8")
            subprocess.run(["cc", "-shared", "-fPIC", str(source), "-o", str(built)], check=True)
            installed.write_bytes(built.read_bytes())
            pg_config = root / "pg_config"
            pg_config.write_text(f'#!/bin/sh\ncase "$1" in --pkglibdir) echo {lib};; --sharedir) echo {share};; esac\n')
            pg_config.chmod(0o755)
            clean_strings = root / "clean-strings"
            clean_strings.write_text("#!/bin/sh\nexit 0\n"); clean_strings.chmod(0o755)
            env = {**os.environ, "PG_CONFIG": str(pg_config), "PG_QI_BUILT_PATH": str(built),
                   "STRINGS": str(clean_strings)}
            result = subprocess.run(["bash", str(BOUNDARY_TOOL), "17", str(root / "artifact")],
                                    env=env, text=True, capture_output=True, check=False)
            self.assertIn("exported symbol", result.stderr)
            source.write_text("void production_symbol(void) {}\n", encoding="utf-8")
            subprocess.run(["cc", "-shared", "-fPIC", str(source), "-o", str(built)], check=True)
            installed.write_bytes(built.read_bytes())
            (lib / "pg_qi_witness.so").write_bytes(b"fixture")
            result = subprocess.run(["bash", str(BOUNDARY_TOOL), "17", str(root / "artifact2")],
                                    env={**os.environ, "PG_CONFIG": str(pg_config),
                                         "PG_QI_BUILT_PATH": str(built)},
                                    text=True, capture_output=True, check=False)
            self.assertIn("test-only pg_qi artefact", result.stderr)

    def test_mapped_file_identity_accepts_current_python_mapping(self):
        result = subprocess.run(
            [sys.executable, str(MAPPED_TOOL), str(os.getpid()), r"/python[0-9.]*"],
            text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hash_tool_failure_and_malformed_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("data", encoding="utf-8")
            for body in ("#!/bin/sh\nexit 9\n", "#!/bin/sh\necho malformed\n"):
                fake = root / "fake-sha256sum"
                fake.write_text(body, encoding="utf-8")
                fake.chmod(0o755)
                result = subprocess.run(
                    ["bash", str(HASH_TOOL), str(target)],
                    env={**os.environ, "SHA256SUM": str(fake)},
                    text=True, capture_output=True, check=False)
                self.assertNotEqual(result.returncode, 0)

    def test_witness_surface_is_rejected(self):
        text = BOUNDARY_TOOL.read_text(encoding="utf-8")
        self.assertIn("pg_qi_witness[._]", text)
        self.assertIn("pg_qi_witness*", text)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lib, share, artifact = root / "lib", root / "share", root / "artifact"
            lib.mkdir(); (share / "extension").mkdir(parents=True)
            payload = Path("/bin/true").read_bytes() + b"pg_qi_witness.armed\0"
            built = root / "pg_qi.so"; installed = lib / "pg_qi.so"
            built.write_bytes(payload); installed.write_bytes(payload)
            pg_config = root / "pg_config"
            pg_config.write_text(
                f'#!/bin/sh\ncase "$1" in --pkglibdir) echo {lib};; '
                f'--sharedir) echo {share};; esac\n', encoding="utf-8")
            pg_config.chmod(0o755)
            result = subprocess.run(
                ["bash", str(BOUNDARY_TOOL), "17", str(artifact)],
                env={**os.environ, "PG_CONFIG": str(pg_config),
                     "PG_QI_BUILT_PATH": str(built)},
                text=True, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("test-only string", result.stderr)

    def test_scanner_failures_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lib = root / "lib"
            share = root / "share"
            artifact = root / "artifact"
            lib.mkdir()
            (share / "extension").mkdir(parents=True)
            built = root / "pg_qi.so"
            installed = lib / "pg_qi.so"
            built.write_bytes(b"production")
            installed.write_bytes(b"production")
            pg_config = root / "pg_config"
            pg_config.write_text(
                f'#!/bin/sh\ncase "$1" in --pkglibdir) echo {lib};; '
                f'--sharedir) echo {share};; esac\n', encoding="utf-8")
            scanner_fail = root / "scanner-fails"
            scanner_fail.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
            scanner_ok = root / "scanner-ok"
            scanner_ok.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            grep_clean = root / "grep-clean"
            grep_clean.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            for executable in (pg_config, scanner_fail, scanner_ok, grep_clean):
                executable.chmod(0o755)
            cases = (
                ({"STRINGS": scanner_fail, "NM": scanner_ok, "GREP": grep_clean},
                 "strings scanner failed"),
                ({"STRINGS": scanner_ok, "NM": scanner_fail, "GREP": grep_clean},
                 "symbol scanner failed"),
                ({"STRINGS": scanner_ok, "NM": scanner_ok, "GREP": scanner_fail},
                 "string-result scanner failed"),
            )
            for tools, expected in cases:
                with self.subTest(expected=expected):
                    result = subprocess.run(
                        ["bash", str(BOUNDARY_TOOL), "17", str(artifact)],
                        env={**os.environ, "PG_CONFIG": str(pg_config),
                             "PG_QI_BUILT_PATH": str(built),
                             **{key: str(value) for key, value in tools.items()}},
                        text=True, capture_output=True, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)


class ParityOracleMutationTests(unittest.TestCase):
    def write(self, root: Path, name: str, lines: list[str]) -> Path:
        path = root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_changed_parity_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = ["ACTUAL_MODE", "SET", "off", "SET"]
            body = [
                "CASE_ORDERED", "id|value", "1|alpha", "ROW_COUNT 1 SQLSTATE 00000",
                "MULTISET_BEGIN", "1|alpha", "MULTISET_END",
                "V2_BEFORE_CASE_ORDERED", "pg_qi.decision_v2", "v=2;mode=off", "(1 row)",
                "V2_AFTER_CASE_ORDERED", "pg_qi.decision_v2", "v=2;mode=off", "(1 row)",
                "V2_BEFORE", "x1", "x2", "x3", "x4",
                "V2_AFTER", "y1", "y2", "y3", "y4",
            ]
            off = self.write(root, "off", prefix + body)
            shadow_lines = ["ACTUAL_MODE", "SET", "shadow_p0", "SET"] + body
            shadow = self.write(root, "shadow", shadow_lines)
            verify_parity.verify_results(off, shadow)
            shadow.write_text(
                shadow.read_text(encoding="utf-8").replace("1|alpha", "1|changed", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "parity output differs"):
                verify_parity.verify_results(off, shadow)

    def test_changed_sqlstate_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = ["ACTUAL_MODE", "off"] + [
                f"{marker} {state}"
                for marker, state in verify_parity.EXPECTED_SQLSTATES.items()
            ] + ["COMMIT"]
            off = self.write(root, "off", receipt)
            shadow = self.write(root, "shadow", ["ACTUAL_MODE", "shadow_p0"] + receipt[2:])
            verify_parity.verify_errors(off, shadow)
            shadow.write_text(
                shadow.read_text(encoding="utf-8").replace(
                    "runtime_timeout 57014", "runtime_timeout 00000"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "error receipt differs"):
                verify_parity.verify_errors(off, shadow)

    def case_receipt(self) -> list[str]:
        def encoded(event: int, p0: str, reason: str) -> str:
            return (
                "v=2;sv=2;s=complete;cid=0123456789abcdef0123456789abcdef;g=1;"
                f"e={event};mode=shadow_p0;enabled=on;context=proven_client;"
                "eligible=not_evaluated;safe=not_evaluated;beneficial=not_evaluated;"
                "barrier=closed;recommendation=insufficient_evidence;reason=0;rule=1;"
                "model=1;certificate=1;policy=pass_through;force=off;engine=unknown;"
                "verification=unverified;observation=no_authoritative_match;"
                f"p0={p0};p0_reason={reason};lifecycle=fresh_execution"
            )
        lines: list[str] = []
        event = 10
        for case, (p0, reason, delta) in verify_parity.CASE_EXPECTED.items():
            lines += [
                "V2_BEFORE_CASE_" + case,
                "pg_qi.decision_v2", encoded(event, p0, reason), "(1 row)",
                "V2_AFTER_CASE_" + case,
                "pg_qi.decision_v2", encoded(event + delta, p0, reason), "(1 row)",
            ]
            event += delta + 1
        return lines

    def test_changed_case_reason_is_rejected(self):
        lines = self.case_receipt()
        verify_parity.verify_case_snapshots(lines)
        position = lines.index("V2_AFTER_CASE_ORDERED") + 2
        lines[position] = lines[position].replace(
            "p0_reason=unsupported_shape", "p0_reason=unsupported_node"
        )
        with self.assertRaisesRegex(ValueError, "unexpected observer facts"):
            verify_parity.verify_case_snapshots(lines)

    def test_changed_case_freshness_is_rejected(self):
        lines = self.case_receipt()
        position = lines.index("V2_AFTER_CASE_COUNT") + 2
        lines[position] = lines[position].replace(";e=19;", ";e=18;")
        with self.assertRaisesRegex(ValueError, "decision-event transition"):
            verify_parity.verify_case_snapshots(lines)

    def test_changed_case_identity_is_rejected(self):
        lines = self.case_receipt()
        position = lines.index("V2_AFTER_CASE_COUNT") + 2
        lines[position] = lines[position].replace(
            "cid=0123456789abcdef0123456789abcdef",
            "cid=fedcba9876543210fedcba9876543210",
        )
        with self.assertRaisesRegex(ValueError, "snapshot identity changed"):
            verify_parity.verify_case_snapshots(lines)

    def test_generic_reuse_must_retain_identical_snapshot(self):
        lines = self.case_receipt()
        position = lines.index("V2_AFTER_CASE_PREPARED_GENERIC_REUSE") + 2
        lines[position] = lines[position].replace(
            ";reason=0;", ";reason=1;"
        )
        with self.assertRaisesRegex(ValueError, "retained snapshot changed"):
            verify_parity.verify_case_snapshots(lines)

    def test_empty_result_header_is_not_normalized_away(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = [
                "ACTUAL_MODE", "SET", "off", "SET",
                "MULTISET_BEGIN", "same", "MULTISET_END",
                "V2_BEFORE", "pg_qi.decision_v2", "none", "(1 row)",
            ]
            off = self.write(root, "off", common + ["id|value", "(0 rows)"])
            shadow_lines = common.copy()
            shadow_lines[2] = "shadow_p0"
            shadow = self.write(root, "shadow", shadow_lines + ["id|changed", "(0 rows)"])
            with self.assertRaisesRegex(ValueError, "parity output differs"):
                verify_parity.verify_results(off, shadow)


if __name__ == "__main__":
    unittest.main()
