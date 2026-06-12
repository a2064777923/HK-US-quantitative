import unittest
from unittest.mock import patch

from scripts import update_portfolio_prices as updater


class UpdatePortfolioPricesTests(unittest.TestCase):
    def test_update_position_snapshot_writes_pnl_rate(self):
        calls = []

        def fake_db(sql):
            calls.append(sql)
            return ""

        with patch.object(
            updater,
            "table_columns",
            return_value={"current_price", "market_value", "unrealized_pnl", "unrealized_pnl_rate", "updated_at"},
        ), patch.object(updater, "db", side_effect=fake_db):
            updater.update_position_snapshot("00700", 110, {"qty": 100, "cost": 100})

        sql = calls[-1]
        self.assertIn("unrealized_pnl_rate = 0.1", sql)
        self.assertIn("market_value = 11000.0", sql)
        self.assertIn("unrealized_pnl = 1000.0", sql)

    def test_update_portfolio_totals_updates_current_capital_and_total_value(self):
        calls = []

        def fake_db(sql):
            calls.append(sql)
            if "SELECT COALESCE(p.available_cash" in sql:
                return "1000|2500"
            return ""

        with patch.object(
            updater,
            "table_columns",
            return_value={"current_capital", "total_value", "updated_at"},
        ), patch.object(updater, "db", side_effect=fake_db):
            updater.update_portfolio_totals()

        update_sql = calls[-1]
        self.assertIn("current_capital = 3500.0", update_sql)
        self.assertIn("total_value = 3500.0", update_sql)


if __name__ == "__main__":
    unittest.main()
