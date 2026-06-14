import unittest
from datetime import datetime

from scripts import rt_signal_engine_v5 as rt
from scripts import strategy_config_proposal as proposal


NOW = datetime(2026, 6, 13, 10, 10, 0)


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


def simulation_performance(status="OK", generated_at="2026-06-13T10:00:00"):
    return {
        "schema": "simulation_performance_report_v1",
        "generated_at": generated_at,
        "status": status,
        "summary": {
            "portfolio_id": 8,
            "total_value_hkd": 102000 if status == "OK" else 94100.62,
            "return_pct_vs_initial": 2.0 if status == "OK" else -5.9,
            "risk_level": "low" if status == "OK" else "high",
            "closed_trade_count": 7,
            "closed_win_rate_pct": 66.67 if status == "OK" else 14.29,
            "closed_pnl_hkd_est": 800.0 if status == "OK" else -933.38,
        },
        "reason_codes": []
        if status == "OK"
        else [
            "simulation_total_return_not_positive",
            "simulation_closed_pnl_not_positive",
            "simulation_closed_win_rate_too_low",
        ],
        "recommendations": ["simulation_performance_clean_continue_shadow_collection"]
        if status == "OK"
        else ["keep_alert_sim_disabled_until_simulation_performance_recovers"],
        "remediation_plan": {
            "schema": "simulation_strategy_remediation_v1",
            "status": "not_required" if status == "OK" else "operator_review_required",
            "proposal_hash": "feedface12345678",
            "manual_review_required": status != "OK",
            "auto_applied": False,
            "actions": []
            if status == "OK"
            else [
                {"action_id": "keep_alert_sim_disabled", "auto_apply": False},
                {"action_id": "reject_or_hold_new_buy_by_default", "auto_apply": False},
            ],
            "operator_contract": {
                "read_only": True,
                "submits_orders": False,
                "changes_execution_mode": False,
                "changes_strategy_config": False,
                "requires_operator_review_before_promotion": status != "OK",
            },
        },
    }


def execution_readiness(status="READY"):
    return {
        "schema": "execution_readiness_report_v1",
        "generated_at": "2026-06-13T10:00:00",
        "status": status,
        "ready_for_execute": status == "READY",
        "blocking_gates": [] if status == "READY" else [{"gate": "hermes_judgment_effect", "status": "BLOCK"}],
        "warning_gates": [] if status == "READY" else [{"gate": "event_store_durability", "status": "WARN"}],
    }


def strategy_learning(
    audit_pass=True,
    audit_fail_count=0,
    audit_missing_count=0,
    approved_resolved=5,
    rejected_resolved=5,
    approved_avg=1.6,
    rejected_avg=-0.3,
    execution_candidate_scope=None,
    execution_candidate_audit_pass=True,
    execution_approved_resolved=5,
    execution_rejected_resolved=5,
    execution_approved_avg=1.4,
    execution_rejected_avg=-0.2,
):
    payload = {
        "schema": "strategy_learning_report_v1",
        "generated_at": "2026-06-13T10:00:00",
        "judgment_effect": {
            "approved_or_reduced": {
                "resolved_count": 8,
                "avg_signed_return_pct": 1.8,
                "win_rate_pct": 62.5,
            },
            "rejected_or_held": {
                "resolved_count": 8,
                "avg_signed_return_pct": -0.4,
                "win_rate_pct": 37.5,
            },
        },
        "judgment_audit_coverage": {
            "audit_report_available": True,
            "audit_report_status": "OK",
            "audit_report_truncated": False,
            "joined_judgment_count": approved_resolved + rejected_resolved + audit_fail_count + audit_missing_count,
            "audit_pass_count": approved_resolved + rejected_resolved,
            "audit_fail_count": audit_fail_count,
            "audit_missing_count": audit_missing_count,
            "approved_or_reduced_count": approved_resolved,
            "approved_or_reduced_audit_pass_count": approved_resolved,
            "approved_or_reduced_audit_fail_or_missing_count": audit_fail_count + audit_missing_count,
            "rejected_or_held_count": rejected_resolved,
            "rejected_or_held_audit_pass_count": rejected_resolved,
            "rejected_or_held_audit_fail_or_missing_count": 0,
        },
    }
    if audit_pass:
        payload["audit_pass_judgment_effect"] = {
            "sample_filter": "judgment_audit_status_PASS",
            "approved_or_reduced": {
                "resolved_count": approved_resolved,
                "avg_signed_return_pct": approved_avg,
                "win_rate_pct": 60.0,
            },
            "rejected_or_held": {
                "resolved_count": rejected_resolved,
                "avg_signed_return_pct": rejected_avg,
                "win_rate_pct": 40.0,
            },
        }
    if execution_candidate_scope is not None:
        payload["execution_candidate_scope"] = execution_candidate_scope
    if execution_candidate_scope is not None and execution_candidate_audit_pass:
        payload["execution_candidate_audit_pass_judgment_effect"] = {
            "sample_filter": "execution_candidate_true_and_judgment_audit_status_PASS",
            "approved_or_reduced": {
                "resolved_count": execution_approved_resolved,
                "avg_signed_return_pct": execution_approved_avg,
                "win_rate_pct": 60.0,
            },
            "rejected_or_held": {
                "resolved_count": execution_rejected_resolved,
                "avg_signed_return_pct": execution_rejected_avg,
                "win_rate_pct": 40.0,
            },
        }
    return payload


