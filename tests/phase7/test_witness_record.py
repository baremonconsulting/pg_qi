#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/phase7"))

from witness_record import WitnessError, parse_witness


POSTGRES = (
    "v=1;seq=2;status=success;engine=postgres;token=" + "a" * 64 +
    ";pid=123;db=5;user=10;meta=20:-1:0;columns=1;"
    "truncated=f;custom_nodes=0;custom_completed=0;duckdb_present=f"
)
DUCKDB = (
    "v=1;seq=4;status=success;engine=duckdb;token=" + "a" * 64 +
    ";pid=123;db=5;user=10;meta=25:-1:100;columns=1;"
    "truncated=f;custom_nodes=1;custom_completed=1;duckdb_present=t"
)
ERROR = (
    "v=1;seq=5;status=error;engine=unknown;token=" + "a" * 64 +
    ";pid=123;db=5;user=10;meta=23:-1:0;columns=1;"
    "truncated=f;custom_nodes=1;custom_completed=0;duckdb_present=t"
)


class WitnessRecordTest(unittest.TestCase):
    def test_valid_native_duckdb_and_error_records(self) -> None:
        self.assertEqual(parse_witness(POSTGRES).engine, "postgres")
        duckdb = parse_witness(DUCKDB)
        self.assertEqual(duckdb.engine, "duckdb")
        self.assertEqual(duckdb.columns[0].collation, 100)
        self.assertEqual(parse_witness(ERROR).status, "error")
        self.assertTrue(parse_witness(
            DUCKDB.replace("truncated=f", "truncated=t")).truncated)

    def test_empty_initial_record_is_exact(self) -> None:
        value = ("v=1;seq=0;status=none;engine=unknown;token=" + "0" * 64 +
                 ";pid=0;db=0;user=0;meta=;columns=0;"
                 "truncated=f;custom_nodes=0;custom_completed=0;duckdb_present=f")
        self.assertEqual(parse_witness(value).sequence, 0)

    def test_mutations_fail_closed(self) -> None:
        mutations = [
            POSTGRES + ";extra=x",
            POSTGRES.replace("v=1", "v=2", 1),
            POSTGRES.replace("seq=2", "seq=02"),
            POSTGRES.replace("engine=postgres", "engine=duckdb"),
            POSTGRES.replace("custom_nodes=0", "custom_nodes=1"),
            DUCKDB.replace("custom_completed=1", "custom_completed=0"),
            DUCKDB.replace("duckdb_present=t", "duckdb_present=f"),
            DUCKDB.replace("columns=1", "columns=2"),
            DUCKDB.replace("truncated=f", "truncated=x"),
            DUCKDB.replace("25:-1:100", "25:+1:100"),
            DUCKDB.replace("meta=25:-1:100", "meta=0:-1:100"),
            DUCKDB.replace("status=success", "status=future"),
            DUCKDB.replace("v=1;seq=4", "seq=4;v=1"),
            DUCKDB + "\N{SNOWMAN}",
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(WitnessError):
                parse_witness(mutation)


if __name__ == "__main__":
    unittest.main()
