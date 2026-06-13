import unittest

from scripts import simulation_performance_report as report


def portfolio_payload(return_pct=2.0, closed_pnl=800.0, win_rate=66.67, notes=None, risk_level="low"):
    return {
        "schema": "portfolio_context_report_v1",
        "generated_at": "2026-06-12T10:00:00",
        "portfolio_reports": [
            {
                "portfolio_id": 8,
                "role": "simulation",
                "total_value_hkd": 102000,
                "return_pct_vs_initial": return_pct,
                "position_count": 1,
                "high_priority_count": 0,
                "positions": [
                    {
                        "symbol": "00700",
                        "name": "Tencent",
                        "quantity": 100,
                        "market_value_hkd": 30000,
                        "unrealized_pnl_hkd": 500,
                        "unrealized_pnl_pct": 1.7,
                        "priority": "normal",
                        "recommendation": "hold",
                        "recommendation_reasons": ["latest_signal_hold"],
                        "signal": {"side": "HOLD"},
                    }
                ],
            }
        ],
        "portfolio_risk": {
            "reports": [
                {
                    "role": "simulation",
                    "risk_level": risk_level,
                    "risk_flags": [],
                }
            ]
        },
        "simulation_trade_review": {
            "portfolio_id": 8,
            "closed_trade_count": 3,
            "closed_win_rate_pct": win_rate,
            "closed_pnl_hkd_est": closed_pnl,
            "review_notes": notes or [],
            "recent_closed": [
                {
                    "symbol": "00700",
                    "pnl_hkd_est": 500,
                    "entry_order_ids": ["ord-buy-00700-a"],
                    "exit_order_id": "ord-sell-00700-a",
                    "closed_at": "2026-06-12T10:00:00",
                },
                {
                    "symbol": "09988",
                    "pnl_hkd_est": -100,
                    "entry_order_ids": ["ord-buy-09988-a"],
                    "exit_order_id": "ord-sell-09988-a",
                    "closed_at": "2026-06-12T10:05:00",
                },
                {
                    "symbol": "00700",
                    "pnl_hkd_est": 400,
                    "entry_order_ids": ["ord-buy-00700-b"],
                    "exit_order_id": "ord-sell-00700-b",
                    "closed_at": "2026-06-12T10:10:00",
                },
            ],
        },
    }


def processed_decision(signal_id, order_id, symbol="00700", side="BUY"):
    return {
        "signal_id": signal_id,
        "status": "submitted",
        "mode": "execute",
        "order_result": {"order_id": order_id},
        "alert": {
            "symbol": symbol,
            "signal_type": side,
            "trigger": "unit-test",
            "full_score": 0.7,
            "generated_at": "2026-06-12T09:55:00",
        },
        "hermes": {
            "status": "APPROVED",
            "decision": "approve",
            "judgment": {"decision": "approve", "confidence": 0.8},
        },
        "checked_at": "2026-06-12T09:56:00",
        "submitted_at": "2026-06-12T09:57:00",
    }


def traceable_order_state():
    return {
        "processed": {
            "sig-buy-00700-a": processed_decision("sig-buy-00700-a", "ord-buy-00700-a"),
            "sig-sell-00700-a": processed_decision("sig-sell-00700-a", "ord-sell-00700-a", side="SELL"),
            "sig-buy-09988-a": processed_decision("sig-buy-09988-a", "ord-buy-09988-a", symbol="09988"),
            "sig-sell-09988-a": processed_decision("sig-sell-09988-a", "ord-sell-09988-a", symbol="09988", side="SELL"),
            "sig-buy-00700-b": processed_decision("sig-buy-00700-b", "ord-buy-00700-b"),
            "sig-sell-00700-b": processed_decision("sig-sell-00700-b", "ord-sell-00700-b", side="SELL"),
        },
        "dry_runs": {},
    }


