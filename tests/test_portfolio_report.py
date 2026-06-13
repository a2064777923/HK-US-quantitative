import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import portfolio_report as report


class PortfolioReportTests(unittest.TestCase):
    def test_get_latest_klines_reads_canonical_daily_bars(self):
        captured = {}

        def fake_psql(sql, timeout=30):
            captured["sql"] = sql
            return type("Result", (), {"returncode": 0, "stdout": "AAPL\t100\t2026-06-12\n", "stderr": ""})()

        with patch.object(report, "psql", side_effect=fake_psql):
            klines = report.get_latest_klines(["AAPL"])

        sql = captured["sql"]
        normalized = " ".join(sql.split())
        self.assertEqual(klines["AAPL"], {"close": 100.0, "date": "2026-06-12"})
        self.assertIn("WITH daily_bar AS", sql)
        self.assertIn("SELECT DISTINCT ON (symbol, timestamp::date)", sql)
        self.assertIn("ORDER BY symbol, timestamp::date, timestamp DESC", normalized)

    def test_build_portfolio_report_separates_user_and_simulation_roles(self):
        position = {
            "symbol": "00700",
            "name": "Tencent",
            "quantity": 100,
            "avg_cost": 300,
            "current_price": 280,
            "status": "holding",
            "exchange": "HKEX",
            "updated_at": "2026-06-12",
        }
        signal = {
            "trade_date": "2026-06-12",
            "side": "SELL",
            "score": -0.72,
            "expected_price": 280,
            "quality": {
                "reasons": ["weak trend"],
                "risk_flags": ["below_ma20"],
                "order_prices": {"stop_loss": 285, "take_profit": 330},
            },
        }
        opportunities = [{"symbol": "09988", "score": 0.83}]

        with (
            patch.object(report, "get_portfolio_row", return_value={"id": 8, "cash_hkd": 10_000}),
            patch.object(report, "get_positions", return_value=[position]),
            patch.object(report, "get_latest_klines", return_value={"00700": {"close": 280, "date": "2026-06-12"}}),
            patch.object(report, "get_latest_signals", return_value={"00700": signal}),
            patch.object(report, "get_top_buy_opportunities", return_value=opportunities) as top,
        ):
            user_payload = report.build_portfolio_report(7, "user")
            sim_payload = report.build_portfolio_report(8, "simulation")

        self.assertEqual(user_payload["role"], "user")
        self.assertEqual(user_payload["top_opportunities"], [])
        self.assertEqual(user_payload["high_priority_count"], 1)
        self.assertEqual(user_payload["positions"][0]["recommendation"], "stop_loss_review")

        self.assertEqual(sim_payload["role"], "simulation")
        self.assertEqual(sim_payload["top_opportunities"], opportunities)
        top.assert_called_once()

    def test_fifo_trade_review_estimates_closed_trade_pnl(self):
        trades = [
            {
                "row_id": "1",
                "trade_id": "trade-buy",
                "order_id": "order-buy",
                "symbol": "00700",
                "side": "buy",
                "price": 100,
                "quantity": 10,
                "fee": 1,
                "trade_value": 1_000,
                "created_at": "2026-06-01",
            },
            {
                "row_id": "2",
                "trade_id": "trade-sell",
                "order_id": "order-sell",
                "symbol": "00700",
                "side": "sell",
                "price": 110,
                "quantity": 4,
                "fee": 1,
                "trade_value": 440,
                "created_at": "2026-06-02",
            },
        ]

        closed = report.fifo_trade_review(trades)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["symbol"], "00700")
        self.assertEqual(closed[0]["quantity"], 4)
        self.assertAlmostEqual(closed[0]["pnl_hkd_est"], 38.6)
        self.assertAlmostEqual(closed[0]["pnl_pct_est"], 10.0)
        self.assertEqual(closed[0]["entry_trade_ids"], ["trade-buy"])
        self.assertEqual(closed[0]["entry_order_ids"], ["order-buy"])
        self.assertEqual(closed[0]["exit_trade_id"], "trade-sell")
        self.assertEqual(closed[0]["exit_order_id"], "order-sell")
        self.assertEqual(closed[0]["entry_legs"][0]["opened_at"], "2026-06-01")

    def test_get_recent_trades_preserves_order_lineage_columns(self):
        captured = {}

        def fake_table_columns(table):
            self.assertEqual(table, "sim_trades")
            return {
                "id",
                "trade_id",
                "order_id",
                "symbol",
                "side",
                "price",
                "quantity",
                "total_fee",
                "trade_value",
                "executed_at",
            }

        def fake_psql(sql, timeout=30):
            captured["sql"] = sql
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": "1\ttrade-1\torder-1\t00700\tBUY\t100\t10\t1\t1000\t2026-06-01T09:30:00\n",
                    "stderr": "",
                },
            )()

        with (
            patch.object(report, "table_columns", side_effect=fake_table_columns),
            patch.object(report, "psql", side_effect=fake_psql),
        ):
            trades = report.get_recent_trades(8, days=30)

        sql = " ".join(captured["sql"].split())
        self.assertIn("SELECT id, trade_id, order_id", sql)
        self.assertIn("executed_at >= NOW()", sql)
        self.assertEqual(trades[0]["row_id"], "1")
        self.assertEqual(trades[0]["trade_id"], "trade-1")
        self.assertEqual(trades[0]["order_id"], "order-1")
        self.assertEqual(trades[0]["side"], "buy")

    def test_save_json_atomic_writes_payload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "portfolio_report.json"
            report.save_json_atomic(str(path), {"schema": "portfolio_context_report_v1", "generated_at": "now"})

            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded["schema"], "portfolio_context_report_v1")

    def test_portfolio_risk_flags_fallback_valuation_when_db_price_is_zero(self):
        position = {
            "symbol": "00700",
            "name": "Tencent",
            "quantity": 100,
            "avg_cost": 60,
            "current_price": 0,
            "status": "holding",
            "exchange": "HKEX",
            "updated_at": "2026-06-12",
        }
        signal = {
            "trade_date": "2026-06-12",
            "side": "HOLD",
            "score": 0.1,
            "expected_price": 0,
            "quality": {"order_prices": {"stop_loss": 45, "take_profit": 70}},
        }

        with (
            patch.object(
                report,
                "get_portfolio_row",
                return_value={
                    "id": 8,
                    "cash_hkd": 1_000,
                    "reported_total_value_hkd": 6_000,
                    "initial_capital_hkd": 100_000,
                },
            ),
            patch.object(report, "get_positions", return_value=[position]),
            patch.object(report, "get_latest_klines", return_value={"00700": {"close": 50, "date": "2026-06-12"}}),
            patch.object(report, "get_latest_signals", return_value={"00700": signal}),
        ):
            payload = report.build_portfolio_report(8, "simulation")

        pos = payload["positions"][0]
        risk = payload["risk_summary"]
        self.assertEqual(pos["current_price"], 50)
        self.assertEqual(pos["valuation_price_source"], "latest_kline_close")
        self.assertIn("all_position_prices_missing_or_zero_in_db", risk["risk_flags"])
        self.assertIn("fallback_valuation_used", risk["risk_flags"])
        self.assertEqual(risk["price_quality"]["fallback_valuation_symbols"], ["00700"])
        self.assertEqual(risk["risk_level"], "critical")
        self.assertEqual(payload["position_review_items"][0]["symbol"], "00700")
        self.assertEqual(payload["position_review_items"][0]["recommended_action"], "reduce_or_exit_review")
        self.assertFalse(payload["position_review_items"][0]["execution_policy"]["submits_orders"])

    def test_build_payload_marks_simulation_trade_position_mismatch_critical(self):
        stale_position = {
            "symbol": "00017",
            "name": "NWD",
            "quantity": 1000,
            "avg_cost": 7.4,
            "current_price": 7.3,
            "status": "holding",
            "exchange": "HKEX",
            "updated_at": "2026-06-12",
        }
        trades = [
            {
                "symbol": "00017",
                "side": "buy",
                "price": 7.4,
                "quantity": 1000,
                "fee": 10,
                "trade_value": 7400,
                "created_at": "2026-06-11T09:30:00",
            },
            {
                "symbol": "00017",
                "side": "sell",
                "price": 7.3,
                "quantity": 1000,
                "fee": 10,
                "trade_value": 7300,
                "created_at": "2026-06-11T10:00:00",
            },
            {
                "symbol": "00929",
                "side": "buy",
                "price": 1.2,
                "quantity": 10000,
                "fee": 20,
                "trade_value": 12000,
                "created_at": "2026-06-11T10:30:00",
            },
        ]

        with (
            patch.object(
                report,
                "get_portfolio_row",
                return_value={
                    "id": 8,
                    "cash_hkd": 80_000,
                    "reported_total_value_hkd": 87_300,
                    "initial_capital_hkd": 100_000,
                },
            ),
            patch.object(report, "get_positions", return_value=[stale_position]),
            patch.object(report, "get_latest_klines", return_value={"00017": {"close": 7.3, "date": "2026-06-12"}}),
            patch.object(report, "get_latest_signals", return_value={}),
            patch.object(report, "get_top_buy_opportunities", return_value=[]),
            patch.object(report, "get_recent_trades", return_value=trades),
        ):
            payload = report.build_payload(sim_portfolio_id=8, user_portfolio_ids=[], review_days=30)

        sim_report = payload["portfolio_reports"][0]
        reconciliation = sim_report["trade_position_reconciliation"]
        self.assertEqual(payload["schema"], "portfolio_context_report_v1")
        self.assertEqual(payload["portfolio_risk"]["schema"], "portfolio_risk_report_v1")
        self.assertEqual(reconciliation["status"], "FAIL")
        self.assertEqual([item["symbol"] for item in reconciliation["missing_from_positions"]], ["00929"])
        self.assertEqual([item["symbol"] for item in reconciliation["closed_but_open_in_positions"]], ["00017"])
        self.assertEqual(sim_report["risk_summary"]["risk_level"], "critical")
        self.assertIn("positions_table_conflicts_with_trade_ledger", sim_report["risk_summary"]["risk_flags"])
        self.assertEqual(payload["position_review"]["schema"], "portfolio_position_review_v1")
        self.assertFalse(payload["position_review"]["submits_orders"])

    def test_position_review_payload_prioritizes_exit_pressure_items(self):
        report_payload = {
            "portfolio_id": 8,
            "role": "simulation",
            "positions": [
                {
                    "symbol": "00700",
                    "name": "Tencent",
                    "quantity": 100,
                    "current_price": 280,
                    "market_value_hkd": 28000,
                    "unrealized_pnl_hkd": -2000,
                    "unrealized_pnl_pct": -6.67,
                    "stop_distance_pct": -1.0,
                    "valuation_price_source": "db_current_price",
                    "kline_date": "2026-06-12",
                    "market": "HK",
                    "priority": "high",
                    "recommendation": "stop_loss_review",
                    "recommendation_reasons": ["price_below_signal_stop_loss"],
                    "signal": {
                        "side": "SELL",
                        "score": -0.7,
                        "trade_date": "2026-06-12",
                        "risk_flags": [],
                        "order_prices": {"stop_loss": 285, "take_profit": 250},
                    },
                }
            ],
        }

        payload = report.build_position_review_payload([report_payload])

        self.assertEqual(payload["item_count"], 1)
        self.assertEqual(payload["counts_by_urgency"]["high"], 1)
        self.assertEqual(payload["items"][0]["recommended_action"], "exit_review")
        self.assertTrue(payload["items"][0]["execution_policy"]["requires_separate_order_path"])

    def test_buy_signal_risk_flag_is_review_but_not_exit_pressure(self):
        position = {
            "symbol": "03888",
            "name": "Test",
            "quantity": 1000,
            "avg_cost": 24,
            "current_price": 25,
            "status": "holding",
            "exchange": "HKEX",
            "updated_at": "2026-06-12",
        }
        signal = {
            "trade_date": "2026-06-12",
            "side": "BUY",
            "score": 0.72,
            "expected_price": 25,
            "quality": {
                "risk_flags": ["upper_band_touch"],
                "order_prices": {"stop_loss": 22, "take_profit": 30},
            },
        }

        enriched = report.enrich_position(position, signal, {"close": 25, "date": "2026-06-12"})
        payload = {
            "portfolio_id": 8,
            "role": "simulation",
            "cash_hkd": 10_000,
            "positions_value_hkd": enriched["market_value_hkd"],
            "total_value_hkd": 35_000,
            "position_count": 1,
            "positions": [enriched],
        }
        risk = report.build_portfolio_risk(payload, {"reported_total_value_hkd": 35_000})
        review = report.build_position_review_item(payload, enriched)

        self.assertEqual(review["recommended_action"], "risk_review")
        self.assertEqual(review["urgency"], "medium")
        self.assertNotIn("exit_pressure_above_30pct", risk["risk_flags"])


if __name__ == "__main__":
    unittest.main()
