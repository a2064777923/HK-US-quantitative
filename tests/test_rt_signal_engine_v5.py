import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts import rt_signal_engine_v5 as rt


class FakeIndicators:
    def __init__(self, avg_volume=1000, score=0.0):
        self.closes = [100] * 30
        self.volumes = [avg_volume] * 30
        self.rsi_14 = None
        self.bb_upper = None
        self.bb_lower = None
        self.ma5 = 100
        self.ma10 = 100
        self.ma20 = 100
        self.atr_14 = 1
        self.score = score

    def get_score(self, quote_context=None):
        return self.score, []


class RtSignalEngineV5Tests(unittest.TestCase):
    def test_realtime_updates_do_not_mutate_daily_history(self):
        ind = rt.IncrementalIndicators("00700")
        for i in range(40):
            close = 100 + i
            ind._update(close, close + 1, close - 1, 1_000 + i)

        original_closes = list(ind.closes)
        original_highs = list(ind.highs)
        original_lows = list(ind.lows)
        original_volumes = list(ind.volumes)

        ind.update_realtime(150, 151, 149, 5_000)
        ind.update_realtime(152, 153, 151, 6_000)
        score, _ = ind.get_score()
        closes, highs, lows, volumes = ind._series()

        self.assertIsNotNone(score)
        self.assertEqual(ind.closes, original_closes)
        self.assertEqual(ind.highs, original_highs)
        self.assertEqual(ind.lows, original_lows)
        self.assertEqual(ind.volumes, original_volumes)
        self.assertEqual(len(closes), len(original_closes) + 1)
        self.assertEqual(closes[-1], 152)
        self.assertEqual(highs[-1], 153)
        self.assertEqual(lows[-1], 151)
        self.assertEqual(volumes[-1], 6_000)

    def test_load_history_reads_canonical_daily_bars(self):
        captured = {}

        def fake_db(sql):
            captured["sql"] = sql
            return "101|102|100|1100\n100|101|99|1000"

        ind = rt.IncrementalIndicators("AAPL")
        with patch.object(rt, "db", side_effect=fake_db):
            ind.load_history(days=2)

        sql = captured["sql"]
        normalized = " ".join(sql.split())
        self.assertIn("WITH daily_bar AS", sql)
        self.assertIn("SELECT DISTINCT ON (timestamp::date)", sql)
        self.assertIn("ORDER BY timestamp::date, timestamp DESC", normalized)
        self.assertIn("FROM daily_bar ORDER BY trade_date DESC LIMIT 2", normalized)
        self.assertEqual(ind.closes[-2:], [100.0, 101.0])

    def test_realtime_score_volume_uses_session_adjusted_cumulative_ratio(self):
        ind = rt.IncrementalIndicators("AAPL")
        for _ in range(30):
            ind._update(100, 101, 99, 1000)

        ind.update_realtime(101, 102, 100, 200)

        score_without_context, reasons_without_context = ind.get_score()
        score_with_context, reasons_with_context = ind.get_score(
            {"market": "US", "time": "2026-06-11 10:00:00"}
        )
        _, _, _, volumes = ind._series()

        self.assertIsNone(ind.score_volume_ratio(volumes))
        self.assertGreater(
            ind.score_volume_ratio(volumes, {"market": "US", "time": "2026-06-11 10:00:00"}),
            2.0,
        )
        self.assertFalse(any(reason.startswith("放量") for reason in reasons_without_context))
        self.assertTrue(any(reason.startswith("放量") for reason in reasons_with_context))
        self.assertIsNotNone(score_without_context)
        self.assertIsNotNone(score_with_context)

    def test_flat_history_rsi_is_neutral_not_overbought(self):
        ind = rt.IncrementalIndicators("AAPL")
        for _ in range(30):
            ind._update(100, 100, 100, 1000)

        self.assertEqual(ind.rsi_14, 50)

        engine = rt.TriggerEngine()
        engine.check(
            "AAPL",
            ind,
            {
                "price": 100,
                "volume": 0,
                "market": "US",
                "time": "2026-06-11 10:00:00",
                "change_pct": 0,
            },
        )

        self.assertNotIn("RSI超買", [item["trigger"] for item in engine.alerts])

    def test_flat_realtime_rsi_is_neutral_not_overbought(self):
        ind = rt.IncrementalIndicators("AAPL")
        for _ in range(30):
            ind._update(100, 100, 100, 1000)

        ind.update_realtime(100, 100, 100, 0)

        self.assertEqual(ind.rsi_14, 50)

    def test_realtime_update_rejects_nonfinite_price_without_overwriting_last_bar(self):
        ind = rt.IncrementalIndicators("AAPL")
        for _ in range(30):
            ind._update(100, 101, 99, 1000)

        self.assertTrue(ind.update_realtime(101, 102, 100, 500))
        self.assertEqual(ind.rt_close, 101)

        self.assertFalse(ind.update_realtime(float("nan"), 200, 1, 9999))
        self.assertEqual(ind.rt_close, 101)
        self.assertEqual(ind.rt_high, 102)
        self.assertEqual(ind.rt_low, 100)
        self.assertEqual(ind.rt_volume, 500)

    def test_trigger_check_ignores_invalid_quote_without_alerts(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20

        for bad_quote in (
            {},
            {"price": None, "volume": 1000, "market": "US", "time": "2026-06-11 10:00:00"},
            {"price": float("nan"), "volume": 1000, "market": "US", "time": "2026-06-11 10:00:00"},
            {"price": -1, "volume": 1000, "market": "US", "time": "2026-06-11 10:00:00"},
        ):
            with self.subTest(bad_quote=bad_quote):
                engine.check("AAPL", indicators, bad_quote)

        self.assertEqual(engine.alerts, [])

    def test_quote_normalization_sanitizes_optional_fields(self):
        quote, reason = rt.normalize_quote(
            {
                "price": "100",
                "high": "nan",
                "low": -5,
                "volume": -10,
                "amount": -1,
                "change_pct": "nan",
                "market": "US",
            }
        )

        self.assertIsNone(reason)
        self.assertEqual(quote["price"], 100)
        self.assertEqual(quote["high"], 100)
        self.assertEqual(quote["low"], 100)
        self.assertEqual(quote["volume"], 0)
        self.assertEqual(quote["amount"], 0)
        self.assertEqual(quote["change_pct"], 0)

    def test_send_alert_writes_latest_file_and_append_only_queue(self):
        alerts = [
            {"signal_id": "a1", "symbol": "00700", "signal_type": "BUY"},
            {"signal_id": "a2", "symbol": "AAPL", "signal_type": "SELL"},
        ]
        with tempfile.TemporaryDirectory() as td:
            latest = str(Path(td) / "latest.json")
            queue = str(Path(td) / "queue.jsonl")

            with patch.object(rt, "ALERT_FILE", latest), patch.object(rt, "ALERT_QUEUE_FILE", queue):
                rt.send_alert(alerts)
                rt.send_alert([alerts[0]])

            latest_payload = json.loads(Path(latest).read_text(encoding="utf-8"))
            queue_lines = [
                json.loads(line)
                for line in Path(queue).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(latest_payload, [alerts[0]])
            self.assertEqual([item["signal_id"] for item in queue_lines], ["a1", "a2", "a1"])

    def test_cumulative_volume_ratio_uses_elapsed_session_fraction(self):
        ratio = rt.cumulative_volume_ratio(
            quote_volume=700,
            avg_daily_volume=1000,
            market="US",
            quote_time="2026-06-11 14:00:00",
        )

        self.assertAlmostEqual(ratio, 700 / (1000 * (270 / 390)), places=4)
        self.assertLess(ratio, 2)

    def test_market_open_flags_handle_us_overnight_hkt_weekday_rollover(self):
        hk_open, us_open = rt.market_open_flags_hkt(datetime(2026, 6, 13, 3, 59))
        self.assertFalse(hk_open)
        self.assertTrue(us_open)

        hk_open, us_open = rt.market_open_flags_hkt(datetime(2026, 6, 15, 1, 0))
        self.assertFalse(hk_open)
        self.assertFalse(us_open)

        hk_open, us_open = rt.market_open_flags_hkt(datetime(2026, 6, 15, 21, 30))
        self.assertFalse(hk_open)
        self.assertTrue(us_open)

        hk_open, us_open = rt.market_open_flags_hkt(datetime(2026, 6, 14, 22, 0))
        self.assertFalse(hk_open)
        self.assertFalse(us_open)

    def test_volume_watch_not_triggered_by_normal_cumulative_volume(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(avg_volume=1000)

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 100,
                "volume": 700,
                "market": "US",
                "time": "2026-06-11 14:00:00",
                "change_pct": 0,
            },
        )

        self.assertEqual(engine.alerts, [])

    def test_volume_watch_triggers_for_true_cumulative_anomaly(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(avg_volume=1000)

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 100,
                "volume": 4000,
                "market": "US",
                "time": "2026-06-11 14:00:00",
                "change_pct": 0,
            },
        )

        self.assertEqual(len(engine.alerts), 1)
        self.assertEqual(engine.alerts[0]["trigger"], "成交量異動")
        self.assertEqual(engine.alerts[0]["signal_type"], "WATCH")

    def test_ma5_trigger_uses_latest_historical_close_as_previous_state(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.8)
        indicators.closes = [100] * 25 + [90, 90, 90, 90, 110]
        indicators.ma5 = 98.2
        indicators.ma10 = 100
        indicators.ma20 = 101

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 111,
                "volume": 0,
                "market": "US",
                "time": "2026-06-11 10:00:00",
                "change_pct": 0,
            },
        )

        self.assertNotIn("站上MA5", [item["trigger"] for item in engine.alerts])

    def test_ma_cross_trigger_uses_latest_historical_mas_as_previous_state(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.8)
        indicators.closes = [100] * 29 + [200]
        indicators.ma5 = 100
        indicators.ma10 = 111
        indicators.ma20 = 105.5

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 100,
                "volume": 0,
                "market": "US",
                "time": "2026-06-11 10:00:00",
                "change_pct": 0,
            },
        )

        self.assertNotIn("MA金叉", [item["trigger"] for item in engine.alerts])

    def test_load_watchlists_from_json_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "watchlist.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "rt_signal_watchlist_v1",
                        "markets": {
                            "HK": {"symbols": ["00700", "03690", "00700"]},
                            "US": {"symbols": ["aapl", "MSFT"]},
                        },
                    }
                ),
                encoding="utf-8",
            )

            hk, us, context = rt.load_watchlists(env={}, file_path=str(path))

        self.assertEqual(hk, ["00700", "03690"])
        self.assertEqual(us, ["AAPL", "MSFT"])
        self.assertEqual(context["markets"]["HK"]["source"], "file")
        self.assertEqual(context["markets"]["US"]["count"], 2)
        self.assertEqual(len(context["watchlist_id"]), 16)
        self.assertEqual(context["warnings"], [])

    def test_env_watchlist_overrides_file_by_market(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "watchlist.json"
            path.write_text(json.dumps({"HK": ["00700"], "US": ["AAPL"]}), encoding="utf-8")

            hk, us, context = rt.load_watchlists(
                env={"RT_SIGNAL_US_WATCHLIST": "TSLA, nvda"},
                file_path=str(path),
            )

        self.assertEqual(hk, ["00700"])
        self.assertEqual(us, ["TSLA", "NVDA"])
        self.assertEqual(context["markets"]["HK"]["source"], "file")
        self.assertEqual(context["markets"]["US"]["source"], "env")

    def test_trigger_alert_includes_watchlist_metadata(self):
        engine = rt.TriggerEngine(
            watchlist_context={
                "watchlist_id": "watchlist-test",
                "markets": {"US": {"source": "file", "count": 2}},
            }
        )
        indicators = FakeIndicators(avg_volume=1000)

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 100,
                "volume": 4000,
                "market": "US",
                "time": "2026-06-11 14:00:00",
                "change_pct": 0,
            },
        )

        self.assertEqual(engine.alerts[0]["watchlist_id"], "watchlist-test")
        self.assertEqual(engine.alerts[0]["watchlist_source"], "file")
        self.assertEqual(engine.alerts[0]["watchlist_count"], 2)

    def test_load_strategy_config_from_json_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "strategy.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "rt_signal_strategy_config_v1",
                        "version": "unit-test",
                        "confirmation_thresholds": {"BUY": {"min_full_score": 0.5}},
                        "trigger_overrides": {"BUY:站上MA5": {"min_full_score": 0.6}},
                    }
                ),
                encoding="utf-8",
            )

            config, context = rt.load_strategy_config(env={}, file_path=str(path))

        self.assertEqual(context["source"], "file")
        self.assertEqual(context["version"], "unit-test")
        self.assertEqual(config["confirmation_thresholds"]["BUY"]["min_full_score"], 0.5)
        self.assertEqual(config["trigger_overrides"]["BUY:站上MA5"]["min_full_score"], 0.6)
        self.assertEqual(len(config["config_id"]), 16)

    def test_strategy_config_can_disable_trigger(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "trigger_overrides": {
                    "WATCH:成交量異動": {"enabled": False},
                }
            }
        )
        indicators = FakeIndicators(avg_volume=1000)

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 100,
                "volume": 4000,
                "market": "US",
                "time": "2026-06-11 14:00:00",
                "change_pct": 0,
            },
        )

        self.assertEqual(engine.alerts, [])

    def test_strategy_config_tightens_trigger_confirmation_threshold(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "trigger_overrides": {
                    "BUY:站上MA5": {"min_full_score": 0.6},
                }
            },
            strategy_context={
                "strategy_config_id": "strategy-test",
                "source": "inline",
                "version": "unit-test",
            },
        )
        indicators = FakeIndicators(score=0.3)

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 101,
                "volume": 700,
                "market": "US",
                "time": "2026-06-11 14:00:00",
                "change_pct": 0,
            },
        )

        ma5_alert = [item for item in engine.alerts if item["trigger"] == "站上MA5"][0]
        self.assertFalse(ma5_alert["confirmed"])
        self.assertEqual(ma5_alert["signal_type"], "WATCH")
        self.assertEqual(ma5_alert["candidate_signal_type"], "BUY")
        self.assertEqual(ma5_alert["suppressed_directional_reason"], "unconfirmed_directional")
        self.assertFalse(ma5_alert["execution_candidate"])
        self.assertIsNone(ma5_alert["stop_loss"])
        self.assertIsNotNone(ma5_alert["candidate_stop_loss"])
        self.assertEqual(ma5_alert["strategy_config_id"], "strategy-test")
        self.assertEqual(ma5_alert["strategy_config_source"], "inline")

    def test_strategy_config_shadow_only_emits_watch_for_confirmed_directional(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "emission": {"emit_unconfirmed_directional_as_watch": False},
                "trigger_overrides": {
                    "BUY:站上MA5": {"review_mode": "shadow_only_pending_sample"},
                },
            }
        )
        indicators = FakeIndicators(score=0.8)

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 101,
                "volume": 700,
                "market": "US",
                "time": "2026-06-11 14:00:00",
                "change_pct": 0,
            },
        )

        ma5_alert = [item for item in engine.alerts if item["trigger"] == "站上MA5"][0]
        self.assertTrue(ma5_alert["confirmed"])
        self.assertEqual(ma5_alert["signal_type"], "WATCH")
        self.assertEqual(ma5_alert["candidate_signal_type"], "BUY")
        self.assertEqual(ma5_alert["trigger_review_mode"], "shadow_only_pending_sample")
        self.assertTrue(ma5_alert["strategy_policy_shadow_only"])
        self.assertEqual(ma5_alert["suppressed_directional_reason"], "strategy_review_shadow_only")
        self.assertFalse(ma5_alert["execution_candidate"])
        self.assertIsNone(ma5_alert["stop_loss"])
        self.assertIsNone(ma5_alert["take_profit"])
        self.assertIsNotNone(ma5_alert["candidate_stop_loss"])
        self.assertIsNotNone(ma5_alert["candidate_take_profit"])

    def test_strategy_config_can_preserve_unconfirmed_directional_for_research(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "emission": {"emit_unconfirmed_directional_as_watch": False},
                "trigger_overrides": {
                    "BUY:站上MA5": {"min_full_score": 0.6},
                },
            }
        )
        indicators = FakeIndicators(score=0.3)

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 101,
                "volume": 700,
                "market": "US",
                "time": "2026-06-11 14:00:00",
                "change_pct": 0,
            },
        )

        ma5_alert = [item for item in engine.alerts if item["trigger"] == "站上MA5"][0]
        self.assertFalse(ma5_alert["confirmed"])
        self.assertEqual(ma5_alert["signal_type"], "BUY")
        self.assertEqual(ma5_alert["candidate_signal_type"], "BUY")
        self.assertIsNone(ma5_alert["suppressed_directional_reason"])
        self.assertFalse(ma5_alert["execution_candidate"])
        self.assertIsNotNone(ma5_alert["stop_loss"])

    def test_cooldown_key_is_independent_of_hkt_calendar_date(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None

        cooldown_key = engine.alert_cooldown_key("aapl", "BUY", "RSI超賣")
        with patch.object(rt.time, "time", return_value=1_000_000):
            engine.cooldowns[cooldown_key] = 1_000_000 - 60
            engine.check(
                "AAPL",
                indicators,
                {
                    "price": 100,
                    "volume": 0,
                    "market": "US",
                    "time": "2026-06-13 03:59:00",
                    "change_pct": 0,
                },
            )

        self.assertEqual(engine.alerts, [])
        self.assertEqual(engine.cooldowns[cooldown_key], 1_000_000 - 60)

    def test_signal_id_bucket_uses_configured_trigger_cooldown(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "trigger_overrides": {
                    "BUY:RSI超賣": {"cooldown_seconds": 300},
                },
            }
        )
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None
        quote = {
            "price": 100,
            "volume": 0,
            "market": "US",
            "time": "2026-06-13 03:59:00",
            "change_pct": 0,
        }

        with patch.object(rt.time, "time", side_effect=[1_000_000, 1_000_301]):
            engine.check("AAPL", indicators, quote)
            engine.check("AAPL", indicators, quote)

        self.assertEqual(len(engine.alerts), 2)
        first_id = engine.alerts[0]["signal_id"]
        second_id = engine.alerts[1]["signal_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(first_id.rsplit(":", 1)[-1], str(1_000_000 // 300))
        self.assertEqual(second_id.rsplit(":", 1)[-1], str(1_000_301 // 300))

    def test_invalid_buy_risk_geometry_is_downgraded_to_watch(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "emission": {"emit_unconfirmed_directional_as_watch": False},
            }
        )
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None
        indicators.atr_14 = 1

        engine.check(
            "LOW",
            indicators,
            {
                "price": 1,
                "volume": 0,
                "market": "US",
                "time": "2026-06-11 10:00:00",
                "change_pct": 0,
            },
        )

        alert = [item for item in engine.alerts if item["trigger"] == "RSI超賣"][0]
        self.assertTrue(alert["confirmed"])
        self.assertEqual(alert["signal_type"], "WATCH")
        self.assertEqual(alert["candidate_signal_type"], "BUY")
        self.assertFalse(alert["execution_candidate"])
        self.assertFalse(alert["risk_geometry_valid"])
        self.assertEqual(alert["risk_geometry_reason"], "non_positive_risk_price")
        self.assertIsNone(alert["stop_loss"])
        self.assertIsNone(alert["take_profit"])
        self.assertLessEqual(alert["candidate_stop_loss"], 0)

    def test_invalid_sell_risk_geometry_is_downgraded_to_watch(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "emission": {"emit_unconfirmed_directional_as_watch": False},
            }
        )
        indicators = FakeIndicators(score=-0.8)
        indicators.rsi_14 = 80
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None
        indicators.atr_14 = 1

        engine.check(
            "LOW",
            indicators,
            {
                "price": 1,
                "volume": 0,
                "market": "US",
                "time": "2026-06-11 10:00:00",
                "change_pct": 0,
            },
        )

        alert = [item for item in engine.alerts if item["trigger"] == "RSI超買"][0]
        self.assertTrue(alert["confirmed"])
        self.assertEqual(alert["signal_type"], "WATCH")
        self.assertEqual(alert["candidate_signal_type"], "SELL")
        self.assertFalse(alert["execution_candidate"])
        self.assertFalse(alert["risk_geometry_valid"])
        self.assertEqual(alert["risk_geometry_reason"], "non_positive_risk_price")
        self.assertIsNone(alert["stop_loss"])
        self.assertIsNone(alert["take_profit"])
        self.assertLessEqual(alert["candidate_take_profit"], 0)


if __name__ == "__main__":
    unittest.main()
