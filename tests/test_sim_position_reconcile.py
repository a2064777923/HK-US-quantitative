import unittest
from unittest.mock import patch

from scripts import sim_position_reconcile as reconcile


class SimPositionReconcileTests(unittest.TestCase):
    def test_fetch_latest_prices_reads_canonical_daily_bars(self):
        captured = {}

        def fake_psql(sql, timeout=60):
            captured["sql"] = sql
            return type("Result", (), {"returncode": 0, "stdout": "AAPL\t100\t2026-06-12\n", "stderr": ""})()

        with patch.object(reconcile, "psql", side_effect=fake_psql):
            prices = reconcile.fetch_latest_prices(["AAPL"])

        sql = captured["sql"]
        normalized = " ".join(sql.split())
        self.assertEqual(prices["AAPL"], {"price": 100.0, "date": "2026-06-12"})
        self.assertIn("WITH daily_bar AS", sql)
        self.assertIn("SELECT DISTINCT ON (symbol, timestamp::date)", sql)
        self.assertIn("ORDER BY symbol, timestamp::date, timestamp DESC", normalized)

    def test_derive_expected_positions_from_buy_sell_ledger(self):
        trades = [
            {"symbol": "00017", "side": "buy", "price": 10, "quantity": 1000, "created_at": "t1"},
            {"symbol": "00017", "side": "sell", "price": 11, "quantity": 400, "created_at": "t2"},
            {"symbol": "00929", "side": "buy", "price": 1.2, "quantity": 10000, "created_at": "t3"},
        ]

        expected, warnings = reconcile.derive_expected_positions(trades)

        self.assertEqual(warnings, [])
        self.assertEqual(expected["00017"]["quantity"], 600)
        self.assertEqual(expected["00017"]["avg_cost"], 10)
        self.assertEqual(expected["00017"]["realized_pnl_quote"], 400)
        self.assertEqual(expected["00929"]["quantity"], 10000)

    def test_build_plan_closes_stale_and_inserts_missing_positions(self):
        portfolio = {"id": 8, "initial_capital": 100000, "available_cash": 15000, "total_value": 15000}
        current = [
            {
                "id": 1,
                "symbol": "00017",
                "quantity": 1000,
                "avg_cost": 7.4,
                "current_price": 7.3,
                "market_value": 7300,
                "unrealized_pnl": -100,
                "unrealized_pnl_rate": -0.01,
                "exchange": "HKEX",
                "status": "holding",
            }
        ]
        expected = {
            "00929": {
                "symbol": "00929",
                "symbol_name": "Test",
                "exchange": "HKEX",
                "currency": "HKD",
                "quantity": 10000,
                "avg_cost": 1.2,
                "current_price": 1.21,
                "total_cost_hkd": 12000,
                "market_value_hkd": 12100,
                "unrealized_pnl_hkd": 100,
                "unrealized_pnl_rate": 0.00833333,
                "realized_pnl_hkd": 0,
                "weight": 1.0,
            }
        }

        actions = reconcile.build_plan(portfolio, current, expected)

        self.assertEqual([a["action"] for a in actions], ["insert_open_position", "close_stale_position", "update_portfolio_totals"])
        self.assertEqual(actions[0]["symbol"], "00929")
        self.assertEqual(actions[1]["symbol"], "00017")
        self.assertEqual(actions[2]["computed_total_value_hkd"], 27100)

    def test_build_sql_script_contains_transaction_and_expected_updates(self):
        actions = [
            {
                "action": "close_stale_position",
                "symbol": "00017",
                "position_id": 1,
                "current": {},
                "expected": None,
            },
            {
                "action": "update_portfolio_totals",
                "portfolio_id": 8,
                "computed_total_value_hkd": 27100,
                "initial_capital_hkd": 100000,
            },
        ]

        sql = reconcile.build_sql_script(actions, 8)

        self.assertTrue(sql.startswith("BEGIN;"))
        self.assertIn("UPDATE positions SET quantity = 0", sql)
        self.assertIn("UPDATE portfolios SET", sql)
        self.assertTrue(sql.endswith("COMMIT;\n"))

    def test_insert_sql_sets_not_null_realized_pnl(self):
        action = {
            "action": "insert_open_position",
            "symbol": "00929",
            "expected": {
                "symbol": "00929",
                "symbol_name": "Test",
                "exchange": "HKEX",
                "currency": "HKD",
                "quantity": 10000,
                "avg_cost": 1.2,
                "current_price": 1.21,
                "total_cost_hkd": 12000,
                "market_value_hkd": 12100,
                "unrealized_pnl_hkd": 100,
                "unrealized_pnl_rate": 0.00833333,
                "realized_pnl_hkd": 0,
                "weight": 1.0,
            },
        }

        sql = reconcile.sql_for_action(action, 8)

        self.assertIn("realized_pnl", sql)
        self.assertNotIn("NULL", sql.split("realized_pnl", 1)[1].split(")", 1)[0])


if __name__ == "__main__":
    unittest.main()
