#!/usr/bin/env python3
"""PQ-9 corpus qualification and complete typed-oracle tests."""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "corpus_oracle", ROOT / "tools/phase5/corpus_oracle.py")
ORACLE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = ORACLE
SPEC.loader.exec_module(ORACLE)


def write_spool(path: Path, columns: list[tuple[str, int]], rows: list[tuple[object, ...]]) -> None:
    data = bytearray(ORACLE.MAGIC) + struct.pack(">I", len(columns))
    for name, oid in columns:
        encoded = name.encode()
        data += struct.pack(">I", len(encoded)) + encoded + struct.pack(">I", oid)
    for row in rows:
        data += struct.pack(">I", len(row))
        for value in row:
            if value is None:
                data += struct.pack(">I", 0xFFFFFFFF)
            else:
                encoded = str(value).encode()
                data += struct.pack(">I", len(encoded)) + encoded
    path.write_bytes(data)
    os.chmod(path, 0o600)


def qualification() -> dict:
    authority = {"state": "AUTHORISED", "reference": "approval-1",
                 "authorized_before_acquisition": True}
    return {"schema_version": 1, "document_type": "corpus_qualification",
        "qualification_id": "qualification-1",
        "authorities": {"inventory": dict(authority), "executed_sql": dict(authority),
                        "dataset": dict(authority)},
        "storage_policy": {"authorization_reference": "storage-approval-1",
            "raw_root": "/private/raw", "spool_root": "/private/spool",
            "scratch_root": "/private/scratch", "required_mode": "0700",
            "retention_class": "retention-30d", "retention_authority": "evidence-owner",
            "authorized_before_acquisition": True},
        "baseline_authority": {"authorization_reference": "baseline-approval-1",
            "authorized_before_acquisition": True, "clean_instance_attested": True,
            "server_build_sha256": "1" * 64, "schema_sha256": "2" * 64,
            "data_sha256": "3" * 64, "configuration_sha256": "4" * 64,
            "provider_identity": "POSTGRESQL_NATIVE_NO_DELEGATING_PROVIDER",
            "cases": [{"query_id": "natural-eligible", "spool_path": "raw/baseline.spool",
                "spool_sha256": "5" * 64, "spool_bytes": 12}]},
        "oracle_limits": {"max_spool_bytes": 1048576, "max_rows": 10000,
            "max_columns": 32, "max_field_bytes": 128, "max_rows_in_memory": 64,
            "max_open_runs": 8},
        "natural_inventory": [
            {"query_id": "natural-eligible", "inventory_origin": "natural_internal",
             "inventory_source_ref": "inventory-1", "eligibility": "ELIGIBLE",
             "reason_family": "supported", "frequency_weight": 4,
             "baseline_time_weight": 10},
            {"query_id": "natural-unsupported", "inventory_origin": "natural_internal",
             "inventory_source_ref": "inventory-1", "eligibility": "UNSUPPORTED",
             "reason_family": "type", "frequency_weight": 3,
             "baseline_time_weight": None},
            {"query_id": "natural-not-executable", "inventory_origin": "natural_internal",
             "inventory_source_ref": "inventory-1", "eligibility": "NOT_EXECUTABLE",
             "reason_family": "authority", "frequency_weight": 2,
             "baseline_time_weight": None},
            {"query_id": "natural-unknown", "inventory_origin": "natural_internal",
             "inventory_source_ref": "inventory-1", "eligibility": "UNKNOWN",
             "reason_family": "unresolved", "frequency_weight": None,
             "baseline_time_weight": None}],
        "execution_corpus": [
            {"query_id": "natural-eligible", "natural_query_id": "natural-eligible",
             "executed_sql_origin": "natural_internal", "executed_sql_ref": "sql-1",
             "sql_sha256": "a" * 64,
             "dataset_origin": "natural_internal", "dataset_ref": "data-1",
             "concurrent_writes": False},
            {"query_id": "derived-1", "natural_query_id": "natural-unsupported",
             "executed_sql_origin": "derived_reproduction", "executed_sql_ref": "sql-2",
             "sql_sha256": "b" * 64,
             "dataset_origin": "synthetic_control", "dataset_ref": "data-2",
             "concurrent_writes": False}],
        "synthetic_controls": [
            {"query_id": "control-1", "control_kind": "negative-routing",
             "executed_sql_ref": "sql-control", "dataset_ref": "data-control"}]}


