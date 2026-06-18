import unittest

from scripts import simulation_postmortem_audit_report as audit
from scripts import simulation_postmortem_note_draft_report as report


def simulation_performance(status="FAIL"):
    return {
        "schema": "simulation_performance_report_v1",
        "status": status,
        "summary": {
            "portfolio_id": 8,
            "return_pct_vs_initial": -5.9,
            "closed_win_rate_pct": 14.29,
            "closed_pnl_hkd_est": -933.38,
        },
        "worst_closed_symbols": [
            {"symbol": "LI", "pnl_hkd_est": -265.33, "closed_trade_count": 1, "win_rate_pct": 0.0},
        ],
        "open_position_risk": [
            {
                "symbol": "00929",
                "name": "国际精密",
                "market": "HK",
                "priority": "high",
                "unrealized_pnl_pct": -36.4,
                "recommendation": "hold",
            }
        ],
        "failure_postmortem": {
            "schema": "simulation_failure_postmortem_v1",
            "status": "ACTION_REQUIRED",
            "required_learning_record": {
                "schema": "simulation_trade_postmortem_note_requirements_v1",
                "required_fields": [
                    "symbol",
                    "entry_order_id",
                    "signal_lineage_status",
                    "next_evidence_required",
                ],
            },
        },
    }


def postmortem_audit_payload():
    return {
        "schema": "simulation_postmortem_audit_report_v1",
        "status": "WARN",
        "coverage": {"required_target_count": 2, "missing_target_count": 2},
        "missing_required_targets": [
            {
                "target_id": "closed_trade:LI",
                "target_type": "closed_trade",
                "symbol": "LI",
                "reason": "worst_closed_symbol_negative_pnl",
                "evidence": {"symbol": "LI", "pnl_hkd_est": -265.33, "entry_order_ids": ["ord-li-1"]},
            },
            {
                "target_id": "open_position:00929",
                "target_type": "open_position",
                "symbol": "00929",
                "reason": "high_priority_open_position_risk",
                "evidence": {"symbol": "00929", "unrealized_pnl_pct": -36.4},
            },
        ],
    }


class SimulationPostmortemNoteDraftReportTests(unittest.TestCase):
    def test_builds_read_only_drafts_for_missing_targets(self):
        payload = report.build_report(
            simulation_performance=simulation_performance(),
            simulation_postmortem_audit=postmortem_audit_payload(),
            market_context={"status": "RISK"},
            intraday_context={"status": "CLOSED"},
            external_market_context={
                "status": "RISK",
                "items": [
                    {
                        "id": "news-li-1",
                        "title": "LI earnings warning",
                        "symbol": "LI",
                        "sentiment": "negative",
                        "impact_score": 0.9,
                    },
                    {
                        "id": "false-li-substring",
                        "title": "Reliability rally does not mention the ticker structurally",
                        "url": "https://example.test/reliability",
                        "sentiment": "positive",
                    }
                ],
            },
            event_catalysts={"status": "RISK"},
            market_sentiment={"status": "OK"},
            fundamentals_context={"status": "RISK"},
            source_reliability={"status": "DEGRADED"},
        )

        self.assertEqual(payload["schema"], "simulation_postmortem_note_draft_report_v1")
        self.assertEqual(payload["status"], "ACTION_REQUIRED")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["writes_note_file"])
        self.assertEqual(payload["summary"]["draft_count"], 2)
        closed = payload["drafts"][0]
        self.assertTrue(closed["draft_only"])
        self.assertEqual(closed["schema"], "simulation_trade_postmortem_note_v1")
        self.assertEqual(closed["portfolio_id"], 8)
        self.assertEqual(closed["symbol"], "LI")
        self.assertEqual(closed["market_context_status"], "RISK")
        self.assertEqual(closed["intraday_context_status"], "CLOSED")
        self.assertEqual(closed["fundamentals_context_status"], "RISK")
        self.assertEqual(closed["source_reliability_status"], "DEGRADED")
        self.assertEqual(closed["event_or_news_context_ids"], ["news-li-1"])
        self.assertEqual(closed["entry_order_id"], "ord-li-1")
        self.assertEqual(closed["signal_lineage_status"], "UNKNOWN")
        self.assertEqual(
            closed["next_evidence_required"],
            "lineage_qualified_v5_closed_trade_sample_and_forward_outcome_recovery",
        )
        self.assertIn("<replace:", closed["reviewed_at"])
        self.assertTrue(payload["append_instructions"]["manual_only"])
        self.assertTrue(payload["append_instructions"]["remove_draft_only_before_append"])

    def test_draft_object_cannot_pass_audit_unchanged(self):
        payload = report.build_report(
            simulation_performance=simulation_performance(),
            simulation_postmortem_audit=postmortem_audit_payload(),
        )

        audit_payload = audit.build_report(simulation_performance(), [payload["drafts"][0]])
        reasons = audit_payload["note_audits"][0]["reasons"]

        self.assertEqual(audit_payload["status"], "FAIL")
        self.assertIn("draft_only_note_cannot_pass_audit", reasons)
        self.assertIn("placeholder_value_not_replaced:reviewed_at", reasons)
        self.assertIn("placeholder_value_not_replaced:lesson", reasons)

    def test_no_drafts_when_audit_has_no_missing_targets(self):
        payload = report.build_report(
            simulation_performance={"schema": "simulation_performance_report_v1", "status": "OK"},
            simulation_postmortem_audit={
                "schema": "simulation_postmortem_audit_report_v1",
                "status": "OK",
                "missing_required_targets": [],
            },
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["draft_count"], 0)
        self.assertIn("simulation_postmortem_note_drafts_not_required", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
