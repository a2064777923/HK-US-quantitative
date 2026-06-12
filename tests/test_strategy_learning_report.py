import json
import tempfile
import unittest
from pathlib import Path

from scripts import strategy_learning_report as learning


def alert(signal_id="sig-1", trigger="MA"):
    return {
        "signal_id": signal_id,
        "symbol": "00700",
        "market": "HK",
        "signal_type": "BUY",
        "trigger": trigger,
        "confirmed": True,
        "full_score": 0.7,
        "strategy_config_id": "cfg",
        "watchlist_id": "wl",
        "generated_at": "2026-06-12T10:00:00",
    }


def judgment(signal_id="sig-1", decision="approve"):
    return {
        "schema": "hermes_trade_judgment_v1",
        "packet_id": "packet-1",
        "signal_id": signal_id,
        "decision": decision,
        "confidence": 0.8,
        "reviewed_at": "2026-06-12T10:05:00",
        "supporting_factors": ["support"],
        "opposing_factors": ["opposition"],
        "risk_notes": ["risk"],
    }


def intake_decision(signal_id="sig-1", status="dry_run", reason=None):
    item = {
        "signal_id": signal_id,
        "status": status,
        "mode": "dry-run",
        "plan": {"symbol": "00700", "side": "buy", "quantity": 100},
        "checked_at": "2026-06-12T10:06:00",
    }
    if reason:
        item["reasons"] = [reason]
    return item


