import argparse
import contextlib
import io
import unittest

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


class UsRealtimeTests(unittest.TestCase):
    def test_sina_code_mapping_preserves_original_symbol(self):
        symbol = "BRK.B"
        code_map = {us_realtime.sina_code(symbol)[3:]: us_realtime.normalize_us_symbol(symbol)}

        self.assertEqual(code_map["brk_b"], "BRK.B")


if __name__ == "__main__":
    unittest.main()
