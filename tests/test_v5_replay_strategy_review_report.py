import unittest

from scripts import v5_replay_strategy_review_report as report


def replay_payload():
    return {
        "schema": "v5_local_replay_report_v1",
        "generated_at": "2026-06-14T13:00:00",
        "summary": {
            "overall_status": "V5_REPLAY_RESEARCH_ONLY",
            "promotion_ready": False,
            "symbol_count": 95,
            "evaluated_bars": 1000,
            "alert_count": 300,
            "execution_candidate_count": 50,
            "downgraded_directional_count": 180,
        },
        "replay_quality": {"status": "WARN"},
        "replay_breakdown": {
            "schema": "v5_local_replay_breakdown_v1",
            "trigger_groups": [
                {
                    "key": "HK:BUY:站上MA5",
                    "market": "HK",
                    "candidate_signal_type": "BUY",
                    "trigger": "站上MA5",
                    "status": "WARN",
                    "reasons": [
                        "trigger_replay_alert_density_high",
                        "trigger_execution_candidate_density_high",
                    ],
                    "metrics": {
                        "denominator_bars": 500,
                        "alert_count": 80,
                        "pct_of_all_alerts": 26.67,
                        "alert_rate_per_100_bars": 16.0,
                        "execution_candidate_count": 20,
                        "execution_candidate_rate_per_100_bars": 4.0,
                        "confirmed_directional_count": 20,
                        "downgraded_directional_count": 60,
                        "directional_confirmation_ratio_pct": 25.0,
                        "directional_downgrade_ratio_pct": 75.0,
                        "execution_candidate_ratio_pct": 25.0,
                    },
                },
                {
                    "key": "US:BUY:站上MA5",
                    "market": "US",
                    "candidate_signal_type": "BUY",
                    "trigger": "站上MA5",
                    "status": "WARN",
                    "reasons": ["trigger_execution_candidate_density_high"],
                    "metrics": {
                        "denominator_bars": 500,
                        "alert_count": 40,
                        "alert_rate_per_100_bars": 8.0,
                        "execution_candidate_count": 12,
                        "execution_candidate_rate_per_100_bars": 2.4,
                        "confirmed_directional_count": 12,
                        "downgraded_directional_count": 28,
                    },
                },
                {
                    "key": "HK:BUY:布林下軌突破",
                    "market": "HK",
                    "candidate_signal_type": "BUY",
                    "trigger": "布林下軌突破",
                    "status": "WARN",
                    "reasons": [
                        "trigger_replay_alert_density_high",
                        "trigger_directional_confirmation_ratio_low",
                        "trigger_directional_downgrade_ratio_high",
                    ],
                    "metrics": {
                        "denominator_bars": 500,
                        "alert_count": 70,
                        "alert_rate_per_100_bars": 14.0,
                        "execution_candidate_count": 0,
                        "execution_candidate_rate_per_100_bars": 0.0,
                        "confirmed_directional_count": 0,
                        "downgraded_directional_count": 70,
                        "directional_confirmation_ratio_pct": 0.0,
                        "directional_downgrade_ratio_pct": 100.0,
                    },
                },
            ],
        },
    }


class V5ReplayStrategyReviewReportTests(unittest.TestCase):
    def test_build_report_maps_replay_noise_to_non_promotable_policies(self):
        payload = report.build_report(replay_payload())

        self.assertEqual(payload["schema"], "v5_replay_strategy_review_report_v1")
        self.assertEqual(payload["summary"]["status"], "RESEARCH_REVIEW_ONLY")
        self.assertFalse(payload["summary"]["promotion_eligible"])
        self.assertFalse(payload["operator_contract"]["changes_strategy_config"])
        self.assertTrue(payload["source"]["not_strategy_config_proposal_input"])
        self.assertNotIn("trigger_policies", payload)
        self.assertIn("do_not_promote_strategy_config_from_replay_only", payload["recommendations"])

        by_strategy_key = {row["strategy_key"]: row for row in payload["strategy_trigger_summary"]}
        ma5 = by_strategy_key["BUY:站上MA5"]
        bollinger = by_strategy_key["BUY:布林下軌突破"]
        self.assertEqual(ma5["policy"], "tighten_thresholds")
        self.assertEqual(ma5["metrics"]["alert_count"], 120)
        self.assertEqual(ma5["metrics"]["execution_candidate_count"], 32)
        self.assertEqual(bollinger["policy"], "shadow_only")
        self.assertIn("replay_noisy_directional_candidates_should_stay_shadow", bollinger["reasons"])
        self.assertEqual(payload["overall_policy"]["policy"], "keep_shadow_or_dry_run")

    def test_missing_replay_report_is_not_promotable(self):
        payload = report.build_report({})

        self.assertEqual(payload["summary"]["status"], "MISSING")
        self.assertEqual(payload["replay_trigger_policies"], [])
        self.assertFalse(payload["overall_policy"]["promotion_eligible"])
        self.assertEqual(payload["checks"][0]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
