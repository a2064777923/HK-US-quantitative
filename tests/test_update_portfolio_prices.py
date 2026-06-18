import unittest
import os
from unittest.mock import patch

from scripts import update_portfolio_prices as updater


class UpdatePortfolioPricesTests(unittest.TestCase):
    def test_portfolio_id_can_be_configured_by_environment(self):
        self.assertEqual(
            updater.PORTFOLIO_ID,
            int(os.environ.get("QM_PRICE_UPDATE_PORTFOLIO_ID", os.environ.get("QM_PORTFOLIO_ID", "8"))),
        )

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

    def test_portfolio_three_price_update_is_holding_only(self):
        calls = []

        def fake_db(sql):
            calls.append(sql)
            return ""

        with (
            patch.object(updater, "PORTFOLIO_ID", 3),
            patch.object(
                updater,
                "table_columns",
                return_value={"current_price", "market_value", "unrealized_pnl", "unrealized_pnl_rate", "updated_at"},
            ),
            patch.object(updater, "db", side_effect=fake_db),
        ):
            updater.update_position_snapshot("PDD", 80, {"qty": 10, "cost": 82})

        sql = calls[-1]
        self.assertIn("portfolio_id = 3", sql)
        self.assertIn("status = 'holding'", sql)
        self.assertNotIn("status IN ('active','holding')", sql)

    def test_configured_user_portfolio_price_update_is_holding_only(self):
        with patch.object(updater, "PORTFOLIO_ID", 7), patch.dict(
            "os.environ",
            {"QM_USER_PORTFOLIO_IDS": "7,9"},
            clear=False,
        ):
            self.assertEqual(updater.position_status_sql(), "status = 'holding'")
            self.assertEqual(updater.position_status_sql(alias="pos"), "pos.status = 'holding'")

    def test_simulation_price_update_keeps_active_compatibility(self):
        with patch.object(updater, "PORTFOLIO_ID", 8), patch.dict("os.environ", {"QM_USER_PORTFOLIO_IDS": "3"}, clear=False):
            self.assertEqual(updater.position_status_sql(), "status IN ('active','holding')")

    def test_user_price_update_does_not_rebuild_from_sim_trades_when_positions_empty(self):
        calls = []

        def fake_db(sql):
            calls.append(sql)
            if "SELECT symbol, quantity, avg_cost, exchange" in sql:
                return ""
            if "SELECT COALESCE(p.available_cash" in sql:
                return "1000|0"
            return ""

        with (
            patch.object(updater, "PORTFOLIO_ID", 3),
            patch.object(updater, "table_columns", return_value={"current_capital", "total_value", "updated_at"}),
            patch.object(updater, "db", side_effect=fake_db),
        ):
            updater.update_redis_prices()

        self.assertFalse(any("FROM sim_trades" in sql for sql in calls))

    def test_simulation_price_update_keeps_trade_rebuild_compatibility_when_positions_empty(self):
        calls = []

        def fake_db(sql):
            calls.append(sql)
            if "SELECT symbol, quantity, avg_cost, exchange" in sql:
                return ""
            if "FROM sim_trades" in sql:
                return ""
            return ""

        with (
            patch.object(updater, "PORTFOLIO_ID", 8),
            patch.object(updater, "db", side_effect=fake_db),
        ):
            updater.update_redis_prices()

        self.assertTrue(any("FROM sim_trades" in sql for sql in calls))

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

    def test_hk_realtime_price_parses_tencent_quote(self):
        class Response:
            def read(self):
                return 'v_hk00700="00700~Tencent Holdings~Tencent~528.000~520.000~"'.encode("gbk")

        with patch.object(updater.urllib.request, "urlopen", return_value=Response()):
            self.assertEqual(updater.fetch_hk_realtime_price("700"), 528.0)

    def test_us_realtime_price_parses_sina_quote(self):
        class Response:
            def read(self):
                return 'var hq_str_gb_pdd="Pinduoduo Inc,79.86,1.23,80.00"'.encode("gb18030")

        with patch.object(updater.urllib.request, "urlopen", return_value=Response()):
            self.assertEqual(updater.fetch_us_realtime_price("PDD"), 79.86)


if __name__ == "__main__":
    unittest.main()
