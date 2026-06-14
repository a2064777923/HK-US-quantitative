import unittest
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from scripts import hermes_judgment_audit_report as audit
from scripts import hermes_review_packet as packet


def alert(signal_id="sig-packet"):
    return {
        "signal_id": signal_id,
        "source": "rt_signal_engine_v5",
        "symbol": "00700",
        "market": "HK",
        "signal_type": "BUY",
        "trigger": "unit-test",
        "confirmed": True,
        "execution_candidate": True,
        "full_score": 0.72,
        "entry_price": 300,
        "stop_loss": 290,
        "take_profit": 330,
        "rr_ratio": 3.0,
        "candidate_signal_type": "BUY",
        "candidate_entry_price": 300,
        "candidate_stop_loss": 290,
        "candidate_take_profit": 330,
        "candidate_rr_ratio": 3.0,
        "generated_at": "2026-06-12T10:00:00",
    }


def intake_result(signal_id="sig-packet", status="dry_run"):
    return {
        "signal_id": signal_id,
        "status": status,
        "plan": {
            "symbol": "00700",
            "side": "buy",
            "quantity": 100,
            "notional_hkd": 30_000,
            "risk_hkd": 1_000,
        },
        "hermes": {"status": "DRY_RUN_ONLY", "request": {"signal_id": signal_id}},
    }


def ready_execution_readiness(**overrides):
    payload = {
        "schema": "execution_readiness_report_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "READY",
        "ready_for_execute": True,
    }
    payload.update(overrides)
    return payload


def watch_alert(signal_id="watch-1"):
    item = alert(signal_id)
    item["signal_type"] = "WATCH"
    item["stop_loss"] = None
    item["take_profit"] = None
    item["rr_ratio"] = None
    return item


def unconfirmed_alert(signal_id="unconfirmed-1"):
    item = alert(signal_id)
    item["confirmed"] = False
    item["full_score"] = 0.1
    return item


def sell_alert(signal_id="sell-1"):
    item = alert(signal_id)
    item["signal_type"] = "SELL"
    item["full_score"] = -0.72
    item["entry_price"] = 280
    item["stop_loss"] = 295
    item["take_profit"] = 250
    return item


