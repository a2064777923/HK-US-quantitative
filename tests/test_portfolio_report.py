import unittest
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts import portfolio_report as report


class PortfolioReportTests(unittest.TestCase):
    def test_user_portfolio_ids_accept_holdings_env_and_default_to_three(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(report.user_portfolio_ids_from_env(), [3])

        with patch.dict(
            "os.environ",
            {
                "QM_HOLDINGS_PORTFOLIO_ID": "5",
                "QM_USER_PORTFOLIO_ID": "7",
                "QM_USER_PORTFOLIO_IDS": "7,9",
            },
            clear=True,
        ):
            self.assertEqual(report.user_portfolio_ids_from_env(), [5, 7, 9])

    def test_market_for_position_accepts_listing_exchange_aliases(self):
        self.assertEqual(report.market_for_position({"symbol": "PDD", "exchange": "NASDAQ"}), "US")
        self.assertEqual(report.market_for_position({"symbol": "09988", "exchange": "HKEX"}), "HK")
        self.assertEqual(report.market_for_position({"symbol": "600519", "exchange": "SSE"}), "HK")

    def test_get_latest_klines_reads_canonical_daily_bars(self):
        captured = {}

        def fake_psql(sql, timeout=30):
            captured["sql"] = sql
            return type("Result", (), {"returncode": 0, "stdout": "AAPL\t100\t2026-06-12\n", "stderr": ""})()

        with patch.object(report, "psql", side_effect=fake_psql):
            klines = report.get_latest_klines(["AAPL"])

        sql = captured["sql"]
        normalized = " ".join(sql.split())
        self.assertEqual(klines["AAPL"], {"close": 100.0, "change_pct": None, "date": "2026-06-12"})
        self.assertIn("WITH daily_bar AS", sql)
        self.assertIn("SELECT DISTINCT ON (symbol, timestamp::date)", sql)
        self.assertIn("change_percent", sql)
        self.assertIn("ORDER BY symbol, timestamp::date, timestamp DESC", normalized)

    def test_position_review_catches_user_holding_daily_surge_without_v4_signal(self):
        position = {
            "symbol": "SPCX",
            "name": "Space Exploration Technologies",
            "quantity": 7,
            "avg_cost": 169.59,
            "current_price": 171.60,
            "status": "holding",
            "exchange": "NASDAQ",
            "updated_at": "2026-06-15",
        }

        enriched = report.enrich_position(
            position,
            {},
            {"close": 177.75, "change_pct": 3.5, "date": "2026-06-15"},
        )
        payload = {
            "portfolio_id": 3,
            "role": "user",
            "cash_hkd": 91,
            "positions_value_hkd": enriched["market_value_hkd"],
            "total_value_hkd": enriched["market_value_hkd"] + 91,
            "position_count": 1,
            "positions": [enriched],
        }
        review = report.build_position_review_item(payload, enriched)

        self.assertEqual(enriched["recommendation"], "trail_or_hold_review")
        self.assertEqual(enriched["priority"], "medium")
        self.assertEqual(enriched["latest_daily_change_pct"], 3.5)
        self.assertIn("latest_daily_gain_above_3pct", enriched["recommendation_reasons"])
        self.assertEqual(review["recommended_action"], "take_profit_or_trailing_stop_review")
        self.assertEqual(review["urgency"], "medium")
        self.assertEqual(review["advisory_plan"]["primary_action"], "review_trailing_stop_or_partial_take_profit")
        self.assertFalse(review["advisory_plan"]["add_allowed_after_review"])
        self.assertIsNone(review["advisory_plan"]["manual_max_quantity_hint"])
        decision_points = review["advisory_plan"]["operator_decision_points"]
        self.assertEqual(decision_points[0]["decision"], "trail_stop")
        self.assertTrue(decision_points[0]["manual_only"])
        self.assertFalse(decision_points[0]["submits_orders"])
        self.assertEqual(decision_points[1]["decision"], "reduce")
        self.assertEqual(decision_points[1]["quantity_hint"], 1.75)
        dynamic = review["advisory_plan"]["dynamic_management_context"]
        self.assertEqual(dynamic["schema"], "position_dynamic_management_context_v1")
        self.assertEqual(dynamic["target_status"], "momentum_profit_review")
        self.assertEqual(dynamic["latest_daily_change_pct"], 3.5)
        self.assertFalse(dynamic["price_snapshot_fresh"])
        self.assertIn("price_snapshot_stale", dynamic["price_data_flags"])
        self.assertTrue(dynamic["requires_hermes_dynamic_review"])
        self.assertIn("review_intraday_or_daily_strength_for_trailing_floor", dynamic["review_focus"])
        self.assertTrue(review["execution_policy"]["advice_only"])
        self.assertFalse(review["execution_policy"]["submits_orders"])

    def test_position_review_marks_stale_price_snapshot_for_hermes_context(self):
        with patch.object(report, "datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 6, 16, 12, 0, 0)
            fake_datetime.fromisoformat.side_effect = datetime.fromisoformat
            fake_datetime.strptime.side_effect = datetime.strptime
            position = {
                "symbol": "MSFT",
                "name": "Microsoft",
                "quantity": 2,
                "avg_cost": 500,
                "current_price": 470,
                "status": "holding",
                "exchange": "NASDAQ",
                "updated_at": "2026-06-15 00:00:00",
            }
            enriched = report.enrich_position(
                position,
                {
                    "trade_date": "2026-06-15",
                    "side": "SELL",
                    "score": -0.6,
                    "quality": {"order_prices": {"stop_loss": 478, "take_profit": 530}},
                },
                {"close": 470, "change_pct": -1.2, "date": "2026-06-15"},
            )

        review = report.build_position_review_item({"portfolio_id": 3, "role": "user"}, enriched)
        dynamic = review["advisory_plan"]["dynamic_management_context"]

        self.assertEqual(enriched["price_snapshot_age_hours"], 36.0)
        self.assertIn("price_snapshot_stale", enriched["price_data_flags"])
        self.assertEqual(review["position"]["price_snapshot_age_hours"], 36.0)
        self.assertFalse(dynamic["price_snapshot_fresh"])
        self.assertIn("price_snapshot_stale", dynamic["price_data_flags"])
        self.assertEqual(dynamic["target_status"], "below_signal_stop")

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

    def test_build_portfolio_report_uses_holding_only_for_user_positions(self):
        captured = []
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

        def fake_get_positions(portfolio_id, statuses=None):
            captured.append((portfolio_id, statuses))
            return [position]

        with (
            patch.object(report, "get_portfolio_row", return_value={"id": 3, "cash_hkd": 10_000}),
            patch.object(report, "get_positions", side_effect=fake_get_positions),
            patch.object(report, "get_latest_klines", return_value={"00700": {"close": 280, "date": "2026-06-12"}}),
            patch.object(report, "get_latest_signals", return_value={}),
        ):
            report.build_portfolio_report(3, "user")
            report.build_portfolio_report(8, "simulation")

        self.assertEqual(captured[0], (3, ("holding",)))
        self.assertEqual(captured[1], (8, ("active", "holding")))

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

    def test_portfolio_risk_flags_fallback_valuation_when_db_price_is_inconsistent_with_kline(self):
        position = {
            "symbol": "PDD",
            "name": "PDD Holdings",
            "quantity": 10,
            "avg_cost": 82.48,
            "current_price": 637.65,
            "status": "holding",
            "exchange": "NASDAQ",
            "updated_at": "2026-06-12",
        }
        signal = {
            "trade_date": "2026-06-12",
            "side": "HOLD",
            "score": 0.0,
            "expected_price": 79.86,
            "quality": {"order_prices": {}},
        }

        with (
            patch.object(
                report,
                "get_portfolio_row",
                return_value={
                    "id": 3,
                    "cash_hkd": 91,
                    "reported_total_value_hkd": 8_000,
                    "initial_capital_hkd": 100_000,
                },
            ),
            patch.object(report, "get_positions", return_value=[position]),
            patch.object(report, "get_latest_klines", return_value={"PDD": {"close": 79.86, "date": "2026-06-12"}}),
            patch.object(report, "get_latest_signals", return_value={"PDD": signal}),
        ):
            payload = report.build_portfolio_report(3, "user")

        pos = payload["positions"][0]
        risk = payload["risk_summary"]
        self.assertEqual(pos["current_price"], 79.86)
        self.assertEqual(pos["valuation_price_source"], "latest_kline_close")
        self.assertEqual(pos["db_current_price"], 637.65)
        self.assertGreater(pos["db_latest_kline_price_ratio"], 3.0)
        self.assertIn("db_current_price_inconsistent_with_latest_kline", pos["price_data_flags"])
        self.assertIn("fallback_valuation_used", risk["risk_flags"])
        self.assertIn("PDD", risk["price_quality"]["fallback_valuation_symbols"])
        self.assertIn("PDD", risk["price_quality"]["db_invalid_price_symbols"])

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
        self.assertEqual(payload["items"][0]["review_thread_key"], "simulation:8:00700")
        self.assertEqual(payload["items"][0]["recommended_action"], "reduce_or_exit_review")
        self.assertEqual(
            payload["items"][0]["advisory_plan"]["primary_action"],
            "review_reduce_half_or_exit_if_context_worsens",
        )
        self.assertEqual(
            payload["items"][0]["advisory_plan"]["reference_price_scope"],
            "latest_signal_geometry_not_position_order",
        )
        self.assertEqual(payload["items"][0]["advisory_plan"]["reduce_fraction_hint"], 0.5)
        self.assertEqual(payload["items"][0]["advisory_plan"]["reference_prices"]["signal_side"], "SELL")
        self.assertEqual(
            payload["items"][0]["advisory_plan"]["dynamic_management_context"]["target_status"],
            "below_signal_stop",
        )
        self.assertIn(
            "confirm_exit_pressure_with_market_and_intraday_context",
            payload["items"][0]["advisory_plan"]["dynamic_management_context"]["review_focus"],
        )
        self.assertTrue(payload["items"][0]["execution_policy"]["requires_separate_order_path"])

    def test_stop_breach_with_major_loss_still_maps_to_exit_review(self):
        position = {
            "symbol": "00700",
            "name": "Tencent",
            "quantity": 100,
            "current_price": 240,
            "market_value_hkd": 24000,
            "unrealized_pnl_hkd": -7000,
            "unrealized_pnl_pct": -22.0,
            "stop_distance_pct": -1.0,
            "valuation_price_source": "db_current_price",
            "kline_date": "2026-06-12",
            "market": "HK",
            "priority": "high",
            "recommendation": "stop_loss_review",
            "recommendation_reasons": ["price_below_signal_stop_loss", "position_loss_below_minus_20pct"],
            "signal": {
                "side": "SELL",
                "score": -0.7,
                "trade_date": "2026-06-12",
                "risk_flags": [],
                "order_prices": {"stop_loss": 285, "take_profit": 250},
            },
        }

        item = report.build_position_review_item({"portfolio_id": 8, "role": "simulation"}, position)

        self.assertEqual(item["recommended_action"], "exit_review")
        self.assertEqual(item["advisory_plan"]["primary_action"], "review_exit_all_or_fast_reduce")

    def test_profit_review_target_extension_reference_stays_above_current_price(self):
        position = {
            "symbol": "AAPL",
            "quantity": 10,
            "avg_cost": 15,
            "current_price": 22,
            "unrealized_pnl_pct": 46.67,
            "recommendation_reasons": ["price_reached_signal_take_profit"],
            "signal": {
                "side": "BUY",
                "order_prices": {"stop_loss": 16, "take_profit": 20},
            },
        }

        plan = report.build_position_advisory_plan(
            position,
            "take_profit_or_trailing_stop_review",
            "user",
        )

        extension = [point for point in plan["operator_decision_points"] if point["decision"] == "watch"][0]
        self.assertGreater(extension["price_reference"], 22)
        self.assertEqual(extension["condition"], "requires fresh momentum confirmation")
        self.assertTrue(extension["manual_only"])
        self.assertFalse(extension["submits_orders"])

    def test_large_unrealized_loss_overrides_hold_signal_for_position_review(self):
        position = {
            "symbol": "00929",
            "name": "International Precision",
            "quantity": 10_000,
            "avg_cost": 1.2106,
            "current_price": 0.73,
            "status": "holding",
            "exchange": "HKEX",
            "updated_at": "2026-06-15",
        }
        signal = {
            "trade_date": "2026-06-15",
            "side": "HOLD",
            "score": 0.4562,
            "expected_price": 0.73,
            "quality": {"reasons": ["跌破5日線"], "risk_flags": [], "order_prices": {}},
        }

        enriched = report.enrich_position(position, signal, {"close": 0.73, "date": "2026-06-15"})
        payload = {
            "portfolio_id": 8,
            "role": "simulation",
            "cash_hkd": 10_000,
            "positions_value_hkd": enriched["market_value_hkd"],
            "total_value_hkd": 17_300,
            "position_count": 1,
            "positions": [enriched],
        }
        review = report.build_position_review_item(payload, enriched)
        text = report.build_text_report(
            {
                "schema": "portfolio_context_report_v1",
                "generated_at": "2026-06-16T07:00:00",
                "portfolio_reports": [
                    {
                        **payload,
                        "return_pct_vs_initial": -6.5,
                        "high_priority_count": 1,
                        "risk_summary": {"risk_level": "high", "risk_flags": ["exit_pressure_above_30pct"]},
                        "position_review_items": [review],
                        "top_opportunities": [],
                    }
                ],
            }
        )

        self.assertEqual(enriched["recommendation"], "exit_or_reduce_review")
        self.assertIn("position_loss_below_minus_20pct", enriched["recommendation_reasons"])
        self.assertEqual(review["recommended_action"], "exit_review")
        self.assertEqual(review["urgency"], "high")
        self.assertEqual(review["advisory_plan"]["primary_action"], "review_exit_all_or_fast_reduce")
        self.assertEqual(review["advisory_plan"]["reduce_fraction_hint"], 1.0)
        self.assertEqual(review["advisory_plan"]["manual_max_quantity_hint"], 10000)
        self.assertEqual(review["advisory_plan"]["operator_decision_points"][0]["decision"], "exit")
        self.assertEqual(review["advisory_plan"]["operator_decision_points"][0]["quantity_hint"], 10000)
        self.assertEqual(review["advisory_plan"]["operator_decision_points"][1]["decision"], "reduce")
        self.assertEqual(review["advisory_plan"]["operator_decision_points"][1]["quantity_hint"], 5000)
        self.assertFalse(review["advisory_plan"]["add_allowed_after_review"])
        self.assertIn("quantity_hint_not_lot_adjusted", review["advisory_plan"]["review_flags"])
        self.assertIn("HIGH 00929 pnl=-39.7%", text)
        self.assertIn("action=exit_review recommendation=exit_or_reduce_review", text)

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
