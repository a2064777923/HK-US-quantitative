import unittest
import json
import tempfile
from pathlib import Path

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
        "full_score": 0.72,
        "entry_price": 300,
        "stop_loss": 290,
        "take_profit": 330,
        "rr_ratio": 3.0,
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
            execution_readiness_payload={
                "schema": "execution_readiness_report_v1",
                "status": "BLOCKED",
                "ready_for_execute": False,
                "source": {"read_only": True, "submits_orders": False},
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
                "source": {"read_only": True, "submits_orders": False},
            },
            market_context_payload={"schema": "market_context_report_v1", "markets": {"HK": {"regime": "mixed"}}},
            data_health_payload={"schema": "data_health_report_v1", "status": "OK", "markets": {}},
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
                "markets": {"HK": {"problem_symbol_count": 0}},
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
        self.assertEqual(payload["execution_readiness"]["schema"], "execution_readiness_report_v1")
        self.assertFalse(payload["execution_readiness"]["ready_for_execute"])
        self.assertEqual(payload["strategy_learning"]["schema"], "strategy_learning_report_v1")
        self.assertFalse(payload["strategy_learning"]["source"]["submits_orders"])
        self.assertEqual(payload["strategy_learning_brief"]["schema"], "hermes_strategy_learning_brief_v1")
        self.assertFalse(payload["strategy_learning_brief"]["submits_orders"])
        self.assertEqual(payload["strategy_learning_brief"]["sample_scope"]["strategy_config_id"], "cfg-1")
        self.assertEqual(payload["strategy_learning_brief"]["intake_coverage"]["directional_pct"], 100.0)
        self.assertEqual(
            payload["strategy_learning_brief"]["judgment_effect"]["approved_or_reduced"]["avg_signed_return_pct"],
            1.0,
        )
        self.assertEqual(
            payload["strategy_learning_brief"]["judgment_effect"]["rejected_or_held"]["avg_signed_return_pct"],
            -0.5,
        )
        self.assertFalse(payload["strategy_learning_brief"]["outcome_evidence"]["minimum_sample_met"])
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
        self.assertEqual(payload["universe_context"]["schema"], "universe_rank_report_v1")
        self.assertFalse(payload["universe_context"]["source"]["auto_applies_watchlist"])
        self.assertEqual(payload["watchlist_diff"]["schema"], "watchlist_diff_report_v1")
        self.assertFalse(payload["watchlist_diff"]["source"]["auto_applies_watchlist"])
        self.assertEqual(payload["universe_hygiene"]["schema"], "universe_hygiene_report_v1")
        self.assertFalse(payload["universe_hygiene"]["source"]["auto_applies_stock_changes"])
        self.assertEqual(payload["judgment_audit"]["schema"], "hermes_judgment_audit_report_v1")
        self.assertEqual(
            payload["position_judgment_audit"]["schema"],
            "hermes_position_judgment_audit_report_v1",
        )
        self.assertEqual(payload["position_judgment_contract"]["judgment_file"], "/tmp/position_judgments.jsonl")
        self.assertFalse(payload["position_judgment_contract"]["append_jsonl_object"]["submits_orders"])
        self.assertTrue(
            any("symbol_conflict" in rule for rule in payload["judgment_contract"]["hard_rules"])
        )
        self.assertEqual(payload["portfolio_risk"]["reports"][0]["role"], "simulation")
        self.assertEqual(payload["alert_selection"]["review_alert_count"], 1)
        self.assertEqual(payload["alert_selection"]["directional_count"], 1)

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


if __name__ == "__main__":
    unittest.main()