def baseline() -> dict:
    return {"schema_version": 1, "document_type": "baseline_manifest",
        "baseline_id": "baseline-1", "server_build_sha256": "1" * 64,
        "schema_sha256": "2" * 64, "data_sha256": "3" * 64,
        "configuration_sha256": "4" * 64,
        "provider_identity": "POSTGRESQL_NATIVE_NO_DELEGATING_PROVIDER",
        "provider_absence_verified": True, "static_data": True,
        "concurrent_writes": False,
        "cases": [{"query_id": "q-1", "spool_path": "raw/baseline.spool",
                   "spool_sha256": "5" * 64, "spool_bytes": 32}]}


class QualificationTests(unittest.TestCase):
    def test_natural_denominator_excludes_derived_and_controls(self) -> None:
        report = ORACLE.validate_qualification(qualification())
        self.assertEqual(report["denominator"], 4)
        self.assertEqual(report["confirmed_eligible"], 1)
        self.assertEqual(report["upper_eligible"], 2)
        self.assertEqual(report["confirmed_ratio"], .25)
        self.assertEqual(report["upper_ratio"], .5)
        self.assertIsNone(report["frequency_ratio"])
        self.assertEqual(report["execution_items"], 2)
        self.assertEqual(report["synthetic_controls"], 1)
        self.assertEqual(report["natural_items_linked_to_execution"], 1)

    def test_authority_storage_lineage_and_concurrency_are_fail_closed(self) -> None:
        for mutation in ("authority", "mode", "parent", "concurrency", "origin"):
            with self.subTest(mutation=mutation):
                value = qualification()
                if mutation == "authority":
                    value["authorities"]["dataset"]["state"] = "PROHIBITED"
                elif mutation == "mode":
                    value["storage_policy"]["required_mode"] = "0755"
                elif mutation == "parent":
                    value["execution_corpus"][0]["natural_query_id"] = "missing"
                elif mutation == "concurrency":
                    value["execution_corpus"][0]["concurrent_writes"] = True
                else:
                    value["natural_inventory"][0]["inventory_origin"] = "synthetic_control"
                with self.assertRaises(ORACLE.OracleError):
                    ORACLE.validate_qualification(value)

    def test_natural_execution_lineage_and_identifier_spaces_are_closed(self) -> None:
        for mutation in ("missing_parent", "origin_mismatch", "control_collision"):
            with self.subTest(mutation=mutation):
                value = qualification()
                if mutation == "missing_parent":
                    value["execution_corpus"][0]["natural_query_id"] = None
                elif mutation == "origin_mismatch":
                    value["execution_corpus"][0]["executed_sql_origin"] = "natural_customer"
                else:
                    value["synthetic_controls"][0]["query_id"] = "natural-eligible"
                with self.assertRaises(ORACLE.OracleError):
                    ORACLE.validate_qualification(value)

    def test_empty_inventory_is_not_false_coverage(self) -> None:
        value = qualification(); value["natural_inventory"] = []
        value["execution_corpus"] = []
        report = ORACLE.validate_qualification(value)
        self.assertEqual(report["denominator"], 0)
        self.assertIsNone(report["confirmed_ratio"])
        self.assertIsNone(report["upper_ratio"])

    def test_baseline_requires_static_provider_free_identity(self) -> None:
        ORACLE.validate_baseline(baseline())
        for mutation in ("provider", "absence", "static", "writes"):
            with self.subTest(mutation=mutation):
                value = baseline()
                if mutation == "provider": value["provider_identity"] = "ROUTING_OFF"
                elif mutation == "absence": value["provider_absence_verified"] = False
                elif mutation == "static": value["static_data"] = False
                else: value["concurrent_writes"] = True
                with self.assertRaises(ORACLE.OracleError):
                    ORACLE.validate_baseline(value)


