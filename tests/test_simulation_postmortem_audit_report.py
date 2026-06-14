import unittest

from scripts import simulation_postmortem_audit_report as report


def simulation_performance(status="FAIL"):
    return {
        "schema": "simulation_performance_report_v1",
        "status": status,
        "summary": {"portfolio_id": 8},
        "worst_closed_symbols": [
            {"symbol": "LI", "pnl_hkd_est": -265.33, "closed_trade_count": 1},
            {"symbol": "09922", "pnl_hkd_est": -187.12, "closed_trade_count": 1},
        ],
        "open_position_risk": [
            {"symbol": "00929", "priority": "high", "unrealized_pnl_pct": -36.4},
            {"symbol": "00816", "priority": "normal", "unrealized_pnl_pct": -8.1},
        ],
        "failure_postmortem": {
            "schema": "simulation_failure_postmortem_v1",
            "status": "ACTION_REQUIRED",
            "required_learning_record": {
                "schema": "simulation_trade_postmortem_note_requirements_v1",
                "required_fields": ["symbol", "failure_category", "lesson"],
            },
        },
    }


def note(symbol="LI", target_type="closed_trade", **overrides):
    item = {
        "schema": "simulation_trade_postmortem_note_v1",
        "portfolio_id": 8,
        "symbol": symbol,
        "target_type": target_type,
        "reviewed_at": "2026-06-14T00:20:00",
        "reviewer": "hermes",
        "read_only": True,
        "submits_orders": False,
        "changes_strategy": False,
        "changes_portfolio": False,
        "auto_apply": False,
        "closed_at": "2026-06-12" if target_type == "closed_trade" else "open_position",
        "entry_signal_id_or_trade_id": "trade-1",
        "exit_reason": "stop_loss" if target_type == "closed_trade" else "open_position_not_closed",
        "failure_category": "entry_timing",
        "market_context_status": "risk_off",
        "intraday_context_status": "CLOSED",
        "event_or_news_context_ids": [],
        "fundamentals_context_status": "partial",
        "source_reliability_status": "DEGRADED",
        "lesson": "entry followed weak context and should be held for review",
        "proposed_change": "none",
        "promotion_gate": "manual_and_hash_confirmed_before_strategy_or_watchlist_change",
    }
    item.update(overrides)
    return item


class SimulationPostmortemAuditReportTests(unittest.TestCase):
    def test_ok_when_postmortem_not_required(self):
        payload = report.build_report(
            {
                "schema": "simulation_performance_report_v1",
                "status": "OK",
                "worst_closed_symbols": [],
                "open_position_risk": [],
            },
            [],
        )

        self.assertEqual(payload["schema"], "simulation_postmortem_audit_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["coverage"]["required_target_count"], 0)
        self.assertIn("simulation_postmortem_notes_not_required", payload["recommendations"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_strategy"])

    def test_missing_required_notes_warns(self):
        payload = report.build_report(simulation_performance(), [])

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["coverage"]["required_target_count"], 3)
        self.assertEqual(payload["coverage"]["missing_target_count"], 3)
        self.assertIn("write_simulation_postmortem_notes:3", payload["recommendations"])
        target_ids = [row["target_id"] for row in payload["missing_required_targets"]]
        self.assertIn("closed_trade:LI", target_ids)
        self.assertIn("closed_trade:09922", target_ids)
        self.assertIn("open_position:00929", target_ids)
        self.assertIn("failure_category", payload["note_contract"]["required_fields"])

    def test_complete_notes_cover_required_targets(self):
        payload = report.build_report(
            simulation_performance(),
            [
                note("LI", "closed_trade"),
                note("09922", "closed_trade"),
                note("00929", "open_position"),
            ],
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["coverage"]["required_target_count"], 3)
        self.assertEqual(payload["coverage"]["covered_target_count"], 3)
        self.assertEqual(payload["coverage"]["missing_target_count"], 0)
        self.assertEqual(payload["coverage"]["failed_note_count"], 0)
        self.assertIn("simulation_postmortem_notes_cover_required_targets", payload["recommendations"])

    def test_unsafe_or_incomplete_note_fails(self):
        bad = note(
            "LI",
            "closed_trade",
            lesson="",
            submits_orders=True,
            changes_strategy=True,
            promotion_gate="auto_apply",
        )

        payload = report.build_report(simulation_performance(), [bad])
        row = payload["note_audits"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("required_field_missing:lesson", row["reasons"])
        self.assertIn("submits_orders_must_be_false", row["reasons"])
        self.assertIn("changes_strategy_must_be_false", row["reasons"])
        self.assertIn("promotion_gate_must_require_manual_or_hash_review", row["reasons"])
        self.assertIn("repair_invalid_simulation_postmortem_notes:1", payload["recommendations"])

    def test_unreplaced_draft_template_cannot_pass(self):
        bad = note(
            "LI",
            "closed_trade",
            reviewed_at="<replace: reviewed ISO datetime>",
            entry_signal_id_or_trade_id="<replace: trade id or unknown>",
            market_context_status="reviewed market context status",
            event_or_news_context_ids=["<replace: event ids or empty list>"],
            lesson="<replace: concrete lesson>",
            draft_only=True,
        )

        payload = report.build_report(simulation_performance(), [bad])
        row = payload["note_audits"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("placeholder_value_not_replaced:reviewed_at", row["reasons"])
        self.assertIn("placeholder_value_not_replaced:entry_signal_id_or_trade_id", row["reasons"])
        self.assertIn("placeholder_value_not_replaced:market_context_status", row["reasons"])
        self.assertIn("placeholder_value_not_replaced:event_or_news_context_ids", row["reasons"])
        self.assertIn("placeholder_value_not_replaced:lesson", row["reasons"])
        self.assertIn("draft_only_note_cannot_pass_audit", row["reasons"])

    def test_unmatched_note_does_not_cover_required_target(self):
        payload = report.build_report(simulation_performance(), [note("TSLA", "closed_trade")])

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("note_does_not_match_required_target", payload["note_audits"][0]["reasons"])
        self.assertEqual(payload["coverage"]["missing_target_count"], 3)


if __name__ == "__main__":
    unittest.main()
