import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import system_health_check as health


class SystemHealthAlertContractTests(unittest.TestCase):
    def test_save_json_atomic_writes_payload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "health.json"
            health.save_json_atomic(str(path), {"status": "OK", "checked_at": "2026-06-12T10:00:00"})

            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded["status"], "OK")

    def test_old_directional_alert_missing_v5_fields_fails_contract(self):
        checks = []
        alert = {
            "symbol": "AAPL",
            "signal_type": "BUY",
            "trigger": "站上MA5",
            "entry_price": 100,
            "stop_loss": 95,
            "take_profit": 110,
            "rr_ratio": 2,
        }

        health.alert_contract_check(checks, [alert])

        self.assertEqual(checks[0]["name"], "alert_contract")
        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertIn("missing_signal_id", checks[0]["data"]["bad"][0]["errors"])
        self.assertIn("missing_full_score", checks[0]["data"]["bad"][0]["errors"])

    def test_complete_directional_alert_passes_contract(self):
        checks = []
        alert = {
            "signal_id": "sig-ok",
            "symbol": "00700",
            "signal_type": "BUY",
            "trigger": "unit",
            "confirmed": True,
            "full_score": 0.7,
            "entry_price": 300,
            "stop_loss": 290,
            "take_profit": 330,
            "rr_ratio": 3,
            "generated_at": "2026-06-12T10:00:00",
        }

        health.alert_contract_check(checks, [alert])

        self.assertEqual(checks[0]["status"], "OK")

    def test_data_health_fail_is_exposed_as_health_failure(self):
        payload = {
            "schema": "data_health_report_v1",
            "generated_at": "2026-06-12T10:00:00",
            "status": "FAIL",
            "markets": {
                "HK": {
                    "status": "FAIL",
                    "latest_date": "2026-06-12",
                    "coverage": {"latest_date_coverage_pct": 100.0},
                    "integrity": {"invalid_latest_ohlc_count": 1},
                }
            },
            "feature_run": {"status": "OK"},
            "recommendations": ["HK:block_execution_until_data_failure_fixed:invalid_latest_ohlc"],
        }

        checks = []
        with patch.object(health, "data_health_payload", return_value=payload):
            health.data_health_check(checks)

        self.assertEqual(checks[0]["name"], "data_health")
        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertIn("invalid_ohlc=1", checks[0]["detail"])
        self.assertIn("HK:block_execution_until_data_failure_fixed:invalid_latest_ohlc", checks[0]["data"]["recommendations"])

    def test_data_health_detail_includes_feature_run_notes(self):
        payload = {
            "schema": "data_health_report_v1",
            "generated_at": "2026-06-12T11:30:00",
            "status": "FAIL",
            "markets": {},
            "feature_run": {
                "status": "FAIL",
                "latest": {"run_id": "signal_v4_20260612", "trade_date": "2026-06-12"},
                "notes": [
                    "current_session_before_daily_signal_ready_time",
                    "latest_daily_signal_run_generated_before_full_day_cutoff",
                ],
            },
            "recommendations": ["block_execution_until_signal_v4_full_day_run_ready"],
        }

        checks = []
        with patch.object(health, "data_health_payload", return_value=payload):
            health.data_health_check(checks)

        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertIn("feature_run=FAIL", checks[0]["detail"])
        self.assertIn("current_session_before_daily_signal_ready_time", checks[0]["detail"])

    def test_data_health_unavailable_warns(self):
        checks = []
        with patch.object(health, "data_health_payload", side_effect=RuntimeError("db down")):
            health.data_health_check(checks)

        self.assertEqual(checks[0]["name"], "data_health")
        self.assertEqual(checks[0]["status"], "WARN")
        self.assertIn("db down", checks[0]["detail"])

    def test_no_directional_alerts_warns_not_fails(self):
        checks = []

        health.alert_contract_check(checks, [{"signal_type": "WATCH"}])

        self.assertEqual(checks[0]["status"], "WARN")

    def test_process_check_prefers_systemd_main_pid(self):
        calls = []

        def fake_run_cmd(args, timeout=10):
            calls.append(args)
            if args[:2] == ["systemctl", "is-active"]:
                return type("Result", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})()
            if args[:2] == ["systemctl", "show"]:
                return type("Result", (), {"returncode": 0, "stdout": "12345\n", "stderr": ""})()
            raise AssertionError(f"unexpected command: {args}")

        checks = []
        with patch.object(health, "run_cmd", side_effect=fake_run_cmd):
            health.process_check(checks)

        self.assertEqual(checks[0]["status"], "OK")
        self.assertIn("MainPID=12345", checks[0]["detail"])
        self.assertNotIn(["pgrep", "-af", "/root/rt_signal_engine_v5.py"], calls)

    def test_simulation_ledger_drift_fails_health(self):
        payload = {
            "plan_hash": "abc",
            "summary": {"action_count": 2, "by_action": {"close_stale_position": 1}},
            "actions": [{"action": "close_stale_position", "symbol": "00017"}],
            "warnings": [],
        }

        checks = []
        with patch.object(health, "ledger_reconcile_payload", return_value=payload):
            health.simulation_ledger_check(checks)

        self.assertEqual(checks[0]["name"], "simulation_ledger")
        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertEqual(checks[0]["data"]["plan_hash"], "abc")

    def test_simulation_ledger_valuation_only_drift_without_fresh_snapshot_warns(self):
        payload = {
            "plan_hash": "valuation",
            "summary": {"action_count": 2, "by_action": {"update_open_position": 1, "update_portfolio_totals": 1}},
            "actions": [
                {
                    "action": "update_open_position",
                    "symbol": "00700",
                    "diff_fields": ["current_price", "market_value", "unrealized_pnl", "unrealized_pnl_rate"],
                },
                {"action": "update_portfolio_totals", "portfolio_id": 8},
            ],
            "warnings": [],
        }

        checks = []
        with patch.object(health, "ledger_reconcile_payload", return_value=payload):
            health.simulation_ledger_check(checks)

        self.assertEqual(checks[0]["name"], "simulation_ledger")
        self.assertEqual(checks[0]["status"], "WARN")
        self.assertIn("valuation fields drift", checks[0]["detail"])

    def test_simulation_ledger_fresh_live_valuation_drift_passes(self):
        payload = {
            "plan_hash": "valuation",
            "summary": {"action_count": 2, "by_action": {"update_open_position": 1, "update_portfolio_totals": 1}},
            "actions": [
                {
                    "action": "update_open_position",
                    "symbol": "00700",
                    "diff_fields": ["current_price", "market_value", "unrealized_pnl", "unrealized_pnl_rate"],
                    "current": {
                        "current_price": 110,
                        "market_value": 11000,
                        "updated_at": "2026-06-12 11:15:00",
                    },
                    "expected": {"price_date": "2026-06-12"},
                },
                {"action": "update_portfolio_totals", "portfolio_id": 8},
            ],
            "warnings": [],
        }

        checks = []
        with patch.object(health, "ledger_reconcile_payload", return_value=payload):
            health.simulation_ledger_check(checks)

        self.assertEqual(checks[0]["name"], "simulation_ledger")
        self.assertEqual(checks[0]["status"], "OK")
        self.assertIn("fresh live valuation differs", checks[0]["detail"])
        self.assertEqual(checks[0]["data"]["valuation_status"], "fresh_live_snapshot")

    def test_simulation_ledger_match_passes_health(self):
        payload = {
            "plan_hash": "clean",
            "summary": {"action_count": 0, "by_action": {}},
            "actions": [],
            "warnings": [],
        }

        checks = []
        with patch.object(health, "ledger_reconcile_payload", return_value=payload):
            health.simulation_ledger_check(checks)

        self.assertEqual(checks[0]["status"], "OK")
        self.assertIn("positions match sim_trades", checks[0]["detail"])


if __name__ == "__main__":
    unittest.main()