class OracleTests(unittest.TestCase):
    columns = [("small", 21), ("regular", 23), ("large", 20)]

    def compare(self, left_rows: list[tuple[object, ...]],
                right_rows: list[tuple[object, ...]], *,
                left_columns: list[tuple[str, int]] | None = None,
                right_columns: list[tuple[str, int]] | None = None,
                rows_in_memory: int = 2) -> dict:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); scratch = root / "scratch"; scratch.mkdir(mode=0o700)
            left = root / "left.spool"; right = root / "right.spool"
            write_spool(left, left_columns or self.columns, left_rows)
            write_spool(right, right_columns or self.columns, right_rows)
            return ORACLE.compare_spools(left, right, scratch,
                max_rows=1000, max_columns=8, max_field_bytes=64,
                max_rows_in_memory=rows_in_memory, max_open_runs=2)

    def test_reordering_null_bounds_and_duplicate_multiplicity_pass(self) -> None:
        rows = [(-32768, None, -(2**63)), (32767, 2**31 - 1, 2**63 - 1),
                (0, -1, 7), (0, -1, 7)]
        report = self.compare(rows, list(reversed(rows)))
        self.assertEqual(report["result"], "PASS")
        self.assertNotEqual(report["baseline_sha256"], report["observed_sha256"])
        self.assertGreater(report["baseline_runs"], 1)

    def test_extra_missing_changed_and_multiplicity_fail(self) -> None:
        baseline_rows = [(1, 2, 3), (1, 2, 3), (4, 5, 6)]
        variants = [baseline_rows[:-1], baseline_rows + [(7, 8, 9)],
                    [(1, 2, 3), (1, 2, 4), (4, 5, 6)],
                    [(1, 2, 3), (4, 5, 6), (4, 5, 6)]]
        for rows in variants:
            with self.subTest(rows=rows):
                self.assertEqual(self.compare(baseline_rows, rows)["result"], "FAIL")

    def test_empty_result_still_compares_column_types_and_order(self) -> None:
        self.assertEqual(self.compare([], [])["result"], "PASS")
        self.assertEqual(self.compare([], [], right_columns=[("small", 23),
            ("regular", 23), ("large", 20)])["reason"], "descriptor_mismatch")
        self.assertEqual(self.compare([], [], right_columns=list(reversed(self.columns)))["result"],
                         "FAIL")

    def test_descriptor_mismatch_does_not_hide_malformed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); scratch = root / "scratch"; scratch.mkdir(mode=0o700)
            left = root / "left.spool"; right = root / "right.spool"
            write_spool(left, [("v", 23)], [(1,)])
            write_spool(right, [("v", 20)], [(1,)])
            right.write_bytes(right.read_bytes()[:-1]); right.chmod(0o600)
            with self.assertRaises(ORACLE.OracleError):
                ORACLE.compare_spools(left, right, scratch)

    def test_null_differs_from_zero_and_text_type_is_refused(self) -> None:
        self.assertEqual(self.compare([(None, 1, 2)], [(0, 1, 2)])["result"], "FAIL")
        with self.assertRaises(ORACLE.OracleError):
            self.compare([(1,)], [(1,)], left_columns=[("value", 25)],
                         right_columns=[("value", 25)])

    def test_malformed_noncanonical_and_out_of_range_values_are_refused(self) -> None:
        for payload in (b"+1", b"01", b"-0", b" 1", b"32768"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as name:
                root = Path(name); scratch = root / "scratch"; scratch.mkdir(mode=0o700)
                valid = root / "valid"; bad = root / "bad"
                write_spool(valid, [("v", 21)], [(1,)])
                data = bytearray(ORACLE.MAGIC) + struct.pack(">I", 1)
                data += struct.pack(">I", 1) + b"v" + struct.pack(">I", 21)
                data += struct.pack(">I", 1) + struct.pack(">I", len(payload)) + payload
                bad.write_bytes(data)
                bad.chmod(0o600)
                with self.assertRaises(ORACLE.OracleError):
                    ORACLE.compare_spools(valid, bad, scratch, max_rows_in_memory=1)

    def test_truncation_bad_magic_width_and_permissive_scratch_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); scratch = root / "scratch"; scratch.mkdir(mode=0o700)
            good = root / "good"; bad = root / "bad"
            write_spool(good, [("v", 23)], [(1,)])
            for content in (good.read_bytes()[:-1], b"not-a-spool",
                            good.read_bytes() + struct.pack(">I", 2)):
                bad.write_bytes(content)
                bad.chmod(0o600)
                with self.assertRaises(ORACLE.OracleError):
                    ORACLE.compare_spools(good, bad, scratch, max_rows_in_memory=1)
            os.chmod(scratch, 0o755)
            with self.assertRaises(ORACLE.OracleError):
                ORACLE.compare_spools(good, good, scratch)

    def test_equal_non_spool_bytes_are_not_hash_only_proof(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); scratch = root / "scratch"; scratch.mkdir(mode=0o700)
            left = root / "left.spool"; right = root / "right.spool"
            left.write_bytes(b"same invalid bytes"); right.write_bytes(b"same invalid bytes")
            left.chmod(0o600); right.chmod(0o600)
            with self.assertRaisesRegex(ORACLE.OracleError, "unsupported spool version"):
                ORACLE.compare_spools(left, right, scratch)


if __name__ == "__main__":
    unittest.main()
