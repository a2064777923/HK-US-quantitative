import unittest

from scripts import strategy_config_proposal as proposal


def review_payload():
    return {
        "schema": "strategy_review_report_v1",
        "overall_policy": {"policy": "keep_shadow_or_dry_run"},
        "trigger_policies": [
            {
                "key": "BUY:weak",
                "signal_type": "BUY",
                "trigger": "weak",
                "policy": "disable_execution_review",
                "reasons": ["trigger_avg_return_not_positive"],
            },
            {
                "key": "SELL:breakdown",
                "signal_type": "SELL",
                "trigger": "breakdown",
                "policy": "tighten_thresholds",
                "reasons": ["validation_pass_rate_below_50"],
            },
            {
                "key": "BUY:new",
                "signal_type": "BUY",
                "trigger": "new",
                "policy": "shadow_only",
                "reasons": ["trigger_outcome_sample_below_10"],
            },
        ],
    }


class StrategyConfigProposalTests(unittest.TestCase):
    def test_build_report_proposes_manual_strategy_config_changes(self):
        payload = proposal.build_report(
            review_payload(),
            {
                "schema": "rt_signal_strategy_config_v1",
                "version": "current",
                "confirmation_thresholds": {
                    "BUY": {"min_full_score": 0.25},
                    "SELL": {"max_full_score": -0.25},
                },
                "trigger_overrides": {},
            },
        )
        proposed = payload["proposed_config"]
        overrides = proposed["trigger_overrides"]

        self.assertEqual(payload["schema"], "rt_signal_strategy_config_proposal_v1")
        self.assertFalse(payload["source"]["auto_applied"])
        self.assertTrue(payload["source"]["manual_review_required"])
        self.assertEqual(payload["change_count"], 3)
        self.assertFalse(overrides["BUY:weak"]["enabled"])
        self.assertEqual(overrides["SELL:breakdown"]["max_full_score"], -0.35)
        self.assertEqual(overrides["BUY:new"]["review_mode"], "shadow_only_pending_sample")
        self.assertEqual(len(payload["proposal_hash"]), 16)

    def test_candidate_allow_policy_does_not_create_change(self):
        review = {
            "schema": "strategy_review_report_v1",
            "overall_policy": {"policy": "candidate_for_limited_paper_execution_review"},
            "trigger_policies": [
                {
                    "key": "BUY:good",
                    "signal_type": "BUY",
                    "trigger": "good",
                    "policy": "candidate_allow_after_other_gates",
                    "reasons": [],
                }
            ],
        }

        payload = proposal.build_report(review, {"schema": "rt_signal_strategy_config_v1"})

        self.assertEqual(payload["change_count"], 0)
        self.assertEqual(payload["changes"], [])


if __name__ == "__main__":
    unittest.main()
