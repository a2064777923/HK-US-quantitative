import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RtAlertBridgeTests(unittest.TestCase):
    def load_bridge(self, **env):
        keys = {
            "RT_ALERT_REMOTE",
            "RT_ALERT_FILE",
            "RT_ALERT_QUEUE_FILE",
            "RT_ALERT_SENT_FILE",
            "RT_ALERT_EXECUTION_MODE",
            "RT_ALERT_SEND_FEISHU",
            "RT_ALERT_INCLUDE_POSITION_REVIEW",
            "RT_ALERT_REQUIRE_PACKET_ELIGIBLE",
            "RT_ALERT_NOTIFY_INELIGIBLE_SIGNALS",
            "RT_ALERT_MARK_INELIGIBLE_SENT",
            "RT_POSITION_REVIEW_SENT_FILE",
            "RT_POSITION_REVIEW_ROLES",
            "RT_POSITION_REVIEW_URGENCY",
            "RT_POSITION_REVIEW_LIMIT",
            "RT_POSITION_REVIEW_REMINDER_HOURS",
            "RT_OPERATOR_ACTION_SENT_FILE",
            "RT_OPERATOR_ACTION_PRIORITIES",
            "RT_OPERATOR_ACTION_LIMIT",
            "RT_OPERATOR_ACTION_REMINDER_HOURS",
            "RT_OPERATOR_ACTION_SUPPRESSED_IDS",
            "HERMES_REVIEW_PACKET_FILE",
            "RT_ORDER_EXECUTE_PILOT_ENABLED",
            "RT_ORDER_US_BROKER",
            "RT_ORDER_REQUIRE_EXECUTION_READINESS",
            "RT_ORDER_REQUIRE_STRATEGY_EVIDENCE",
            "RT_ORDER_REQUIRE_HERMES_JUDGMENT",
            "RT_ORDER_REQUIRE_MARKET_CONTEXT",
            "RT_ORDER_REQUIRE_NO_SYMBOL_CONFLICT",
            "ALPACA_API_KEY",
            "ALPACA_SECRET_KEY",
            "ALPACA_BASE_URL",
        }
        old = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ.update(env)
            import scripts.rt_alert_bridge as bridge

            return importlib.reload(bridge)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def fresh_alert(self):
        return {
            "signal_id": "sig-bridge",
            "symbol": "AAPL",
            "signal_type": "BUY",
            "trigger": "unit-test",
            "detail": "bridge test",
            "confirmed": True,
            "execution_candidate": True,
            "entry_price": 100,
            "stop_loss": 95,
            "take_profit": 112,
            "rr_ratio": 2.4,
            "full_score": 0.7,
            "factor_evidence_basis": {
                "completed_daily_ohlcv": 2,
                "current_session_quote": 1,
            },
            "current_session_quote_evidence": {
                "schema": "current_session_quote_evidence_v1",
                "used_in_full_score": True,
                "mutates_completed_daily_history": False,
                "replaces_completed_daily_bar": False,
            },
            "price": 100,
            "change_pct": 1.2,
        }

    def watch_alert(self):
        alert = self.fresh_alert()
        alert["signal_id"] = "sig-watch"
        alert["signal_type"] = "WATCH"
        return alert

    def packet_with_position_review(self):
        return {
            "schema": "hermes_signal_review_packet_v1",
            "packet_id": "packet-bridge",
            "position_judgment_audit": {
                "coverage": {
                    "unjudged_high_urgency_review_count": 1,
                    "unjudged_high_urgency_examples": [{"review_id": "user:3:AAPL:2026-06-12:risk_review"}],
                }
            },
            "position_judgment_worklist": {
                "schema": "hermes_position_judgment_worklist_v1",
                "advisory_only": True,
                "submits_orders": False,
                "items": [
                    {
                        "review_id": "user:3:AAPL:2026-06-12:risk_review",
                        "review_thread_key": "user:3:AAPL",
                        "portfolio_id": 3,
                        "role": "user",
                        "symbol": "AAPL",
                        "urgency": "high",
                        "recommended_action": "risk_review",
                        "context_summary": {
                            "dynamic_management": {
                                "target_status": "below_signal_stop",
                                "unrealized_pnl_pct": -9.5,
                                "latest_daily_change_pct": -3.2,
                                "distance_above_signal_stop_loss_pct": -1.2,
                                "price_snapshot_age_hours": 36.0,
                            },
                            "intraday_position_evidence": {
                                "action_intent": "risk_reduction",
                                "alignment": "supports_recommended_action",
                                "status": "OK",
                                "session_momentum": "strong_down",
                                "session_change_pct": -3.2,
                                "timeframe_alignment": "bearish_aligned",
                                "support_codes": [
                                    "session_down_supports_reduce_exit",
                                    "multi_timeframe_bearish_supports_reduce_exit",
                                ],
                                "challenge_codes": [],
                                "limit_codes": [],
                            }
                        },
                        "required_attention_codes": [
                            "high_urgency_position_requires_contextual_rationale",
                            "position_market_sentiment_risk_requires_discussion",
                            "position_intraday_evidence_requires_discussion",
                        ],
                        "required_output_fields": {
                            "schema": "hermes_position_judgment_v1",
                            "advisory_only": True,
                            "submits_orders": False,
                        },
                    }
                ],
            },
            "position_review": {
                "schema": "portfolio_position_review_v1",
                "items": [
                    {
                        "review_id": "user:3:AAPL:2026-06-12:risk_review",
                        "portfolio_id": 3,
                        "role": "user",
                        "symbol": "AAPL",
                        "market": "US",
                        "urgency": "high",
                        "recommended_action": "risk_review",
                        "position": {
                            "quantity": 10,
                            "unrealized_pnl_pct": -9.5,
                            "stop_distance_pct": -1.2,
                        },
                        "latest_signal": {"side": "SELL", "score": -0.6},
                        "execution_policy": {"submits_orders": False, "requires_separate_order_path": True},
                        "advisory_plan": {
                            "schema": "position_advisory_plan_v1",
                            "advisory_only": True,
                            "submits_orders": False,
                            "primary_action": "review_reduce_half_or_exit_if_context_worsens",
                            "reference_price_scope": "latest_signal_geometry_not_position_order",
                            "add_allowed_after_review": False,
                            "manual_max_quantity_hint": 5,
                            "operator_decision_points": [
                                {"decision": "reduce", "quantity_hint": 5, "price_reference": 180, "manual_only": True},
                                {"decision": "exit", "quantity_hint": 10, "price_reference": 178, "manual_only": True},
                            ],
                            "intraday_review_contract": {
                                "required_timeframes": ["session", "5m", "15m", "60m"],
                                "required_checks": [
                                    "confirm_symbol_session_direction_and_change_pct",
                                    "review_5m_15m_60m_alignment_before_action",
                                    "confirm_sell_or_stop_pressure_is_not_only_stale_daily_signal",
                                ],
                                "decision_use": "can_support_reduce_exit_only_after_fresh_intraday_or_market_confirmation",
                            },
                            "reference_prices": {
                                "signal_stop_loss": 180,
                                "signal_take_profit": 220,
                                "trailing_stop_floor_reference": 180,
                            },
                            "dynamic_management_context": {
                                "schema": "position_dynamic_management_context_v1",
                                "target_status": "below_signal_stop",
                                "unrealized_pnl_pct": -9.5,
                                "latest_daily_change_pct": -3.2,
                                "distance_to_signal_take_profit_pct": 24.3,
                                "distance_above_signal_stop_loss_pct": -1.2,
                                "price_snapshot_age_hours": 36.0,
                                "trail_floor_reference": None,
                                "requires_hermes_dynamic_review": True,
                            },
                        },
                        "context_digest": {
                            "position_attention": [
                                "high_urgency_position_requires_contextual_rationale",
                                "position_market_sentiment_risk_requires_discussion",
                            ]
                        },
                    }
                ],
            },
        }

    def packet_with_operator_actions(self):
        return {
            "schema": "hermes_signal_review_packet_v1",
            "packet_id": "packet-operator-actions",
            "operator_action_queue": {
                "schema": "operator_action_queue_report_v1",
                "status": "ACTION_REQUIRED",
                "summary": {"action_count": 2, "priority_counts": {"P0": 2}},
                "actions": [
                    {
                        "id": "write_high_urgency_position_judgments",
                        "priority": "P0",
                        "title": "Write advisory Hermes judgments for high-urgency position reviews",
                        "summary": "Position reviews have a dedicated bridge section.",
                        "operator_effect": {
                            "submits_orders": False,
                            "changes_portfolio": False,
                            "changes_strategy": False,
                        },
                    },
                    {
                        "id": "review_simulation_performance_failure",
                        "priority": "P0",
                        "title": "Review failed simulation performance before new exposure",
                        "summary": "Simulation performance attribution is FAIL.",
                        "evidence": {
                            "postmortem_note_write_plan": [
                                {"target_id": "closed_trade:LI", "symbol": "LI", "draft_available": True},
                                {"target_id": "closed_trade:TSLA", "symbol": "TSLA", "draft_available": True},
                            ]
                        },
                        "operator_effect": {
                            "submits_orders": False,
                            "changes_portfolio": False,
                            "changes_strategy": False,
                            "writes_postmortem_notes": False,
                        },
                    },
                ],
            },
            "position_review": {"schema": "portfolio_position_review_v1", "items": []},
        }

    def packet_with_signal_review(self):
        return {
            "schema": "hermes_signal_review_packet_v1",
            "packet_id": "packet-signal",
            "generated_at": "2026-06-12T10:01:00",
            "execution_readiness": {"status": "WARN", "ready_for_execute": False},
            "simulation_performance": {"status": "FAIL", "reason_codes": ["recent_closed_trades_negative"]},
            "strategy_learning_brief": {
                "hermes_alpha_evidence": {
                    "status": "INSUFFICIENT",
                    "approved_or_reduced_sample": 2,
                    "rejected_or_held_sample": 1,
                }
            },
            "review_items": [
                {
                    "signal_id": "sig-bridge",
                    "eligible_for_approval": False,
                    "recommended_judgment": "reject_or_hold",
                    "blocking_reasons": ["execution_readiness_would_block_execute", "simulation_performance_fail"],
                    "context_digest": {
                        "market_context": {"regime": "risk_off", "risk_level": "high"},
                        "intraday_signal_evidence": {
                            "alignment": "challenges",
                            "codes": ["intraday_down_momentum"],
                            "requires_judgment_acknowledgement": True,
                        },
                        "external_market_context": {"status": "RISK", "relevant_item_count": 1},
                        "event_catalysts": {
                            "status": "RISK",
                            "negative_candidate_count": 1,
                            "positive_candidate_count": 0,
                        },
                        "event_catalyst_signals": {
                            "status": "RISK",
                            "challenge_buy_count": 1,
                            "support_buy_count": 0,
                        },
                        "market_sentiment": {"status": "RISK", "relevant_indicator_count": 1},
                        "fundamentals_context": {"status": "STALE", "relevant_item_count": 0},
                        "source_limits": {
                            "source_reliability_status": "DEGRADED",
                            "components": [{"name": "fundamentals_context", "reasons": ["partial_metric_coverage"]}],
                        },
                    },
                }
            ],
        }

    def packet_with_eligible_signal_review(self):
        packet = self.packet_with_signal_review()
        packet["execution_readiness"] = {
            "schema": "execution_readiness_report_v1",
            "status": "READY",
            "ready_for_execute": True,
        }
        packet["simulation_performance"] = {"status": "PASS", "reason_codes": []}
        packet["review_items"][0]["eligible_for_approval"] = True
        packet["review_items"][0]["recommended_judgment"] = "approve_or_reduce_allowed_after_llm_review"
        packet["review_items"][0]["blocking_reasons"] = []
        return packet

    def test_local_mode_reads_queue_and_writes_sent_without_ssh(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                RT_ALERT_EXECUTION_MODE="notify",
                RT_ALERT_REQUIRE_PACKET_ELIGIBLE="0",
            )

            with patch("builtins.print") as printed, patch.object(bridge.subprocess, "run") as run:
                code = bridge.main()

            self.assertEqual(code, 0)
            run.assert_not_called()
            self.assertTrue(sent.exists())
            self.assertEqual(json.loads(sent.read_text(encoding="utf-8"))[0]["signal_id"], "sig-bridge")
            self.assertIn("RT_ALERT_EXECUTION_MODE=notify", printed.call_args.args[0])

    def test_alert_sim_passes_pilot_and_alpaca_env_to_intake(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_eligible_signal_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="alert-sim",
                RT_ORDER_EXECUTE_PILOT_ENABLED="1",
                RT_ORDER_US_BROKER="alpaca-paper",
            )

            with patch.dict(
                os.environ,
                {
                    "RT_ORDER_EXECUTE_PILOT_ENABLED": "1",
                    "RT_ORDER_US_BROKER": "alpaca-paper",
                    "RT_ORDER_REQUIRE_EXECUTION_READINESS": "0",
                    "RT_ORDER_REQUIRE_STRATEGY_EVIDENCE": "0",
                    "RT_ORDER_REQUIRE_HERMES_JUDGMENT": "0",
                    "ALPACA_API_KEY": "paper-key",
                    "ALPACA_SECRET_KEY": "paper-secret",
                    "ALPACA_BASE_URL": "https://paper-api.alpaca.markets/v2",
                },
                clear=False,
            ), patch.object(bridge, "run_cmd", return_value='{"results":[{"status":"submitted"}]}') as run_cmd, patch(
                "builtins.print"
            ):
                bridge.main()

            command = run_cmd.call_args.args[0]
            self.assertIn("RT_ORDER_EXECUTE_PILOT_ENABLED=1", command)
            self.assertIn("RT_ORDER_US_BROKER=alpaca-paper", command)
            self.assertIn("RT_ORDER_REQUIRE_EXECUTION_READINESS=1", command)
            self.assertIn("RT_ORDER_REQUIRE_STRATEGY_EVIDENCE=1", command)
            self.assertIn("RT_ORDER_REQUIRE_HERMES_JUDGMENT=1", command)
            self.assertIn("RT_ORDER_REQUIRE_MARKET_CONTEXT=1", command)
            self.assertIn("RT_ORDER_REQUIRE_NO_SYMBOL_CONFLICT=1", command)
            self.assertNotIn("RT_ORDER_REQUIRE_EXECUTION_READINESS=0", command)
            self.assertNotIn("RT_ORDER_REQUIRE_STRATEGY_EVIDENCE=0", command)
            self.assertNotIn("RT_ORDER_REQUIRE_HERMES_JUDGMENT=0", command)
            self.assertIn("ALPACA_API_KEY=paper-key", command)
            self.assertIn("ALPACA_SECRET_KEY=paper-secret", command)
            self.assertIn("ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2", command)
            self.assertIn("RT_ORDER_EXECUTION_MODE=execute", command)

    def test_signal_notification_requires_hermes_eligible_and_ready_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_signal_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
            )

            with patch("builtins.print") as printed, patch.object(bridge, "run_order_intake") as intake:
                code = bridge.main()

            self.assertEqual(code, 0)
            printed.assert_not_called()
            intake.assert_not_called()
            self.assertTrue(sent.exists())
            self.assertEqual(json.loads(sent.read_text(encoding="utf-8"))[0]["signal_id"], "sig-bridge")

    def test_ineligible_signal_can_remain_unsent_for_later_packet_eligibility(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_signal_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
                RT_ALERT_MARK_INELIGIBLE_SENT="0",
            )

            with patch("builtins.print") as printed, patch.object(bridge, "run_order_intake") as intake:
                code = bridge.main()

            self.assertEqual(code, 0)
            printed.assert_not_called()
            intake.assert_not_called()
            self.assertFalse(sent.exists())

    def test_ineligible_signal_diagnostic_opt_in_includes_compact_hermes_context_without_intake(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_signal_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="alert-sim",
                RT_ALERT_NOTIFY_INELIGIBLE_SIGNALS="1",
            )

            with patch("builtins.print") as printed, patch.object(bridge, "run_order_intake") as intake:
                code = bridge.main()

            self.assertEqual(code, 0)
            intake.assert_not_called()
            text = printed.call_args.args[0]
            self.assertIn("候選信號（安全門未放行）", text)
            self.assertNotIn("實時操作信號", text)
            self.assertIn("Hermes審核：eligible=False judgment=reject_or_hold", text)
            self.assertIn("執行準備：WARN ready=False", text)
            self.assertIn("模擬表現：FAIL reasons=recent_closed_trades_negative", text)
            self.assertIn("Hermes Alpha：INSUFFICIENT", text)
            self.assertIn("市場：risk_off/high", text)
            self.assertIn("分鐘證據：challenges codes=intraday_down_momentum ack=required", text)
            self.assertIn("事件審核：challenge=1 support=0", text)
            self.assertIn("來源可靠性：DEGRADED fundamentals_context:partial_metric_coverage", text)

    def test_eligible_signal_notification_uses_review_candidate_title(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_eligible_signal_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
            )

            with patch("builtins.print") as printed:
                code = bridge.main()

            self.assertEqual(code, 0)
            text = printed.call_args.args[0]
            self.assertIn("Hermes可審操作候選", text)
            self.assertNotIn("實時操作信號", text)
            self.assertIn("Hermes審核：eligible=True", text)
            self.assertIn("證據來源：completed_daily_ohlcv=2 current_session_quote=1 quote_in_score=true", text)

    def test_signal_notification_marks_missing_packet_match(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            packet = self.packet_with_signal_review()
            packet["review_items"][0]["signal_id"] = "other-signal"
            packet_file.write_text(json.dumps(packet), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
                RT_ALERT_NOTIFY_INELIGIBLE_SIGNALS="1",
            )

            with patch("builtins.print") as printed:
                code = bridge.main()

            self.assertEqual(code, 0)
            self.assertIn("Hermes審核：NO_MATCH", printed.call_args.args[0])

    def test_feishu_success_updates_sent_state(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_eligible_signal_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
                RT_ALERT_SEND_FEISHU="1",
            )

            with patch.object(bridge, "send_feishu_text", return_value=True) as send, patch("builtins.print"):
                code = bridge.main()

            self.assertEqual(code, 0)
            send.assert_called_once()
            self.assertTrue(sent.exists())
            self.assertEqual(json.loads(sent.read_text(encoding="utf-8"))[0]["signal_id"], "sig-bridge")

    def test_feishu_failure_leaves_alert_unsent_for_retry(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_eligible_signal_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
                RT_ALERT_SEND_FEISHU="1",
            )

            with patch.object(bridge, "send_feishu_text", return_value=False), patch("builtins.print"):
                code = bridge.main()

            self.assertEqual(code, 2)
            self.assertFalse(sent.exists())

    def test_position_review_notifies_without_new_alerts_and_uses_separate_sent_state(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            alert_sent = Path(td) / "alert_sent.json"
            review_sent = Path(td) / "position_sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text("", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_position_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(alert_sent),
                RT_POSITION_REVIEW_SENT_FILE=str(review_sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
            )

            with patch("builtins.print") as printed:
                code = bridge.main()

            self.assertEqual(code, 0)
            self.assertFalse(alert_sent.exists())
            self.assertTrue(review_sent.exists())
            self.assertEqual(
                json.loads(review_sent.read_text(encoding="utf-8"))[0]["review_id"],
                "user:3:AAPL:2026-06-12:risk_review",
            )
            text = printed.call_args.args[0]
            self.assertIn("Hermes持倉審核待辦（不下單）", text)
            self.assertIn("不代表已通過 Hermes 交易審批", text)
            self.assertIn("候選動作：reduce qty=5 ref=180; exit qty=10 ref=178", text)
            self.assertIn("盤中審核：tf=session,5m,15m,60m", text)
            self.assertIn("use=can_support_reduce_exit_only_after_fresh_intraday_or_market_confirmation", text)
            self.assertIn("Hermes優先入口：position_judgment_worklist.items[]", text)
            self.assertIn("Worklist輸出：schema=hermes_position_judgment_v1 advisory_only=True submits_orders=False", text)
            self.assertIn("Worklist盤中證據：align=supports_recommended_action", text)
            self.assertIn("intent=risk_reduction", text)
            self.assertIn("support=session_down_supports_reduce_exit", text)
            self.assertIn("Worklist必回應：high_urgency_position_requires_contextual_rationale", text)
            self.assertIn("position_intraday_evidence_requires_discussion", text)
            self.assertIn("order_submission=false", text)

    def test_operator_action_queue_notifies_without_alerts_or_position_reviews(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            alert_sent = Path(td) / "alert_sent.json"
            review_sent = Path(td) / "position_sent.json"
            operator_sent = Path(td) / "operator_sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text("", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_operator_actions()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(alert_sent),
                RT_POSITION_REVIEW_SENT_FILE=str(review_sent),
                RT_OPERATOR_ACTION_SENT_FILE=str(operator_sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
            )

            with patch("builtins.print") as printed:
                code = bridge.main()

            self.assertEqual(code, 0)
            self.assertFalse(alert_sent.exists())
            self.assertFalse(review_sent.exists())
            self.assertTrue(operator_sent.exists())
            sent_rows = json.loads(operator_sent.read_text(encoding="utf-8"))
            self.assertEqual(sent_rows[0]["action_id"], "review_simulation_performance_failure")
            text = printed.call_args.args[0]
            self.assertIn("Hermes系統待辦（不下單）", text)
            self.assertIn("review_simulation_performance_failure", text)
            self.assertIn("Simulation performance attribution is FAIL", text)
            self.assertIn("postmortem_targets=closed_trade:LI,closed_trade:TSLA", text)
            self.assertNotIn("write_high_urgency_position_judgments", text)
            self.assertIn("order_submission=false", text)

    def test_position_review_uses_stable_thread_key_for_action_churn(self):
        bridge = self.load_bridge(
            RT_ALERT_REMOTE="local",
            RT_ALERT_EXECUTION_MODE="notify",
            RT_POSITION_REVIEW_REMINDER_HOURS="6",
        )
        packet = self.packet_with_position_review()
        item = packet["position_review"]["items"][0]
        item["review_thread_key"] = "user:3:AAPL"
        item["review_id"] = "user:3:AAPL:2026-06-13:reduce_or_exit_review"
        item["recommended_action"] = "reduce_or_exit_review"
        item["urgency"] = "high"
        current = bridge.position_review_items(packet)[0]
        notice_fingerprint = bridge.position_review_notice_fingerprint(current)
        sent_rows = [
            {
                "review_id": "user:3:AAPL:2026-06-12:exit_review",
                "symbol": "AAPL",
                "urgency": "high",
                "recommended_action": "exit_review",
                "sent_at_epoch": 1000,
                "notice_fingerprint": notice_fingerprint,
            }
        ]

        pending = bridge.pending_position_reviews(packet, sent_rows, now_epoch=1100)

        self.assertEqual(pending, [])

    def test_position_review_default_reminder_is_daily(self):
        bridge = self.load_bridge(
            RT_ALERT_REMOTE="local",
            RT_ALERT_EXECUTION_MODE="notify",
        )
        packet = self.packet_with_position_review()
        item = packet["position_review"]["items"][0]
        item["review_thread_key"] = "user:3:AAPL"
        current = bridge.position_review_items(packet)[0]
        notice_fingerprint = bridge.position_review_notice_fingerprint(current)
        sent_rows = [
            {
                "review_thread_key": "user:3:AAPL",
                "review_id": "user:3:AAPL:2026-06-12:risk_review",
                "symbol": "AAPL",
                "urgency": "high",
                "recommended_action": "risk_review",
                "sent_at_epoch": 1000,
                "notice_fingerprint": notice_fingerprint,
            }
        ]

        pending = bridge.pending_position_reviews(packet, sent_rows, now_epoch=1000 + 7 * 3600)

        self.assertEqual(pending, [])

    def test_position_review_worklist_notice_change_realerts_before_reminder_window(self):
        bridge = self.load_bridge(
            RT_ALERT_REMOTE="local",
            RT_ALERT_EXECUTION_MODE="notify",
            RT_POSITION_REVIEW_REMINDER_HOURS="24",
        )
        packet = self.packet_with_position_review()
        item = packet["position_review"]["items"][0]
        item["review_thread_key"] = "user:3:AAPL"
        sent_rows = [
            {
                "review_thread_key": "user:3:AAPL",
                "review_id": "user:3:AAPL:2026-06-12:risk_review",
                "symbol": "AAPL",
                "urgency": "high",
                "recommended_action": "risk_review",
                "sent_at_epoch": 1000,
            }
        ]

        pending = bridge.pending_position_reviews(packet, sent_rows, now_epoch=1000 + 7 * 3600)

        self.assertEqual([row["review_id"] for row in pending], ["user:3:AAPL:2026-06-12:risk_review"])

    def test_position_review_intraday_evidence_change_realerts_before_reminder_window(self):
        bridge = self.load_bridge(
            RT_ALERT_REMOTE="local",
            RT_ALERT_EXECUTION_MODE="notify",
            RT_POSITION_REVIEW_REMINDER_HOURS="24",
        )
        packet = self.packet_with_position_review()
        item = packet["position_review"]["items"][0]
        item["review_thread_key"] = "user:3:AAPL"
        current = bridge.position_review_items(packet)[0]
        notice_fingerprint = bridge.position_review_notice_fingerprint(current)

        evidence = packet["position_judgment_worklist"]["items"][0]["context_summary"]["intraday_position_evidence"]
        evidence["session_change_pct"] = -2.8
        self.assertEqual(bridge.position_review_notice_fingerprint(current), notice_fingerprint)
        evidence["alignment"] = "challenges_recommended_action"
        evidence["support_codes"] = []
        evidence["challenge_codes"] = ["session_up_challenges_immediate_reduce_exit"]
        evidence["timeframe_alignment"] = "bullish_aligned"
        self.assertNotEqual(bridge.position_review_notice_fingerprint(current), notice_fingerprint)
        sent_rows = [
            {
                "review_thread_key": "user:3:AAPL",
                "review_id": "user:3:AAPL:2026-06-12:risk_review",
                "symbol": "AAPL",
                "urgency": "high",
                "recommended_action": "risk_review",
                "sent_at_epoch": 1000,
                "notice_fingerprint": notice_fingerprint,
            }
        ]

        pending = bridge.pending_position_reviews(packet, sent_rows, now_epoch=1000 + 7 * 3600)

        self.assertEqual([row["review_id"] for row in pending], ["user:3:AAPL:2026-06-12:risk_review"])

    def test_compact_position_review_sent_keeps_latest_per_thread(self):
        bridge = self.load_bridge(
            RT_ALERT_REMOTE="local",
            RT_ALERT_EXECUTION_MODE="notify",
        )
        sent_rows = [
            {
                "review_id": "user:3:AAPL:2026-06-12:risk_review",
                "symbol": "AAPL",
                "urgency": "medium",
                "recommended_action": "risk_review",
                "sent_at_epoch": 1000,
            },
            {
                "review_thread_key": "user:3:AAPL",
                "review_id": "user:3:AAPL:2026-06-13:exit_review",
                "symbol": "AAPL",
                "urgency": "high",
                "recommended_action": "exit_review",
                "sent_at_epoch": 1100,
            },
            {
                "review_thread_key": "user:3:MSFT",
                "review_id": "user:3:MSFT:2026-06-13:risk_review",
                "symbol": "MSFT",
                "urgency": "medium",
                "recommended_action": "risk_review",
                "sent_at_epoch": 1050,
            },
        ]

        compacted = bridge.compact_position_review_sent(sent_rows)

        self.assertEqual([row["review_id"] for row in compacted], [
            "user:3:MSFT:2026-06-13:risk_review",
            "user:3:AAPL:2026-06-13:exit_review",
        ])

    def test_main_compacts_position_sent_even_when_alerts_are_present(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            alert_sent = Path(td) / "alert_sent.json"
            review_sent = Path(td) / "position_sent.json"
            packet_file = Path(td) / "packet.json"
            packet = self.packet_with_position_review()
            bridge_for_fingerprint = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_EXECUTION_MODE="notify",
            )
            current_item = bridge_for_fingerprint.position_review_items(packet)[0]
            notice_fingerprint = bridge_for_fingerprint.position_review_notice_fingerprint(current_item)
            packet["execution_readiness"] = {
                "schema": "execution_readiness_report_v1",
                "status": "BLOCKED",
                "ready_for_execute": False,
            }
            packet_file.write_text(json.dumps(packet), encoding="utf-8")
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            review_sent.write_text(
                json.dumps(
                    [
                        {
                            "review_id": "user:3:AAPL:2026-06-12:risk_review",
                            "symbol": "AAPL",
                            "urgency": "medium",
                            "recommended_action": "risk_review",
                            "sent_at_epoch": 1000,
                        },
                        {
                            "review_thread_key": "user:3:AAPL",
                            "review_id": "user:3:AAPL:2026-06-13:exit_review",
                            "symbol": "AAPL",
                            "urgency": "high",
                            "recommended_action": "exit_review",
                            "sent_at_epoch": 1100,
                            "notice_fingerprint": notice_fingerprint,
                        },
                    ]
                ),
                encoding="utf-8",
            )
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(alert_sent),
                RT_POSITION_REVIEW_SENT_FILE=str(review_sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
                RT_ALERT_NOTIFY_INELIGIBLE_SIGNALS="0",
                RT_ALERT_MARK_INELIGIBLE_SENT="0",
                RT_POSITION_REVIEW_REMINDER_HOURS="0",
            )

            with patch("builtins.print") as printed:
                code = bridge.main()

            self.assertEqual(code, 0)
            printed.assert_not_called()
            compacted = json.loads(review_sent.read_text(encoding="utf-8"))
            self.assertEqual(len(compacted), 1)
            self.assertEqual(compacted[0]["review_id"], "user:3:AAPL:2026-06-13:exit_review")

    def test_position_review_thread_allows_immediate_risk_escalation(self):
        bridge = self.load_bridge(
            RT_ALERT_REMOTE="local",
            RT_ALERT_EXECUTION_MODE="notify",
            RT_POSITION_REVIEW_REMINDER_HOURS="6",
        )
        packet = self.packet_with_position_review()
        item = packet["position_review"]["items"][0]
        item["review_thread_key"] = "user:3:AAPL"
        item["review_id"] = "user:3:AAPL:2026-06-13:exit_review"
        item["recommended_action"] = "exit_review"
        item["urgency"] = "high"
        sent_rows = [
            {
                "review_thread_key": "user:3:AAPL",
                "review_id": "user:3:AAPL:2026-06-12:reduce_or_exit_review",
                "symbol": "AAPL",
                "urgency": "medium",
                "recommended_action": "reduce_or_exit_review",
                "sent_at_epoch": 1000,
            }
        ]

        pending = bridge.pending_position_reviews(packet, sent_rows, now_epoch=1100)

        self.assertEqual([row["review_id"] for row in pending], ["user:3:AAPL:2026-06-13:exit_review"])

    def test_position_review_summary_counts_filtered_items_not_global_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            alert_sent = Path(td) / "alert_sent.json"
            review_sent = Path(td) / "position_sent.json"
            packet_file = Path(td) / "packet.json"
            packet = self.packet_with_position_review()
            packet["position_judgment_audit"]["coverage"]["unjudged_high_urgency_review_count"] = 7
            simulation_item = json.loads(json.dumps(packet["position_review"]["items"][0]))
            simulation_item.update(
                {
                    "review_id": "simulation:8:SIM:2026-06-12:risk_review",
                    "portfolio_id": 8,
                    "role": "simulation",
                    "symbol": "SIM",
                }
            )
            packet["position_review"]["items"].append(simulation_item)
            queue.write_text("", encoding="utf-8")
            packet_file.write_text(json.dumps(packet), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(alert_sent),
                RT_POSITION_REVIEW_SENT_FILE=str(review_sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
            )

            with patch("builtins.print") as printed:
                code = bridge.main()

            self.assertEqual(code, 0)
            text = printed.call_args.args[0]
            self.assertIn("本次提醒持倉：1（high=1, medium=0, roles=user）", text)
            self.assertIn("packet全局未審核高優先級：7", text)
            self.assertIn("AAPL", text)
            self.assertNotIn("SIM", text)

    def test_position_review_suppresses_simulation_role_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            alert_sent = Path(td) / "alert_sent.json"
            review_sent = Path(td) / "position_sent.json"
            packet_file = Path(td) / "packet.json"
            packet = self.packet_with_position_review()
            packet["position_judgment_audit"]["coverage"]["unjudged_high_urgency_examples"] = [
                {"review_id": "simulation:8:AAPL:2026-06-12:risk_review"}
            ]
            packet["position_review"]["items"][0].update(
                {
                    "review_id": "simulation:8:AAPL:2026-06-12:risk_review",
                    "portfolio_id": 8,
                    "role": "simulation",
                }
            )
            queue.write_text("", encoding="utf-8")
            packet_file.write_text(json.dumps(packet), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(alert_sent),
                RT_POSITION_REVIEW_SENT_FILE=str(review_sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
            )

            with patch("builtins.print") as printed:
                code = bridge.main()

            self.assertEqual(code, 0)
            printed.assert_not_called()
            self.assertFalse(review_sent.exists())

    def test_position_review_includes_medium_urgency_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            alert_sent = Path(td) / "alert_sent.json"
            review_sent = Path(td) / "position_sent.json"
            packet_file = Path(td) / "packet.json"
            packet = self.packet_with_position_review()
            packet["position_judgment_audit"]["coverage"]["unjudged_high_urgency_review_count"] = 0
            packet["position_review"]["items"][0].update(
                {
                    "review_id": "user:3:SPCX:no_signal_date:take_profit_or_trailing_stop_review",
                    "portfolio_id": 3,
                    "role": "user",
                    "symbol": "SPCX",
                    "urgency": "medium",
                    "recommended_action": "take_profit_or_trailing_stop_review",
                }
            )
            packet["position_review"]["items"][0]["position"]["unrealized_pnl_pct"] = 1.19
            packet["position_review"]["items"][0]["position"]["latest_daily_change_pct"] = 3.5
            queue.write_text("", encoding="utf-8")
            packet_file.write_text(json.dumps(packet), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(alert_sent),
                RT_POSITION_REVIEW_SENT_FILE=str(review_sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
            )

            with patch("builtins.print") as printed:
                code = bridge.main()

            self.assertEqual(code, 0)
            text = printed.call_args.args[0]
            self.assertIn("SPCX", text)
            self.assertIn("urgency=medium", text)
            self.assertIn("review_action=take_profit_or_trailing_stop_review", text)
            self.assertIn("審核草案：review_reduce_half_or_exit_if_context_worsens", text)
            self.assertIn("add_allowed=False", text)
            self.assertIn("qty_hint=5", text)
            self.assertIn("sig_stop_ref=180", text)
            self.assertIn("sig_target_ref=220", text)
            self.assertIn("動態管理：status=below_signal_stop", text)
            self.assertIn("dist_stop=-1.2%", text)
            self.assertIn("px_age=36.0h", text)
            self.assertNotIn(" stop=180", text)
            self.assertNotIn(" action=take_profit_or_trailing_stop_review", text)

    def test_feishu_failure_with_position_review_does_not_mark_watch_alert_sent(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            alert_sent = Path(td) / "alert_sent.json"
            review_sent = Path(td) / "position_sent.json"
            packet_file = Path(td) / "packet.json"
            queue.write_text(json.dumps(self.watch_alert()) + "\n", encoding="utf-8")
            packet_file.write_text(json.dumps(self.packet_with_position_review()), encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(alert_sent),
                RT_POSITION_REVIEW_SENT_FILE=str(review_sent),
                HERMES_REVIEW_PACKET_FILE=str(packet_file),
                RT_ALERT_EXECUTION_MODE="notify",
                RT_ALERT_SEND_FEISHU="1",
            )

            with patch.object(bridge, "send_feishu_text", return_value=False), patch("builtins.print"):
                code = bridge.main()

            self.assertEqual(code, 2)
            self.assertFalse(alert_sent.exists())
            self.assertFalse(review_sent.exists())


if __name__ == "__main__":
    unittest.main()