def outcome(signal_id="sig-1", value=1.0, trigger="MA"):
    return {
        "signal_id": signal_id,
        "symbol": "00700",
        "market": "HK",
        "signal_type": "BUY",
        "trigger": trigger,
        "confirmed": True,
        "status": "resolved",
        "strategy_config_id": "cfg",
        "watchlist_id": "wl",
        "outcomes": {
            "1d": {
                "status": "resolved",
                "signed_close_return_pct": value,
                "win": value > 0,
            }
        },
    }


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class StrategyLearningReportTests(unittest.TestCase):
    def test_build_report_joins_alert_judgment_intake_and_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            alerts = root / "alerts.jsonl"
            judgments = root / "judgments.jsonl"
            state = root / "state.json"
            outcomes = root / "outcome.json"
            write_jsonl(alerts, [alert("sig-1", "MA"), alert("sig-2", "RSI")])
            write_jsonl(judgments, [judgment("sig-1", "approve"), judgment("sig-2", "hold")])
            state.write_text(
                json.dumps(
                    {
                        "dry_runs": {
                            "sig-1": intake_decision("sig-1", "dry_run"),
                            "sig-2": intake_decision("sig-2", "rejected", "strategy_evidence_gate_failed"),
                        },
                        "processed": {},
                    }
                ),
                encoding="utf-8",
            )
            outcomes.write_text(
                json.dumps(
                    {
                        "schema": "rt_signal_outcome_report_v1",
                        "generated_at": "2026-06-12T11:00:00",
                        "status": "INSUFFICIENT_SAMPLE",
                        "evaluated_signal_count": 2,
                        "resolved_signal_count": 2,
                        "pending_signal_count": 0,
                        "evaluations": [outcome("sig-1", 1.5, "MA"), outcome("sig-2", -2.0, "RSI")],
                    }
                ),
                encoding="utf-8",
            )

            payload = learning.build_report(
                alert_queue_file=str(alerts),
                judgment_file=str(judgments),
                intake_state_file=str(state),
                outcome_report_file=str(outcomes),
                queue_scan_limit=50,
            )

        self.assertEqual(payload["schema"], "strategy_learning_report_v1")
        self.assertEqual(payload["sample_scope"]["mode"], "latest_strategy_config_and_watchlist")
        self.assertEqual(payload["join_counts"]["joined_signal_count"], 2)
        self.assertEqual(payload["join_counts"]["signals_with_judgment_and_outcome"], 2)
        self.assertEqual(payload["overall"]["resolved_count"], 2)
        self.assertEqual(payload["overall"]["avg_signed_return_pct"], -0.25)
        self.assertEqual(payload["judgment_effect"]["approved_or_reduced"]["avg_signed_return_pct"], 1.5)
        self.assertEqual(payload["judgment_effect"]["rejected_or_held"]["avg_signed_return_pct"], -2.0)
        by_reason = {row["key"]: row for row in payload["by_intake_reason"]}
        self.assertEqual(by_reason["accepted_dry_run"]["count"], 1)
        self.assertEqual(by_reason["strategy_evidence_gate_failed"]["count"], 1)
        by_actionability = {row["key"]: row for row in payload["by_actionability"]}
        self.assertEqual(by_actionability["trade_candidate"]["count"], 1)
        self.assertEqual(by_actionability["blocked_strategy_evidence"]["count"], 1)
        self.assertEqual(payload["recent_joined_rows"][0]["actionability_category"], "trade_candidate")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])

    def test_default_sample_scope_filters_to_latest_strategy_and_watchlist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            alerts = root / "alerts.jsonl"
            judgments = root / "judgments.jsonl"
            state = root / "state.json"
            outcomes = root / "outcome.json"
            old_alert = alert("old", "MA")
            old_alert.update({"strategy_config_id": "cfg-old", "watchlist_id": "wl-old", "generated_at": "2026-06-12T09:00:00"})
            current_alert = alert("current", "RSI")
            current_alert.update({"strategy_config_id": "cfg-new", "watchlist_id": "wl-new", "generated_at": "2026-06-12T10:00:00"})
            write_jsonl(alerts, [old_alert, current_alert])
            write_jsonl(judgments, [])
            state.write_text(
                json.dumps(
                    {
                        "dry_runs": {
                            "old": intake_decision("old", "rejected", "quantity_zero_after_risk_and_lot_rounding"),
                            "current": intake_decision("current", "dry_run"),
                        },
                        "processed": {},
                    }
                ),
                encoding="utf-8",
            )
            outcomes.write_text(
                json.dumps(
                    {
                        "schema": "rt_signal_outcome_report_v1",
                        "evaluations": [
                            outcome("old", -1.0, "MA"),
                            outcome("current", 2.0, "RSI"),
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = learning.build_report(
                alert_queue_file=str(alerts),
                judgment_file=str(judgments),
                intake_state_file=str(state),
                outcome_report_file=str(outcomes),
            )
            all_payload = learning.build_report(
                alert_queue_file=str(alerts),
                judgment_file=str(judgments),
                intake_state_file=str(state),
                outcome_report_file=str(outcomes),
                sample_scope_mode="all",
            )

        self.assertEqual(payload["sample_scope"]["strategy_config_id"], "cfg-new")
        self.assertEqual(payload["sample_scope"]["watchlist_id"], "wl-new")
        self.assertEqual(payload["sample_scope"]["excluded_joined_signal_count"], 1)
        self.assertEqual(payload["all_join_counts"]["joined_signal_count"], 2)
        self.assertEqual(payload["join_counts"]["joined_signal_count"], 1)
        self.assertEqual(payload["overall"]["avg_signed_return_pct"], 2.0)
        self.assertEqual(payload["by_intake_reason"][0]["key"], "accepted_dry_run")
        self.assertEqual(payload["intake_coverage"]["coverage_pct"], 100.0)
        self.assertEqual(all_payload["sample_scope"]["mode"], "all_joined_signals")
        self.assertEqual(all_payload["join_counts"]["joined_signal_count"], 2)

    def test_intake_coverage_reports_missing_decisions_without_dominant_blocker(self):
        rows = [
            {
                "signal_id": "missing-1",
                "symbol": "00700",
                "signal_type": "WATCH",
                "trigger_key": "BUY:MA",
                "generated_at": "2026-06-12T10:00:00",
                "strategy_config_id": "cfg",
                "watchlist_id": "wl",
                "intake_reason_bucket": "missing_intake_decision",
            },
            {
                "signal_id": "missing-2",
                "symbol": "03988",
                "signal_type": "WATCH",
                "trigger_key": "BUY:MA",
                "generated_at": "2026-06-12T10:01:00",
                "strategy_config_id": "cfg",
                "watchlist_id": "wl",
                "intake_reason_bucket": "missing_intake_decision",
            },
            {
                "signal_id": "accepted",
                "symbol": "06690",
                "signal_type": "BUY",
                "trigger_key": "BUY:RSI",
                "generated_at": "2026-06-12T10:02:00",
                "strategy_config_id": "cfg",
                "watchlist_id": "wl",
                "intake_reason_bucket": "accepted_dry_run",
            },
        ]
        coverage = learning.build_intake_coverage(rows)
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intake_reason": [{"key": "missing_intake_decision", "count": 2}],
            "by_actionability": [{"key": "missing_intake_decision", "count": 2}],
            "intake_coverage": {
                "joined_signal_count": 10,
                "with_intake_decision_count": 4,
                "missing_intake_decision_count": 6,
                "coverage_pct": 40.0,
                "directional": {
                    "joined_signal_count": 4,
                    "with_intake_decision_count": 4,
                    "missing_intake_decision_count": 0,
                    "coverage_pct": 100.0,
                },
            },
            "sizing_blocker_diagnostics": {"by_binding_limit": []},
        }

        recs = learning.build_recommendations(payload)

        self.assertEqual(coverage["joined_signal_count"], 3)
        self.assertEqual(coverage["with_intake_decision_count"], 1)
        self.assertEqual(coverage["missing_intake_decision_count"], 2)
        self.assertEqual(coverage["coverage_pct"], 33.33)
        self.assertEqual(coverage["directional"]["coverage_pct"], 100.0)
        self.assertEqual(coverage["watch"]["coverage_pct"], 0.0)
        self.assertEqual(coverage["missing_by_trigger"][0], {"key": "BUY:MA", "count": 2})
        self.assertIn("overall_intake_coverage_below_50pct_due_to_observations", recs)
        self.assertNotIn("dominant_intake_blocker:missing_intake_decision", recs)

    def test_low_directional_intake_coverage_is_learning_incomplete(self):
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intake_reason": [{"key": "missing_intake_decision", "count": 8}],
            "by_actionability": [{"key": "missing_intake_decision", "count": 8}],
            "intake_coverage": {
                "joined_signal_count": 10,
                "with_intake_decision_count": 2,
                "missing_intake_decision_count": 8,
                "coverage_pct": 20.0,
                "directional": {
                    "joined_signal_count": 10,
                    "with_intake_decision_count": 2,
                    "missing_intake_decision_count": 8,
                    "coverage_pct": 20.0,
                },
            },
            "sizing_blocker_diagnostics": {"by_binding_limit": []},
        }

        recs = learning.build_recommendations(payload)

        self.assertIn("directional_intake_coverage_below_80pct_learning_incomplete", recs)

    def test_recommends_collecting_when_resolved_sample_is_small(self):
        payload = learning.build_report(
            alert_queue_file="/missing-alerts",
            judgment_file="/missing-judgments",
            intake_state_file="/missing-state",
            outcome_report_file="/missing-outcome",
        )

        self.assertIn("learning_sample_below_5_keep_collecting_outcomes", payload["recommendations"])
        self.assertEqual(payload["overall"]["resolved_count"], 0)
        self.assertNotIn("dominant_intake_blocker:no_reason", payload["recommendations"])

    def test_missing_intake_is_not_reported_as_dominant_blocker(self):
        rows = [{"intake_reason_bucket": "missing_intake_decision"} for _ in range(10)]
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intake_reason": [{"key": "missing_intake_decision", "count": 10}],
        }

        recs = learning.build_recommendations(payload)

        self.assertNotIn("dominant_intake_blocker:missing_intake_decision", recs)

    def test_sell_without_position_is_observation_actionability_not_blocker(self):
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intake_reason": [{"key": "sell_without_position", "count": 10}],
            "by_actionability": [{"key": "observation_only_no_position", "count": 10}],
        }

        row = learning.build_join_rows(
            alerts={"sig-sell": alert("sig-sell")},
            judgments={},
            intake_decisions={
                "sig-sell": intake_decision("sig-sell", status="rejected", reason="sell_without_position")
            },
            outcomes={},
        )[0]
        recs = learning.build_recommendations(payload)

        self.assertEqual(row["actionability_category"], "observation_only_no_position")
        self.assertNotIn("dominant_intake_blocker:sell_without_position", recs)
        self.assertNotIn("dominant_actionability_blocker:observation_only_no_position", recs)

    def test_stale_alert_is_observation_actionability_not_blocker(self):
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intake_reason": [{"key": "alert_too_old", "count": 10}],
            "by_actionability": [{"key": "observation_only_stale_alert", "count": 10}],
        }

        row = learning.build_join_rows(
            alerts={"sig-old": alert("sig-old")},
            judgments={},
            intake_decisions={"sig-old": intake_decision("sig-old", status="rejected", reason="alert_too_old")},
            outcomes={},
        )[0]
        recs = learning.build_recommendations(payload)

        self.assertEqual(row["actionability_category"], "observation_only_stale_alert")
        self.assertNotIn("dominant_intake_blocker:alert_too_old", recs)
        self.assertNotIn("dominant_actionability_blocker:observation_only_stale_alert", recs)

    def test_sizing_blocker_diagnostics_explain_zero_after_lot_rounding(self):
        expensive = alert("sig-size")
        expensive.update(
            {
                "entry_price": 300,
                "stop_loss": 280,
                "take_profit": 360,
            }
        )
        decision = intake_decision(
            "sig-size",
            status="rejected",
            reason="quantity_zero_after_risk_and_lot_rounding",
        )
        decision["context"] = {"cash_hkd": 10_000, "equity_hkd": 100_000, "positions": []}

        row = learning.build_join_rows(
            alerts={"sig-size": expensive},
            judgments={},
            intake_decisions={"sig-size": decision},
            outcomes={},
        )[0]
        sizing_summary = learning.build_sizing_blocker_diagnostics([row])
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intake_reason": [{"key": "quantity_zero_after_risk_and_lot_rounding", "count": 5}],
            "by_actionability": [{"key": "blocked_sizing_or_lot", "count": 5}],
            "sizing_blocker_diagnostics": {
                "by_binding_limit": [{"key": "allocation_budget_below_one_lot", "count": 5}]
            },
        }

        recs = learning.build_recommendations(payload)
        diag = row["sizing_diagnostics"]

        self.assertEqual(row["actionability_category"], "blocked_sizing_or_lot")
        self.assertEqual(diag["status"], "diagnosed")
        self.assertEqual(diag["lot_size"], 100)
        self.assertEqual(diag["rounded_quantity"], 0)
        self.assertIn("allocation_budget_below_one_lot", diag["binding_limits"])
        self.assertIn("risk_budget_below_one_lot", diag["binding_limits"])
        self.assertEqual(sizing_summary["count"], 1)
        self.assertEqual(sizing_summary["by_symbol"][0]["key"], "00700")
        self.assertIn("review_sizing_rule:allocation_budget_below_one_lot", recs)

    def test_sizing_blocker_remediation_links_to_watchlist_proposal(self):
        row = {
            "signal_id": "sig-size",
            "symbol": "00700",
            "market": "HK",
            "sizing_diagnostics": {"binding_limits": ["allocation_budget_below_one_lot"]},
        }
        watchlist_diff = {
            "schema": "watchlist_diff_report_v1",
            "proposal": {
                "proposal_hash": "abc123",
                "markets": {
                    "HK": {"remove_symbols": ["00700"]},
                    "US": {"remove_symbols": []},
                },
            },
        }
        remediation = learning.build_sizing_blocker_remediation([row], watchlist_diff)
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intake_reason": [{"key": "quantity_zero_after_risk_and_lot_rounding", "count": 5}],
            "by_actionability": [{"key": "blocked_sizing_or_lot", "count": 5}],
            "intake_coverage": {},
            "sizing_blocker_diagnostics": {
                "by_binding_limit": [{"key": "allocation_budget_below_one_lot", "count": 5}]
            },
            "sizing_blocker_remediation": {
                "sizing_blocker_count": 5,
                "covered_by_watchlist_removal_count": 5,
                "uncovered_count": 0,
                "watchlist_proposal_hash": "abc123",
            },
        }

        recs = learning.build_recommendations(payload)

        self.assertEqual(remediation["covered_by_watchlist_removal_count"], 1)
        self.assertEqual(remediation["uncovered_count"], 0)
        self.assertEqual(remediation["covered_symbols"], ["00700"])
        self.assertEqual(remediation["watchlist_proposal_hash"], "abc123")
        self.assertIn("review_watchlist_proposal_for_sizing_blockers:abc123", recs)
        self.assertNotIn("dominant_intake_blocker:quantity_zero_after_risk_and_lot_rounding", recs)
        self.assertNotIn("dominant_actionability_blocker:blocked_sizing_or_lot", recs)
        self.assertNotIn("review_sizing_rule:allocation_budget_below_one_lot", recs)


if __name__ == "__main__":
    unittest.main()
