import unittest
from datetime import datetime

from scripts import execution_readiness_report as report


NOW = datetime(2026, 6, 12, 10, 30, 0)
FRESH_TIME = "2026-06-12T10:00:00"
STALE_TIME = "2026-06-12T08:00:00"


def healthy_inputs():
    return {
        "system_health": {"status": "OK", "generated_at": FRESH_TIME},
        "data_health": {"status": "OK", "generated_at": FRESH_TIME},
        "outcome_report": {
            "generated_at": FRESH_TIME,
            "resolved_signal_count": 8,
            "pending_signal_count": 1,
            "evaluated_signal_count": 9,
            "primary_horizon_metric": {
                "target_hit_rate_pct": 37.5,
                "stop_hit_rate_pct": 25.0,
                "favorable_to_adverse_ratio": 1.4,
            },
        },
        "strategy_learning": {
            "generated_at": FRESH_TIME,
            "overall": {"resolved_count": 8, "avg_signed_return_pct": 1.2, "win_rate_pct": 62.5},
            "judgment_effect": {
                "approved_or_reduced": {
                    "resolved_count": 5,
                    "avg_signed_return_pct": 1.8,
                    "win_rate_pct": 60.0,
                },
                "rejected_or_held": {
                    "resolved_count": 5,
                    "avg_signed_return_pct": -0.5,
                    "win_rate_pct": 40.0,
                },
            },
            "intake_coverage": {
                "coverage_pct": 50.0,
                "directional": {"coverage_pct": 100.0, "joined_signal_count": 8},
                "watch": {"coverage_pct": 0.0, "joined_signal_count": 8},
            },
            "sizing_blocker_remediation": {
                "sizing_blocker_count": 0,
                "covered_by_watchlist_removal_count": 0,
                "uncovered_count": 0,
            },
        },
        "portfolio_report": {
            "generated_at": FRESH_TIME,
            "portfolio_reports": [
                {
                    "portfolio_id": 8,
                    "role": "simulation",
                    "total_value_hkd": 102_000,
                    "return_pct_vs_initial": 2.0,
                }
            ],
            "simulation_trade_review": {
                "lookback_days": 30,
                "trade_count": 6,
                "closed_trade_count": 3,
                "closed_win_rate_pct": 66.67,
                "closed_pnl_hkd_est": 1200.0,
                "largest_loss": {"symbol": "00017", "pnl_hkd_est": -200.0},
                "largest_win": {"symbol": "00700", "pnl_hkd_est": 900.0},
                "review_notes": [],
            },
            "portfolio_risk": {
                "reports": [
                    {
                        "role": "simulation",
                        "risk_level": "low",
                        "trade_position_reconciliation_status": "OK",
                        "unrealized_pnl": {
                            "unrealized_pnl_hkd": 500,
                            "unrealized_pnl_pct_of_cost": 1.0,
                        },
                    }
                ]
            }
        },
        "watchlist_diff": {
            "generated_at": FRESH_TIME,
            "proposal": {
                "proposal_hash": "hash-1",
                "current_watchlist_id": "wl-1",
                "proposed_watchlist_id": "wl-2",
                "manual_review_required": True,
                "auto_applied": False,
            }
        },
        "market_context": {
            "schema": "market_context_report_v1",
            "generated_at": FRESH_TIME,
            "markets": {
                "HK": {
                    "regime": "mixed",
                    "risk_level": "medium",
                    "latest_date": "2026-06-12",
                    "notes": ["mixed_regime_require_stronger_signal_confluence"],
                },
                "US": {
                    "regime": "risk_on",
                    "risk_level": "low",
                    "latest_date": "2026-06-12",
                    "notes": ["normal_buy_review_allowed_if_signal_and_risk_pass"],
                },
            },
        },
        "alert_quality": {
            "status": "OK",
            "generated_at": FRESH_TIME,
            "directional_alert_count": 8,
            "watch_alert_count": 8,
        },
        "judgment_audit": {
            "status": "OK",
            "generated_at": FRESH_TIME,
            "counts": {"judgment_count": 4},
        },
        "simulation_performance": {
            "status": "OK",
            "generated_at": FRESH_TIME,
            "summary": {
                "return_pct_vs_initial": 2.0,
                "closed_trade_count": 3,
                "closed_win_rate_pct": 66.67,
                "closed_pnl_hkd_est": 1200.0,
            },
            "reason_codes": [],
            "recommendations": ["simulation_performance_clean_continue_shadow_collection"],
        },
        "position_judgment_audit": {
            "status": "OK",
            "generated_at": FRESH_TIME,
            "counts": {"judgment_count": 3},
        },
    }


