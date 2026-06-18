import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import hermes_judgment_audit_report as audit
from scripts import strategy_learning_report as learning


def context_review(**overrides):
    item = {
        "technical_signal_reviewed": True,
        "portfolio_risk_reviewed": True,
        "strategy_evidence_reviewed": True,
        "data_health_reviewed": True,
        "execution_readiness_reviewed": True,
        "market_context_reviewed": True,
        "intraday_context_reviewed": True,
        "external_market_context_reviewed": True,
        "event_catalysts_reviewed": True,
        "event_catalyst_signals_reviewed": True,
        "market_sentiment_reviewed": True,
        "fundamentals_context_reviewed": True,
        "source_reliability_reviewed": True,
        "simulation_performance_reviewed": True,
        "cron_wiring_reviewed": True,
        "notes": ["unit test context reviewed"],
    }
    item.update(overrides)
    return item


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
        "context_review": context_review(),
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


def outcome(signal_id="sig-1", value=1.0, trigger="MA", intraday_alignment=None):
    item = {
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
    if intraday_alignment:
        item["intraday_signal_context"] = {
            "schema": "intraday_signal_context_v1",
            "status": "OK",
            "alignment": intraday_alignment,
        }
    return item

def audit_row(signal_id="sig-1", decision="approve", status="PASS", reasons=None):
    return {
        "signal_id": signal_id,
        "decision": decision,
        "reviewed_at": "2026-06-12T10:05:00",
        "confidence": 0.8,
        "packet_id": "packet-1",
        "packet_source": "packet_archive",
        "status": status,
        "reasons": reasons or [],
    }


def audit_report(rows):
    status_counts = {}
    reason_counts = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        for reason in row.get("reasons") or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "schema": "hermes_judgment_audit_report_v1",
        "generated_at": "2026-06-12T10:10:00",
        "status": "FAIL" if status_counts.get("FAIL") else "OK",
        "counts": {
            "judgment_count": len(rows),
            "status_counts": status_counts,
            "reason_counts": reason_counts,
        },
        "judgments": rows,
        "recommendations": [],
    }


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class StrategyLearningReportTests(unittest.TestCase):
    def test_context_review_required_flags_match_judgment_audit(self):
        self.assertEqual(
            learning.REQUIRED_CONTEXT_REVIEW_FLAGS,
            audit.REQUIRED_CONTEXT_REVIEW_FLAGS,
        )

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
                        "evaluations": [
                            outcome("sig-1", 1.5, "MA", intraday_alignment="supports_signal"),
                            outcome("sig-2", -2.0, "RSI", intraday_alignment="challenges_signal"),
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
        self.assertEqual(
            payload["context_review_effect"]["approved_or_reduced_context_complete"]["avg_signed_return_pct"],
            1.5,
        )
        self.assertEqual(payload["context_review_quality"]["complete_context_review_count"], 1)
        self.assertEqual(payload["context_review_quality"]["incomplete_context_review_count"], 0)
        by_reason = {row["key"]: row for row in payload["by_intake_reason"]}
        self.assertEqual(by_reason["accepted_dry_run"]["count"], 1)
        self.assertEqual(by_reason["strategy_evidence_gate_failed"]["count"], 1)
        by_actionability = {row["key"]: row for row in payload["by_actionability"]}
        self.assertEqual(by_actionability["trade_candidate"]["count"], 1)
        self.assertEqual(by_actionability["blocked_strategy_evidence"]["count"], 1)
        by_intraday = {row["key"]: row for row in payload["by_intraday_signal_alignment"]}
        self.assertEqual(by_intraday["supports_signal"]["avg_signed_return_pct"], 1.5)
        self.assertEqual(by_intraday["challenges_signal"]["avg_signed_return_pct"], -2.0)
        self.assertEqual(payload["recent_joined_rows"][0]["intraday_signal_alignment"], "supports_signal")
        self.assertEqual(payload["recent_joined_rows"][0]["actionability_category"], "trade_candidate")
        self.assertEqual(payload["intraday_alignment_effect"]["status"], "INSUFFICIENT")
        self.assertIn(
            "support_alignment_sample_below_minimum",
            payload["intraday_alignment_effect"]["reasons"],
        )
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])

    def test_context_review_effect_tracks_incomplete_approval_cohort(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            alerts = root / "alerts.jsonl"
            judgments = root / "judgments.jsonl"
            state = root / "state.json"
            outcomes = root / "outcome.json"
            write_jsonl(alerts, [alert("complete", "MA"), alert("incomplete", "RSI"), alert("held", "BB")])
            incomplete = judgment("incomplete", "approve")
            incomplete["context_review"] = context_review(market_sentiment_reviewed=False)
            write_jsonl(
                judgments,
                [
                    judgment("complete", "approve"),
                    incomplete,
                    judgment("held", "hold"),
                ],
            )
            state.write_text(
                json.dumps(
                    {
                        "dry_runs": {
                            "complete": intake_decision("complete", "dry_run"),
                            "incomplete": intake_decision("incomplete", "dry_run"),
                            "held": intake_decision("held", "dry_run"),
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
                            outcome("complete", 2.0, "MA"),
                            outcome("incomplete", -1.0, "RSI"),
                            outcome("held", -0.5, "BB"),
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

        effect = payload["context_review_effect"]
        quality = payload["context_review_quality"]
        by_cohort = {row["key"]: row for row in payload["by_context_review_cohort"]}

        self.assertEqual(effect["approved_or_reduced_context_complete"]["resolved_count"], 1)
        self.assertEqual(effect["approved_or_reduced_context_complete"]["avg_signed_return_pct"], 2.0)
        self.assertEqual(effect["approved_or_reduced_context_incomplete"]["avg_signed_return_pct"], -1.0)
        self.assertEqual(quality["approved_or_reduced_count"], 2)
        self.assertEqual(quality["complete_context_review_count"], 1)
        self.assertEqual(quality["incomplete_context_review_count"], 1)
        self.assertEqual(quality["missing_flag_counts"][0], {"key": "market_sentiment_reviewed", "count": 1})
        self.assertEqual(by_cohort["approved_or_reduced_context_incomplete"]["count"], 1)
        self.assertIn("context_review_incomplete_for_approved_or_reduced_judgments", payload["recommendations"])

    def test_audit_failed_approval_is_excluded_from_audit_pass_effect(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            alerts = root / "alerts.jsonl"
            judgments = root / "judgments.jsonl"
            audit_file = root / "judgment_audit.json"
            state = root / "state.json"
            outcomes = root / "outcome.json"
            write_jsonl(alerts, [alert("clean", "MA"), alert("invalid", "RSI"), alert("held", "BB")])
            write_jsonl(
                judgments,
                [
                    judgment("clean", "approve"),
                    judgment("invalid", "approve"),
                    judgment("held", "hold"),
                ],
            )
            audit_file.write_text(
                json.dumps(
                    audit_report(
                        [
                            audit_row("clean", "approve", "PASS"),
                            audit_row(
                                "invalid",
                                "approve",
                                "FAIL",
                                ["missing_intraday_context_acknowledgement"],
                            ),
                            audit_row("held", "hold", "PASS"),
                        ]
                    )
                ),
                encoding="utf-8",
            )
            state.write_text(
                json.dumps(
                    {
                        "dry_runs": {
                            "clean": intake_decision("clean", "dry_run"),
                            "invalid": intake_decision("invalid", "dry_run"),
                            "held": intake_decision("held", "dry_run"),
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
                            outcome("clean", 2.0, "MA"),
                            outcome("invalid", 10.0, "RSI"),
                            outcome("held", -1.0, "BB"),
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = learning.build_report(
                alert_queue_file=str(alerts),
                judgment_file=str(judgments),
                judgment_audit_file=str(audit_file),
                intake_state_file=str(state),
                outcome_report_file=str(outcomes),
            )

        raw_effect = payload["judgment_effect"]
        audit_effect = payload["audit_pass_judgment_effect"]
        coverage = payload["judgment_audit_coverage"]
        rows_by_id = {row["signal_id"]: row for row in payload["recent_joined_rows"]}

        self.assertEqual(raw_effect["approved_or_reduced"]["avg_signed_return_pct"], 6.0)
        self.assertEqual(audit_effect["sample_filter"], "judgment_audit_status_PASS")
        self.assertEqual(audit_effect["approved_or_reduced"]["resolved_count"], 1)
        self.assertEqual(audit_effect["approved_or_reduced"]["avg_signed_return_pct"], 2.0)
        self.assertEqual(audit_effect["excluded_approved_or_reduced"]["resolved_count"], 1)
        self.assertEqual(audit_effect["excluded_approved_or_reduced"]["avg_signed_return_pct"], 10.0)
        self.assertEqual(coverage["approved_or_reduced_audit_fail_or_missing_count"], 1)
        self.assertEqual(coverage["failed_reason_counts"][0], {"key": "missing_intraday_context_acknowledgement", "count": 1})
        self.assertEqual(rows_by_id["invalid"]["judgment_audit_status"], "FAIL")
        self.assertIn("audit_failed_or_missing_approvals_excluded_from_hermes_effect_learning", payload["recommendations"])

    def test_missing_source_reliability_review_flag_marks_context_incomplete(self):
        item = judgment("sig-1", "approve")
        item["context_review"].pop("source_reliability_reviewed")

        row = learning.build_join_rows(
            alerts={"sig-1": alert("sig-1")},
            judgments={"sig-1": item},
            intake_decisions={"sig-1": intake_decision("sig-1", "dry_run")},
            outcomes={"sig-1": outcome("sig-1", 1.0)},
        )[0]

        self.assertFalse(row["context_review_complete"])
        self.assertIn("source_reliability_reviewed", row["context_review_missing_flags"])
        self.assertEqual(row["context_review_cohort"], "approved_or_reduced_context_incomplete")

    def test_missing_intraday_review_flag_marks_context_incomplete(self):
        item = judgment("sig-1", "approve")
        item["context_review"].pop("intraday_context_reviewed")

        row = learning.build_join_rows(
            alerts={"sig-1": alert("sig-1")},
            judgments={"sig-1": item},
            intake_decisions={"sig-1": intake_decision("sig-1", "dry_run")},
            outcomes={"sig-1": outcome("sig-1", 1.0)},
        )[0]
        quality = learning.build_context_review_quality([row])

        self.assertFalse(row["context_review_complete"])
        self.assertIn("intraday_context_reviewed", row["context_review_missing_flags"])
        self.assertIn("intraday_context_reviewed", quality["required_flags"])
        self.assertEqual(row["context_review_cohort"], "approved_or_reduced_context_incomplete")

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

            with patch.object(
                learning.rt_runtime_scope,
                "current_runtime_sample_scope",
                return_value={"mode": "runtime_scope_unavailable"},
            ):
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

    def test_default_sample_scope_prefers_runtime_strategy_watchlist_over_latest_old_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            alerts = root / "alerts.jsonl"
            judgments = root / "judgments.jsonl"
            state = root / "state.json"
            outcomes = root / "outcome.json"
            runtime_alert = alert("runtime", "RSI")
            runtime_alert.update(
                {"strategy_config_id": "cfg-runtime", "watchlist_id": "wl-current", "generated_at": "2026-06-12T09:00:00"}
            )
            old_latest = alert("old-latest", "MA")
            old_latest.update(
                {"strategy_config_id": "cfg-old", "watchlist_id": "wl-current", "generated_at": "2026-06-12T10:00:00"}
            )
            write_jsonl(alerts, [runtime_alert, old_latest])
            write_jsonl(judgments, [])
            state.write_text(
                json.dumps(
                    {
                        "dry_runs": {
                            "runtime": intake_decision("runtime", "dry_run"),
                            "old-latest": intake_decision("old-latest", "dry_run"),
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
                            outcome("runtime", 2.0, "RSI"),
                            outcome("old-latest", -1.0, "MA"),
                        ],
                    }
                ),
                encoding="utf-8",
            )
            runtime_scope = {
                "mode": "runtime_strategy_config_and_watchlist",
                "strategy_config_id": "cfg-runtime",
                "watchlist_id": "wl-current",
            }

            with patch.object(learning.rt_runtime_scope, "current_runtime_sample_scope", return_value=runtime_scope):
                payload = learning.build_report(
                    alert_queue_file=str(alerts),
                    judgment_file=str(judgments),
                    intake_state_file=str(state),
                    outcome_report_file=str(outcomes),
                )

        self.assertEqual(payload["sample_scope"]["mode"], "runtime_strategy_config_and_watchlist")
        self.assertEqual(payload["sample_scope"]["strategy_config_id"], "cfg-runtime")
        self.assertEqual(payload["sample_scope"]["excluded_joined_signal_count"], 1)
        self.assertEqual(payload["overall"]["avg_signed_return_pct"], 2.0)

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

    def test_intake_coverage_uses_decision_observation_window_when_state_starts_late(self):
        rows = [
            {
                "signal_id": "old-missing",
                "symbol": "AAPL",
                "signal_type": "BUY",
                "trigger_key": "BUY:MA",
                "generated_at": "2026-06-12T09:00:00",
                "intake_reason_bucket": "missing_intake_decision",
            },
            {
                "signal_id": "new-covered",
                "symbol": "MSFT",
                "signal_type": "BUY",
                "trigger_key": "BUY:MA",
                "generated_at": "2026-06-12T10:01:00",
                "intake_status": "dry_run",
                "intake_ledger": "dry_runs",
                "intake_reason_bucket": "accepted_dry_run",
                "intake_checked_at": "2026-06-12T10:01:30",
            },
            {
                "signal_id": "new-watch",
                "symbol": "MSFT",
                "signal_type": "WATCH",
                "trigger_key": "WATCH:RSI",
                "generated_at": "2026-06-12T10:02:00",
                "intake_reason_bucket": "missing_intake_decision",
            },
        ]

        coverage = learning.build_intake_coverage(rows)

        self.assertEqual(coverage["coverage_scope"], "intake_observation_window")
        self.assertEqual(coverage["observation_window"]["starts_at"], "2026-06-12T10:01:00")
        self.assertEqual(coverage["all_time"]["directional"]["coverage_pct"], 50.0)
        self.assertEqual(coverage["directional"]["joined_signal_count"], 1)
        self.assertEqual(coverage["directional"]["coverage_pct"], 100.0)
        self.assertEqual(coverage["watch"]["joined_signal_count"], 1)
        self.assertEqual(coverage["watch"]["coverage_pct"], 0.0)
        self.assertEqual(coverage["joined_signal_count"], 2)
        self.assertEqual(coverage["coverage_pct"], 50.0)

    def test_low_directional_intake_coverage_is_learning_incomplete(self):
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intraday_signal_alignment": [],
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

    def test_intraday_challenge_alignment_recommendation_when_underperforming(self):
        rows = [
            {
                "signal_id": f"c{i}",
                "signed_return_pct": -1.0,
                "intraday_signal_alignment": "challenges_signal",
                "trigger_key": "BUY:MA",
                "judgment_decision": "missing",
                "context_review_cohort": "missing_judgment",
                "intake_reason_bucket": "missing_intake_decision",
                "actionability_category": "missing_intake_decision",
            }
            for i in range(learning.MIN_LEARNING_SAMPLE)
        ]
        payload = {
            "overall": learning.metric_summary(rows),
            "judgment_effect": learning.compare_judgment_effect(rows),
            "by_trigger": learning.grouped_summary(rows, lambda row: row["trigger_key"]),
            "by_intraday_signal_alignment": learning.grouped_summary(
                rows,
                lambda row: row["intraday_signal_alignment"],
            ),
            "by_intake_reason": learning.grouped_summary(rows, lambda row: row["intake_reason_bucket"]),
            "by_actionability": learning.grouped_summary(rows, lambda row: row["actionability_category"]),
            "intake_coverage": {"directional": {}},
        }

        recs = learning.build_recommendations(payload)

        self.assertIn("intraday_challenge_alignment_underperforms_consider_hermes_hold_rule", recs)

    def test_intraday_alignment_effect_classifies_supportive_forward_evidence(self):
        rows = [
            {"signal_id": "s1", "signed_return_pct": 1.0, "intraday_signal_alignment": "supports_signal"},
            {"signal_id": "s2", "signed_return_pct": 2.0, "intraday_signal_alignment": "supports_with_limits"},
            {"signal_id": "c1", "signed_return_pct": -1.0, "intraday_signal_alignment": "challenges_signal"},
            {"signal_id": "c2", "signed_return_pct": -0.5, "intraday_signal_alignment": "challenges_signal"},
        ]

        effect = learning.intraday_alignment_effect(rows, minimum_sample=2)

        self.assertEqual(effect["schema"], "intraday_alignment_effect_v1")
        self.assertEqual(effect["status"], "SUPPORTIVE")
        self.assertEqual(effect["supports_signal_like"]["resolved_count"], 2)
        self.assertEqual(effect["challenges_signal"]["avg_signed_return_pct"], -0.75)
        self.assertEqual(effect["support_vs_challenge_delta_pct"], 2.25)
        self.assertEqual(effect["reasons"], [])
        self.assertEqual(
            effect["policy"],
            "use_support_as_soft_confirmation_and_challenge_as_confidence_cap",
        )

    def test_intraday_alignment_effect_blocks_promotion_when_challenges_are_profitable(self):
        rows = [
            {"signal_id": "s1", "signed_return_pct": 0.3, "intraday_signal_alignment": "supports_signal"},
            {"signal_id": "s2", "signed_return_pct": 0.4, "intraday_signal_alignment": "supports_signal"},
            {"signal_id": "c1", "signed_return_pct": 1.0, "intraday_signal_alignment": "challenges_signal"},
            {"signal_id": "c2", "signed_return_pct": 1.2, "intraday_signal_alignment": "challenges_signal"},
        ]

        effect = learning.intraday_alignment_effect(rows, minimum_sample=2)

        self.assertEqual(effect["status"], "NEGATIVE")
        self.assertIn("challenge_alignment_avg_return_positive", effect["reasons"])
        self.assertIn("support_alignment_not_outperforming_challenge", effect["reasons"])

    def test_intraday_alignment_effect_normalizes_legacy_labels(self):
        rows = [
            {
                "signal_id": "legacy-conflict",
                "signed_return_pct": None,
                "intraday_signal_alignment": "conflicting_intraday_context",
            },
            {
                "signal_id": "legacy-missing",
                "signed_return_pct": None,
                "intraday_signal_alignment": "missing_minute_rows_before_signal",
            },
        ]

        effect = learning.intraday_alignment_effect(rows, minimum_sample=1)
        group_keys = {row["key"] for row in effect["groups"]}

        self.assertIn("conflicting_timeframes", group_keys)
        self.assertIn("unavailable_or_stale", group_keys)
        self.assertNotIn("conflicting_intraday_context", group_keys)

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

    def test_dry_run_execution_readiness_would_block_is_not_trade_candidate(self):
        decision = intake_decision("sig-ready", status="dry_run")
        decision["execution_readiness"] = {
            "status": "DRY_RUN_ONLY",
            "would_block_execute": True,
            "reasons": ["execution_readiness_status_blocked"],
        }

        row = learning.build_join_rows(
            alerts={"sig-ready": alert("sig-ready")},
            judgments={},
            intake_decisions={"sig-ready": decision},
            outcomes={},
        )[0]
        payload = {
            "overall": {"resolved_count": 0},
            "judgment_effect": {
                "approved_or_reduced": {"avg_signed_return_pct": None},
                "rejected_or_held": {"avg_signed_return_pct": None},
            },
            "by_trigger": [],
            "by_intake_reason": [{"key": "execution_readiness_would_block_execute", "count": 5}],
            "by_actionability": [{"key": "blocked_execution_readiness", "count": 5}],
        }

        recs = learning.build_recommendations(payload)

        self.assertEqual(row["intake_reason_bucket"], "execution_readiness_would_block_execute")
        self.assertEqual(row["actionability_category"], "blocked_execution_readiness")
        self.assertIn(
            "execution_readiness:execution_readiness_status_blocked",
            learning.effective_intake_reasons(decision),
        )
        self.assertIn("dominant_actionability_blocker:blocked_execution_readiness", recs)

    def test_execute_execution_readiness_rejection_has_specific_actionability(self):
        decision = intake_decision("sig-ready-reject", status="rejected", reason="execution_readiness_gate_failed")
        decision["execution_readiness"] = {
            "status": "REJECTED",
            "readiness_status": "BLOCKED",
            "ready_for_execute": False,
            "reasons": [
                "execution_readiness_status_blocked",
                "execution_readiness_ready_for_execute_false",
            ],
        }

        row = learning.build_join_rows(
            alerts={"sig-ready-reject": alert("sig-ready-reject")},
            judgments={},
            intake_decisions={"sig-ready-reject": decision},
            outcomes={},
        )[0]

        self.assertEqual(row["intake_reason_bucket"], "execution_readiness_gate_failed")
        self.assertEqual(row["actionability_category"], "blocked_execution_readiness")
        self.assertIn(
            "execution_readiness:execution_readiness_ready_for_execute_false",
            learning.effective_intake_reasons(decision),
        )

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
