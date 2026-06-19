import unittest

from scripts import strategy_review_report as report


def outcome_row(key, resolved=20, avg=0.3, win=55, target=35, stop=20):
    return {
        "key": key,
        "count": resolved,
        "horizons": {
            "1d": {
                "resolved_count": resolved,
                "avg_signed_close_return_pct": avg,
                "win_rate_pct": win,
                "target_hit_rate_pct": target,
                "stop_hit_rate_pct": stop,
            }
        },
    }


def quality_row(side, trigger, count=20, validation=80, review=10, eligible=8, move=0.2):
    return {
        "signal_type": side,
        "trigger": trigger,
        "count": count,
        "confirmed_rate_pct": 70,
        "validation_pass_rate_pct": validation,
        "packet_review_count": review,
        "packet_eligible_count": eligible,
        "avg_signed_move_pct": move,
    }


def outcome_report(rows, overall_resolved=40, overall_avg=0.2, overall_win=55):
    return {
        "schema": "rt_signal_outcome_report_v1",
        "generated_at": "2026-06-12T10:00:00",
        "overall": {
            "horizons": {
                "1d": {
                    "resolved_count": overall_resolved,
                    "avg_signed_close_return_pct": overall_avg,
                    "win_rate_pct": overall_win,
                }
            }
        },
        "by_trigger": rows,
    }


def current_scope_empty_outcome_report():
    payload = outcome_report([], overall_resolved=0, overall_avg=None, overall_win=None)
    payload["sample_scope"] = {
        "mode": "runtime_strategy_config_and_watchlist",
        "strategy_config_id": "cfg-v56",
        "strategy_config_version": "v5.6-test",
        "watchlist_id": "wl-current",
        "raw_alert_count_before_filter": 25,
        "raw_alert_count": 0,
        "excluded_alert_count": 25,
        "directional_alert_count_before_filter": 18,
        "directional_alert_count": 0,
        "excluded_directional_alert_count": 18,
    }
    payload["counts"] = {
        "raw_alert_count": 0,
        "evaluated_signal_count": 0,
        "directional_alert_count": 0,
    }
    return payload


def quality_report(rows):
    return {
        "generated_at": "2026-06-12T10:05:00",
        "directional_quality": {"validation_pass_rate_pct": 70},
        "packet_review": {"eligible_rate_pct": 50},
        "trigger_quality": rows,
        "symbol_conflicts": [],
    }


def current_scope_empty_quality_report():
    payload = quality_report([])
    payload["sample_scope"] = {
        "mode": "runtime_strategy_config_and_watchlist",
        "strategy_config_id": "cfg-v56",
        "strategy_config_version": "v5.6-test",
        "watchlist_id": "wl-current",
        "raw_alert_count_before_filter": 25,
        "raw_alert_count": 0,
        "excluded_alert_count": 25,
        "directional_candidate_count_before_filter": 18,
        "directional_candidate_count": 0,
        "excluded_directional_candidate_count": 18,
    }
    payload["counts"] = {"total_alerts": 0, "directional_alerts": 0}
    return payload