def execution_candidate_scope(downgraded_directional_count=0, execution_candidate_count=10):
    return {
        "schema": "strategy_learning_execution_candidate_scope_v1",
        "joined_signal_count": execution_candidate_count + downgraded_directional_count,
        "execution_candidate_count": execution_candidate_count,
        "non_execution_candidate_count": downgraded_directional_count,
        "downgraded_directional_count": downgraded_directional_count,
        "unknown_execution_candidate_count": 0,
        "execution_candidate": {"resolved_count": execution_candidate_count},
        "promotion_evidence_requirement": "execution_candidate_only_learning_required"
        if downgraded_directional_count
        else "standard_audit_pass_learning",
    }


def trigger_evidence_convergence(
    status="SUPPORTIVE_WITH_LIMITS",
    converged_risk_count=0,
    replay_challenges_forward_count=0,
    insufficient_forward_sample_count=0,
):
    return {
        "schema": "trigger_evidence_convergence_report_v1",
        "generated_at": "2026-06-13T10:00:00",
        "operator_contract": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy_config": False,
            "promotion_eligible": False,
        },
        "summary": {
            "status": status,
            "promotion_ready": False,
            "promotion_eligible": False,
            "trigger_count": 2,
            "converged_risk_count": converged_risk_count,
            "replay_challenges_forward_count": replay_challenges_forward_count,
            "insufficient_forward_sample_count": insufficient_forward_sample_count,
            "status_counts": {
                "CONVERGED_CLEAN": 2,
                "CONVERGED_RISK": converged_risk_count,
                "REPLAY_CHALLENGES_FORWARD": replay_challenges_forward_count,
                "INSUFFICIENT_FORWARD_SAMPLE": insufficient_forward_sample_count,
            },
        },
        "trigger_evidence": [
            {
                "key": "BUY:weak",
                "status": "REPLAY_CHALLENGES_FORWARD" if replay_challenges_forward_count else "CONVERGED_CLEAN",
                "confidence": "LOW",
                "reasons": ["forward_allows_but_replay_flags_noise"] if replay_challenges_forward_count else [],
                "forward": {"policy": "candidate_allow_after_other_gates"},
                "replay": {"policy": "tighten_thresholds" if replay_challenges_forward_count else "candidate_allow_after_other_gates"},
            },
            {
                "key": "SELL:breakdown",
                "status": "INSUFFICIENT_FORWARD_SAMPLE" if insufficient_forward_sample_count else "CONVERGED_CLEAN",
                "confidence": "LOW",
                "reasons": ["forward_outcome_sample_missing"] if insufficient_forward_sample_count else [],
                "forward": {"policy": "tighten_thresholds"},
                "replay": {"policy": "tighten_thresholds"},
            },
        ],
        "recommendations": ["continue_shadow_observation_until_convergence_is_supportive"],
    }


