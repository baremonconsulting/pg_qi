#!/usr/bin/env python3

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/phase7"))

from decision_snapshot import SnapshotError, parse_snapshot, parse_v1, parse_v2


COMPLETE = (
    "v=2;sv=1;s=complete;cid=0123456789abcdef0123456789abcdef;g=1;e=1;"
    "mode=shadow_p0;enabled=on;context=proven_client;eligible=not_evaluated;"
    "safe=not_evaluated;beneficial=not_evaluated;barrier=closed;"
    "recommendation=insufficient_evidence;reason=0;rule=1;model=1;"
    "certificate=1;policy=pass_through;force=off;engine=unknown;"
    "verification=unverified;observation=no_authoritative_match;"
    "p0=bounded;p0_reason=ok"
)


class SnapshotReaderTest(unittest.TestCase):
    def test_complete_and_none_v2(self):
        self.assertEqual(parse_v2(COMPLETE)["mode"], "shadow_p0")
        none = "v=2;sv=1;s=none;cid=0123456789abcdef0123456789abcdef;g=2;e=0"
        self.assertEqual(parse_v2(none)["s"], "none")
        with self.assertRaises(SnapshotError):
            parse_v2(none.replace("v=2;sv=1", "sv=1;v=2"))

    def test_non_evidence_states_are_minimal(self):
        for state in ("denied", "withheld", "unavailable", "exhausted", "invalid"):
            self.assertEqual(parse_v2(f"v=2;sv=1;s={state}")["s"], state)
            with self.assertRaises(SnapshotError):
                parse_v2(f"v=2;sv=1;s={state};cid=secret")

    def test_v2_mutations_fail_closed(self):
        mutations = [
            COMPLETE + ";extra=x",
            COMPLETE.replace("v=2", "v=3", 1),
            COMPLETE.replace("reason=0", "reason=900"),
            COMPLETE.replace("context=proven_client", "context=nested"),
            COMPLETE.replace("e=1", "e=-1"),
            COMPLETE.replace("mode=shadow_p0", "mode=future"),
            COMPLETE.replace("p0=bounded", "p0=bounded;p0=unknown"),
            COMPLETE.replace("recommendation=insufficient_evidence",
                             "recommendation=would_delegate"),
            COMPLETE.replace("reason=0", "reason=2147483648"),
            COMPLETE.replace("rule=1", "rule=2147483648"),
            COMPLETE.replace("v=2;sv=1;s=complete", "sv=1;v=2;s=complete"),
            COMPLETE.replace("verification=unverified", "verification=verified"),
            COMPLETE.replace("p0_reason=ok", "p0_reason=no_query"),
            COMPLETE.replace("policy=pass_through", "policy=request_delegation"),
            COMPLETE.replace("force=off", "force=qi_stack"),
            COMPLETE.replace(";g=1;e=1;", ";g=01;e=1;"),
        ]
        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(SnapshotError):
                    parse_v2(value)

    def test_unknown_reason_is_only_audit_data(self):
        value = COMPLETE.replace("reason=0", "reason=12345")
        self.assertEqual(parse_v2(value)["reason"], "12345")
        with self.assertRaises(SnapshotError):
            parse_v2(value.replace("eligible=not_evaluated", "eligible=veto"))

    def test_fail_closed_p0_reasons_are_audit_data(self):
        for reason in ("unsupported_shape", "unsupported_node",
                       "unsupported_relation", "catalog_mismatch"):
            value = COMPLETE.replace(
                "p0=bounded;p0_reason=ok",
                "p0=unknown;p0_reason=" + reason)
            with self.subTest(reason=reason):
                parsed = parse_v2(value)
                self.assertEqual(parsed["p0"], "unknown")
                self.assertEqual(parsed["p0_reason"], reason)

    def test_v1_is_strict_and_never_a_v2_fallback(self):
        value = {
            "pg_qi.shadow_contract_version": "1",
            "pg_qi.shadow_decision_id": "7",
            "pg_qi.shadow_context": "proven_client",
            "pg_qi.shadow_policy_intent": "pass_through",
            "pg_qi.shadow_reason": "policy_missing",
            "pg_qi.shadow_force_origin": "off",
            "pg_qi.shadow_classifier_status": "candidate",
            "pg_qi.shadow_classifier_reason": "candidate",
            "pg_qi.shadow_observed_engine": "unknown",
            "pg_qi.shadow_verification": "unverified",
            "pg_qi.shadow_observation": "no_authoritative_match",
            "pg_qi.shadow_request_applied": "off",
        }
        self.assertEqual(parse_v1(value)["pg_qi.shadow_decision_id"], 7)
        bad = copy.copy(value); bad["extra"] = "x"
        with self.assertRaises(SnapshotError): parse_v1(bad)
        bad = copy.copy(value); bad["pg_qi.shadow_reason"] = "future"
        with self.assertRaises(SnapshotError): parse_v1(bad)
        bad = copy.copy(value); bad["pg_qi.shadow_decision_id"] = "0" * 300 + "7"
        with self.assertRaises(SnapshotError): parse_v1(bad)
        bad = copy.copy(value); bad["pg_qi.shadow_reason"] = "delegation_verified"
        with self.assertRaises(SnapshotError): parse_v1(bad)
        bad = copy.copy(value); bad["pg_qi.shadow_reason"] = "delegation_declined"
        with self.assertRaises(SnapshotError): parse_v1(bad)
        bad = copy.copy(value)
        bad.update({
            "pg_qi.shadow_reason": "delegation_verified",
            "pg_qi.shadow_policy_intent": "request_delegation",
            "pg_qi.shadow_force_origin": "qi_stack",
            "pg_qi.shadow_observed_engine": "duckdb",
            "pg_qi.shadow_verification": "verified",
            "pg_qi.shadow_observation": "duckdb_scan_at_downstream_boundary",
            "pg_qi.shadow_request_applied": "on",
            "pg_qi.shadow_context": "unproved_outermost",
            "pg_qi.shadow_classifier_status": "rejected",
            "pg_qi.shadow_classifier_reason": "security_context",
        })
        with self.assertRaises(SnapshotError): parse_v1(bad)
        bad = copy.copy(value); bad["pg_qi.shadow_decision_id"] = "2147483648"
        with self.assertRaises(SnapshotError): parse_v1(bad)
        with self.assertRaises(SnapshotError):
            parse_snapshot(version=2, v1=value, v2="v=3;sv=1;s=denied")


if __name__ == "__main__":
    unittest.main()