class StrategyReviewReportTests(unittest.TestCase):
    def test_positive_trigger_can_be_candidate_allow_after_other_gates(self):
        payload = report.build_report(
            outcome_report([outcome_row("BUY:breakout")]),
            quality_report([quality_row("BUY", "breakout")]),
        )
        row = payload["trigger_policies"][0]

        self.assertEqual(row["policy"], "candidate_allow_after_other_gates")
        self.assertTrue(row["execution_allowed_by_report"])
        self.assertEqual(payload["overall_policy"]["policy"], "candidate_for_limited_paper_execution_review")
        self.assertFalse(payload["overall_policy"]["execution_allowed_by_report"])
        self.assertTrue(payload["operator_contract"]["read_only"])
        self.assertFalse(payload["operator_contract"]["submits_orders"])
        self.assertFalse(payload["operator_contract"]["changes_strategy_config"])
        self.assertTrue(payload["operator_contract"]["trigger_policies_are_strategy_config_proposal_input"])
        self.assertTrue(payload["operator_contract"]["requires_strategy_config_proposal_gate_before_promotion"])

    def test_negative_trigger_is_disable_execution_review(self):
        payload = report.build_report(
            outcome_report([outcome_row("BUY:weak", avg=-0.4, win=30, stop=55, target=10)]),
            quality_report([quality_row("BUY", "weak", validation=90, eligible=9)]),
        )
        row = payload["trigger_policies"][0]

        self.assertEqual(row["policy"], "disable_execution_review")
        self.assertIn("trigger_avg_return_not_positive", row["reasons"])
        self.assertIn("trigger_win_rate_below_45", row["reasons"])
        self.assertIn("disable_or_rework_trigger:BUY:weak", payload["recommendations"])

    def test_low_quality_trigger_tightens_thresholds(self):
        payload = report.build_report(
            outcome_report([outcome_row("SELL:breakdown", avg=0.4, win=60)]),
            quality_report([quality_row("SELL", "breakdown", validation=20, review=10, eligible=1, move=-0.1)]),
        )
        row = payload["trigger_policies"][0]

        self.assertEqual(row["policy"], "tighten_thresholds")
        self.assertIn("validation_pass_rate_below_50", row["reasons"])
        self.assertIn("packet_eligible_rate_below_25", row["reasons"])
        self.assertIn("negative_intraday_queue_mark", row["reasons"])

    def test_insufficient_outcome_sample_stays_shadow_only(self):
        payload = report.build_report(
            outcome_report([outcome_row("BUY:new", resolved=2, avg=1.0, win=100)], overall_resolved=2),
            quality_report([quality_row("BUY", "new", validation=90, eligible=9)]),
        )
        row = payload["trigger_policies"][0]

        self.assertEqual(row["policy"], "shadow_only")
        self.assertIn("trigger_outcome_sample_below_10", row["reasons"])
        self.assertEqual(payload["overall_policy"]["policy"], "keep_shadow_or_dry_run")
        self.assertIn("keep_alert_sim_disabled_until_strategy_review_passes", payload["recommendations"])
        self.assertTrue(payload["operator_contract"]["insufficient_trigger_samples_remain_shadow_only"])

    def test_insufficient_outcome_sample_caps_low_quality_at_shadow_only(self):
        payload = report.build_report(
            outcome_report([outcome_row("BUY:noisy", resolved=0, avg=None, win=None)], overall_resolved=0),
            quality_report([quality_row("BUY", "noisy", validation=0, review=10, eligible=0, move=-1.2)]),
        )
        row = payload["trigger_policies"][0]

        self.assertEqual(row["policy"], "shadow_only")
        self.assertIn("trigger_outcome_sample_below_10", row["reasons"])
        self.assertIn("validation_pass_rate_below_50", row["reasons"])
        self.assertIn("packet_eligible_rate_below_25", row["reasons"])
        self.assertIn("negative_intraday_queue_mark", row["reasons"])
        self.assertNotIn("tighten_trigger_thresholds:BUY:noisy", payload["recommendations"])
        self.assertIn("collect_more_forward_outcomes_before_execution_review", payload["recommendations"])

    def test_current_runtime_scope_empty_is_reported_as_forward_evidence_gap(self):
        payload = report.build_report(
            current_scope_empty_outcome_report(),
            current_scope_empty_quality_report(),
        )

        scope = payload["forward_evidence_scope"]
        self.assertEqual(scope["status"], "CURRENT_SCOPE_EMPTY")
        self.assertTrue(scope["blocks_strategy_promotion"])
        self.assertEqual(scope["runtime_strategy_config_id"], "cfg-v56")
        self.assertEqual(scope["runtime_watchlist_id"], "wl-current")
        self.assertEqual(scope["outcome_sample_scope"]["excluded_alert_count"], 25)
        self.assertEqual(payload["trigger_policies"], [])
        self.assertIn("current_strategy_scope_has_no_forward_alerts", payload["warnings"])
        self.assertIn("wait_for_current_strategy_forward_alerts", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
