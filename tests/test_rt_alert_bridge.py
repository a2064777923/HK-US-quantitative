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
            "RT_POSITION_REVIEW_SENT_FILE",
            "HERMES_REVIEW_PACKET_FILE",
            "RT_ORDER_EXECUTE_PILOT_ENABLED",
            "RT_ORDER_US_BROKER",
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
            "entry_price": 100,
            "stop_loss": 95,
            "take_profit": 112,
            "rr_ratio": 2.4,
            "full_score": 0.7,
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
                    "unjudged_high_urgency_examples": [{"review_id": "simulation:8:AAPL:2026-06-12:risk_review"}],
                }
            },
            "position_review": {
                "schema": "portfolio_position_review_v1",
                "items": [
                    {
                        "review_id": "simulation:8:AAPL:2026-06-12:risk_review",
                        "portfolio_id": 8,
                        "role": "simulation",
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
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
                RT_ALERT_EXECUTION_MODE="alert-sim",
                RT_ORDER_EXECUTE_PILOT_ENABLED="1",
                RT_ORDER_US_BROKER="alpaca-paper",
            )

            with patch.dict(
                os.environ,
                {"RT_ORDER_EXECUTE_PILOT_ENABLED": "1", "RT_ORDER_US_BROKER": "alpaca-paper"},
                clear=False,
            ), patch.object(bridge, "run_cmd", return_value='{"results":[{"status":"submitted"}]}') as run_cmd, patch(
                "builtins.print"
            ):
                bridge.main()

            command = run_cmd.call_args.args[0]
            self.assertIn("RT_ORDER_EXECUTE_PILOT_ENABLED=1", command)
            self.assertIn("RT_ORDER_US_BROKER=alpaca-paper", command)
            self.assertIn("RT_ORDER_EXECUTION_MODE=execute", command)

    def test_feishu_success_updates_sent_state(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            sent = Path(td) / "sent.json"
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
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
            queue.write_text(json.dumps(self.fresh_alert()) + "\n", encoding="utf-8")
            bridge = self.load_bridge(
                RT_ALERT_REMOTE="local",
                RT_ALERT_QUEUE_FILE=str(queue),
                RT_ALERT_SENT_FILE=str(sent),
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
                "simulation:8:AAPL:2026-06-12:risk_review",
            )
            self.assertIn("Hermes持倉風險審核", printed.call_args.args[0])

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