class ExecutionReadinessReportTests(unittest.TestCase):
    def test_blocks_when_forward_evidence_is_insufficient(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["overall"]["resolved_count"] = 0
        inputs["outcome_report"]["resolved_signal_count"] = 0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["schema"], "execution_readiness_report_v1")
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertFalse(payload["ready_for_execute"])
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertIn("forward_outcome_evidence", [gate["gate"] for gate in payload["blocking_gates"]])

    def test_ready_when_hard_gates_pass_and_no_manual_watchlist_action_is_pending(self):
        payload = report.build_report(**healthy_inputs(), now=NOW)

        self.assertEqual(payload["status"], "READY")
        self.assertTrue(payload["ready_for_execute"])
        self.assertEqual(payload["blocking_gates"], [])
        self.assertEqual(payload["warning_gates"], [])

    def test_stale_input_report_blocks_readiness(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["generated_at"] = STALE_TIME

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "report_freshness"][0]
        self.assertIn("strategy_learning", gate["detail"])

    def test_missing_report_timestamp_blocks_readiness(self):
        inputs = healthy_inputs()
        del inputs["portfolio_report"]["generated_at"]

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "report_freshness"][0]
        self.assertIn("portfolio_report", gate["detail"])

    def test_stale_watchlist_diff_blocks_readiness(self):
        inputs = healthy_inputs()
        inputs["watchlist_diff"]["generated_at"] = STALE_TIME

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "report_freshness"][0]
        self.assertIn("watchlist_diff", gate["detail"])

    def test_market_context_risk_off_warns_not_ready(self):
        inputs = healthy_inputs()
        inputs["market_context"]["markets"]["HK"]["regime"] = "risk_off"

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "WARN")
        self.assertFalse(payload["ready_for_execute"])
        gate = [gate for gate in payload["warning_gates"] if gate["gate"] == "market_context"][0]
        self.assertIn("risk_off=HK", gate["detail"])

    def test_failed_hermes_judgment_audit_blocks(self):
        inputs = healthy_inputs()
        inputs["judgment_audit"]["status"] = "FAIL"
        inputs["judgment_audit"]["counts"] = {
            "reason_counts": {"approval_for_ineligible_review_item": 1}
        }
        inputs["judgment_audit"]["recommendations"] = [
            "fix_or_reject_judgments:approval_for_ineligible_review_item"
        ]

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "hermes_judgment_audit"][0]
        self.assertEqual(gate["data"]["status"], "FAIL")

    def test_failed_hermes_position_judgment_audit_blocks(self):
        inputs = healthy_inputs()
        inputs["position_judgment_audit"]["status"] = "FAIL"
        inputs["position_judgment_audit"]["counts"] = {
            "reason_counts": {"submits_orders_must_be_false": 1}
        }
        inputs["position_judgment_audit"]["recommendations"] = [
            "fix_position_judgments:submits_orders_must_be_false"
        ]

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [
            gate for gate in payload["blocking_gates"]
            if gate["gate"] == "hermes_position_judgment_audit"
        ][0]
        self.assertEqual(gate["data"]["status"], "FAIL")

    def test_failed_simulation_performance_attribution_blocks(self):
        inputs = healthy_inputs()
        inputs["simulation_performance"]["status"] = "FAIL"
        inputs["simulation_performance"]["reason_codes"] = [
            "simulation_closed_pnl_not_positive",
            "simulation_closed_win_rate_too_low",
        ]

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [
            gate for gate in payload["blocking_gates"]
            if gate["gate"] == "simulation_performance_attribution"
        ][0]
        self.assertEqual(gate["data"]["status"], "FAIL")

    def test_missing_market_context_schema_blocks(self):
        inputs = healthy_inputs()
        inputs["market_context"] = {"generated_at": FRESH_TIME, "markets": {}}

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "market_context"][0]
        self.assertIn("schema", gate["detail"])

    def test_manual_watchlist_proposal_keeps_status_warn_not_ready(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["sizing_blocker_remediation"] = {
            "sizing_blocker_count": 2,
            "covered_by_watchlist_removal_count": 2,
            "uncovered_count": 0,
            "watchlist_proposal_hash": "hash-1",
        }

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "WARN")
        self.assertFalse(payload["ready_for_execute"])
        self.assertEqual(payload["warning_gates"][0]["gate"], "watchlist_proposal")
        self.assertEqual(payload["warning_gates"][0]["data"]["proposed_watchlist_id"], "wl-2")

    def test_critical_simulation_portfolio_risk_blocks(self):
        inputs = healthy_inputs()
        inputs["portfolio_report"]["portfolio_risk"]["reports"][0]["risk_level"] = "critical"

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("simulation_portfolio_risk", [gate["gate"] for gate in payload["blocking_gates"]])

    def test_missing_simulation_portfolio_risk_blocks(self):
        inputs = healthy_inputs()
        inputs["portfolio_report"] = {}

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "simulation_portfolio_risk"][0]
        self.assertEqual(gate["data"]["simulation_report_count"], 0)

    def test_non_positive_forward_return_blocks_even_with_enough_samples(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["overall"]["avg_signed_return_pct"] = 0.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "forward_outcome_evidence"][0]
        self.assertIn("not positive", gate["detail"])

    def test_missing_average_forward_return_blocks_even_with_enough_samples(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["overall"]["avg_signed_return_pct"] = None

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "forward_outcome_evidence"][0]
        self.assertIn("average signed return is missing", gate["detail"])

    def test_low_win_rate_blocks_even_when_average_return_is_positive(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["overall"]["avg_signed_return_pct"] = 2.5
        inputs["strategy_learning"]["overall"]["win_rate_pct"] = 50.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "forward_outcome_evidence"][0]
        self.assertIn("win rate 50.0% is not above required 50", gate["detail"])

    def test_missing_win_rate_blocks_even_when_average_return_is_positive(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["overall"]["avg_signed_return_pct"] = 2.5
        inputs["strategy_learning"]["overall"]["win_rate_pct"] = None

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "forward_outcome_evidence"][0]
        self.assertIn("win rate is missing", gate["detail"])

    def test_outcome_primary_metric_fills_learning_metric_gaps(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["overall"] = {"resolved_count": 8}
        inputs["outcome_report"]["primary_horizon_metric"] = {
            "avg_signed_close_return_pct": 1.1,
            "win_rate_pct": 62.5,
            "target_hit_rate_pct": 37.5,
            "stop_hit_rate_pct": 25.0,
            "favorable_to_adverse_ratio": 1.4,
        }

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "READY")
        gate = [gate for gate in payload["gates"] if gate["gate"] == "forward_outcome_evidence"][0]
        self.assertEqual(gate["data"]["avg_signed_return_pct"], 1.1)
        self.assertEqual(gate["data"]["win_rate_pct"], 62.5)

    def test_stop_hits_exceeding_targets_blocks(self):
        inputs = healthy_inputs()
        inputs["outcome_report"]["primary_horizon_metric"]["target_hit_rate_pct"] = 25.0
        inputs["outcome_report"]["primary_horizon_metric"]["stop_hit_rate_pct"] = 37.5

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "forward_outcome_evidence"][0]
        self.assertIn("exceeds target hit rate", gate["detail"])

    def test_high_stop_hit_rate_blocks_even_when_targets_match(self):
        inputs = healthy_inputs()
        inputs["outcome_report"]["primary_horizon_metric"]["target_hit_rate_pct"] = 70.0
        inputs["outcome_report"]["primary_horizon_metric"]["stop_hit_rate_pct"] = 60.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "forward_outcome_evidence"][0]
        self.assertIn("exceeds maximum", gate["detail"])

    def test_weak_favorable_to_adverse_ratio_blocks(self):
        inputs = healthy_inputs()
        inputs["outcome_report"]["primary_horizon_metric"]["favorable_to_adverse_ratio"] = 1.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "forward_outcome_evidence"][0]
        self.assertIn("favorable/adverse ratio", gate["detail"])

    def test_insufficient_hermes_approval_sample_blocks(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["judgment_effect"]["approved_or_reduced"]["resolved_count"] = 4

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "hermes_judgment_effect"][0]
        self.assertIn("approved/reduced resolved sample", gate["detail"])

    def test_insufficient_hermes_rejection_comparison_sample_blocks(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["judgment_effect"]["rejected_or_held"]["resolved_count"] = 4

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "hermes_judgment_effect"][0]
        self.assertIn("rejected/held comparison sample", gate["detail"])

    def test_hermes_approval_not_outperforming_rejections_blocks(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["judgment_effect"]["approved_or_reduced"]["avg_signed_return_pct"] = 0.7
        inputs["strategy_learning"]["judgment_effect"]["rejected_or_held"]["avg_signed_return_pct"] = 0.9

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "hermes_judgment_effect"][0]
        self.assertIn("does not outperform", gate["detail"])

    def test_hermes_approval_low_win_rate_blocks(self):
        inputs = healthy_inputs()
        inputs["strategy_learning"]["judgment_effect"]["approved_or_reduced"]["win_rate_pct"] = 50.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "hermes_judgment_effect"][0]
        self.assertIn("approved/reduced win rate", gate["detail"])

    def test_insufficient_simulation_closed_trades_blocks(self):
        inputs = healthy_inputs()
        inputs["portfolio_report"]["simulation_trade_review"]["closed_trade_count"] = 2

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "simulation_trade_review"][0]
        self.assertIn("closed trade sample", gate["detail"])

    def test_non_positive_simulation_total_return_blocks(self):
        inputs = healthy_inputs()
        inputs["portfolio_report"]["portfolio_reports"][0]["return_pct_vs_initial"] = 0.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [
            gate for gate in payload["blocking_gates"] if gate["gate"] == "simulation_portfolio_performance"
        ][0]
        self.assertIn("simulation return", gate["detail"])

    def test_large_unrealized_simulation_loss_blocks(self):
        inputs = healthy_inputs()
        inputs["portfolio_report"]["portfolio_risk"]["reports"][0]["unrealized_pnl"][
            "unrealized_pnl_pct_of_cost"
        ] = -6.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [
            gate for gate in payload["blocking_gates"] if gate["gate"] == "simulation_portfolio_performance"
        ][0]
        self.assertIn("unrealized PnL", gate["detail"])

    def test_negative_simulation_closed_pnl_blocks(self):
        inputs = healthy_inputs()
        inputs["portfolio_report"]["simulation_trade_review"]["closed_pnl_hkd_est"] = -1.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "simulation_trade_review"][0]
        self.assertIn("closed trade PnL", gate["detail"])

    def test_low_simulation_closed_win_rate_blocks(self):
        inputs = healthy_inputs()
        inputs["portfolio_report"]["simulation_trade_review"]["closed_win_rate_pct"] = 50.0

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "simulation_trade_review"][0]
        self.assertIn("closed trade win rate", gate["detail"])

    def test_simulation_trade_review_blocking_notes_block(self):
        inputs = healthy_inputs()
        inputs["portfolio_report"]["simulation_trade_review"]["review_notes"] = ["loss_rate_above_60pct"]

        payload = report.build_report(**inputs, now=NOW)

        self.assertEqual(payload["status"], "BLOCKED")
        gate = [gate for gate in payload["blocking_gates"] if gate["gate"] == "simulation_trade_review"][0]
        self.assertIn("blocking notes", gate["detail"])


if __name__ == "__main__":
    unittest.main()
