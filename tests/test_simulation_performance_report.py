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
                {"symbol": "00700", "pnl_hkd_est": 500},
                {"symbol": "09988", "pnl_hkd_est": -100},
                {"symbol": "00700", "pnl_hkd_est": 400},
            ],
        },
    }


class SimulationPerformanceReportTests(unittest.TestCase):
    def test_ok_when_simulation_return_and_closed_trades_are_positive(self):
        payload = report.build_report(portfolio_payload())

        self.assertEqual(payload["schema"], "simulation_performance_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertEqual(payload["closed_trade_attribution_by_symbol"][0]["symbol"], "09988")

    def test_failed_when_recent_simulation_trades_are_losing(self):
        payload = report.build_report(
            portfolio_payload(
                return_pct=-5.9,
                closed_pnl=-933.38,
                win_rate=14.29,
                notes=["recent_closed_trades_negative", "loss_rate_above_60pct"],
                risk_level="high",
            )
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("simulation_total_return_not_positive", payload["reason_codes"])
        self.assertIn("simulation_closed_pnl_not_positive", payload["reason_codes"])
        self.assertIn("simulation_trade_review_blocking_notes", payload["reason_codes"])
        self.assertIn("keep_alert_sim_disabled_until_simulation_performance_recovers", payload["recommendations"])

    def test_high_risk_without_losing_trades_is_warning(self):
        payload = report.build_report(portfolio_payload(risk_level="high"))

        self.assertEqual(payload["status"], "WARN")
        self.assertIn("simulation_portfolio_risk_high", payload["reason_codes"])


if __name__ == "__main__":
    unittest.main()
