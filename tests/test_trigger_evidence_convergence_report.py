import unittest

from scripts import trigger_evidence_convergence_report as report


def strategy_review():
    return {
        "schema": "strategy_review_report_v1",
        "trigger_policies": [
            {
                "key": "BUY:站上MA5",
                "signal_type": "BUY",
                "trigger": "站上MA5",
                "policy": "tighten_thresholds",
                "reasons": ["validation_pass_rate_below_50"],
                "sample": {"outcome_count": 18, "quality_count": 20, "confirmed_rate_pct": 60.0},
                "metrics": {
                    "resolved_count": 18,
                    "avg_signed_close_return_pct": -0.2,
                    "win_rate_pct": 38.0,
                    "target_hit_rate_pct": 20.0,
                    "stop_hit_rate_pct": 35.0,
                },
            },
            {
                "key": "BUY:breakout",
                "signal_type": "BUY",
                "trigger": "breakout",
                "policy": "candidate_allow_after_other_gates",
                "reasons": [],
                "sample": {"outcome_count": 22, "quality_count": 22, "confirmed_rate_pct": 80.0},
                "metrics": {
                    "resolved_count": 22,
                    "avg_signed_close_return_pct": 0.6,
                    "win_rate_pct": 58.0,
                    "target_hit_rate_pct": 40.0,
                    "stop_hit_rate_pct": 12.0,
                },
            },
            {
                "key": "SELL:new",
                "signal_type": "SELL",
                "trigger": "new",
                "policy": "shadow_only",
                "reasons": ["trigger_outcome_sample_below_10"],
                "sample": {"outcome_count": 2, "quality_count": 8, "confirmed_rate_pct": 50.0},
                "metrics": {
                    "resolved_count": 2,
                    "avg_signed_close_return_pct": 0.4,
                    "win_rate_pct": 50.0,
                },
            },
        ],
    }


def replay_review():
    return {
        "schema": "v5_replay_strategy_review_report_v1",
        "strategy_trigger_summary": [
            {
                "strategy_key": "BUY:站上MA5",
                "policy": "tighten_thresholds",
                "promotion_eligible": False,
                "markets": ["HK", "US"],
                "reasons": ["replay_execution_candidate_density_high"],
                "metrics": {
                    "alert_count": 120,
                    "alert_rate_per_100_bars": 12.0,
                    "execution_candidate_count": 30,
                    "execution_candidate_rate_per_100_bars": 3.0,
                },
            },
            {
                "strategy_key": "BUY:breakout",
                "policy": "shadow_only",
                "promotion_eligible": False,
                "markets": ["HK"],
                "reasons": ["replay_noisy_directional_candidates_should_stay_shadow"],
                "metrics": {
                    "alert_count": 80,
                    "alert_rate_per_100_bars": 8.0,
                    "execution_candidate_count": 0,
                    "execution_candidate_rate_per_100_bars": 0.0,
                },
            },
            {
                "strategy_key": "SELL:new",
                "policy": "shadow_only",
                "promotion_eligible": False,
                "markets": ["US"],
                "reasons": ["replay_execution_candidates_exist"],
                "metrics": {
                    "alert_count": 12,
                    "alert_rate_per_100_bars": 1.2,
                    "execution_candidate_count": 1,
                    "execution_candidate_rate_per_100_bars": 0.1,
                },
            },
        ],
    }


class TriggerEvidenceConvergenceReportTests(unittest.TestCase):
    def test_build_report_classifies_forward_and_replay_convergence(self):
        payload = report.build_report(strategy_review(), replay_review())

        self.assertEqual(payload["schema"], "trigger_evidence_convergence_report_v1")
        self.assertEqual(payload["summary"]["status"], "REVIEW_REQUIRED")
        self.assertFalse(payload["summary"]["promotion_eligible"])
        self.assertFalse(payload["operator_contract"]["changes_strategy_config"])
        self.assertTrue(payload["source"]["not_strategy_config_proposal_input"])

        by_key = {row["key"]: row for row in payload["trigger_evidence"]}
        self.assertEqual(by_key["BUY:站上MA5"]["status"], "CONVERGED_RISK")
        self.assertEqual(by_key["BUY:站上MA5"]["confidence"], "HIGH")
        self.assertIn("forward_avg_return_not_positive", by_key["BUY:站上MA5"]["reasons"])
        self.assertEqual(by_key["BUY:breakout"]["status"], "REPLAY_CHALLENGES_FORWARD")
        self.assertEqual(by_key["SELL:new"]["status"], "INSUFFICIENT_FORWARD_SAMPLE")
        self.assertIn(
            "prioritize_trigger_rework_or_threshold_review:BUY:站上MA5",
            payload["recommendations"],
        )
        self.assertIn(
            "cap_hermes_confidence_until_forward_and_replay_align:BUY:breakout",
            payload["recommendations"],
        )

    def test_missing_forward_review_blocks_convergence(self):
        payload = report.build_report({}, replay_review())

        self.assertEqual(payload["summary"]["status"], "MISSING")
        self.assertEqual(payload["checks"][0]["status"], "FAIL")
        self.assertFalse(payload["operator_contract"]["promotion_eligible"])


if __name__ == "__main__":
    unittest.main()
