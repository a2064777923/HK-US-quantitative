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

    def test_trade_notional_hkd_uses_quote_currency_fx(self):
        self.assertEqual(trade_update.trade_notional_hkd(10, 82.48, "USD"), 6433.44)
        self.assertEqual(trade_update.trade_notional_hkd(200, 85.5, "HKD"), 17100.0)

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

    def test_buy_adjusts_available_cash_by_default(self):
        class Cursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((sql, params))

            def fetchone(self):
                return None if "SELECT id, quantity" in self.calls[-1][0] else (42,)

        class Connection:
            def __init__(self):
                self.cur = Cursor()
                self.committed = False

            def cursor(self):
                return self.cur

            def commit(self):
                self.committed = True

            def close(self):
                pass

        conn = Connection()
        args = argparse.Namespace(
            portfolio_id=3,
            symbol="PDD",
            exchange="NASDAQ",
            qty=10,
            cost=82.48,
            currency=None,
            name=None,
            cash_adjust=True,
        )

        with patch.object(trade_update, "get_connection", return_value=conn), contextlib.redirect_stdout(io.StringIO()):
            code = trade_update.cmd_buy(args)

        self.assertEqual(code, 0)
        cash_params = next(params for sql, params in conn.cur.calls if "SET available_cash" in sql)
        self.assertEqual(cash_params[0], -6433.44)
        self.assertTrue(conn.committed)

    def test_no_cash_adjust_skips_available_cash_update(self):
        parser = trade_update.build_parser()
        args = parser.parse_args(
            [
                "buy",
                "--symbol",
                "PDD",
                "--exchange",
                "NASDAQ",
                "--qty",
                "10",
                "--cost",
                "82.48",
                "--no-cash-adjust",
            ]
        )

        self.assertFalse(args.cash_adjust)

    def test_add_updates_current_price_snapshot_when_existing_price_missing(self):
        class Cursor:
            def __init__(self):
                self.calls = []
                self.fetchone_rows = [(42, 10, 80.0, 800.0, None, "USD")]

            def execute(self, sql, params):
                self.calls.append((sql, params))

            def fetchone(self):
                return self.fetchone_rows.pop(0)

        class Connection:
            def __init__(self):
                self.cur = Cursor()

            def cursor(self):
                return self.cur

            def commit(self):
                pass

            def close(self):
                pass

        conn = Connection()
        args = argparse.Namespace(portfolio_id=3, symbol="PDD", qty=5, cost=82.0, cash_adjust=False)

        with patch.object(trade_update, "get_connection", return_value=conn), contextlib.redirect_stdout(io.StringIO()):
            code = trade_update.cmd_add(args)

        self.assertEqual(code, 0)
        update_sql, update_params = next(
            (sql, params) for sql, params in conn.cur.calls if "UPDATE positions" in sql
        )
        self.assertIn("current_price = %s", update_sql)
        self.assertEqual(update_params[4], 82.0)

    def test_write_commands_refuse_non_user_portfolio(self):
        with patch.dict("os.environ", {"QM_USER_PORTFOLIO_IDS": "3"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Refusing to mutate portfolio 7"):
                trade_update.ensure_user_portfolio_mutation(7)

    def test_write_commands_refuse_sim_portfolio_even_if_misconfigured_as_user(self):
        with patch.dict(
            "os.environ",
            {"QM_USER_PORTFOLIO_IDS": "3,8", "QM_SIM_PORTFOLIO_ID": "8"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulation portfolio 8"):
                trade_update.ensure_user_portfolio_mutation(8)

    def test_full_sell_adjusts_cash_and_realized_pnl(self):
        class Cursor:
            def __init__(self):
                self.calls = []
                self.fetchone_rows = [(42, 10, 82.48, 824.8, 90.0, "USD")]

            def execute(self, sql, params):
                self.calls.append((sql, params))

            def fetchone(self):
                return self.fetchone_rows.pop(0)

        class Connection:
            def __init__(self):
                self.cur = Cursor()

            def cursor(self):
                return self.cur

            def commit(self):
                pass

            def close(self):
                pass

        conn = Connection()
        args = argparse.Namespace(portfolio_id=3, symbol="PDD", qty=None, price=90.0, cash_adjust=True)

        with patch.object(trade_update, "get_connection", return_value=conn), contextlib.redirect_stdout(io.StringIO()):
            code = trade_update.cmd_sell(args)

        self.assertEqual(code, 0)
        close_sql, close_params = next((sql, params) for sql, params in conn.cur.calls if "SET status = 'closed'" in sql)
        self.assertIn("realized_pnl = COALESCE(realized_pnl, 0) + %s", close_sql)
        self.assertEqual(close_params[0], 586.56)
        cash_params = next(params for sql, params in conn.cur.calls if "SET available_cash" in sql)
        self.assertEqual(cash_params[0], 7020.0)

    def test_delete_soft_closes_by_default(self):
        class Cursor:
            def __init__(self):
                self.calls = []
                self.rowcount = 0

            def execute(self, sql, params):
                self.calls.append((sql, params))
                if "UPDATE positions" in sql and "SET status = 'closed'" in sql:
                    self.rowcount = 1

        class Connection:
            def __init__(self):
                self.cur = Cursor()
                self.committed = False

            def cursor(self):
                return self.cur

            def commit(self):
                self.committed = True

            def close(self):
                pass

        conn = Connection()
        args = argparse.Namespace(portfolio_id=3, symbol="PDD", hard=False)

        with patch.object(trade_update, "get_connection", return_value=conn), contextlib.redirect_stdout(io.StringIO()):
            code = trade_update.cmd_delete(args)

        self.assertEqual(code, 0)
        close_sql, close_params = next(
            (sql, params) for sql, params in conn.cur.calls if "SET status = 'closed'" in sql
        )
        self.assertIn("status = 'holding'", close_sql)
        self.assertEqual(close_params[2:], (3, "PDD"))
        self.assertFalse(any(sql.strip().upper().startswith("DELETE FROM") for sql, _params in conn.cur.calls))
        self.assertTrue(conn.committed)

    def test_hard_delete_requires_env_gate(self):
        args = argparse.Namespace(portfolio_id=3, symbol="PDD", hard=True, confirm_symbol="PDD")

        with patch.dict("os.environ", {"QM_USER_PORTFOLIO_IDS": "3"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Hard delete requires"):
                trade_update.cmd_delete(args)

    def test_hard_delete_requires_symbol_confirmation(self):
        args = argparse.Namespace(portfolio_id=3, symbol="PDD", hard=True, confirm_symbol="BABA")

        with patch.dict(
            "os.environ",
            {"QM_USER_PORTFOLIO_IDS": "3", "QM_ALLOW_HARD_DELETE_POSITIONS": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "confirm-symbol"):
                trade_update.cmd_delete(args)


class UsRealtimeTests(unittest.TestCase):
    def test_sina_code_mapping_preserves_original_symbol(self):
        symbol = "BRK.B"
        code_map = {us_realtime.sina_code(symbol)[3:]: us_realtime.normalize_us_symbol(symbol)}

        self.assertEqual(code_map["brk_b"], "BRK.B")


if __name__ == "__main__":
    unittest.main()