class SimulationPerformanceReportTests(unittest.TestCase):
    def test_ok_when_simulation_return_and_closed_trades_are_positive(self):
        payload = report.build_report(portfolio_payload(), order_state_payload=traceable_order_state())

        self.assertEqual(payload["schema"], "simulation_performance_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertEqual(payload["closed_trade_attribution_by_symbol"][0]["symbol"], "09988")
        self.assertEqual(payload["closed_trade_signal_traceability"]["status"], "OK")
        self.assertEqual(payload["closed_trade_signal_traceability"]["fully_traceable_count"], 3)
        self.assertEqual(payload["summary"]["closed_trade_entry_traceable_pct"], 100.0)
        self.assertEqual(payload["remediation_plan"]["status"], "not_required")
        self.assertFalse(payload["remediation_plan"]["auto_applied"])
        self.assertFalse(payload["remediation_plan"]["operator_contract"]["submits_orders"])

    def test_failed_when_recent_simulation_trades_are_losing(self):
        payload = report.build_report(
            portfolio_payload(
                return_pct=-5.9,
                closed_pnl=-933.38,
                win_rate=14.29,
                notes=["recent_closed_trades_negative", "loss_rate_above_60pct"],
                risk_level="high",
            ),
            order_state_payload={"processed": {}, "dry_runs": {"sig-shadow": {"status": "dry_run"}}},
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("simulation_total_return_not_positive", payload["reason_codes"])
        self.assertIn("simulation_closed_pnl_not_positive", payload["reason_codes"])
        self.assertIn("simulation_trade_review_blocking_notes", payload["reason_codes"])
        self.assertIn("closed_trade_signal_traceability_missing", payload["reason_codes"])
        self.assertEqual(payload["closed_trade_signal_traceability"]["status"], "MISSING")
        postmortem = payload["failure_postmortem"]
        self.assertEqual(postmortem["schema"], "simulation_failure_postmortem_v1")
        self.assertEqual(postmortem["status"], "ACTION_REQUIRED")
        self.assertTrue(postmortem["read_only"])
        self.assertFalse(postmortem["submits_orders"])
        self.assertFalse(postmortem["changes_strategy"])
        hypothesis_ids = [row["id"] for row in postmortem["hypotheses"]]
        self.assertIn("entry_filter_or_signal_quality_weak", hypothesis_ids)
        self.assertIn("closed_trade_signal_lineage_missing", hypothesis_ids)
        self.assertIn("loss_concentration_requires_symbol_postmortem", hypothesis_ids)
        self.assertIn("portfolio_level_recovery_not_proven", hypothesis_ids)
        self.assertIn("failure_category", postmortem["required_learning_record"]["required_fields"])
        self.assertIn("keep_alert_sim_disabled_until_simulation_performance_recovers", payload["recommendations"])
        self.assertIn("repair_sim_trade_signal_lineage_before_strategy_tuning", payload["recommendations"])
        remediation = payload["remediation_plan"]
        self.assertEqual(remediation["schema"], "simulation_strategy_remediation_v1")
        self.assertEqual(remediation["status"], "operator_review_required")
        self.assertEqual(len(remediation["proposal_hash"]), 16)
        self.assertTrue(remediation["manual_review_required"])
        self.assertFalse(remediation["auto_applied"])
        self.assertFalse(remediation["operator_contract"]["submits_orders"])
        self.assertFalse(remediation["operator_contract"]["changes_execution_mode"])
        self.assertFalse(remediation["operator_contract"]["changes_strategy_config"])
        self.assertIn(
            "keep_alert_sim_disabled",
            [action["action_id"] for action in remediation["actions"]],
        )
        self.assertIn(
            "reject_or_hold_new_buy_by_default",
            [action["action_id"] for action in remediation["actions"]],
        )
        self.assertIn(
            "repair_closed_trade_signal_lineage",
            [action["action_id"] for action in remediation["actions"]],
        )

    def test_high_risk_without_losing_trades_is_warning(self):
        payload = report.build_report(
            portfolio_payload(risk_level="high"),
            order_state_payload=traceable_order_state(),
        )

        self.assertEqual(payload["status"], "WARN")
        self.assertIn("simulation_portfolio_risk_high", payload["reason_codes"])
        self.assertEqual(payload["remediation_plan"]["status"], "operator_review_required")
        self.assertIn(
            "keep_strategy_changes_manual_and_shadow_only",
            [action["action_id"] for action in payload["remediation_plan"]["actions"]],
        )

    def test_positive_simulation_without_signal_lineage_fails(self):
        payload = report.build_report(
            portfolio_payload(),
            order_state_payload={"processed": {}, "dry_runs": {"sig-1": {"status": "dry_run"}}},
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("closed_trade_signal_traceability_missing", payload["reason_codes"])
        self.assertEqual(payload["closed_trade_signal_traceability"]["processed_decision_count"], 0)
        self.assertEqual(payload["closed_trade_signal_traceability"]["dry_run_decision_count"], 1)
        self.assertIn("repair_sim_trade_signal_lineage_before_strategy_tuning", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
