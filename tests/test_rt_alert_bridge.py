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


if __name__ == "__main__":
    unittest.main()