class HermesReviewPacketTests(unittest.TestCase):
    def test_alert_summary_preserves_candidate_geometry_for_non_execution_research_rows(self):
        item = alert("research-1")
        item.update(
            {
                "confirmed": False,
                "execution_candidate": False,
                "execution_blocked_reasons": ["not_confirmed"],
                "entry_price": None,
                "stop_loss": None,
                "take_profit": None,
                "rr_ratio": None,
            }
        )

        summary = packet.alert_summary(item)

        self.assertFalse(summary["execution_candidate"])
        self.assertEqual(summary["execution_blocked_reasons"], ["not_confirmed"])
        self.assertIsNone(summary["entry_price"])
        self.assertIsNone(summary["stop_loss"])
        self.assertIsNone(summary["take_profit"])
        self.assertEqual(summary["candidate_signal_type"], "BUY")
        self.assertEqual(summary["candidate_entry_price"], 300)
        self.assertEqual(summary["candidate_stop_loss"], 290)
        self.assertEqual(summary["candidate_take_profit"], 330)
        self.assertEqual(summary["candidate_rr_ratio"], 3.0)

    def test_packet_marks_valid_dry_run_as_eligible_for_review_approval(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {
            "generated_at": "2026-06-12T10:01:00",
            "portfolio_reports": [
                {
                    "portfolio_id": 8,
                    "role": "simulation",
                    "total_value_hkd": 102_000,
                    "return_pct_vs_initial": 2.0,
                }
            ],
            "portfolio_risk": {
                "reports": [
                    {
                        "role": "simulation",
                        "unrealized_pnl": {"unrealized_pnl_pct_of_cost": 1.0},
                    }
                ]
            },
            "simulation_trade_review": {
                "lookback_days": 30,
                "trade_count": 6,
                "closed_trade_count": 3,
                "closed_win_rate_pct": 66.67,
                "closed_pnl_hkd_est": 1200.0,
                "review_notes": [],
            },
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            judgment_file="/tmp/judgments.jsonl",
            strategy_evidence_payload={"schema": "rt_signal_outcome_report_v1", "overall": {"resolved_signal_count": 3}},
            alert_quality_payload={
                "schema": "alert_quality_report_v1",
                "status": "WARN",
                "directional_alert_count": 4,
            },
            alert_event_store_payload={
                "schema": "rt_alert_event_store_report_v1",
                "status": "dry_run",
                "event_count": 4,
                "safety": {"does_not_submit_orders": True},
            },
            judgment_event_store_payload={
                "schema": "hermes_judgment_event_store_report_v1",
                "status": "dry_run",
                "event_count": 1,
                "safety": {"does_not_submit_orders": True, "does_not_change_intake_state": True},
            },
            intake_event_store_payload={
                "schema": "rt_order_intake_event_store_report_v1",
                "status": "dry_run",
                "event_count": 2,
                "safety": {"does_not_submit_orders": True, "does_not_change_intake_state": True},
            },
            outcome_event_store_payload={
                "schema": "rt_signal_outcome_event_store_report_v1",
                "status": "dry_run",
                "event_count": 2,
                "safety": {"does_not_submit_orders": True, "does_not_change_strategy_config": True},
            },
            strategy_review_payload={
                "schema": "strategy_review_report_v1",
                "overall_policy": {"policy": "keep_shadow_or_dry_run"},
            },
            execution_readiness_payload=ready_execution_readiness(
                source={"read_only": True, "submits_orders": False},
            ),
            external_market_context_payload={
                "schema": "external_market_context_report_v1",
                "status": "OK",
                "summary": {"fresh_item_count": 2, "high_impact_count": 1},
                "source": {"read_only": True, "submits_orders": False},
            },
            event_catalyst_payload={
                "schema": "event_catalyst_report_v1",
                "status": "OK",
                "summary": {"candidate_count": 1, "negative_candidate_count": 0},
                "source": {"read_only": True, "submits_orders": False, "changes_alert_queue": False},
            },
            event_catalyst_signal_payload={
                "schema": "event_catalyst_signal_report_v1",
                "status": "OK",
                "summary": {"signal_count": 1, "related_v5_signal_count": 1},
                "source": {"read_only": True, "submits_orders": False, "writes_alert_queue": False},
            },
            market_sentiment_payload={
                "schema": "market_sentiment_report_v1",
                "status": "OK",
                "summary": {"fresh_indicator_count": 2, "overall_score": 0.25, "risk_off_count": 0},
                "source": {"read_only": True, "submits_orders": False},
            },
            fundamentals_context_payload={
                "schema": "fundamentals_context_report_v1",
                "status": "OK",
                "summary": {"fresh_item_count": 1, "risky_item_count": 0},
                "source": {"read_only": True, "submits_orders": False, "changes_strategy": False},
            },
            trusted_source_preflight_payload={
                "schema": "trusted_source_preflight_report_v1",
                "status": "WARN",
                "summary": {
                    "component_count": 3,
                    "failed_component_count": 0,
                    "warning_or_missing_component_count": 1,
                },
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "changes_strategy": False,
                    "changes_alert_queue": False,
                    "writes_ingest_files": False,
                },
                "components": [{"name": "external_market_context_inputs", "status": "WARN"}],
                "recommendations": [
                    "wire_wudao_mcp_broker_or_official_macro_provider_before_claiming_trusted_event_awareness"
                ],
            },
            cron_audit_payload={
                "schema": "cron_audit_report_v1",
                "status": "OK",
                "summary": {"missing_required_job_count": 0, "dangerous_enabled_count": 0},
                "source": {"read_only": True, "submits_orders": False, "changes_crontab": False},
            },
            source_reliability_payload={
                "schema": "source_reliability_report_v1",
                "status": "DEGRADED",
                "summary": {"component_count": 8, "degraded_or_worse_count": 1},
                "components": [
                    {
                        "name": "fundamentals_context",
                        "reliability_status": "DEGRADED",
                        "reasons": ["fundamentals_partial_metric_coverage"],
                    }
                ],
                "source": {"read_only": True, "submits_orders": False},
            },
            operator_action_queue_payload={
                "schema": "operator_action_queue_report_v1",
                "status": "ACTION_REQUIRED",
                "summary": {"action_count": 2, "priority_counts": {"P0": 1, "P1": 1}},
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "writes_judgments": False,
                    "changes_crontab": False,
                    "changes_portfolio": False,
                    "changes_strategy": False,
                },
                "actions": [
                    {
                        "id": "write_high_urgency_position_judgments",
                        "priority": "P0",
                        "category": "advisory_review",
                    }
                ],
            },
            strategy_learning_payload={
                "schema": "strategy_learning_report_v1",
                "sample_scope": {
                    "mode": "latest_strategy_config_and_watchlist",
                    "strategy_config_id": "cfg-1",
                    "watchlist_id": "wl-1",
                    "joined_signal_count": 12,
                    "excluded_joined_signal_count": 3,
                },
                "overall": {"resolved_count": 2, "avg_signed_return_pct": 0.5, "win_rate_pct": 50.0},
                "judgment_effect": {
                    "approved_or_reduced": {
                        "resolved_count": 1,
                        "avg_signed_return_pct": 1.0,
                        "win_rate_pct": 100.0,
                    },
                    "rejected_or_held": {
                        "resolved_count": 1,
                        "avg_signed_return_pct": -0.5,
                        "win_rate_pct": 0.0,
                    },
                },
                "audit_pass_judgment_effect": {
                    "sample_filter": "judgment_audit_status_PASS",
                    "approved_or_reduced": {
                        "resolved_count": 1,
                        "avg_signed_return_pct": 0.75,
                        "win_rate_pct": 100.0,
                    },
                    "rejected_or_held": {
                        "resolved_count": 1,
                        "avg_signed_return_pct": -0.4,
                        "win_rate_pct": 0.0,
                    },
                    "excluded_approved_or_reduced": {
                        "resolved_count": 1,
                        "avg_signed_return_pct": 5.0,
                        "win_rate_pct": 100.0,
                    },
                },
                "judgment_audit_coverage": {
                    "audit_report_available": True,
                    "audit_report_status": "FAIL",
                    "audit_report_truncated": False,
                    "joined_judgment_count": 3,
                    "audit_pass_count": 2,
                    "audit_fail_count": 1,
                    "audit_missing_count": 0,
                    "approved_or_reduced_audit_pass_count": 1,
                    "approved_or_reduced_audit_fail_or_missing_count": 1,
                    "rejected_or_held_audit_pass_count": 1,
                    "rejected_or_held_audit_fail_or_missing_count": 0,
                    "failed_reason_counts": [
                        {"key": "missing_intraday_context_acknowledgement", "count": 1}
                    ],
                },
                "context_review_effect": {
                    "approved_or_reduced_context_complete": {
                        "resolved_count": 1,
                        "avg_signed_return_pct": 1.25,
                        "win_rate_pct": 100.0,
                    },
                    "approved_or_reduced_context_incomplete": {
                        "resolved_count": 1,
                        "avg_signed_return_pct": -0.25,
                        "win_rate_pct": 0.0,
                    },
                    "rejected_or_held": {
                        "resolved_count": 1,
                        "avg_signed_return_pct": -0.5,
                        "win_rate_pct": 0.0,
                    },
                },
                "context_review_quality": {
                    "approved_or_reduced_count": 2,
                    "complete_context_review_count": 1,
                    "incomplete_context_review_count": 1,
                    "complete_context_review_pct": 50.0,
                    "missing_flag_counts": [{"key": "market_sentiment_reviewed", "count": 1}],
                },
                "by_intraday_signal_alignment": [
                    {
                        "key": "supports_signal",
                        "count": 3,
                        "resolved_count": 2,
                        "pending_or_missing_count": 1,
                        "avg_signed_return_pct": 1.2,
                        "win_rate_pct": 50.0,
                    },
                    {
                        "key": "challenges_signal",
                        "count": 2,
                        "resolved_count": 2,
                        "pending_or_missing_count": 0,
                        "avg_signed_return_pct": -0.8,
                        "win_rate_pct": 0.0,
                    },
                ],
                "intraday_alignment_effect": {
                    "schema": "intraday_alignment_effect_v1",
                    "read_only": True,
                    "submits_orders": False,
                    "status": "INSUFFICIENT",
                    "minimum_sample": 5,
                    "supports_signal_like": {
                        "count": 3,
                        "resolved_count": 2,
                        "pending_or_missing_count": 1,
                        "avg_signed_return_pct": 1.2,
                        "win_rate_pct": 50.0,
                    },
                    "challenges_signal": {
                        "count": 2,
                        "resolved_count": 2,
                        "pending_or_missing_count": 0,
                        "avg_signed_return_pct": -0.8,
                        "win_rate_pct": 0.0,
                    },
                    "support_vs_challenge_delta_pct": 2.0,
                    "reasons": [
                        "support_alignment_sample_below_minimum",
                        "challenge_alignment_sample_below_minimum",
                    ],
                    "policy": "keep_alignment_read_only_until_support_and_challenge_samples_mature",
                    "hermes_note": "intraday_alignment_samples_below_threshold_keep_collecting_before_using_as_hard_rule",
                },
                "intake_coverage": {
                    "coverage_pct": 33.33,
                    "directional": {"coverage_pct": 100.0, "joined_signal_count": 4},
                    "watch": {"coverage_pct": 0.0, "joined_signal_count": 8},
                },
                "sizing_blocker_remediation": {
                    "sizing_blocker_count": 2,
                    "covered_by_watchlist_removal_count": 2,
                    "uncovered_count": 0,
                    "covered_symbols": ["00700", "09626"],
                    "watchlist_proposal_hash": "proposal-1",
                },
                "recommendations": [
                    "learning_sample_below_5_keep_collecting_outcomes",
                    "review_watchlist_proposal_for_sizing_blockers:proposal-1",
                ],
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "outcomes": {
                        "outcome_maturity": {
                            "primary_horizon": "1d",
                            "needed_future_days": 1,
                            "latest_signal_date": "2026-06-12",
                            "latest_kline_date": "2026-06-12",
                            "pending_or_invalid_count": 2,
                            "min_missing_future_days_for_pending": 1,
                            "max_missing_future_days_for_pending": 1,
                            "earliest_primary_horizon_date_for_pending": "2026-06-13",
                            "missing_symbol_kline_count": 1,
                            "missing_symbol_kline_unique_symbol_count": 1,
                            "missing_symbol_kline_diagnostics": [
                                {
                                    "symbol": "BAD",
                                    "status": "not_found_in_stocks_no_klines",
                                    "affected_signal_count": 3,
                                }
                            ],
                            "no_future_daily_kline_count": 2,
                        }
                    },
                },
            },
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "mixed"}}},
            data_health_payload={"schema": "data_health_report_v1", "status": "OK", "markets": {}},
            data_source_inventory_payload={
                "schema": "data_source_inventory_report_v1",
                "status": "DEGRADED",
                "source": {
                    "read_only": True,
                    "queries_database": True,
                    "writes_database": False,
                    "submits_orders": False,
                    "changes_crontab": False,
                },
                "summary": {
                    "table_status_counts": {"present": 7},
                    "context_file_status_counts": {"present": 12, "missing": 1},
                    "kline_source_counts": {"tencent": 1000, "missing": 2},
                    "weakness_count": 1,
                },
                "weaknesses": [{"code": "kline_data_source_missing", "severity": "WARN"}],
            },
            kline_source_granularity_payload={
                "schema": "kline_source_granularity_report_v1",
                "status": "ACTION_REQUIRED",
                "source": {
                    "dry_run_default": True,
                    "read_only": True,
                    "writes_database": False,
                    "changes_schema": False,
                    "submits_orders": False,
                    "does_not_change_ohlcv_prices_or_volumes": True,
                },
                "summary": {
                    "source_granularity_column_exists": False,
                    "proposal_action_count": 2,
                    "estimated_backfill_row_count": 100,
                },
                "proposal": {
                    "schema": "kline_source_granularity_proposal_v1",
                    "proposal_hash": "granularity1234",
                    "manual_review_required": True,
                    "auto_applied": False,
                },
            },
            intraday_kline_batch_payload={
                "schema": "intraday_kline_batch_report_v1",
                "status": "ACTIONABLE",
                "plan_hash": "intraday-plan",
                "summary": {"action_count": 1, "planned_row_count": 120},
                "source": {"submits_orders": False, "repairs_daily_klines": False},
            },
            intraday_context_payload={
                "schema": "intraday_context_report_v1",
                "status": "OK",
                "source": {"read_only": True, "submits_orders": False},
                "markets": {
                    "HK": {
                        "status": "OK",
                        "latest_timestamp": "2026-06-12T10:01:00",
                        "breadth": {"session_up_pct": 70.0, "session_down_pct": 10.0},
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "OK",
                                "latest_timestamp": "2026-06-12T10:01:00",
                                "latest_age_minutes": 1.0,
                                "session": {"change_pct": -1.2, "momentum": "strong_down"},
                                "latest_5m": {
                                    "change_pct": -0.4,
                                    "momentum": "down",
                                    "volume_state": "expanding",
                                },
                                "latest_60m": {"change_pct": -1.2, "momentum": "strong_down"},
                                "multi_timeframe_confirmation": {
                                    "schema": "intraday_multi_timeframe_confirmation_v1",
                                    "alignment": "bearish_aligned",
                                    "dominant_direction": "down",
                                    "buy_confirmation": False,
                                    "sell_confirmation": True,
                                    "contradictions": [],
                                },
                                "hermes_notes": [
                                    "intraday_session_down_against_new_buy_review",
                                    "intraday_multi_timeframe_bearish_challenges_buy_review",
                                ],
                            }
                        ],
                    }
                },
            },
            intraday_timeframe_quality_payload={
                "schema": "intraday_timeframe_quality_report_v1",
                "status": "DEGRADED",
                "source": {
                    "read_only": True,
                    "input_file": "/tmp/intraday_context_report.json",
                    "queries_database": False,
                    "submits_orders": False,
                    "writes_database": False,
                    "changes_strategy": False,
                    "changes_crontab": False,
                },
                "summary": {
                    "symbol_count": 1,
                    "degraded_symbol_count": 1,
                    "limited_timeframe_symbol_count": 1,
                    "missing_timeframe_symbol_count": 0,
                    "conflict_symbol_count": 0,
                    "low_fidelity_symbol_count": 1,
                    "snapshot_like_symbol_count": 1,
                    "missing_source_granularity_symbol_count": 1,
                    "timeframes": {
                        "30m": {"ok_symbol_count": 0, "limited_symbol_count": 1, "missing_symbol_count": 0},
                        "60m": {"ok_symbol_count": 0, "limited_symbol_count": 1, "missing_symbol_count": 0},
                    },
                },
                "markets": {
                    "HK": {
                        "market": "HK",
                        "status": "DEGRADED",
                        "symbol_count": 1,
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "DEGRADED",
                                "source_status": "OK",
                                "decision_use": "cap_or_challenge_only",
                                "allowed_effects": ["cap_confidence", "challenge_signal"],
                                "alignment": "bearish_aligned",
                                "dominant_direction": "down",
                                "limited_timeframes": ["30m", "60m"],
                                "missing_timeframes": [],
                                "reasons": [
                                    "timeframe_coverage_limited",
                                    "low_fidelity_minute_source",
                                    "snapshot_like_minute_rows",
                                ],
                                "quality": {
                                    "status": "WARN",
                                    "valid_point_count": 60,
                                    "full_ohlc_row_count": 0,
                                    "low_fidelity_point_count": 60,
                                    "snapshot_like_row_count": 60,
                                    "missing_source_granularity_count": 1,
                                },
                            }
                        ],
                    }
                },
                "recommendations": [
                    "do_not_raise_confidence_from_limited_30m_60m_coverage",
                    "treat_snapshot_minute_timeframes_as_advisory_until_full_ohlcv",
                ],
                "decision_policy": {
                    "schema": "intraday_timeframe_decision_policy_v1",
                    "confidence_use": "cap_or_challenge_only",
                    "may_raise_confidence": False,
                    "requires_forward_evidence_before_confidence_raise": True,
                    "can_override_daily_gates": False,
                    "execution_permission": False,
                    "allowed_effects": ["cap_confidence", "challenge_signal"],
                    "reason_codes": [
                        "timeframe_coverage_limited",
                        "low_fidelity_minute_source",
                        "snapshot_like_minute_rows",
                    ],
                    "timeframe_roles": {
                        "5m": "entry_timing_noise_check",
                        "15m": "near_term_confirmation",
                        "30m": "session_structure_confirmation",
                        "60m": "session_structure_context",
                    },
                },
            },
            intraday_market_session_overrides_payload={
                "schema": "intraday_market_session_overrides_report_v1",
                "status": "WARN",
                "summary": {"configured_market_count": 1, "missing_market_count": 1},
                "recommendations": ["add_US_market_session_overrides"],
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "changes_crontab": False,
                    "changes_strategy": False,
                },
            },
            kline_gap_source_diagnostic_payload={
                "schema": "kline_gap_source_diagnostic_report_v1",
                "status": "REVIEW",
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "applies_kline_repairs": False,
                    "changes_watchlists": False,
                    "changes_stock_universe": False,
                    "auto_excludes_from_evidence": False,
                },
                "summary": {"classified_count": 1},
                "classifications": [{"symbol": "03333", "category": "provider_stopped_or_mapping_stale"}],
            },
            kline_gap_alternate_provider_probe_payload={
                "schema": "kline_gap_alternate_provider_probe_v1",
                "status": "ACTION_REQUIRED",
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "auto_uses_alternate_provider_for_repairs": False,
                },
                "summary": {
                    "probed_count": 1,
                    "alternate_current_count": 1,
                    "category_counts": {"alternate_provider_has_current_daily_rows": 1},
                },
                "probes": [
                    {
                        "symbol": "00959",
                        "provider_symbol": "0959.HK",
                        "category": "alternate_provider_has_current_daily_rows",
                        "alternate_latest_date": "2026-06-12",
                    }
                ],
            },
            kline_gap_alternate_provider_repair_plan_payload={
                "schema": "kline_gap_alternate_provider_repair_plan_v1",
                "status": "REVIEW",
                "plan_hash": "alt-plan-1",
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "auto_applies_repairs": False,
                    "auto_uses_alternate_provider_for_repairs": False,
                },
                "summary": {
                    "candidate_count": 1,
                    "manual_repair_candidate_count": 0,
                    "review_only_count": 1,
                },
                "candidates": [
                    {
                        "symbol": "00959",
                        "status": "review_only_quality_not_sufficient_for_repair_plan",
                        "quality": {"zero_volume_pct": 100.0, "flat_ohlc_pct": 100.0},
                    }
                ],
                "operator_contract": {
                    "manual_review_required": True,
                    "manual_apply_command": None,
                },
            },
            judgment_audit_payload={"schema": "hermes_judgment_audit_report_v1", "counts": {"judgment_count": 0}},
            universe_payload={
                "schema": "universe_rank_report_v1",
                "source": {"auto_applies_watchlist": False},
                "markets": {"HK": {"candidate_count": 1}},
            },
            watchlist_diff_payload={
                "schema": "watchlist_diff_report_v1",
                "source": {"read_only": True, "auto_applies_watchlist": False},
                "proposal": {
                    "proposal_hash": "proposal-1",
                    "current_watchlist_id": "wl-1",
                    "proposed_watchlist_id": "wl-2",
                    "manual_review_required": True,
                    "auto_applied": False,
                    "does_not_restart_services": True,
                    "does_not_submit_orders": True,
                },
                "markets": {"HK": {"add_count": 1, "remove_count": 1}},
            },
            universe_hygiene_payload={
                "schema": "universe_hygiene_report_v1",
                "source": {"auto_applies_stock_changes": False},
                "proposal": {
                    "schema": "stock_universe_hygiene_proposal_v1",
                    "source": {
                        "manual_review_required": True,
                        "auto_applied": False,
                    },
                },
                "markets": {
                    "HK": {
                        "problem_symbol_count": 1,
                        "high_priority_candidates": [
                            {
                                "market": "HK",
                                "symbol": "00011",
                                "exchange": "HKEX",
                                "name": "Hang Seng Bank",
                                "recommended_action": "candidate_deactivate_or_symbol_mapping",
                                "issues": ["latest_kline_stale_ge_30d"],
                                "latest_date": "2026-01-14",
                                "history_rows_120d": 0,
                            }
                        ],
                    }
                },
            },
            position_judgment_file="/tmp/position_judgments.jsonl",
            position_judgment_audit_payload={
                "schema": "hermes_position_judgment_audit_report_v1",
                "counts": {"judgment_count": 0},
            },
        )

        self.assertEqual(payload["schema"], "hermes_signal_review_packet_v1")
        self.assertTrue(payload["execution_safety"]["review_only"])
        self.assertFalse(payload["execution_safety"]["submits_orders"])
        self.assertEqual(payload["review_items"][0]["eligible_for_approval"], True)
        self.assertEqual(
            payload["review_items"][0]["recommended_judgment"],
            "approve_or_reduce_allowed_after_llm_review",
        )
        self.assertEqual(payload["judgment_contract"]["judgment_file"], "/tmp/judgments.jsonl")
        self.assertEqual(payload["strategy_evidence"]["schema"], "rt_signal_outcome_report_v1")
        self.assertEqual(payload["alert_quality_summary"]["schema"], "alert_quality_report_v1")
        self.assertEqual(payload["alert_quality_summary"]["directional_alert_count"], 4)
        self.assertEqual(payload["alert_event_store"]["schema"], "rt_alert_event_store_report_v1")
        self.assertEqual(payload["alert_event_store"]["event_count"], 4)
        self.assertTrue(payload["alert_event_store"]["safety"]["does_not_submit_orders"])
        self.assertEqual(payload["judgment_event_store"]["schema"], "hermes_judgment_event_store_report_v1")
        self.assertEqual(payload["judgment_event_store"]["event_count"], 1)
        self.assertTrue(payload["judgment_event_store"]["safety"]["does_not_change_intake_state"])
        self.assertEqual(payload["order_intake_event_store"]["schema"], "rt_order_intake_event_store_report_v1")
        self.assertEqual(payload["order_intake_event_store"]["event_count"], 2)
        self.assertTrue(payload["order_intake_event_store"]["safety"]["does_not_submit_orders"])
        self.assertEqual(payload["signal_outcome_event_store"]["schema"], "rt_signal_outcome_event_store_report_v1")
        self.assertEqual(payload["signal_outcome_event_store"]["event_count"], 2)
        self.assertTrue(payload["signal_outcome_event_store"]["safety"]["does_not_change_strategy_config"])
        self.assertEqual(payload["strategy_review"]["schema"], "strategy_review_report_v1")
        self.assertEqual(payload["strategy_review"]["overall_policy"]["policy"], "keep_shadow_or_dry_run")
        self.assertEqual(payload["trusted_source_preflight"]["schema"], "trusted_source_preflight_report_v1")
        self.assertEqual(payload["trusted_source_preflight"]["status"], "WARN")
        self.assertFalse(payload["trusted_source_preflight"]["source"]["writes_ingest_files"])
        self.assertTrue(
            any("Trusted source preflight is a read-only payload validator" in note for note in payload["operator_notes"])
        )
        self.assertEqual(payload["execution_readiness"]["schema"], "execution_readiness_report_v1")
        self.assertTrue(payload["execution_readiness"]["ready_for_execute"])
        self.assertEqual(payload["external_market_context"]["schema"], "external_market_context_report_v1")
        self.assertFalse(payload["external_market_context"]["source"]["submits_orders"])
        self.assertEqual(payload["event_catalysts"]["schema"], "event_catalyst_report_v1")
        self.assertFalse(payload["event_catalysts"]["source"]["submits_orders"])
        self.assertFalse(payload["event_catalysts"]["source"]["changes_alert_queue"])
        self.assertEqual(payload["event_catalyst_signals"]["schema"], "event_catalyst_signal_report_v1")
        self.assertFalse(payload["event_catalyst_signals"]["source"]["submits_orders"])
        self.assertFalse(payload["event_catalyst_signals"]["source"]["writes_alert_queue"])
        self.assertEqual(payload["market_sentiment"]["schema"], "market_sentiment_report_v1")
        self.assertFalse(payload["market_sentiment"]["source"]["submits_orders"])
        self.assertEqual(payload["fundamentals_context"]["schema"], "fundamentals_context_report_v1")
        self.assertFalse(payload["fundamentals_context"]["source"]["submits_orders"])
        self.assertFalse(payload["fundamentals_context"]["source"]["changes_strategy"])
        self.assertEqual(payload["source_reliability"]["schema"], "source_reliability_report_v1")
        self.assertEqual(payload["source_reliability"]["status"], "DEGRADED")
        self.assertEqual(payload["data_source_inventory"]["schema"], "data_source_inventory_report_v1")
        self.assertEqual(payload["data_source_inventory"]["status"], "DEGRADED")
        self.assertTrue(payload["data_source_inventory"]["source"]["read_only"])
        self.assertFalse(payload["data_source_inventory"]["source"]["writes_database"])
        self.assertTrue(
            any("Data source inventory is a read-only visibility ledger" in note for note in payload["operator_notes"])
        )
        self.assertEqual(payload["kline_source_granularity"]["schema"], "kline_source_granularity_report_v1")
        self.assertEqual(payload["kline_source_granularity"]["status"], "ACTION_REQUIRED")
        self.assertTrue(payload["kline_source_granularity"]["source"]["read_only"])
        self.assertFalse(payload["kline_source_granularity"]["source"]["writes_database"])
        self.assertFalse(payload["kline_source_granularity"]["source"]["changes_schema"])
        self.assertTrue(
            any("K-line source granularity is a dry-run/hash-gated provenance proposal" in note for note in payload["operator_notes"])
        )
        self.assertEqual(payload["operator_action_queue"]["schema"], "operator_action_queue_report_v1")
        self.assertEqual(payload["operator_action_queue"]["status"], "ACTION_REQUIRED")
        self.assertFalse(payload["operator_action_queue"]["source"]["submits_orders"])
        self.assertFalse(payload["operator_action_queue"]["source"]["writes_judgments"])
        self.assertFalse(payload["operator_action_queue"]["source"]["changes_crontab"])
        self.assertTrue(
            any("Operator action queue is a read-only remediation priority list" in note for note in payload["operator_notes"])
        )
        self.assertIn("source_reliability_reviewed", payload["judgment_contract"]["append_jsonl_object"]["context_review"])
        self.assertIn("intraday_context_reviewed", payload["judgment_contract"]["append_jsonl_object"]["context_review"])
        self.assertIn("external_market_context_risk_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("external_market_context_ids", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("external_market_context_notes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("external_market_context_support_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("external_market_context_support_ids", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("external_market_context_support_notes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("event_catalyst_support_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("event_catalyst_support_signal_ids", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("event_catalyst_support_notes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("market_sentiment_risk_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("market_sentiment_indicator_ids", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("market_sentiment_notes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("market_sentiment_support_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("market_sentiment_support_indicator_ids", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("market_sentiment_support_notes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("fundamentals_context_support_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("fundamentals_context_support_symbols", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("fundamentals_context_support_metrics", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("fundamentals_context_support_notes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("source_reliability_limit_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("hermes_alpha_evidence_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("hermes_alpha_evidence_status", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("hermes_alpha_evidence_reasons", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("hermes_alpha_evidence_notes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("intraday_context_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("intraday_signal_evidence_acknowledged", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("intraday_signal_evidence_alignment", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("intraday_signal_evidence_codes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertIn("intraday_signal_evidence_notes", payload["judgment_contract"]["append_jsonl_object"])
        self.assertTrue(
            any("external_market_context_risk_acknowledged" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertTrue(
            any(
                "external_market_context_support_acknowledged" in rule
                for rule in payload["judgment_contract"]["hard_rules"]
            )
        )
        self.assertTrue(
            any("event_catalyst_support_acknowledged" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertTrue(
            any("market_sentiment_risk_acknowledged" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertTrue(
            any("market_sentiment_support_acknowledged" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertTrue(
            any(
                "fundamentals_context_support_acknowledged" in rule
                for rule in payload["judgment_contract"]["hard_rules"]
            )
        )
        self.assertTrue(
            any("source_reliability.status" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertTrue(
            any("hermes_alpha_evidence.status" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertTrue(
            any(
                "MISSING" in rule and "INVALID" in rule and "evidence object is absent" in rule
                for rule in payload["judgment_contract"]["hard_rules"]
                if "hermes_alpha_evidence.status" in rule
            )
        )
        self.assertTrue(
            any("intraday_context_acknowledged" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertTrue(
            any("intraday_signal_evidence_acknowledged" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertEqual(payload["cron_audit"]["schema"], "cron_audit_report_v1")
        self.assertFalse(payload["cron_audit"]["source"]["submits_orders"])
        self.assertFalse(payload["cron_audit"]["source"]["changes_crontab"])
        self.assertEqual(payload["strategy_learning"]["schema"], "strategy_learning_report_v1")
        self.assertFalse(payload["strategy_learning"]["source"]["submits_orders"])
        self.assertEqual(payload["strategy_learning_brief"]["schema"], "hermes_strategy_learning_brief_v1")
        self.assertFalse(payload["strategy_learning_brief"]["submits_orders"])
        self.assertEqual(payload["strategy_learning_brief"]["sample_scope"]["strategy_config_id"], "cfg-1")
        self.assertEqual(payload["strategy_learning_brief"]["intake_coverage"]["directional_pct"], 100.0)
        self.assertEqual(
            payload["strategy_learning_brief"]["judgment_effect"]["approved_or_reduced"]["avg_signed_return_pct"],
            0.75,
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["judgment_effect"]["rejected_or_held"]["avg_signed_return_pct"],
            -0.4,
        )
        self.assertEqual(payload["strategy_learning_brief"]["judgment_effect"]["sample_filter"], "judgment_audit_status_PASS")
        self.assertEqual(
            payload["strategy_learning_brief"]["raw_judgment_effect"]["approved_or_reduced"]["avg_signed_return_pct"],
            1.0,
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["judgment_audit_coverage"]["approved_or_reduced_audit_fail_or_missing_count"],
            1,
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["judgment_audit_coverage"]["failed_reason_counts"][0],
            {"key": "missing_intraday_context_acknowledgement", "count": 1},
        )
        self.assertEqual(payload["strategy_learning_brief"]["hermes_alpha_evidence"]["status"], "INSUFFICIENT")
        self.assertIn(
            "approved_or_reduced_audit_pass_sample_below_minimum",
            payload["strategy_learning_brief"]["hermes_alpha_evidence"]["reasons"],
        )
        self.assertIn(
            "rejected_or_held_audit_pass_sample_below_minimum",
            payload["strategy_learning_brief"]["hermes_alpha_evidence"]["reasons"],
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["context_review_effect"]["approved_or_reduced_context_complete"]["avg_signed_return_pct"],
            1.25,
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["context_review_effect"]["approved_or_reduced_context_incomplete"]["avg_signed_return_pct"],
            -0.25,
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["context_review_effect"]["quality"]["incomplete_context_review_count"],
            1,
        )
        self.assertTrue(payload["strategy_learning_brief"]["intraday_signal_alignment"]["read_only"])
        self.assertEqual(
            payload["strategy_learning_brief"]["intraday_signal_alignment"]["source"],
            "strategy_learning.by_intraday_signal_alignment",
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["intraday_signal_alignment"]["supports_signal"]["avg_signed_return_pct"],
            1.2,
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["intraday_signal_alignment"]["challenges_signal"]["avg_signed_return_pct"],
            -0.8,
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["intraday_signal_alignment"]["evidence_status"],
            "INSUFFICIENT",
        )
        self.assertIn(
            "support_alignment_sample_below_minimum",
            payload["strategy_learning_brief"]["intraday_signal_alignment"]["evidence_reasons"],
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["intraday_signal_alignment"]["support_vs_challenge_delta_pct"],
            2.0,
        )
        self.assertFalse(payload["strategy_learning_brief"]["intraday_signal_alignment"]["minimum_sample_met"])
        self.assertIn(
            "intraday_alignment_samples_below_threshold",
            payload["strategy_learning_brief"]["intraday_signal_alignment"]["hermes_note"],
        )
        self.assertIn(
            "audit-pass judgment_effect",
            " ".join(payload["strategy_learning_brief"]["hermes_use"]),
        )
        self.assertIn(
            "intraday_signal_alignment",
            " ".join(payload["strategy_learning_brief"]["hermes_use"]),
        )
        self.assertIn(
            "Treat context-reviewed approval outcomes as unproven",
            " ".join(payload["strategy_learning_brief"]["hermes_use"]),
        )
        self.assertFalse(payload["strategy_learning_brief"]["outcome_evidence"]["minimum_sample_met"])
        self.assertEqual(payload["strategy_learning_brief"]["outcome_maturity"]["latest_kline_date"], "2026-06-12")
        self.assertEqual(
            payload["strategy_learning_brief"]["outcome_maturity"]["earliest_primary_horizon_date_for_pending"],
            "2026-06-13",
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["outcome_maturity"]["missing_symbol_kline_status_counts"],
            {"not_found_in_stocks_no_klines": 3},
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["sizing_blocker_remediation"]["status"],
            "fully_covered_by_manual_watchlist_proposal",
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["sizing_blocker_remediation"]["proposed_watchlist_id"],
            "wl-2",
        )
        self.assertEqual(
            payload["simulation_trade_review_brief"]["schema"],
            "hermes_simulation_trade_review_brief_v1",
        )
        self.assertFalse(payload["simulation_trade_review_brief"]["submits_orders"])
        self.assertEqual(payload["simulation_trade_review_brief"]["return_pct_vs_initial"], 2.0)
        self.assertEqual(payload["simulation_trade_review_brief"]["unrealized_pnl_pct_of_cost"], 1.0)
        self.assertEqual(payload["simulation_trade_review_brief"]["closed_trade_count"], 3)
        self.assertEqual(payload["simulation_trade_review_brief"]["closed_pnl_hkd_est"], 1200.0)
        self.assertIn(
            "review_watchlist_proposal_for_sizing_blockers:proposal-1",
            payload["strategy_learning_brief"]["recommendations"],
        )
        self.assertEqual(payload["market_context"]["schema"], "market_context_report_v1")
        self.assertEqual(payload["data_health"]["schema"], "data_health_report_v1")
        self.assertEqual(payload["intraday_kline_batch"]["schema"], "intraday_kline_batch_report_v1")
        self.assertEqual(payload["intraday_kline_batch"]["plan_hash"], "intraday-plan")
        self.assertTrue(any("Intraday K-line batch" in note for note in payload["operator_notes"]))
        producer_digest = payload["review_items"][0]["context_digest"]["intraday_minute_producer"]
        self.assertEqual(producer_digest["schema"], "hermes_review_item_intraday_minute_producer_digest_v1")
        self.assertEqual(producer_digest["status"], "ACTIONABLE")
        self.assertEqual(producer_digest["plan_hash"], "intraday-plan")
        self.assertIn("intraday_minute_apply_pending", producer_digest["notes"])
        self.assertIn(
            "intraday_minute_producer_limit_requires_acknowledgement",
            payload["review_items"][0]["context_digest"]["required_judgment_attention"],
        )
        self.assertEqual(payload["intraday_context"]["schema"], "intraday_context_report_v1")
        self.assertEqual(payload["intraday_timeframe_quality"]["schema"], "intraday_timeframe_quality_report_v1")
        self.assertEqual(payload["intraday_timeframe_quality"]["status"], "DEGRADED")
        self.assertFalse(payload["intraday_timeframe_quality"]["source"]["submits_orders"])
        self.assertFalse(payload["intraday_timeframe_quality"]["source"]["writes_database"])
        self.assertFalse(payload["intraday_timeframe_quality"]["source"]["changes_strategy"])
        self.assertTrue(any("Intraday timeframe quality" in note for note in payload["operator_notes"]))
        timeframe_policy = payload["review_items"][0]["context_digest"]["intraday_timeframe_policy"]
        self.assertEqual(
            timeframe_policy["schema"],
            "hermes_review_item_intraday_timeframe_policy_digest_v1",
        )
        self.assertEqual(timeframe_policy["confidence_use"], "cap_or_challenge_only")
        self.assertFalse(timeframe_policy["may_raise_confidence"])
        self.assertFalse(timeframe_policy["can_override_daily_gates"])
        self.assertFalse(timeframe_policy["execution_permission"])
        self.assertIn("timeframe_coverage_limited", timeframe_policy["reason_codes"])
        self.assertIn("cap_confidence", timeframe_policy["allowed_effects"])
        self.assertTrue(timeframe_policy["requires_judgment_acknowledgement"])
        timeframe_decision = payload["review_items"][0]["context_digest"]["intraday_timeframe_decision"]
        self.assertEqual(
            timeframe_decision["schema"],
            "hermes_review_item_intraday_timeframe_decision_v1",
        )
        self.assertTrue(timeframe_decision["matched"])
        self.assertEqual(timeframe_decision["symbol"], "00700")
        self.assertEqual(timeframe_decision["decision_use"], "cap_or_challenge_only")
        self.assertEqual(timeframe_decision["allowed_effects"], ["cap_confidence", "challenge_signal"])
        self.assertEqual(timeframe_decision["limited_timeframes"], ["30m", "60m"])
        self.assertIn("timeframe_coverage_limited", timeframe_decision["reasons"])
        self.assertEqual(timeframe_decision["quality"]["full_ohlc_row_count"], 0)
        self.assertEqual(timeframe_decision["quality"]["low_fidelity_point_count"], 60)
        self.assertIn(
            "intraday_timeframe_policy_requires_acknowledgement",
            payload["review_items"][0]["context_digest"]["required_judgment_attention"],
        )
        self.assertTrue(
            any("intraday_timeframe_policy_requires_acknowledgement" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertEqual(
            payload["intraday_market_session_overrides"]["schema"],
            "intraday_market_session_overrides_report_v1",
        )
        self.assertEqual(payload["intraday_market_session_overrides"]["status"], "WARN")
        self.assertTrue(any("Intraday market-session overrides" in note for note in payload["operator_notes"]))
        intraday_digest = payload["review_items"][0]["context_digest"]["intraday_context"]
        self.assertEqual(intraday_digest["schema"], "hermes_review_item_intraday_context_digest_v1")
        self.assertEqual(intraday_digest["status"], "OK")
        self.assertEqual(intraday_digest["session"]["momentum"], "strong_down")
        self.assertEqual(intraday_digest["multi_timeframe_confirmation"]["alignment"], "bearish_aligned")
        self.assertIn(
            "intraday_context_challenges_buy_requires_discussion",
            payload["review_items"][0]["context_digest"]["required_judgment_attention"],
        )
        self.assertEqual(payload["kline_gap_source_diagnostic"]["schema"], "kline_gap_source_diagnostic_report_v1")
        self.assertEqual(payload["kline_gap_source_diagnostic"]["classifications"][0]["symbol"], "03333")
        self.assertEqual(
            payload["kline_gap_alternate_provider_probe"]["schema"],
            "kline_gap_alternate_provider_probe_v1",
        )
        self.assertEqual(payload["kline_gap_alternate_provider_probe"]["summary"]["alternate_current_count"], 1)
        self.assertEqual(payload["kline_gap_alternate_provider_probe"]["probes"][0]["provider_symbol"], "0959.HK")
        self.assertEqual(
            payload["kline_gap_alternate_provider_repair_plan"]["schema"],
            "kline_gap_alternate_provider_repair_plan_v1",
        )
        self.assertEqual(payload["kline_gap_alternate_provider_repair_plan"]["summary"]["review_only_count"], 1)
        self.assertIsNone(
            payload["kline_gap_alternate_provider_repair_plan"]["operator_contract"]["manual_apply_command"]
        )
        self.assertEqual(payload["universe_context"]["schema"], "universe_rank_report_v1")
        self.assertFalse(payload["universe_context"]["source"]["auto_applies_watchlist"])
        self.assertEqual(payload["watchlist_diff"]["schema"], "watchlist_diff_report_v1")
        self.assertFalse(payload["watchlist_diff"]["source"]["auto_applies_watchlist"])
        self.assertEqual(payload["universe_hygiene"]["schema"], "universe_hygiene_report_v1")
        self.assertFalse(payload["universe_hygiene"]["source"]["auto_applies_stock_changes"])
        self.assertEqual(
            payload["stock_universe_hygiene_promotion_plan"]["schema"],
            "stock_universe_hygiene_promotion_report_v1",
        )
        self.assertEqual(payload["stock_universe_hygiene_promotion_plan"]["status"], "dry_run")
        self.assertFalse(payload["stock_universe_hygiene_promotion_plan"]["applied"])
        self.assertEqual(payload["stock_universe_hygiene_promotion_plan"]["selected_count"], 0)
        self.assertEqual(
            payload["stock_universe_hygiene_promotion_plan"]["operator_review_plan"]["status"],
            "operator_review_required",
        )
        self.assertEqual(
            payload["stock_universe_hygiene_promotion_plan"]["operator_review_plan"]["items"][0]["symbol"],
            "00011",
        )
        self.assertTrue(payload["stock_universe_hygiene_promotion_plan"]["safety"]["read_only_payload_build"])
        self.assertFalse(payload["stock_universe_hygiene_promotion_plan"]["safety"]["queries_database"])
        self.assertTrue(payload["stock_universe_hygiene_promotion_plan"]["safety"]["does_not_change_stock_universe"])
        self.assertEqual(payload["judgment_audit"]["schema"], "hermes_judgment_audit_report_v1")
        self.assertEqual(
            payload["position_judgment_audit"]["schema"],
            "hermes_position_judgment_audit_report_v1",
        )
        self.assertEqual(payload["position_judgment_contract"]["judgment_file"], "/tmp/position_judgments.jsonl")
        self.assertFalse(payload["position_judgment_contract"]["append_jsonl_object"]["submits_orders"])
        self.assertIn(
            "position_attention_acknowledged",
            payload["position_judgment_contract"]["append_jsonl_object"],
        )
        self.assertIn(
            "position_attention_codes",
            payload["position_judgment_contract"]["append_jsonl_object"],
        )
        self.assertIn(
            "position_attention_notes",
            payload["position_judgment_contract"]["append_jsonl_object"],
        )
        self.assertIn(
            "position_attention_effects",
            payload["position_judgment_contract"]["append_jsonl_object"],
        )
        self.assertTrue(
            any(
                "context_digest.position_attention" in rule and "position_attention_effects" in rule
                for rule in payload["position_judgment_contract"]["hard_rules"]
            )
        )
        self.assertTrue(
            any("symbol_conflict" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertTrue(
            any("execution_readiness" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertEqual(payload["portfolio_risk"]["reports"][0]["role"], "simulation")
        self.assertEqual(payload["alert_selection"]["review_alert_count"], 1)
        self.assertEqual(payload["alert_selection"]["directional_count"], 1)
        self.assertTrue(any("Cron audit" in note for note in payload["operator_notes"]))
        self.assertTrue(any("Stock-universe hygiene promotion plan is dry-run context only" in note for note in payload["operator_notes"]))
        self.assertTrue(any("K-line gap alternate-provider probe compares unresolved symbols" in note for note in payload["operator_notes"]))
        self.assertTrue(any("K-line gap alternate-provider repair plan is a read-only candidate-quality report" in note for note in payload["operator_notes"]))
        self.assertEqual(payload["review_items"][0]["context_digest"]["schema"], "hermes_review_item_context_digest_v1")
        self.assertFalse(payload["review_items"][0]["context_digest"]["submits_orders"])
        self.assertTrue(
            any("review_items[].context_digest is a read-only attention layer" in note for note in payload["operator_notes"])
        )

    def test_review_item_context_digest_maps_relevant_context_without_changing_eligibility(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            external_market_context_payload={
                "schema": "external_market_context_report_v1",
                "status": "OK",
                "items": [
                    {
                        "id": "ctx-00700",
                        "category": "news",
                        "source": "broker_feed",
                        "provider": "broker",
                        "title": "Tencent result beat",
                        "published_at": "2026-06-12T09:50:00",
                        "age_minutes": 15,
                        "stale": False,
                        "sentiment": "positive",
                        "impact_score": 0.82,
                        "markets": ["HK"],
                        "symbols": ["00700"],
                    },
                    {
                        "id": "ctx-macro",
                        "category": "macro",
                        "source": "official_macro",
                        "provider": "official_macro",
                        "title": "HK liquidity tightens",
                        "published_at": "2026-06-12T09:45:00",
                        "age_minutes": 20,
                        "stale": False,
                        "sentiment": "negative",
                        "impact_score": 0.72,
                        "markets": ["HK"],
                    },
                    {
                        "id": "ctx-other",
                        "category": "news",
                        "title": "Unrelated US chip news",
                        "stale": False,
                        "sentiment": "positive",
                        "impact_score": 0.9,
                        "markets": ["US"],
                        "symbols": ["NVDA"],
                    },
                ],
            },
            event_catalyst_payload={
                "schema": "event_catalyst_report_v1",
                "status": "RISK",
                "candidates": [
                    {
                        "id": "event-00700",
                        "scope": "symbol",
                        "category": "event",
                        "title": "Regulatory review headline",
                        "published_at": "2026-06-12T09:30:00",
                        "age_minutes": 30,
                        "stale": False,
                        "sentiment": "negative",
                        "impact_score": 0.91,
                        "matched_markets": ["HK"],
                        "matched_symbols": ["00700"],
                    },
                    {
                        "id": "event-00700-positive",
                        "scope": "symbol",
                        "category": "event",
                        "title": "Tencent result beat follow-up",
                        "published_at": "2026-06-12T09:35:00",
                        "age_minutes": 25,
                        "stale": False,
                        "sentiment": "positive",
                        "impact_score": 0.82,
                        "matched_markets": ["HK"],
                        "matched_symbols": ["00700"],
                    }
                ],
            },
            event_catalyst_signal_payload={
                "schema": "event_catalyst_signal_report_v1",
                "status": "RISK",
                "signals": [
                    {
                        "signal_id": "event-sig-1",
                        "event_catalyst_id": "event-00700",
                        "review_signal_type": "CHALLENGE_BUY_REVIEW",
                        "priority": "critical",
                        "direction": "negative_catalyst",
                        "title": "Challenge Tencent buy",
                        "sentiment": "negative",
                        "impact_score": 0.91,
                        "symbols": ["00700"],
                        "markets": ["HK"],
                        "related_v5_signal_ids": ["sig-packet"],
                    }
                ],
            },
            market_sentiment_payload={
                "schema": "market_sentiment_report_v1",
                "status": "RISK",
                "indicators": [
                    {
                        "id": "hk-flow",
                        "indicator_type": "capital_flow",
                        "name": "Southbound flow",
                        "source": "broker_feed",
                        "observed_at": "2026-06-12T09:55:00",
                        "age_minutes": 10,
                        "stale": False,
                        "markets": ["HK"],
                        "direction": "risk_off",
                        "score": "-0.35",
                        "summary": "risk-off flow",
                    },
                    {
                        "id": "vix-risk-on",
                        "indicator_type": "volatility",
                        "name": "VIX",
                        "source": "broker_feed",
                        "observed_at": "2026-06-12T09:55:00",
                        "age_minutes": 10,
                        "stale": False,
                        "markets": ["HK"],
                        "direction": "risk_on",
                        "score": "0.35",
                        "summary": "risk-on volatility easing",
                    }
                ],
            },
            fundamentals_context_payload={
                "schema": "fundamentals_context_report_v1",
                "status": "OK",
                "items": [
                    {
                        "symbol": "00700",
                        "market": "HK",
                        "source": "tencent_quote_snapshot",
                        "as_of": "2026-06-12T09:45:00",
                        "age_days": 0,
                        "stale": False,
                        "pe_ttm": 24.0,
                        "valuation_flags": ["partial_fundamentals"],
                        "fundamental_completeness": {
                            "level": "partial",
                            "missing_metrics": ["pb", "roe_pct", "earnings_growth_pct"],
                        },
                    }
                ],
            },
            trusted_source_preflight_payload={
                "schema": "trusted_source_preflight_report_v1",
                "status": "WARN",
                "components": [{"name": "external_market_context_inputs", "status": "WARN", "warnings": ["fallback_only"]}],
                "recommendations": ["wire_wudao_or_broker_context"],
            },
            source_reliability_payload={
                "schema": "source_reliability_report_v1",
                "status": "DEGRADED",
                "components": [
                    {
                        "name": "market_context",
                        "reliability_status": "DEGRADED",
                        "reasons": ["market_context_native_index_public_fallback_only"],
                        "native_index_context": [
                            {
                                "market": "HK",
                                "status": "OK",
                                "alignment": "confirms_breadth",
                                "provider_grade": "public_fallback",
                            }
                        ],
                    },
                    {
                        "name": "fundamentals_context",
                        "reliability_status": "DEGRADED",
                        "reasons": ["fundamentals_partial_metric_coverage"],
                    }
                ],
                "recommendations": ["do_not_raise_confidence_for_partial_fundamentals"],
            },
            market_context_payload={
                "schema": "market_context_report_v1",
                "markets": {
                    "HK": {
                        "regime": "risk_off",
                        "risk_level": "medium",
                        "latest_date": "2026-06-12",
                        "notes": ["tighten_new_buy_approval_or_reduce_size", "buy_signals_against_weak_breadth"],
                        "breadth": {"above_ma20_pct": 20.42},
                        "returns": {"avg_5d_pct": -1.92, "avg_20d_pct": -9.05},
                        "native_index_context": {
                            "schema": "market_context_native_index_v1",
                            "status": "OK",
                            "index_direction": "risk_off",
                            "alignment": "confirms_breadth",
                            "latest_lag_days_vs_stock_pool": 1,
                            "available_index_count": 2,
                            "primary_index": {
                                "symbol": "^HSI",
                                "name": "Hang Seng Index",
                                "latest_date": "2026-06-11",
                                "history_days": 119,
                                "return_5d_pct": -2.4,
                                "return_20d_pct": -7.1,
                                "source_table": "market_index_context_inputs",
                                "source": "yahoo_chart_snapshot",
                                "provider_grade": "public_fallback",
                            },
                        },
                        "cross_market": {
                            "schema": "market_context_cross_market_v1",
                            "status": "incomplete",
                            "alignment": "incomplete",
                        },
                    }
                },
            },
        )

        item = payload["review_items"][0]
        digest = item["context_digest"]

        self.assertTrue(item["eligible_for_approval"])
        self.assertEqual(digest["schema"], "hermes_review_item_context_digest_v1")
        self.assertTrue(digest["read_only"])
        self.assertFalse(digest["submits_orders"])
        self.assertEqual(digest["external_market_context"]["relevant_item_count"], 2)
        self.assertEqual(digest["market_context"]["regime"], "risk_off")
        self.assertEqual(digest["market_context"]["native_index_context"]["status"], "OK")
        self.assertEqual(digest["market_context"]["native_index_context"]["primary_index"]["symbol"], "^HSI")
        self.assertEqual(
            digest["market_context"]["native_index_context"]["primary_index"]["provider_grade"],
            "public_fallback",
        )
        self.assertEqual(digest["event_catalysts"]["negative_candidate_count"], 1)
        self.assertEqual(digest["event_catalysts"]["positive_candidate_count"], 1)
        self.assertEqual(digest["event_catalyst_signals"]["challenge_buy_count"], 1)
        self.assertEqual(digest["fundamentals_context"]["relevant_item_count"], 1)
        self.assertTrue(digest["fundamentals_context"]["limit_acknowledgement_required"])
        self.assertEqual(digest["source_limits"]["trusted_source_preflight_status"], "WARN")
        self.assertEqual(digest["source_limits"]["source_reliability_status"], "DEGRADED")
        self.assertIn(
            "event_catalyst_signal_challenges_buy_requires_acknowledgement",
            digest["required_judgment_attention"],
        )
        # RISK report status is itself a coverage limitation, separate from the matched challenge signal.
        self.assertIn(
            "event_catalyst_signal_coverage_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )
        self.assertIn(
            "fundamentals_context_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )
        self.assertIn(
            "source_reliability_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )
        self.assertIn(
            "risk_off_market_context_requires_exception_for_buy",
            digest["required_judgment_attention"],
        )
        self.assertIn(
            "market_sentiment_risk_or_negative_requires_confidence_check",
            digest["required_judgment_attention"],
        )
        self.assertIn(
            "market_sentiment_support_requires_acknowledgement",
            digest["required_judgment_attention"],
        )
        self.assertIn(
            "buy_signal_against_weak_breadth_requires_explicit_review",
            digest["required_judgment_attention"],
        )
        self.assertIn(
            "native_index_public_fallback_requires_source_limit_acknowledgement",
            digest["required_judgment_attention"],
        )
        source_components = digest["source_limits"]["source_reliability_problem_components"]
        self.assertEqual(source_components[0]["name"], "market_context")
        self.assertIn("native_index_context", source_components[0])
        self.assertNotIn("ctx-other", [row.get("id") for row in digest["external_market_context"]["items"]])

    def test_source_reliability_digest_exposes_outcome_intraday_path_fidelity(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            source_reliability_payload={
                "schema": "source_reliability_report_v1",
                "status": "DEGRADED",
                "components": [
                    {
                        "name": "rt_signal_outcome",
                        "reliability_status": "DEGRADED",
                        "reasons": ["outcome_intraday_path_low_fidelity"],
                        "coverage": {
                            "ambiguous_daily_count": 4,
                            "resolved_count": 2,
                            "missing_count": 0,
                            "same_minute_ambiguous_count": 0,
                            "unresolved_count": 0,
                            "low_fidelity_count": 1,
                            "effective_unresolved_first_hit_rate_pct": 12.5,
                        },
                    }
                ],
                "recommendations": [
                    "collect_full_ohlcv_minute_path_evidence_before_claiming_intraday_path_resolution"
                ],
            },
        )

        digest = payload["review_items"][0]["context_digest"]
        source_components = digest["source_limits"]["source_reliability_problem_components"]
        outcome = [row for row in source_components if row["name"] == "rt_signal_outcome"][0]

        self.assertIn("source_reliability_limit_requires_acknowledgement", digest["required_judgment_attention"])
        self.assertIn("outcome_intraday_path_low_fidelity", outcome["reasons"])
        self.assertEqual(outcome["intraday_path_fidelity"]["low_fidelity_count"], 1)
        self.assertEqual(outcome["intraday_path_fidelity"]["effective_unresolved_first_hit_rate_pct"], 12.5)

    def test_source_reliability_digest_exposes_intraday_timeframe_decision_use(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            source_reliability_payload={
                "schema": "source_reliability_report_v1",
                "status": "DEGRADED",
                "components": [
                    {
                        "name": "intraday_timeframe_quality",
                        "reliability_status": "DEGRADED",
                        "reasons": ["intraday_timeframe_diagnostic_only_symbols"],
                        "coverage": {
                            "decision_use_counts_present": True,
                            "decision_use_counts": {"cap_or_challenge_only": 2, "diagnostic_only": 1},
                            "soft_confirmation_eligible_symbol_count": 0,
                            "cap_or_challenge_only_symbol_count": 2,
                            "diagnostic_only_symbol_count": 1,
                            "confidence_use": "cap_or_challenge_only",
                        },
                    }
                ],
                "recommendations": [
                    "treat_diagnostic_only_intraday_timeframes_as_unavailable_for_confirmation"
                ],
            },
        )

        digest = payload["review_items"][0]["context_digest"]
        source_components = digest["source_limits"]["source_reliability_problem_components"]
        timeframe = [row for row in source_components if row["name"] == "intraday_timeframe_quality"][0]

        self.assertIn("source_reliability_limit_requires_acknowledgement", digest["required_judgment_attention"])
        self.assertIn("intraday_timeframe_diagnostic_only_symbols", timeframe["reasons"])
        self.assertEqual(timeframe["intraday_timeframe_decision_use"]["soft_confirmation_eligible_symbol_count"], 0)
        self.assertEqual(timeframe["intraday_timeframe_decision_use"]["cap_or_challenge_only_symbol_count"], 2)
        self.assertEqual(timeframe["intraday_timeframe_decision_use"]["diagnostic_only_symbol_count"], 1)
        self.assertEqual(
            timeframe["intraday_timeframe_decision_use"]["decision_use_counts"],
            {"cap_or_challenge_only": 2, "diagnostic_only": 1},
        )

    def test_intraday_context_digest_exposes_source_granularity_and_fidelity_limits(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            intraday_context_payload={
                "schema": "intraday_context_report_v1",
                "status": "OK",
                "granularity_policy": {
                    "schema": "intraday_granularity_usage_policy_v1",
                    "daily_forward_outcomes_remain_authority": True,
                    "timeframes": {
                        "60m": {"role": "intraday_regime_confirmation_or_challenge"},
                        "1m": {
                            "role": "execution_quality_and_path_diagnostics",
                            "forbidden_uses": ["core_alpha_generation"],
                        },
                    },
                },
                "markets": {
                    "HK": {
                        "status": "OK",
                        "latest_timestamp": "2026-06-12T10:00:00",
                        "breadth": {"session_up_pct": 100.0, "session_down_pct": 0.0},
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "OK",
                                "point_count": 40,
                                "latest_timestamp": "2026-06-12T10:00:00",
                                "data_sources": ["tencent_minute_query"],
                                "source_granularities": ["minute_snapshot_price"],
                                "quality": {
                                    "schema": "intraday_symbol_quality_v1",
                                    "status": "WARN",
                                    "low_fidelity_point_count": 40,
                                    "snapshot_like_row_count": 40,
                                    "full_ohlc_row_count": 0,
                                    "notes": ["low_fidelity_intraday_source", "snapshot_like_intraday_rows"],
                                },
                                "hermes_notes": ["intraday_context_quality_degraded_requires_disclosure"],
                            }
                        ],
                    }
                },
            },
        )

        digest = payload["review_items"][0]["context_digest"]
        intraday = digest["intraday_context"]

        self.assertEqual(intraday["source_granularities"], ["minute_snapshot_price"])
        self.assertEqual(
            intraday["granularity_policy"]["timeframes"]["60m"]["role"],
            "intraday_regime_confirmation_or_challenge",
        )
        self.assertIn(
            "core_alpha_generation",
            intraday["granularity_policy"]["timeframes"]["1m"]["forbidden_uses"],
        )
        self.assertEqual(intraday["quality"]["low_fidelity_point_count"], 40)
        self.assertEqual(intraday["quality"]["snapshot_like_row_count"], 40)
        self.assertIn(
            "intraday_context_quality_degraded_requires_disclosure",
            digest["required_judgment_attention"],
        )

    def test_intraday_rolling_window_coverage_limits_require_hermes_disclosure(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"overall_status": "OK", "checks": []},
            portfolio_payload={},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "mixed"}}},
            data_health_payload={"schema": "data_health_report_v1", "status": "OK", "markets": {}},
            intraday_context_payload={
                "schema": "intraday_context_report_v1",
                "status": "OK",
                "source": {"read_only": True, "submits_orders": False},
                "markets": {
                    "HK": {
                        "status": "OK",
                        "latest_timestamp": "2026-06-12T09:49:00",
                        "breadth": {"session_up_pct": 70.0, "session_down_pct": 10.0},
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "OK",
                                "session": {"change_pct": 1.2, "momentum": "strong_up"},
                                "latest_5m": {
                                    "change_pct": 0.4,
                                    "momentum": "up",
                                    "coverage_status": "OK",
                                    "row_count": 5,
                                    "expected_minute_count": 5,
                                },
                                "latest_15m": {
                                    "change_pct": 0.9,
                                    "momentum": "up",
                                    "coverage_status": "OK",
                                    "row_count": 15,
                                    "expected_minute_count": 15,
                                },
                                "latest_30m": {
                                    "change_pct": 1.2,
                                    "momentum": "strong_up",
                                    "coverage_status": "LIMITED",
                                    "row_count": 20,
                                    "expected_minute_count": 30,
                                },
                                "latest_60m": {
                                    "change_pct": 1.2,
                                    "momentum": "strong_up",
                                    "coverage_status": "LIMITED",
                                    "row_count": 20,
                                    "expected_minute_count": 60,
                                },
                                "rolling_windows": {
                                    "30m": {
                                        "coverage_status": "LIMITED",
                                        "row_count": 20,
                                        "expected_minute_count": 30,
                                    },
                                    "60m": {
                                        "coverage_status": "LIMITED",
                                        "row_count": 20,
                                        "expected_minute_count": 60,
                                    },
                                },
                                "multi_timeframe_confirmation": {
                                    "schema": "intraday_multi_timeframe_confirmation_v1",
                                    "alignment": "bullish_aligned",
                                    "dominant_direction": "up",
                                    "buy_confirmation": True,
                                    "sell_confirmation": False,
                                    "contradictions": [],
                                },
                                "hermes_notes": [
                                    "intraday_30m_window_coverage_limited_requires_disclosure",
                                    "intraday_60m_window_coverage_limited_requires_disclosure",
                                ],
                            }
                        ],
                    }
                },
            },
        )

        digest = payload["review_items"][0]["context_digest"]
        intraday = digest["intraday_context"]
        evidence = digest["intraday_signal_evidence"]

        self.assertEqual(intraday["latest_15m"]["coverage_status"], "OK")
        self.assertEqual(intraday["latest_30m"]["coverage_status"], "LIMITED")
        self.assertEqual(intraday["rolling_windows"]["60m"]["expected_minute_count"], 60)
        self.assertEqual(evidence["alignment"], "supports_with_limits")
        self.assertIn("multi_timeframe_supports_buy", evidence["support_codes"])
        self.assertIn(
            "intraday_60m_window_coverage_limited_requires_disclosure",
            evidence["limit_codes"],
        )
        self.assertIn(
            "intraday_timeframe_coverage_limited_requires_disclosure",
            digest["required_judgment_attention"],
        )

    def test_intraday_timeframe_decision_missing_use_is_fail_visible(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "risk_on"}}},
            intraday_timeframe_quality_payload={
                "schema": "intraday_timeframe_quality_report_v1",
                "status": "OK",
                "source": {"read_only": True, "submits_orders": False, "writes_database": False},
                "summary": {"symbol_count": 1, "soft_confirmation_eligible_symbol_count": 1},
                "decision_policy": {
                    "schema": "intraday_timeframe_decision_policy_v1",
                    "confidence_use": "soft_confirmation_eligible",
                    "may_raise_confidence": False,
                    "requires_forward_evidence_before_confidence_raise": True,
                    "can_override_daily_gates": False,
                    "execution_permission": False,
                    "allowed_effects": ["soft_confirm_signal", "cap_confidence", "challenge_signal"],
                    "reason_codes": [],
                },
                "markets": {
                    "HK": {
                        "market": "HK",
                        "status": "OK",
                        "symbol_count": 1,
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "OK",
                                "allowed_effects": ["soft_confirm_signal", "cap_confidence", "challenge_signal"],
                                "reasons": [],
                                "quality": {
                                    "status": "OK",
                                    "valid_point_count": 60,
                                    "full_ohlc_row_count": 60,
                                    "low_fidelity_point_count": 0,
                                },
                            }
                        ],
                    }
                },
            },
        )

        digest = payload["review_items"][0]["context_digest"]
        decision = digest["intraday_timeframe_decision"]

        self.assertTrue(decision["matched"])
        self.assertFalse(decision["decision_use_present"])
        self.assertFalse(decision["decision_use_valid"])
        self.assertEqual(decision["decision_use"], "diagnostic_only")
        self.assertEqual(decision["allowed_effects"], [])
        self.assertIn("intraday_timeframe_decision_use_missing", decision["reasons"])
        self.assertIn(
            "intraday_timeframe_decision_contract_requires_acknowledgement",
            digest["required_judgment_attention"],
        )
        self.assertTrue(
            any(
                "intraday_timeframe_decision_contract_requires_acknowledgement" in rule
                for rule in payload["judgment_contract"]["hard_rules"]
            )
        )

    def test_stale_event_catalyst_signal_report_requires_coverage_acknowledgement(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "risk_on"}}},
            event_catalyst_signal_payload={
                "schema": "event_catalyst_signal_report_v1",
                "status": "STALE",
                "signals": [],
                "warnings": ["event_catalyst_signal_report_stale"],
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["event_catalyst_signals"]["status"], "STALE")
        self.assertEqual(digest["event_catalyst_signals"]["relevant_signal_count"], 0)
        self.assertIn(
            "event_catalyst_signal_coverage_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )

    def test_stale_event_catalyst_report_requires_coverage_acknowledgement(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "risk_on"}}},
            event_catalyst_payload={
                "schema": "event_catalyst_report_v1",
                "status": "STALE",
                "candidates": [],
                "warnings": ["event_catalyst_report_stale"],
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["event_catalysts"]["status"], "STALE")
        self.assertEqual(digest["event_catalysts"]["relevant_candidate_count"], 0)
        self.assertIn(
            "event_catalyst_coverage_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )

    def test_stale_external_market_context_report_requires_coverage_acknowledgement(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "risk_on"}}},
            external_market_context_payload={
                "schema": "external_market_context_report_v1",
                "status": "STALE",
                "items": [],
                "warnings": ["external_market_context_report_stale"],
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["external_market_context"]["status"], "STALE")
        self.assertEqual(digest["external_market_context"]["relevant_item_count"], 0)
        self.assertIn(
            "external_market_context_coverage_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )

    def test_risk_context_statuses_without_detail_rows_require_coverage_acknowledgement(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "risk_on"}}},
            external_market_context_payload={
                "schema": "external_market_context_report_v1",
                "status": "RISK",
                "items": [],
            },
            event_catalyst_payload={
                "schema": "event_catalyst_report_v1",
                "status": "RISK",
                "candidates": [],
            },
            event_catalyst_signal_payload={
                "schema": "event_catalyst_signal_report_v1",
                "status": "RISK",
                "signals": [],
            },
            market_sentiment_payload={
                "schema": "market_sentiment_report_v1",
                "status": "RISK",
                "indicators": [],
            },
            fundamentals_context_payload={
                "schema": "fundamentals_context_report_v1",
                "status": "RISK",
                "items": [],
            },
        )

        attention = payload["review_items"][0]["context_digest"]["required_judgment_attention"]

        self.assertIn("external_market_context_coverage_limit_requires_acknowledgement", attention)
        self.assertIn("event_catalyst_coverage_limit_requires_acknowledgement", attention)
        self.assertIn("event_catalyst_signal_coverage_limit_requires_acknowledgement", attention)
        self.assertIn("market_sentiment_coverage_limit_requires_acknowledgement", attention)
        self.assertIn("fundamentals_context_coverage_limit_requires_acknowledgement", attention)

    def test_stale_market_context_report_requires_coverage_acknowledgement(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={
                "schema": "market_context_report_v1",
                "status": "STALE",
                "markets": {"HK": {"regime": "risk_on", "breadth": {"above_ma20_pct": 62.0}}},
                "warnings": ["market_context_report_stale"],
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["market_context"]["status"], "STALE")
        self.assertEqual(digest["market_context"]["regime"], "risk_on")
        self.assertIn(
            "market_context_coverage_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )

    def test_missing_signal_market_context_requires_coverage_acknowledgement(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={
                "schema": "market_context_report_v1",
                "status": "OK",
                "markets": {"US": {"regime": "risk_on"}},
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["market_context"]["status"], "MISSING")
        self.assertIn("market_context_missing_for_signal_market", digest["required_judgment_attention"])
        self.assertIn(
            "market_context_coverage_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )

    def test_stale_market_sentiment_report_requires_coverage_acknowledgement(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "risk_on"}}},
            market_sentiment_payload={
                "schema": "market_sentiment_report_v1",
                "status": "STALE",
                "indicators": [],
                "warnings": ["market_sentiment_report_stale"],
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["market_sentiment"]["status"], "STALE")
        self.assertEqual(digest["market_sentiment"]["relevant_indicator_count"], 0)
        self.assertIn(
            "market_sentiment_coverage_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )

    def test_stale_fundamentals_context_report_requires_coverage_acknowledgement(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"status": "OK", "checks": []},
            portfolio_payload={"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "risk_on"}}},
            fundamentals_context_payload={
                "schema": "fundamentals_context_report_v1",
                "status": "STALE",
                "items": [],
                "warnings": ["fundamentals_context_report_stale"],
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["fundamentals_context"]["status"], "STALE")
        self.assertEqual(digest["fundamentals_context"]["relevant_item_count"], 0)
        self.assertIn(
            "fundamentals_context_coverage_limit_requires_acknowledgement",
            digest["required_judgment_attention"],
        )

    def test_intraday_timeframe_conflict_requires_hermes_disclosure(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"overall_status": "OK", "checks": []},
            portfolio_payload={},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "mixed"}}},
            data_health_payload={"schema": "data_health_report_v1", "status": "OK", "markets": {}},
            intraday_context_payload={
                "schema": "intraday_context_report_v1",
                "status": "OK",
                "source": {"read_only": True, "submits_orders": False},
                "markets": {
                    "HK": {
                        "status": "OK",
                        "latest_timestamp": "2026-06-12T10:01:00",
                        "breadth": {"session_up_pct": 40.0, "session_down_pct": 40.0},
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "OK",
                                "session": {"change_pct": -1.1, "momentum": "strong_down"},
                                "latest_5m": {"change_pct": 0.4, "momentum": "up"},
                                "latest_60m": {"change_pct": -1.0, "momentum": "strong_down"},
                                "multi_timeframe_confirmation": {
                                    "schema": "intraday_multi_timeframe_confirmation_v1",
                                    "alignment": "mixed_bearish",
                                    "dominant_direction": "down",
                                    "buy_confirmation": False,
                                    "sell_confirmation": False,
                                    "contradictions": [
                                        "latest_5m_contradicts_session",
                                        "latest_5m_contradicts_latest_60m",
                                    ],
                                },
                                "hermes_notes": ["intraday_timeframes_conflicting_requires_disclosure"],
                            }
                        ],
                    }
                },
            },
        )

        digest = payload["review_items"][0]["context_digest"]
        self.assertEqual(
            digest["intraday_context"]["multi_timeframe_confirmation"]["alignment"],
            "mixed_bearish",
        )
        self.assertEqual(digest["intraday_signal_evidence"]["alignment"], "conflicting_timeframes")
        self.assertEqual(digest["intraday_signal_evidence"]["timeframe_alignment"], "mixed_bearish")
        self.assertIn("latest_5m_contradicts_session", digest["intraday_signal_evidence"]["conflict_codes"])
        self.assertTrue(digest["intraday_signal_evidence"]["requires_judgment_acknowledgement"])
        self.assertIn(
            "intraday_context_timeframe_conflict_requires_disclosure",
            digest["required_judgment_attention"],
        )

    def test_intraday_quality_degradation_requires_hermes_disclosure(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"overall_status": "OK", "checks": []},
            portfolio_payload={},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "mixed"}}},
            data_health_payload={"schema": "data_health_report_v1", "status": "OK", "markets": {}},
            intraday_context_payload={
                "schema": "intraday_context_report_v1",
                "status": "OK",
                "source": {"read_only": True, "submits_orders": False},
                "markets": {
                    "HK": {
                        "status": "OK",
                        "latest_timestamp": "2026-06-12T10:01:00",
                        "breadth": {"session_up_pct": 60.0, "session_down_pct": 20.0},
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "OK",
                                "session": {"change_pct": 0.8, "momentum": "up"},
                                "latest_5m": {"change_pct": 0.2, "momentum": "flat"},
                                "latest_60m": {"change_pct": 0.8, "momentum": "up"},
                                "multi_timeframe_confirmation": {
                                    "schema": "intraday_multi_timeframe_confirmation_v1",
                                    "alignment": "mixed_bullish",
                                    "buy_confirmation": True,
                                },
                                "quality": {
                                    "schema": "intraday_symbol_quality_v1",
                                    "status": "WARN",
                                    "large_gap_count": 1,
                                    "notes": ["intraday_minute_gap_detected"],
                                },
                                "hermes_notes": ["intraday_context_quality_degraded_requires_disclosure"],
                            }
                        ],
                    }
                },
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["intraday_context"]["quality"]["status"], "WARN")
        self.assertEqual(digest["intraday_context"]["quality"]["large_gap_count"], 1)
        self.assertEqual(digest["intraday_signal_evidence"]["alignment"], "supports_with_limits")
        self.assertIn("multi_timeframe_supports_buy", digest["intraday_signal_evidence"]["support_codes"])
        self.assertIn("intraday_quality_warn", digest["intraday_signal_evidence"]["quality_codes"])
        self.assertTrue(digest["intraday_signal_evidence"]["requires_judgment_acknowledgement"])
        self.assertIn(
            "intraday_context_quality_degraded_requires_disclosure",
            digest["required_judgment_attention"],
        )

    def test_closed_intraday_market_requires_session_context_disclosure(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"overall_status": "OK", "checks": []},
            portfolio_payload={},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "mixed"}}},
            data_health_payload={"schema": "data_health_report_v1", "status": "OK", "markets": {}},
            intraday_context_payload={
                "schema": "intraday_context_report_v1",
                "status": "CLOSED",
                "source": {"read_only": True, "submits_orders": False},
                "markets": {
                    "HK": {
                        "status": "CLOSED",
                        "market_session": {
                            "schema": "intraday_market_session_v1",
                            "phase": "CLOSED_WEEKEND",
                            "is_regular_session_open": False,
                        },
                        "latest_timestamp": "2026-06-12T15:59:00",
                        "breadth": {"session_up_pct": 50.0, "session_down_pct": 20.0},
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "CLOSED",
                                "market_session": {
                                    "schema": "intraday_market_session_v1",
                                    "phase": "CLOSED_WEEKEND",
                                    "is_regular_session_open": False,
                                },
                                "session": {"change_pct": 0.4, "momentum": "up"},
                                "hermes_notes": ["intraday_market_not_open_requires_session_context"],
                            }
                        ],
                    }
                },
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["intraday_context"]["status"], "CLOSED")
        self.assertEqual(digest["intraday_context"]["market_session"]["phase"], "CLOSED_WEEKEND")
        self.assertIn(
            "intraday_market_not_open_requires_session_context",
            digest["required_judgment_attention"],
        )

    def test_intraday_market_session_override_warning_requires_hermes_disclosure(self):
        payload = packet.build_packet(
            [alert()],
            health_payload={"overall_status": "OK", "checks": []},
            portfolio_payload={},
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "mixed"}}},
            data_health_payload={"schema": "data_health_report_v1", "status": "OK", "markets": {}},
            intraday_context_payload={
                "schema": "intraday_context_report_v1",
                "status": "OK",
                "source": {"read_only": True, "submits_orders": False},
                "markets": {
                    "HK": {
                        "status": "OK",
                        "market_session": {
                            "schema": "intraday_market_session_v1",
                            "phase": "REGULAR",
                            "is_regular_session_open": True,
                        },
                        "latest_timestamp": "2026-06-12T10:01:00",
                        "breadth": {"session_up_pct": 50.0, "session_down_pct": 20.0},
                        "symbols": [{"symbol": "00700", "market": "HK", "status": "OK"}],
                    }
                },
            },
            intraday_market_session_overrides_payload={
                "schema": "intraday_market_session_overrides_report_v1",
                "status": "WARN",
                "summary": {"warning_market_count": 1, "failed_market_count": 0},
                "markets": {
                    "HK": {
                        "market": "HK",
                        "status": "WARN",
                        "coverage_until": None,
                        "future_entry_count": 0,
                        "warnings": ["HK:no_future_session_overrides_or_closed_dates"],
                        "errors": [],
                    }
                },
                "warnings": ["HK:no_future_session_overrides_or_closed_dates"],
                "errors": [],
                "recommendations": ["review_intraday_market_session_override_coverage_for_holidays_and_half_days"],
            },
        )

        digest = payload["review_items"][0]["context_digest"]

        self.assertEqual(digest["intraday_market_session_overrides"]["status"], "WARN")
        self.assertEqual(digest["intraday_market_session_overrides"]["market"], "HK")
        self.assertIn(
            "HK:no_future_session_overrides_or_closed_dates",
            digest["intraday_market_session_overrides"]["warnings"],
        )
        self.assertIn(
            "intraday_market_session_overrides_limit_requires_disclosure",
            digest["required_judgment_attention"],
        )

    def test_strategy_learning_brief_uses_outcome_report_maturity_fallback(self):
        brief = packet.strategy_learning_brief(
            {
                "schema": "strategy_learning_report_v1",
                "sample_scope": {},
                "overall": {},
                "judgment_effect": {},
                "intake_coverage": {},
                "source": {"read_only": True, "submits_orders": False},
            },
            outcome_report_payload={
                "schema": "rt_signal_outcome_report_v1",
                "outcome_maturity": {
                    "primary_horizon": "1d",
                    "latest_signal_date": "2026-06-12",
                    "latest_kline_date": "2026-06-12",
                    "pending_or_invalid_count": 9,
                    "missing_symbol_kline_count": 3,
                    "missing_symbol_kline_unique_symbol_count": 1,
                    "missing_symbol_kline_diagnostics": [
                        {
                            "symbol": "00959",
                            "status": "stock_found_has_day_klines_before_signal_date",
                            "affected_signal_count": 3,
                        }
                    ],
                },
            },
        )

        maturity = brief["outcome_maturity"]

        self.assertEqual(maturity["latest_kline_date"], "2026-06-12")
        self.assertEqual(maturity["missing_symbol_kline_unique_symbol_count"], 1)
        self.assertEqual(
            maturity["missing_symbol_kline_status_counts"],
            {"stock_found_has_day_klines_before_signal_date": 3},
        )

    def test_strategy_learning_brief_falls_back_to_raw_judgment_effect_for_legacy_reports(self):
        brief = packet.strategy_learning_brief(
            {
                "schema": "strategy_learning_report_v1",
                "sample_scope": {},
                "overall": {},
                "judgment_effect": {
                    "approved_or_reduced": {
                        "resolved_count": 5,
                        "avg_signed_return_pct": 1.1,
                        "win_rate_pct": 60.0,
                    },
                    "rejected_or_held": {
                        "resolved_count": 5,
                        "avg_signed_return_pct": -0.2,
                        "win_rate_pct": 40.0,
                    },
                },
                "intake_coverage": {},
                "source": {"read_only": True, "submits_orders": False},
            }
        )

        self.assertEqual(brief["judgment_effect"]["sample_filter"], "raw_judgment_decision")
        self.assertEqual(brief["judgment_effect"]["approved_or_reduced"]["avg_signed_return_pct"], 1.1)
        self.assertEqual(brief["raw_judgment_effect"]["approved_or_reduced"]["avg_signed_return_pct"], 1.1)
        self.assertEqual(brief["intraday_signal_alignment"]["groups"], [])
        self.assertEqual(brief["intraday_signal_alignment"]["supports_signal"], {})
        self.assertEqual(brief["intraday_signal_alignment"]["challenges_signal"], {})
        self.assertFalse(brief["intraday_signal_alignment"]["minimum_sample_met"])
        self.assertEqual(
            brief["intraday_signal_alignment"]["hermes_note"],
            "intraday_alignment_learning_not_available_for_legacy_or_empty_report",
        )
        self.assertEqual(brief["hermes_alpha_evidence"]["status"], "INSUFFICIENT")
        self.assertIn(
            "audit_pass_judgment_effect_missing_raw_effect_only",
            brief["hermes_alpha_evidence"]["reasons"],
        )

    def test_strategy_learning_intraday_alignment_brief_normalizes_legacy_labels(self):
        brief = packet.strategy_learning_intraday_alignment_brief(
            {
                "by_intraday_signal_alignment": [
                    {
                        "key": "conflicting_intraday_context",
                        "count": 2,
                        "resolved_count": 0,
                        "pending_or_missing_count": 2,
                    },
                    {
                        "key": "missing_minute_rows_before_signal",
                        "count": 3,
                        "resolved_count": 0,
                        "pending_or_missing_count": 3,
                    },
                ]
            }
        )

        keys = {row["key"] for row in brief["groups"]}
        self.assertIn("conflicting_timeframes", keys)
        self.assertIn("unavailable_or_stale", keys)
        self.assertNotIn("conflicting_intraday_context", keys)

    def test_hermes_alpha_evidence_supportive_only_with_audit_pass_sample_and_outperformance(self):
        summary = packet.hermes_alpha_evidence_summary(
            {
                "sample_filter": "judgment_audit_status_PASS",
                "approved_or_reduced": {
                    "resolved_count": 5,
                    "avg_signed_return_pct": 1.4,
                    "win_rate_pct": 60.0,
                },
                "rejected_or_held": {
                    "resolved_count": 5,
                    "avg_signed_return_pct": -0.2,
                    "win_rate_pct": 40.0,
                },
            },
            {
                "audit_report_available": True,
                "audit_report_truncated": False,
                "approved_or_reduced_audit_fail_or_missing_count": 0,
            },
        )

        self.assertEqual(summary["schema"], "hermes_alpha_evidence_summary_v1")
        self.assertEqual(summary["status"], "SUPPORTIVE")
        self.assertEqual(summary["approval_vs_rejection_delta_pct"], 1.6)
        self.assertEqual(summary["reasons"], [])

    def test_hermes_alpha_evidence_flags_negative_approval_effect(self):
        summary = packet.hermes_alpha_evidence_summary(
            {
                "sample_filter": "judgment_audit_status_PASS",
                "approved_or_reduced": {
                    "resolved_count": 5,
                    "avg_signed_return_pct": -0.1,
                    "win_rate_pct": 45.0,
                },
                "rejected_or_held": {
                    "resolved_count": 5,
                    "avg_signed_return_pct": 0.2,
                    "win_rate_pct": 55.0,
                },
            },
            {
                "audit_report_available": True,
                "audit_report_truncated": False,
                "approved_or_reduced_audit_fail_or_missing_count": 0,
            },
        )

        self.assertEqual(summary["status"], "NEGATIVE")
        self.assertIn("approved_or_reduced_avg_return_not_positive", summary["reasons"])
        self.assertIn("approved_or_reduced_not_outperforming_rejected_or_held", summary["reasons"])

    def test_strategy_learning_brief_surfaces_gap_source_diagnostic_summary(self):
        brief = packet.strategy_learning_brief(
            {
                "schema": "strategy_learning_report_v1",
                "sample_scope": {},
                "overall": {},
                "judgment_effect": {},
                "intake_coverage": {},
                "source": {"read_only": True, "submits_orders": False},
            },
            outcome_report_payload={
                "schema": "rt_signal_outcome_report_v1",
                "outcome_maturity": {
                    "primary_horizon": "1d",
                    "missing_symbol_kline_count": 2,
                    "missing_symbol_kline_unique_symbol_count": 1,
                    "missing_symbol_kline_diagnostics": [
                        {
                            "symbol": "00959",
                            "status": "stock_found_has_day_klines_before_signal_date",
                            "affected_signal_count": 2,
                            "daily_gap_source_category": "active_universe_or_symbol_mapping_issue",
                        }
                    ],
                    "daily_gap_repair_context": {
                        "status": "UNRESOLVED",
                        "plan_hash": "3a427dd004186bea",
                        "actionable_missing_symbol_count": 0,
                        "unresolved_missing_symbol_count": 1,
                        "not_in_repair_plan_missing_symbol_count": 0,
                    },
                    "daily_gap_source_diagnostic_context": {
                        "status": "ACTION_REQUIRED",
                        "classified_missing_symbol_count": 1,
                        "unclassified_missing_symbol_count": 0,
                        "category_counts": {"active_universe_or_symbol_mapping_issue": 1},
                        "confidence_counts": {"high": 1},
                        "category_affected_signal_counts": {"active_universe_or_symbol_mapping_issue": 2},
                        "active_universe_or_mapping_missing_symbol_count": 1,
                        "provider_lag_missing_symbol_count": 0,
                    },
                },
            },
        )

        maturity = brief["outcome_maturity"]

        self.assertEqual(
            maturity["daily_gap_source_category_affected_signal_counts"],
            {"active_universe_or_symbol_mapping_issue": 2},
        )
        self.assertEqual(maturity["daily_gap_repair_context"]["status"], "UNRESOLVED")
        self.assertEqual(
            maturity["daily_gap_source_diagnostic_context"]["status"],
            "ACTION_REQUIRED",
        )
        self.assertEqual(
            maturity["daily_gap_source_diagnostic_context"]["category_affected_signal_counts"],
            {"active_universe_or_symbol_mapping_issue": 2},
        )

    def test_strategy_learning_brief_uses_gap_source_context_counts_when_rows_are_legacy(self):
        brief = packet.strategy_learning_brief(
            {
                "schema": "strategy_learning_report_v1",
                "sample_scope": {},
                "overall": {},
                "judgment_effect": {},
                "intake_coverage": {},
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "outcomes": {
                        "outcome_maturity": {
                            "missing_symbol_kline_diagnostics": [
                                {
                                    "symbol": "00959",
                                    "status": "stock_found_has_day_klines_before_signal_date",
                                    "affected_signal_count": 5,
                                }
                            ]
                        }
                    },
                },
            },
            outcome_report_payload={
                "schema": "rt_signal_outcome_report_v1",
                "outcome_maturity": {
                    "daily_gap_source_diagnostic_context": {
                        "status": "ACTION_REQUIRED",
                        "category_affected_signal_counts": {"active_universe_or_symbol_mapping_issue": 5},
                    }
                },
            },
        )

        self.assertEqual(
            brief["outcome_maturity"]["daily_gap_source_category_affected_signal_counts"],
            {"active_universe_or_symbol_mapping_issue": 5},
        )

    def test_strategy_learning_brief_merges_incomplete_outcome_maturity_from_current_report(self):
        brief = packet.strategy_learning_brief(
            {
                "schema": "strategy_learning_report_v1",
                "sample_scope": {},
                "overall": {},
                "judgment_effect": {},
                "intake_coverage": {},
                "source": {
                    "read_only": True,
                    "submits_orders": False,
                    "outcomes": {
                        "outcome_maturity": {
                            "primary_horizon": "1d",
                            "latest_signal_date": "2026-06-12",
                            "latest_kline_date": "2026-06-12",
                            "pending_or_invalid_count": 93,
                            "missing_symbol_kline_count": 9,
                            "missing_symbol_kline_unique_symbol_count": None,
                            "missing_symbol_kline_diagnostics": [],
                        }
                    },
                },
            },
            outcome_report_payload={
                "schema": "rt_signal_outcome_report_v1",
                "outcome_maturity": {
                    "primary_horizon": "1d",
                    "latest_signal_date": "2026-06-12",
                    "latest_kline_date": "2026-06-12",
                    "missing_symbol_kline_unique_symbol_count": 2,
                    "missing_symbol_kline_diagnostics": [
                        {
                            "symbol": "00959",
                            "status": "stock_found_has_day_klines_before_signal_date",
                            "affected_signal_count": 5,
                        },
                        {
                            "symbol": "01918",
                            "status": "stock_found_has_day_klines_before_signal_date",
                            "affected_signal_count": 4,
                        },
                    ],
                },
            },
        )

        maturity = brief["outcome_maturity"]

        self.assertEqual(maturity["pending_or_invalid_count"], 93)
        self.assertEqual(maturity["missing_symbol_kline_count"], 9)
        self.assertEqual(maturity["missing_symbol_kline_unique_symbol_count"], 2)
        self.assertEqual(
            maturity["missing_symbol_kline_status_counts"],
            {"stock_found_has_day_klines_before_signal_date": 9},
        )

    def test_health_fail_forces_reject_or_hold_even_when_intake_has_plan(self):
        health = {"status": "FAIL", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("system_health_fail", item["blocking_reasons"])

    def test_intake_rejection_is_not_eligible_for_approval(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        rejected = {
            "signal_id": "sig-packet",
            "status": "rejected",
            "reasons": ["not_confirmed"],
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[rejected],
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertIn("intake_status_rejected", item["blocking_reasons"])
        self.assertIn("not_confirmed", item["blocking_reasons"])

    def test_sell_without_position_is_observation_not_trade_review_item(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        rejected_sell = {
            "signal_id": "sell-no-pos",
            "status": "rejected",
            "reasons": ["sell_without_position"],
        }

        payload = packet.build_packet(
            [sell_alert("sell-no-pos")],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[rejected_sell],
        )

        self.assertEqual(payload["review_items"], [])
        self.assertEqual(payload["non_actionable_observation_count"], 1)
        observation = payload["non_actionable_observations"][0]
        self.assertEqual(observation["signal_id"], "sell-no-pos")
        self.assertEqual(observation["reason"], "sell_without_position")
        self.assertEqual(
            observation["recommended_use"],
            "observation_only_no_trade_judgment_required",
        )
        self.assertEqual(observation["alert"]["signal_type"], "SELL")
        self.assertEqual(payload["review_item_suppression"]["status"], "ALL_SELECTED_ALERTS_SUPPRESSED")
        self.assertEqual(
            payload["review_item_suppression"]["reason_counts"],
            [{"key": "sell_without_position", "count": 1}],
        )
        self.assertIn(
            "treat_sell_without_position_as_position_observation_only",
            payload["review_item_suppression"]["recommendations"],
        )
        self.assertEqual(payload["alert_selection"]["review_alert_count"], 1)

    def test_sell_with_order_plan_remains_trade_review_item(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}

        payload = packet.build_packet(
            [sell_alert("sell-held")],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result("sell-held")],
        )

        self.assertEqual(len(payload["review_items"]), 1)
        self.assertEqual(payload["review_items"][0]["signal_id"], "sell-held")
        self.assertEqual(payload["non_actionable_observations"], [])
        self.assertEqual(payload["non_actionable_observation_count"], 0)

    def test_full_fundamentals_support_requires_hermes_acknowledgement(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        fundamentals = {
            "schema": "fundamentals_context_report_v1",
            "status": "OK",
            "items": [
                {
                    "symbol": "00700",
                    "market": "HK",
                    "source": "broker_fundamentals_snapshot",
                    "as_of": "2026-06-12T09:45:00",
                    "stale": False,
                    "pe_ttm": 24.0,
                    "pb": 4.0,
                    "ps": 7.0,
                    "roe_pct": 18.0,
                    "revenue_growth_pct": 9.5,
                    "earnings_growth_pct": 12.0,
                    "debt_to_equity": 0.4,
                    "valuation_flags": [],
                    "fundamental_completeness": {
                        "level": "full",
                        "available_metrics": ["pe_ttm", "pb", "roe_pct", "earnings_growth_pct", "debt_to_equity"],
                        "missing_metrics": [],
                    },
                }
            ],
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            fundamentals_context_payload=fundamentals,
        )

        digest = payload["review_items"][0]["context_digest"]
        self.assertFalse(digest["fundamentals_context"]["limit_acknowledgement_required"])
        self.assertTrue(digest["fundamentals_context"]["support_acknowledgement_required"])
        self.assertIn(
            "fundamentals_context_support_requires_acknowledgement",
            digest["required_judgment_attention"],
        )

    def test_judgment_contract_context_review_flags_match_trade_audit(self):
        contract = packet.judgment_contract("/tmp/judgments.jsonl")

        self.assertEqual(
            set(contract["append_jsonl_object"]["context_review"]) - {"notes"},
            set(audit.REQUIRED_CONTEXT_REVIEW_FLAGS),
        )
        self.assertIn("intraday_context_reviewed", contract["append_jsonl_object"]["context_review"])
        self.assertIn("intraday_context_acknowledged", contract["append_jsonl_object"])
        self.assertIn("intraday_context_status", contract["append_jsonl_object"])
        self.assertIn("intraday_context_notes", contract["append_jsonl_object"])
        self.assertIn("market_context_coverage_acknowledged", contract["append_jsonl_object"])
        self.assertIn("market_context_coverage_status", contract["append_jsonl_object"])
        self.assertIn("market_context_coverage_notes", contract["append_jsonl_object"])
        self.assertIn("external_market_context_support_acknowledged", contract["append_jsonl_object"])
        self.assertIn("external_market_context_support_ids", contract["append_jsonl_object"])
        self.assertIn("external_market_context_support_notes", contract["append_jsonl_object"])
        self.assertIn("event_catalyst_support_acknowledged", contract["append_jsonl_object"])
        self.assertIn("event_catalyst_support_signal_ids", contract["append_jsonl_object"])
        self.assertIn("event_catalyst_support_notes", contract["append_jsonl_object"])
        self.assertIn("event_catalyst_coverage_acknowledged", contract["append_jsonl_object"])
        self.assertIn("event_catalyst_coverage_status", contract["append_jsonl_object"])
        self.assertIn("event_catalyst_coverage_notes", contract["append_jsonl_object"])
        self.assertIn("market_sentiment_support_acknowledged", contract["append_jsonl_object"])
        self.assertIn("market_sentiment_support_indicator_ids", contract["append_jsonl_object"])
        self.assertIn("market_sentiment_support_notes", contract["append_jsonl_object"])
        self.assertIn("fundamentals_context_support_acknowledged", contract["append_jsonl_object"])
        self.assertIn("fundamentals_context_support_symbols", contract["append_jsonl_object"])
        self.assertIn("fundamentals_context_support_metrics", contract["append_jsonl_object"])
        self.assertIn("fundamentals_context_support_notes", contract["append_jsonl_object"])
        self.assertIn("simulation_performance_acknowledged", contract["append_jsonl_object"])
        self.assertIn("simulation_performance_status", contract["append_jsonl_object"])
        self.assertIn("simulation_performance_reason_codes", contract["append_jsonl_object"])
        self.assertIn("simulation_performance_notes", contract["append_jsonl_object"])
        self.assertTrue(
            any("simulation_performance.status is WARN or FAIL" in rule for rule in contract["hard_rules"])
        )

    def test_stale_alert_is_observation_not_trade_review_item(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        stale = {
            "signal_id": "stale-buy",
            "status": "rejected",
            "reasons": ["alert_too_old"],
        }

        payload = packet.build_packet(
            [alert("stale-buy")],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[stale],
        )

        self.assertEqual(payload["review_items"], [])
        self.assertEqual(payload["non_actionable_observation_count"], 1)
        self.assertEqual(payload["non_actionable_observations"][0]["reason"], "alert_too_old")
        self.assertEqual(payload["review_item_suppression"]["status"], "ALL_SELECTED_ALERTS_SUPPRESSED")
        self.assertEqual(payload["review_item_suppression"]["review_item_count"], 0)
        self.assertEqual(
            payload["review_item_suppression"]["reason_counts"],
            [{"key": "alert_too_old", "count": 1}],
        )
        self.assertIn(
            "wait_for_fresh_confirmed_alerts_or_run_packet_during_market_session",
            payload["review_item_suppression"]["recommendations"],
        )
        self.assertEqual(payload["alert_selection"]["review_alert_count"], 1)

    def test_critical_simulation_portfolio_risk_blocks_approval(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {
            "generated_at": "2026-06-12T10:01:00",
            "portfolio_reports": [],
            "portfolio_risk": {
                "schema": "portfolio_risk_report_v1",
                "reports": [
                    {
                        "portfolio_id": 8,
                        "role": "simulation",
                        "risk_level": "critical",
                        "risk_flags": ["positions_table_conflicts_with_trade_ledger"],
                        "trade_position_reconciliation_status": "FAIL",
                    }
                ],
            },
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("simulation_portfolio_risk_critical", item["blocking_reasons"])
        self.assertIn("portfolio_risk:positions_table_conflicts_with_trade_ledger", item["blocking_reasons"])
        self.assertEqual(payload["portfolio_risk"]["schema"], "portfolio_risk_report_v1")

    def test_failed_simulation_performance_blocks_new_buy_exposure(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        simulation_performance = {
            "schema": "simulation_performance_report_v1",
            "generated_at": "2026-06-12T10:02:00",
            "status": "FAIL",
            "reason_codes": [
                "simulation_total_return_not_positive",
                "simulation_closed_win_rate_too_low",
            ],
            "remediation_plan": {
                "schema": "simulation_strategy_remediation_v1",
                "proposal_hash": "simremed12345678",
                "status": "operator_review_required",
                "operator_contract": {"submits_orders": False},
            },
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            simulation_performance_payload=simulation_performance,
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("simulation_performance_fail", item["blocking_reasons"])
        self.assertIn(
            "simulation_performance:simulation_total_return_not_positive",
            item["blocking_reasons"],
        )
        self.assertIn(
            "simulation_performance:simulation_closed_win_rate_too_low",
            item["blocking_reasons"],
        )
        self.assertIn(
            "simulation_performance_remediation:simremed12345678",
            item["blocking_reasons"],
        )
        self.assertEqual(payload["simulation_performance"]["status"], "FAIL")

    def test_failed_simulation_performance_does_not_block_sell_review(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        simulation_performance = {
            "schema": "simulation_performance_report_v1",
            "generated_at": "2026-06-12T10:02:00",
            "status": "FAIL",
            "reason_codes": ["simulation_closed_pnl_not_positive"],
        }

        payload = packet.build_packet(
            [sell_alert("sell-risk")],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result("sell-risk")],
            execution_readiness_payload=ready_execution_readiness(),
            simulation_performance_payload=simulation_performance,
        )

        item = payload["review_items"][0]
        self.assertTrue(item["eligible_for_approval"])
        self.assertNotIn("simulation_performance_fail", item["blocking_reasons"])

    def test_exit_pressure_blocks_new_buy_but_not_sell_review(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {
            "generated_at": "2026-06-12T10:01:00",
            "portfolio_reports": [],
            "portfolio_risk": {
                "schema": "portfolio_risk_report_v1",
                "reports": [
                    {
                        "portfolio_id": 8,
                        "role": "simulation",
                        "risk_level": "high",
                        "risk_flags": ["exit_pressure_above_30pct"],
                    }
                ],
            },
            "position_review": {
                "schema": "portfolio_position_review_v1",
                "item_count": 2,
                "submits_orders": False,
            },
        }

        payload = packet.build_packet(
            [alert("buy-risk"), sell_alert("sell-risk")],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result("buy-risk"), intake_result("sell-risk")],
            execution_readiness_payload=ready_execution_readiness(),
        )

        buy_item, sell_item = payload["review_items"]
        self.assertFalse(buy_item["eligible_for_approval"])
        self.assertIn("portfolio_risk:exit_pressure_requires_review_before_new_buy", buy_item["blocking_reasons"])
        self.assertTrue(sell_item["eligible_for_approval"])
        self.assertNotIn(
            "portfolio_risk:exit_pressure_requires_review_before_new_buy",
            sell_item["blocking_reasons"],
        )
        self.assertEqual(payload["position_review"]["schema"], "portfolio_position_review_v1")

    def test_position_review_items_get_advisory_context_digest_without_mutating_portfolio_payload(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {
            "generated_at": "2026-06-12T10:01:00",
            "portfolio_reports": [],
            "position_review": {
                "schema": "portfolio_position_review_v1",
                "review_only": True,
                "submits_orders": False,
                "item_count": 1,
                "items": [
                    {
                        "review_id": "simulation:8:00700:2026-06-12:risk_review",
                        "portfolio_id": 8,
                        "role": "simulation",
                        "symbol": "00700",
                        "market": "HK",
                        "urgency": "high",
                        "recommended_action": "risk_review",
                        "execution_policy": {
                            "advice_only": False,
                            "review_only": True,
                            "submits_orders": False,
                            "requires_separate_order_path": True,
                        },
                    }
                ],
            },
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            execution_readiness_payload=ready_execution_readiness(),
            market_context_payload={
                "schema": "market_context_report_v1",
                "markets": {
                    "HK": {
                        "regime": "risk_off",
                        "risk_level": "medium",
                        "notes": ["buy_signals_against_weak_breadth"],
                        "breadth": {"above_ma20_pct": 18.0},
                    }
                },
            },
            intraday_context_payload={
                "schema": "intraday_context_report_v1",
                "status": "OK",
                "markets": {
                    "HK": {
                        "status": "OK",
                        "breadth": {"session_up_pct": 40.0, "session_down_pct": 45.0},
                        "symbols": [
                            {
                                "symbol": "00700",
                                "market": "HK",
                                "status": "STALE",
                                "latest_age_minutes": 45,
                                "session": {"change_pct": -1.1, "momentum": "strong_down"},
                            }
                        ],
                    }
                },
            },
            external_market_context_payload={
                "schema": "external_market_context_report_v1",
                "status": "OK",
                "items": [
                    {
                        "id": "ctx-position-00700",
                        "category": "news",
                        "title": "Tencent regulatory headline",
                        "sentiment": "negative",
                        "impact_score": 0.8,
                        "markets": ["HK"],
                        "symbols": ["00700"],
                    }
                ],
            },
            event_catalyst_payload={
                "schema": "event_catalyst_report_v1",
                "status": "RISK",
                "candidates": [
                    {
                        "id": "event-position-00700",
                        "scope": "symbol",
                        "title": "Policy review",
                        "sentiment": "negative",
                        "impact_score": 0.9,
                        "matched_markets": ["HK"],
                        "matched_symbols": ["00700"],
                    },
                    {
                        "id": "event-position-00700-positive",
                        "scope": "symbol",
                        "title": "Product launch beat",
                        "sentiment": "positive",
                        "impact_score": 0.75,
                        "matched_markets": ["HK"],
                        "matched_symbols": ["00700"],
                    }
                ],
            },
            market_sentiment_payload={
                "schema": "market_sentiment_report_v1",
                "status": "RISK",
                "indicators": [
                    {
                        "id": "hk-risk-off",
                        "name": "HK flow",
                        "markets": ["HK"],
                        "direction": "risk_off",
                        "score": -0.4,
                    }
                ],
            },
            fundamentals_context_payload={
                "schema": "fundamentals_context_report_v1",
                "status": "OK",
                "items": [
                    {
                        "symbol": "00700",
                        "market": "HK",
                        "source": "tencent_quote_snapshot",
                        "valuation_flags": ["partial_fundamentals"],
                        "fundamental_completeness": {
                            "level": "partial",
                            "missing_metrics": ["pb", "roe_pct"],
                        },
                    }
                ],
            },
            trusted_source_preflight_payload={
                "schema": "trusted_source_preflight_report_v1",
                "status": "WARN",
                "components": [{"name": "external_market_context_inputs", "status": "WARN"}],
            },
            source_reliability_payload={
                "schema": "source_reliability_report_v1",
                "status": "DEGRADED",
                "components": [
                    {
                        "name": "fundamentals_context",
                        "reliability_status": "DEGRADED",
                        "reasons": ["fundamentals_partial_metric_coverage"],
                    }
                ],
            },
        )

        review_item = payload["position_review"]["items"][0]
        digest = review_item["context_digest"]
        template = review_item["position_judgment_template"]
        draft = template["draft_jsonl_object"]

        self.assertNotIn("context_digest", portfolio["position_review"]["items"][0])
        self.assertNotIn("position_judgment_template", portfolio["position_review"]["items"][0])
        self.assertEqual(payload["position_review"]["context_enrichment"]["item_context_digest_count"], 1)
        self.assertEqual(
            payload["position_review"]["position_judgment_template_summary"]["template_count"],
            1,
        )
        self.assertEqual(digest["schema"], "hermes_position_review_context_digest_v1")
        self.assertTrue(digest["advisory_only"])
        self.assertFalse(digest["submits_orders"])
        self.assertEqual(digest["symbol"], "00700")
        self.assertEqual(digest["market_context"]["regime"], "risk_off")
        self.assertEqual(digest["external_market_context"]["items"][0]["id"], "ctx-position-00700")
        self.assertEqual(digest["event_catalysts"]["negative_candidate_count"], 1)
        self.assertEqual(digest["event_catalysts"]["positive_candidate_count"], 1)
        self.assertEqual(digest["market_sentiment"]["indicators"][0]["id"], "hk-risk-off")
        self.assertTrue(digest["fundamentals_context"]["limit_acknowledgement_required"])
        self.assertIn("position_negative_external_context_requires_discussion", digest["position_attention"])
        self.assertIn("position_market_sentiment_risk_requires_discussion", digest["position_attention"])
        self.assertIn("position_fundamentals_context_limit_requires_discussion", digest["position_attention"])
        self.assertIn("position_source_reliability_limit_requires_discussion", digest["position_attention"])
        self.assertEqual(template["schema"], "hermes_position_judgment_template_v1")
        self.assertTrue(template["template_only"])
        self.assertFalse(template["ready_to_append_without_hermes_review"])
        self.assertEqual(template["allowed_decisions"], ["hold", "watch", "reduce", "exit", "trail_stop"])
        self.assertEqual(draft["packet_id"], payload["packet_id"])
        self.assertEqual(draft["review_id"], "simulation:8:00700:2026-06-12:risk_review")
        self.assertEqual(draft["portfolio_id"], 8)
        self.assertEqual(draft["role"], "simulation")
        self.assertEqual(draft["symbol"], "00700")
        self.assertEqual(draft["position_attention_codes"], digest["position_attention"])
        self.assertEqual(
            [row["code"] for row in draft["position_attention_effects"]],
            digest["position_attention"],
        )
        self.assertIn("set true only after", draft["context_review"]["position_context_reviewed"])
        self.assertTrue(
            any("position_review.items[].context_digest" in note for note in payload["operator_notes"])
        )
        self.assertTrue(
            any("position_judgment_template is a draft helper" in note for note in payload["operator_notes"])
        )

    def test_user_position_judgment_template_limits_machine_decisions_to_hold_watch(self):
        item = {
            "review_id": "user:1:AAPL:2026-06-12:risk_review",
            "portfolio_id": 1,
            "role": "user",
            "symbol": "AAPL",
            "context_digest": {
                "schema": "hermes_position_review_context_digest_v1",
                "position_attention": ["high_urgency_position_requires_contextual_rationale"],
            },
        }

        template = packet.position_judgment_template_for_item(
            item,
            "packet-user-test",
            "/tmp/position_judgments.jsonl",
        )
        draft = template["draft_jsonl_object"]

        self.assertEqual(template["allowed_decisions"], ["hold", "watch"])
        self.assertEqual(draft["packet_id"], "packet-user-test")
        self.assertEqual(draft["role"], "user")
        self.assertIn("hold|watch", draft["decision"])
        self.assertIn("manual reduce/exit advice", " ".join(template["instructions"]))
        self.assertEqual(
            draft["position_attention_codes"],
            ["high_urgency_position_requires_contextual_rationale"],
        )

    def test_data_health_fail_blocks_approval(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        data_health = {
            "schema": "data_health_report_v1",
            "status": "FAIL",
            "markets": {
                "HK": {
                    "status": "FAIL",
                    "failures": ["invalid_latest_ohlc"],
                }
            },
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            data_health_payload=data_health,
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("data_health_fail", item["blocking_reasons"])
        self.assertIn("data_health:HK:invalid_latest_ohlc", item["blocking_reasons"])
        self.assertEqual(payload["data_health"]["status"], "FAIL")

    def test_kline_daily_gap_repair_is_visible_but_does_not_relax_eligibility(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        data_health = {
            "schema": "data_health_report_v1",
            "status": "FAIL",
            "markets": {
                "HK": {
                    "status": "FAIL",
                    "failures": ["minute_fresh_daily_stale_symbols"],
                }
            },
        }
        repair = {
            "schema": "kline_daily_gap_repair_report_v1",
            "mode": "dry-run",
            "plan_hash": "b4542aaa0774600e",
            "summary": {"candidate_count": 2, "repair_action_count": 1, "unresolved_count": 1},
            "actions": [
                {
                    "symbol": "01918",
                    "source_symbol": "hk01918",
                    "rows": [{"date": "2026-06-12", "close": 12.34}],
                }
            ],
            "unresolved": [
                {
                    "symbol": "00959",
                    "source_symbol": "hk00959",
                    "latest_daily_date": "2025-06-25",
                    "target_end_date": "2026-06-12",
                    "latest_source_date": "2025-06-25",
                    "source_reaches_target_end": False,
                    "source_after_latest_daily": False,
                }
            ],
            "apply_contract": {
                "dry_run_default": True,
                "apply_requires": "--apply --confirm-plan-hash <plan_hash>",
                "backs_up_existing_rows_before_apply": True,
                "does_not_submit_orders": True,
                "does_not_change_crontab": True,
                "updates": ["klines interval=day rows for planned symbol/date gaps only"],
            },
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            data_health_payload=data_health,
            execution_readiness_payload=ready_execution_readiness(),
            kline_daily_gap_repair_payload=repair,
        )

        self.assertEqual(payload["kline_daily_gap_repair"]["schema"], "kline_daily_gap_repair_report_v1")
        self.assertEqual(payload["kline_daily_gap_repair"]["plan_hash"], "b4542aaa0774600e")
        self.assertTrue(payload["kline_daily_gap_repair"]["apply_contract"]["dry_run_default"])
        self.assertTrue(payload["kline_daily_gap_repair"]["apply_contract"]["does_not_submit_orders"])
        self.assertTrue(payload["kline_daily_gap_repair"]["apply_contract"]["does_not_change_crontab"])
        self.assertEqual(payload["kline_daily_gap_repair"]["unresolved"][0]["symbol"], "00959")
        self.assertFalse(payload["kline_daily_gap_repair"]["unresolved"][0]["source_reaches_target_end"])
        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertIn("data_health_fail", item["blocking_reasons"])
        self.assertIn("data_health:HK:minute_fresh_daily_stale_symbols", item["blocking_reasons"])
        self.assertTrue(
            any("K-line daily gap repair is dry-run/manual remediation context only" in note for note in payload["operator_notes"])
        )
        self.assertTrue(
            any("K-line gap source diagnostic classifies unresolved daily-gap symbols" in note for note in payload["operator_notes"])
        )

    def test_signal_symbol_daily_gap_source_issue_is_visible_in_context_digest(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        signal = alert("gap-exposed-signal")
        signal["symbol"] = "00959"
        diagnostic = {
            "schema": "kline_gap_source_diagnostic_report_v1",
            "status": "ACTION_REQUIRED",
            "summary": {
                "unresolved_count": 1,
                "classified_count": 1,
                "current_v5_watchlist_exposed_count": 1,
                "open_position_exposed_count": 0,
                "sample_current_v5_watchlist_exposed_symbols": ["00959"],
            },
            "classifications": [
                {
                    "symbol": "00959",
                    "market": "HK",
                    "category": "active_universe_or_symbol_mapping_issue",
                    "confidence": "high",
                    "recommended_action": "review_active_universe_and_symbol_mapping_before_trusting_symbol",
                    "reason": "source_gap_rows_missing",
                    "latest_daily_date": "2025-06-25",
                    "target_end_date": "2026-06-12",
                    "latest_source_date": "2025-06-25",
                    "source_lag_days_vs_target": 352,
                    "daily_lag_days_vs_target": 352,
                    "exposure": {
                        "schema": "unresolved_daily_gap_exposure_v1",
                        "symbol": "00959",
                        "market": "HK",
                        "in_current_v5_watchlist": True,
                        "watchlist_markets": ["HK"],
                        "has_open_position": False,
                        "deactivation_blockers": ["current_v5_watchlist_member"],
                        "safe_to_deactivate_without_manual_review": False,
                    },
                }
            ],
        }

        payload = packet.build_packet(
            [signal],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result("gap-exposed-signal")],
            execution_readiness_payload=ready_execution_readiness(),
            kline_gap_source_diagnostic_payload=diagnostic,
        )

        digest = payload["review_items"][0]["context_digest"]
        daily_gap = digest["daily_gap_source_diagnostic"]

        self.assertEqual(daily_gap["schema"], "hermes_review_item_daily_gap_source_digest_v1")
        self.assertTrue(daily_gap["matched"])
        self.assertEqual(daily_gap["symbol"], "00959")
        self.assertEqual(daily_gap["category"], "active_universe_or_symbol_mapping_issue")
        self.assertTrue(daily_gap["exposure"]["in_current_v5_watchlist"])
        self.assertIn("current_v5_watchlist_member", daily_gap["exposure"]["deactivation_blockers"])
        self.assertIn(
            "signal_symbol_unresolved_daily_gap_requires_rejection_or_hold",
            digest["required_judgment_attention"],
        )
        self.assertIn(
            "signal_symbol_unresolved_daily_gap_watchlist_member_requires_review",
            digest["required_judgment_attention"],
        )
        self.assertFalse(payload["review_items"][0]["eligible_for_approval"])
        self.assertEqual(payload["review_items"][0]["recommended_judgment"], "reject_or_hold")
        self.assertIn("daily_gap_source_unresolved_symbol", payload["review_items"][0]["blocking_reasons"])
        self.assertIn(
            "daily_gap_source:active_universe_or_symbol_mapping_issue",
            payload["review_items"][0]["blocking_reasons"],
        )
        self.assertIn(
            "daily_gap_source:current_v5_watchlist_member",
            payload["review_items"][0]["blocking_reasons"],
        )
        self.assertFalse(digest["submits_orders"])

    def test_strategy_evidence_dry_run_block_is_not_eligible_for_approval(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        result = intake_result()
        result["strategy_evidence"] = {
            "status": "DRY_RUN_ONLY",
            "would_block_execute": True,
            "reasons": ["overall_outcome_sample_below_30", "trigger_outcome_missing"],
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[result],
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("strategy_evidence_would_block_execute", item["blocking_reasons"])
        self.assertIn("strategy_evidence:overall_outcome_sample_below_30", item["blocking_reasons"])
        self.assertIn("strategy_evidence:trigger_outcome_missing", item["blocking_reasons"])

    def test_symbol_conflict_dry_run_block_is_not_eligible_for_approval(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        result = intake_result()
        result["symbol_conflict"] = {
            "status": "DRY_RUN_ONLY",
            "would_block_execute": True,
            "reasons": ["symbol_conflict_opposite_direction_in_queue"],
            "opposite_count": 1,
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[result],
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("symbol_conflict_would_block_execute", item["blocking_reasons"])
        self.assertIn("symbol_conflict:symbol_conflict_opposite_direction_in_queue", item["blocking_reasons"])

    def test_execution_readiness_dry_run_block_is_not_eligible_for_approval(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        result = intake_result()
        result["execution_readiness"] = {
            "status": "DRY_RUN_ONLY",
            "would_block_execute": True,
            "reasons": ["execution_readiness_blocked", "ready_for_execute_false"],
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[result],
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("execution_readiness_would_block_execute", item["blocking_reasons"])
        self.assertIn("execution_readiness:execution_readiness_blocked", item["blocking_reasons"])
        self.assertIn("execution_readiness:ready_for_execute_false", item["blocking_reasons"])

    def test_top_level_execution_readiness_blocked_forces_reject_or_hold(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        readiness = {
            "schema": "execution_readiness_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "BLOCKED",
            "ready_for_execute": False,
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            execution_readiness_payload=readiness,
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("execution_readiness_status_blocked", item["blocking_reasons"])
        self.assertIn("execution_readiness_ready_for_execute_false", item["blocking_reasons"])

    def test_top_level_execution_readiness_missing_timestamp_forces_reject_or_hold(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        readiness = {
            "schema": "execution_readiness_report_v1",
            "status": "READY",
            "ready_for_execute": True,
        }

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            execution_readiness_payload=readiness,
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("execution_readiness_generated_at_missing", item["blocking_reasons"])

    def test_top_level_execution_readiness_stale_forces_reject_or_hold(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        readiness = ready_execution_readiness(
            generated_at=(datetime.now() - timedelta(hours=packet.MAX_READINESS_REPORT_AGE_HOURS + 1)).isoformat(
                timespec="seconds"
            )
        )

        payload = packet.build_packet(
            [alert()],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result()],
            execution_readiness_payload=readiness,
        )

        item = payload["review_items"][0]
        self.assertFalse(item["eligible_for_approval"])
        self.assertEqual(item["recommended_judgment"], "reject_or_hold")
        self.assertIn("execution_readiness_stale", item["blocking_reasons"])

    def test_select_review_alerts_skips_watch_and_unconfirmed_by_default(self):
        alerts = [
            watch_alert("w1"),
            alert("b1"),
            unconfirmed_alert("u1"),
            watch_alert("w2"),
            alert("b2"),
            watch_alert("w3"),
        ]

        selected = packet.select_review_alerts(alerts, limit=10)

        self.assertEqual([item["signal_id"] for item in selected], ["b1", "b2"])

    def test_select_review_alerts_can_include_unconfirmed_for_debugging(self):
        alerts = [alert("b1"), unconfirmed_alert("u1")]

        selected = packet.select_review_alerts(alerts, limit=10, include_unconfirmed=True)

        self.assertEqual([item["signal_id"] for item in selected], ["b1", "u1"])

    def test_select_review_alerts_can_include_watch_for_debugging(self):
        alerts = [watch_alert("w1"), alert("b1"), watch_alert("w2")]

        selected = packet.select_review_alerts(alerts, limit=10, include_watch=True)

        self.assertEqual([item["signal_id"] for item in selected], ["w1", "b1", "w2"])

    def test_select_review_alerts_defaults_to_latest_strategy_watchlist_scope(self):
        old = alert("old")
        old.update({"strategy_config_id": "cfg-old", "watchlist_id": "wl"})
        current_earlier = alert("current-earlier")
        current_earlier.update({"strategy_config_id": "cfg-current", "watchlist_id": "wl"})
        current = alert("current")
        current.update({"strategy_config_id": "cfg-current", "watchlist_id": "wl"})

        selected = packet.select_review_alerts([old, current_earlier, current], limit=10)

        self.assertEqual([item["signal_id"] for item in selected], ["current-earlier", "current"])

        selected_all = packet.select_review_alerts(
            [old, current_earlier, current],
            limit=10,
            sample_scope_mode="all",
        )
        self.assertEqual([item["signal_id"] for item in selected_all], ["old", "current-earlier", "current"])

    def test_packet_alert_selection_counts_source_noise(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        source_alerts = [watch_alert("w1"), alert("b1"), alert("b2")]

        payload = packet.build_packet(
            [source_alerts[1], source_alerts[2]],
            health_payload=health,
            portfolio_payload=portfolio,
            intake_results=[intake_result("b1"), intake_result("b2")],
            source_alerts=source_alerts,
        )

        self.assertEqual(payload["alert_selection"]["source_alert_count"], 3)
        self.assertEqual(payload["alert_selection"]["review_alert_count"], 2)
        self.assertEqual(payload["alert_selection"]["directional_count"], 2)
        self.assertEqual(payload["alert_selection"]["confirmed_directional_count"], 2)
        self.assertEqual(payload["alert_selection"]["unconfirmed_directional_count"], 0)
        self.assertEqual(payload["alert_selection"]["by_signal_type"]["WATCH"], 1)

    def test_archive_packet_writes_snapshot_by_packet_id(self):
        payload = {
            "schema": "hermes_signal_review_packet_v1",
            "packet_id": "packet:test/1",
            "review_items": [],
        }

        with tempfile.TemporaryDirectory() as td:
            path = packet.archive_packet(payload, td)
            stored = json.loads(Path(path).read_text(encoding="utf-8"))

        self.assertTrue(path.endswith("packet_test_1.json"))
        self.assertEqual(stored["packet_id"], "packet:test/1")

    def test_prune_packet_archive_keeps_newest_snapshots_by_limit(self):
        with tempfile.TemporaryDirectory() as td:
            archive_dir = Path(td)
            for index in range(5):
                path = archive_dir / f"old-{index}.json"
                path.write_text(json.dumps({"packet_id": f"old-{index}"}), encoding="utf-8")
                old_ts = datetime(2026, 6, 12, 10, index).timestamp()
                os.utime(path, (old_ts, old_ts))

            result = packet.prune_packet_archive(
                td,
                max_files=3,
                max_age_hours=0,
                now=datetime(2026, 6, 12, 11, 0),
            )
            remaining = sorted(item.name for item in archive_dir.glob("*.json"))

        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(result["kept_count"], 3)
        self.assertEqual(remaining, ["old-2.json", "old-3.json", "old-4.json"])
        self.assertNotIn("old-0.json", remaining)

    def test_prune_packet_archive_caps_total_archive_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            archive_dir = Path(td)
            for index in range(4):
                path = archive_dir / f"packet-{index}.json"
                path.write_text("x" * 40, encoding="utf-8")
                old_ts = datetime(2026, 6, 12, 10, index).timestamp()
                os.utime(path, (old_ts, old_ts))

            result = packet.prune_packet_archive(
                td,
                max_files=10,
                max_age_hours=0,
                max_bytes=90,
                now=datetime(2026, 6, 12, 11, 0),
            )
            remaining = sorted(item.name for item in archive_dir.glob("*.json"))

        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(result["deleted_bytes"], 80)
        self.assertEqual(result["kept_count"], 2)
        self.assertEqual(result["kept_bytes"], 80)
        self.assertEqual(remaining, ["packet-2.json", "packet-3.json"])

    def test_build_packet_loads_source_reliability_when_not_passed(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        source_reliability = {
            "schema": "source_reliability_report_v1",
            "generated_at": "2026-06-12T10:01:00",
            "status": "DEGRADED",
            "components": [
                {
                    "name": "external_market_context",
                    "reliability_status": "DEGRADED",
                    "reasons": ["external_context_only_public_fallback_sources"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source_reliability.json"
            path.write_text(json.dumps(source_reliability), encoding="utf-8")
            old_path = packet.SOURCE_RELIABILITY_REPORT_FILE
            packet.SOURCE_RELIABILITY_REPORT_FILE = str(path)
            try:
                payload = packet.build_packet(
                    [alert()],
                    health_payload=health,
                    portfolio_payload=portfolio,
                    intake_results=[intake_result()],
                    execution_readiness_payload=ready_execution_readiness(),
                )
            finally:
                packet.SOURCE_RELIABILITY_REPORT_FILE = old_path

        self.assertEqual(payload["source_reliability"]["schema"], "source_reliability_report_v1")
        self.assertEqual(payload["source_reliability"]["status"], "DEGRADED")
        self.assertEqual(payload["source_reliability"]["components"][0]["name"], "external_market_context")

    def test_build_packet_loads_intraday_market_session_overrides_when_not_passed(self):
        health = {"status": "OK", "checked_at": "2026-06-12T10:01:00", "checks": []}
        portfolio = {"generated_at": "2026-06-12T10:01:00", "portfolio_reports": []}
        overrides = {
            "schema": "intraday_market_session_overrides_report_v1",
            "generated_at": "2026-06-12T10:01:00",
            "status": "WARN",
            "summary": {"configured_market_count": 1, "missing_market_count": 1},
            "recommendations": ["add_US_market_session_overrides"],
            "source": {
                "read_only": True,
                "submits_orders": False,
                "changes_crontab": False,
                "changes_strategy": False,
            },
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "intraday_market_session_overrides.json"
            path.write_text(json.dumps(overrides), encoding="utf-8")
            old_path = packet.INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE
            packet.INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE = str(path)
            try:
                payload = packet.build_packet(
                    [alert()],
                    health_payload=health,
                    portfolio_payload=portfolio,
                    intake_results=[intake_result()],
                    execution_readiness_payload=ready_execution_readiness(),
                )
            finally:
                packet.INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE = old_path

        self.assertEqual(
            payload["intraday_market_session_overrides"]["schema"],
            "intraday_market_session_overrides_report_v1",
        )
        self.assertEqual(payload["intraday_market_session_overrides"]["status"], "WARN")
        self.assertEqual(
            payload["intraday_market_session_overrides"]["recommendations"],
            ["add_US_market_session_overrides"],
        )
        self.assertTrue(any("Intraday market-session overrides" in note for note in payload["operator_notes"]))


if __name__ == "__main__":
    unittest.main()
