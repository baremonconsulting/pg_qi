import unittest

from tools.phase7.count_certificate import MAX_LIFETIME, record
from tools.phase7.count_matrix import CASES, case_run_id, site_binding


class CountCertificateRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.started = 1_795_000_000
        self.completed = self.started + 2
        self.site = {
            "postgres_version": "19.0", "cluster_identifier": "12345",
            "site_build_id": "1" * 64, "postgres_build_id": "2" * 64,
            "pg_qi_revision": "3" * 40, "acquired_at_unix": self.started,
            "binary_sha256": {name: prefix * 64 for name, prefix in
                              (("postgres", "4"), ("pg_qi.so", "5"),
                               ("pg_duckdb.so", "6"), ("libduckdb.so", "7"))},
        }
        self.evidence = {"scope": "count_star_serial_rr_ro",
                         "identity": self.site, "completed_at_unix": self.completed}

    def test_record_binds_scope_and_binary_identity(self) -> None:
        proposal = record(self.evidence, "8" * 64, self.completed, self.started + MAX_LIFETIME)
        self.assertTrue(proposal.startswith("v=1;scope=count_star_serial_rr_ro;pg=190000;cluster=12345;"))
        self.assertIn(";qi_bin=" + "5" * 64 + ";", proposal)
        self.assertIn(";proof=" + "8" * 64 + ";", proposal)

    def test_record_cannot_predate_or_extend_evidence(self) -> None:
        for dates in ((self.started, self.completed + 1),
                      (self.completed, self.completed),
                      (self.completed, self.started + MAX_LIFETIME + 1),
                      (self.started + MAX_LIFETIME, self.started + MAX_LIFETIME + 1)):
            with self.subTest(dates=dates), self.assertRaises(ValueError):
                record(self.evidence, "8" * 64, *dates)

    def test_live_binding_includes_cluster_site_major_minor_and_postmaster(self) -> None:
        fields = ["postgres_major=16", "postgres_version=16.16", "pg_qi_revision=unused",
                  "postgres_build_id=unused", "12345", "a" * 64, "0", "0",
                  "acquired_at_unix=100", "postmaster_start_us=100000001", "a", "b", "c", "d"]
        self.assertEqual(site_binding("\n".join(fields).encode()), f"12345:{'a' * 64}:160016:100000001")
        fields[9] = "postmaster_start_us=0"
        with self.assertRaises(ValueError):
            site_binding("\n".join(fields).encode())

    def test_subject_binding_includes_fixture_and_identity(self) -> None:
        name = "count-matrix-pg19.fakeRun1"
        case = CASES[0]
        witness = case_run_id(name, "a" * 64, {"baseline_count": 0}, case)
        self.assertNotEqual(witness, case_run_id(name, "a" * 64, {"baseline_count": 1}, case))
        self.assertNotEqual(witness, case_run_id(name, "b" * 64, {"baseline_count": 0}, case))


if __name__ == "__main__":
    unittest.main()