def local_backtest_reliability(status="RESEARCH_USEFUL_WITH_LIMITATIONS", promotion_ready=False):
    return {
        "schema": "local_backtest_reliability_report_v1",
        "generated_at": "2026-06-13T09:55:00",
        "source": {
            "read_only_inputs": True,
            "local_only": True,
            "changes_v5": False,
            "changes_order_intake": False,
            "changes_simulation": False,
            "uses_credentials": False,
        },
        "summary": {
            "overall_status": status,
            "promotion_ready": promotion_ready,
            "hermes_use": "research_evidence_only",
            "dataset_status": "WARN" if status != "INSUFFICIENT_EVIDENCE" else "FAIL",
            "backtest_status_counts": {"OK": 1, "WARN": 1},
            "best_backtest_by_sharpe": "portfolio_backtest_realistic",
        },
        "dataset": {
            "status": "WARN" if status != "INSUFFICIENT_EVIDENCE" else "FAIL",
            "total_symbol_count": 120,
            "total_row_count": 120000,
            "date_range": {"start": "2021-01-01", "end": "2026-06-12", "span_years": 5.45},
        },
        "backtests": [
            {
                "name": "portfolio_backtest_realistic",
                "status": "OK",
                "metrics": {
                    "total_return_pct": 120.0,
                    "annual_return_pct": 22.0,
                    "sharpe": 1.15,
                    "max_drawdown_pct": 14.0,
                    "trades": 850,
                    "win_rate_pct": 53.0,
                },
            }
        ],
        "recommendations": [
            {"code": "do_not_promote_strategy_from_single_local_backtest"},
            {"code": "run_walk_forward_and_out_of_sample_validation"},
        ],
        "hermes_contract": {
            "contract": "research_evidence_only",
            "forbidden_use": ["do not approve live or simulation execution from this report alone"],
        },
    }


