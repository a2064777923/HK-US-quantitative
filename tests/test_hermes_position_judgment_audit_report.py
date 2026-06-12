import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts import hermes_position_judgment_audit_report as audit


def position_item(review_id="simulation:8:00929:2026-06-12:reduce_or_exit_review", **extra):
    item = {
        "review_id": review_id,
        "portfolio_id": 8,
        "role": "simulation",
        "symbol": "00929",
        "urgency": "medium",
        "recommended_action": "risk_review",
        "execution_policy": {
            "advice_only": False,
            "review_only": True,
            "submits_orders": False,
            "requires_separate_order_path": True,
        },
    }
    item.update(extra)
    return item


def packet(items=None, packet_id="packet-1"):
    return {
        "schema": "hermes_signal_review_packet_v1",
        "packet_id": packet_id,
        "generated_at": "2026-06-12T10:00:00",
        "position_review": {
            "schema": "portfolio_position_review_v1",
            "review_only": True,
            "submits_orders": False,
            "items": items if items is not None else [position_item()],
        },
    }


def judgment(review_id="simulation:8:00929:2026-06-12:reduce_or_exit_review", decision="watch", **extra):
    item = {
        "schema": "hermes_position_judgment_v1",
        "packet_id": "packet-1",
        "review_id": review_id,
        "portfolio_id": 8,
        "role": "simulation",
        "symbol": "00929",
        "decision": decision,
        "confidence": 0.82,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "advisory_only": True,
        "submits_orders": False,
        "supporting_factors": ["unit test support"],
        "opposing_factors": ["unit test opposition"],
        "risk_notes": ["unit test risk"],
    }
    item.update(extra)
    return item


class HermesPositionJudgmentAuditReportTests(unittest.TestCase):
    def test_no_position_judgments_is_not_a_failure(self):
        payload = audit.build_report([], packet())

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["counts"]["judgment_count"], 0)
        self.assertEqual(payload["recommendations"], ["no_position_judgments_observed_yet"])

    def test_clean_position_judgment_passes_against_packet_item(self):
        payload = audit.build_report([judgment()], packet())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])
        self.assertEqual(payload["recommendations"], ["position_judgment_audit_clean_continue_advisory_review"])

    def test_orphan_review_id_is_flagged(self):
        payload = audit.build_report([judgment("missing-review")], packet())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("orphan_position_judgment_not_in_packet", row["reasons"])

    def test_missing_advisory_flags_are_flagged(self):
        item = judgment(advisory_only=False, submits_orders=True)

        payload = audit.build_report([item], packet())
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
        self.assertIn("advisory_only_must_be_true", row["reasons"])
        self.assertIn("submits_orders_must_be_false", row["reasons"])

    def test_user_action_decision_is_flagged_as_advice_only_violation(self):
        review_id = "user:7:AAPL:2026-06-12:risk_review"
        packet_payload = packet(
            [
                position_item(
                    review_id,
                    portfolio_id=7,
                    role="user",
                    symbol="AAPL",
                    execution_policy={
                        "advice_only": True,
                        "review_only": True,
                        "submits_orders": False,
                        "requires_separate_order_path": True,
                    },
                )
            ]
        )
        item = judgment(review_id, decision="reduce", portfolio_id=7, role="user", symbol="AAPL")

        payload = audit.build_report([item], packet_payload)
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
        self.assertIn("user_position_decision_must_remain_advice_only", row["reasons"])

    def test_high_urgency_hold_watch_requires_stronger_rationale(self):
        review_id = "simulation:8:00929:2026-06-12:reduce_or_exit_review"
        packet_payload = packet(
            [
                position_item(
                    review_id,
                    urgency="high",
                    recommended_action="reduce_or_exit_review",
                )
            ]
        )

        payload = audit.build_report([judgment(review_id, decision="hold")], packet_payload)
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
        self.assertIn("high_urgency_hold_or_watch_requires_strong_rationale", row["reasons"])
        self.assertIn("high_urgency_hold_missing_opposing_detail", row["reasons"])

    def test_high_urgency_hold_watch_can_pass_with_explicit_rationale(self):
        review_id = "simulation:8:00929:2026-06-12:reduce_or_exit_review"
        packet_payload = packet(
            [
                position_item(
                    review_id,
                    urgency="high",
                    recommended_action="reduce_or_exit_review",
                )
            ]
        )
        item = judgment(
            review_id,
            decision="watch",
            opposing_factors=["support held above stop", "liquidity risk makes immediate exit worse"],
            risk_notes=["review again next session", "do not add exposure before review"],
        )

        payload = audit.build_report([item], packet_payload)
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_packet_id_uses_archived_packet_instead_of_latest_packet(self):
        archived = packet([position_item("simulation:8:00177:2026-06-12:risk_review", symbol="00177")], "archived-packet")
        latest = packet([position_item("simulation:8:00929:2026-06-12:risk_review")], "latest-packet")
        item = judgment(
            "simulation:8:00177:2026-06-12:risk_review",
            packet_id="archived-packet",
            symbol="00177",
        )

        with tempfile.TemporaryDirectory() as td:
            archive_path = Path(td) / "archived-packet.json"
            archive_path.write_text(json.dumps(archived), encoding="utf-8")

            payload = audit.build_report([item], latest, packet_archive_dir=td)

        row = payload["judgments"][0]
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["packet_source"], "packet_archive")
        self.assertEqual(row["reasons"], [])

    def test_duplicate_review_judgments_are_flagged(self):
        items = [judgment(), judgment(decision="hold")]

        payload = audit.build_report(items, packet())

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["counts"]["status_counts"]["FAIL"], 2)
        self.assertIn("duplicate_position_judgments_for_review", payload["counts"]["reason_counts"])


if __name__ == "__main__":
    unittest.main()
