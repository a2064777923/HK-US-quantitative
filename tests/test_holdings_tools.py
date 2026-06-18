import argparse
import contextlib
import io
import unittest
from unittest.mock import patch

from scripts import read_positions
from scripts import trade_update
from scripts import us_realtime


class ReadPositionsTests(unittest.TestCase):
    def test_us_filter_includes_us_listing_exchanges(self):
        rows = [
            {"symbol": "PDD", "exchange": "NASDAQ"},
            {"symbol": "BABA", "exchange": "US"},
            {"symbol": "JPM", "exchange": "NYSE"},
            {"symbol": "SOME", "exchange": "AMEX"},
            {"symbol": "09896", "exchange": "HKEX"},
        ]

        self.assertEqual(
            [row["symbol"] for row in read_positions.filter_positions(rows, "US")],
            ["PDD", "BABA", "JPM", "SOME"],
        )

    def test_hk_filter_uses_symbol_shape_as_backstop(self):
        rows = [
            {"symbol": "09988", "exchange": ""},
            {"symbol": "PDD", "exchange": "NASDAQ"},
        ]

        self.assertEqual([row["symbol"] for row in read_positions.filter_positions(rows, "HK")], ["09988"])

    def test_summary_shortcut_sets_summary_format(self):
        args = read_positions.build_parser().parse_args(["--summary"])
        self.assertTrue(args.summary)


class TradeUpdateTests(unittest.TestCase):
    def test_buy_parser_accepts_listing_exchanges(self):
        parser = trade_update.build_parser()
        for exchange in ("US", "NASDAQ", "NYSE", "AMEX", "HK", "HKEX"):
            args = parser.parse_args(
                ["buy", "--symbol", "PDD", "--exchange", exchange, "--qty", "10", "--cost", "82.48"]
            )
            self.assertEqual(args.exchange, exchange)

    def test_currency_defaults_follow_market(self):
        self.assertEqual(trade_update.infer_currency("09988", "HK", None), "HKD")
        self.assertEqual(trade_update.infer_currency("PDD", "NASDAQ", None), "USD")

    def test_us_market_value_is_hkd_snapshot_but_total_cost_stays_quote_currency(self):
        values = trade_update.position_values(qty=10, avg_cost=82.48, current_price=79.86, currency="USD")

        self.assertEqual(values["total_cost"], 824.8)
        self.assertEqual(values["market_value"], 6229.08)
        self.assertEqual(values["unrealized_pnl"], -204.36)
        self.assertAlmostEqual(values["unrealized_pnl_rate"], -0.031765, places=6)

    def test_sell_quantity_must_be_positive(self):
        parser = trade_update.build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["sell", "--symbol", "PDD", "--qty", "0"])

    def test_refresh_portfolio_totals_uses_user_holding_status_only(self):
        class Cursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((sql, params))

        cur = Cursor()

        with patch.dict("os.environ", {"QM_USER_PORTFOLIO_IDS": "3"}, clear=False):
            trade_update.refresh_portfolio_totals(cur, 3)

        sql, params = cur.calls[0]
        self.assertIn("status IN (%s)", sql)
        self.assertEqual(params[1:], (3, "holding", 3))

    def test_refresh_portfolio_totals_keeps_non_user_active_compatibility(self):
        class Cursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((sql, params))

        cur = Cursor()

        with patch.dict("os.environ", {"QM_USER_PORTFOLIO_IDS": "3"}, clear=False):
            trade_update.refresh_portfolio_totals(cur, 8)

        sql, params = cur.calls[0]
        self.assertIn("status IN (%s, %s)", sql)
        self.assertEqual(params[1:], (8, "active", "holding", 8))

    def test_full_sell_closes_and_clears_open_position_values(self):
        class Cursor:
            def __init__(self):
                self.calls = []
                self.fetchone_rows = [(42, 10, 82.48, 824.8, 79.86, "USD")]

            def execute(self, sql, params):
                self.calls.append((sql, params))

            def fetchone(self):
                return self.fetchone_rows.pop(0)

        class Connection:
            def __init__(self):
                self.cur = Cursor()
                self.committed = False
                self.closed = False

            def cursor(self):
                return self.cur

            def commit(self):
                self.committed = True

            def close(self):
                self.closed = True

        conn = Connection()
        args = argparse.Namespace(portfolio_id=3, symbol="PDD", qty=None)

        with patch.object(trade_update, "get_connection", return_value=conn), contextlib.redirect_stdout(io.StringIO()):
            code = trade_update.cmd_sell(args)

        self.assertEqual(code, 0)
        close_sql = next(sql for sql, _params in conn.cur.calls if "SET status = 'closed'" in sql)
        for fragment in (
            "quantity = 0",
            "available_quantity = 0",
            "frozen_quantity = 0",
            "total_cost = 0",
            "market_value = 0",
            "unrealized_pnl = 0",
            "unrealized_pnl_rate = 0",
            "weight = 0",
        ):
            self.assertIn(fragment, close_sql)
        self.assertTrue(conn.committed)
        self.assertTrue(conn.closed)


class UsRealtimeTests(unittest.TestCase):
    def test_sina_code_mapping_preserves_original_symbol(self):
        symbol = "BRK.B"
        code_map = {us_realtime.sina_code(symbol)[3:]: us_realtime.normalize_us_symbol(symbol)}

        self.assertEqual(code_map["brk_b"], "BRK.B")


if __name__ == "__main__":
    unittest.main()