class StrategyConfigProposalTests(unittest.TestCase):
    def test_build_report_proposes_manual_strategy_config_changes(self):
        payload = proposal.build_report(
            review_payload(),
            {
                "schema": "rt_signal_strategy_config_v1",
                "version": "current",
                "confirmation_thresholds": {
                    "BUY": {"min_full_score": rt.BUY_CONFIRMATION_MIN_SCORE},
                    "SELL": {"max_full_score": rt.SELL_CONFIRMATION_MAX_SCORE},
                },
                "trigger_overrides": {},
            },
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            local_backtest_reliability("OK", promotion_ready=True),
            now=NOW,
        )
        proposed = payload["proposed_config"]
        overrides = proposed["trigger_overrides"]

        self.assertEqual(payload["schema"], "rt_signal_strategy_config_proposal_v1")
        self.assertFalse(payload["source"]["auto_applied"])
        self.assertTrue(payload["source"]["manual_review_required"])
        self.assertEqual(payload["change_count"], 3)
        self.assertFalse(overrides["BUY:weak"]["enabled"])
        self.assertEqual(overrides["SELL:breakdown"]["max_full_score"], -0.55)
        self.assertEqual(overrides["BUY:new"]["review_mode"], "shadow_only_pending_sample")
        self.assertEqual(len(payload["proposal_hash"]), 16)
        self.assertEqual(payload["simulation_performance_context"]["status"], "OK")
        self.assertEqual(payload["promotion_blockers"], [])
        self.assertEqual(payload["promotion_risk_warnings"], [])

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

        payload = proposal.build_report(
            review,
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertEqual(payload["change_count"], 0)
        self.assertEqual(payload["changes"], [])

    def test_failed_simulation_performance_adds_promotion_blocker(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("FAIL"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        self.assertEqual(
            payload["promotion_blockers"][0]["code"],
            "simulation_performance_fail_blocks_strategy_promotion",
        )
        self.assertEqual(payload["simulation_performance_context"]["status"], "FAIL")
        self.assertEqual(
            payload["simulation_performance_context"]["remediation_plan"]["proposal_hash"],
            "feedface12345678",
        )
        self.assertIn(
            "reject_or_hold_new_buy_by_default",
            payload["simulation_performance_context"]["remediation_plan"]["action_ids"],
        )
        self.assertFalse(
            payload["simulation_performance_context"]["remediation_plan"]["operator_contract"]["submits_orders"]
        )

    def test_warning_simulation_performance_adds_review_warning_not_blocker(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("WARN"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertFalse(payload["promotion"]["blocked"])
        self.assertEqual(payload["promotion_blockers"], [])
        self.assertEqual(
            payload["promotion_risk_warnings"][0]["code"],
            "simulation_performance_warn_requires_operator_review",
        )

    def test_stale_simulation_performance_blocks_promotion_even_if_status_ok(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK", generated_at="2026-06-13T07:00:00"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            now=NOW,
            max_simulation_performance_age_minutes=90,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        self.assertEqual(payload["simulation_performance_context"]["status"], "STALE")
        self.assertEqual(payload["simulation_performance_context"]["report_status"], "OK")
        self.assertEqual(
            payload["promotion_blockers"][0]["code"],
            "simulation_performance_stale_blocks_strategy_promotion",
        )
        self.assertEqual(payload["promotion_blockers"][0]["freshness"]["status"], "stale")

    def test_missing_simulation_performance_blocks_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            {},
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        self.assertEqual(payload["simulation_performance_context"]["status"], "MISSING")
        self.assertEqual(
            payload["promotion_blockers"][0]["code"],
            "simulation_performance_missing_blocks_strategy_promotion",
        )

    def test_unknown_simulation_performance_status_blocks_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("REVIEW"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        self.assertEqual(
            payload["promotion_blockers"][0]["code"],
            "simulation_performance_unknown_status_blocks_strategy_promotion",
        )

    def test_execution_readiness_not_ready_blocks_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("BLOCKED"),
            strategy_learning(),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("execution_readiness_not_ready_blocks_strategy_promotion", codes)
        self.assertEqual(payload["execution_readiness_context"]["status"], "BLOCKED")
        self.assertIn("hermes_judgment_effect", payload["execution_readiness_context"]["blocking_gate_names"])

    def test_missing_audit_pass_strategy_learning_blocks_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(audit_pass=False),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("strategy_learning_audit_pass_effect_missing_blocks_strategy_promotion", codes)
        self.assertTrue(payload["strategy_learning_context"]["raw_judgment_effect_present"])

    def test_strategy_learning_audit_gaps_block_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(audit_fail_count=1, audit_missing_count=1),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("strategy_learning_judgment_audit_gaps_block_strategy_promotion", codes)
        self.assertEqual(payload["strategy_learning_context"]["judgment_audit_coverage"]["audit_fail_count"], 1)

    def test_strategy_learning_audit_pass_sample_too_small_blocks_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(approved_resolved=4, rejected_resolved=5),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("strategy_learning_audit_pass_sample_too_small_blocks_strategy_promotion", codes)

    def test_strategy_learning_audit_pass_negative_effect_blocks_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(approved_avg=-0.1, rejected_avg=0.3),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("strategy_learning_audit_pass_effect_not_supportive_blocks_strategy_promotion", codes)
        blocker = [
            row
            for row in payload["promotion_blockers"]
            if row["code"] == "strategy_learning_audit_pass_effect_not_supportive_blocks_strategy_promotion"
        ][0]
        self.assertEqual(blocker["approved_avg_signed_return_pct"], -0.1)
        self.assertEqual(blocker["rejected_avg_signed_return_pct"], 0.3)
        self.assertEqual(blocker["approval_vs_rejection_delta_pct"], -0.4)

    def test_diagnostic_learning_scope_requires_executable_only_evidence(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(
                execution_candidate_scope=execution_candidate_scope(downgraded_directional_count=3),
                execution_candidate_audit_pass=False,
            ),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("strategy_learning_execution_candidate_evidence_missing_blocks_strategy_promotion", codes)
        self.assertEqual(
            payload["strategy_learning_context"]["execution_candidate_scope"]["downgraded_directional_count"],
            3,
        )

    def test_diagnostic_learning_scope_allows_executable_only_audit_evidence(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(
                execution_candidate_scope=execution_candidate_scope(downgraded_directional_count=3),
                execution_candidate_audit_pass=True,
            ),
            trigger_evidence_convergence(),
            local_backtest_reliability("OK", promotion_ready=True),
            now=NOW,
        )

        self.assertFalse(payload["promotion"]["blocked"])
        self.assertEqual(payload["promotion_blockers"], [])
        self.assertTrue(payload["strategy_learning_context"]["has_execution_candidate_audit_pass_judgment_effect"])

    def test_diagnostic_learning_scope_blocks_negative_executable_only_effect(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(
                execution_candidate_scope=execution_candidate_scope(downgraded_directional_count=3),
                execution_candidate_audit_pass=True,
                execution_approved_avg=0.1,
                execution_rejected_avg=0.4,
            ),
            trigger_evidence_convergence(),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn(
            "strategy_learning_execution_candidate_audit_pass_effect_not_supportive_blocks_strategy_promotion",
            codes,
        )

    def test_missing_trigger_convergence_blocks_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(),
            {},
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("trigger_evidence_convergence_missing_blocks_strategy_promotion", codes)
        self.assertEqual(payload["trigger_evidence_convergence_context"]["status"], "MISSING")
        self.assertEqual(payload["source"]["trigger_evidence_convergence_schema"], None)

    def test_trigger_convergence_risk_and_insufficient_forward_sample_block_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(
                status="REVIEW_REQUIRED",
                replay_challenges_forward_count=1,
                insufficient_forward_sample_count=1,
            ),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("trigger_evidence_convergence_risk_blocks_strategy_promotion", codes)
        self.assertIn("trigger_evidence_forward_sample_insufficient_blocks_strategy_promotion", codes)
        context = payload["trigger_evidence_convergence_context"]
        self.assertEqual(context["replay_challenges_forward_count"], 1)
        self.assertEqual(context["insufficient_forward_sample_count"], 1)
        self.assertEqual(context["top_risk_triggers"][0]["key"], "BUY:weak")
        self.assertFalse(context["operator_contract"]["submits_orders"])
        self.assertFalse(context["operator_contract"]["changes_strategy_config"])

    def test_local_backtest_research_only_adds_review_warning_not_blocker(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            local_backtest_reliability(),
            now=NOW,
        )

        self.assertFalse(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_risk_warnings"]]
        self.assertIn("local_backtest_research_only_requires_operator_review", codes)
        self.assertEqual(payload["local_backtest_reliability_context"]["status"], "RESEARCH_USEFUL_WITH_LIMITATIONS")
        self.assertEqual(payload["local_backtest_reliability_context"]["backtests"][0]["sharpe"], 1.15)
        self.assertFalse(payload["local_backtest_reliability_context"]["source_contract"]["changes_v5"])

    def test_local_backtest_hard_failure_blocks_promotion(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            local_backtest_reliability("INSUFFICIENT_EVIDENCE"),
            now=NOW,
        )

        self.assertTrue(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertIn("local_backtest_insufficient_evidence_blocks_strategy_promotion", codes)
        self.assertEqual(payload["local_backtest_reliability_context"]["dataset_status"], "FAIL")

    def test_missing_local_backtest_adds_review_warning_not_blocker(self):
        payload = proposal.build_report(
            review_payload(),
            {"schema": "rt_signal_strategy_config_v1"},
            simulation_performance("OK"),
            execution_readiness("READY"),
            strategy_learning(),
            trigger_evidence_convergence(),
            {},
            now=NOW,
        )

        self.assertFalse(payload["promotion"]["blocked"])
        codes = [row["code"] for row in payload["promotion_risk_warnings"]]
        self.assertIn("local_backtest_reliability_missing_requires_operator_review", codes)
        self.assertEqual(payload["local_backtest_reliability_context"]["status"], "MISSING")


if __name__ == "__main__":
    unittest.main()
