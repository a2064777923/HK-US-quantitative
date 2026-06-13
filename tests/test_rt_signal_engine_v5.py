import json
import tempfile
import unittest
import builtins
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts import rt_signal_engine_v5 as rt


class FakeIndicators:
    def __init__(self, avg_volume=1000, score=0.0):
        self.closes = [100] * 30
        self.highs = [101] * 30
        self.lows = [99] * 30
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

    def test_load_history_rejects_invalid_symbol_without_db_query(self):
        ind = rt.IncrementalIndicators("AAPL';DROP")

        with patch.object(rt, "db") as fake_db:
            loaded = ind.load_history(days=2)

        self.assertFalse(loaded)
        self.assertFalse(ind.loaded)
        fake_db.assert_not_called()

    def test_load_history_normalizes_symbol_and_days(self):
        captured = {}

        def fake_db(sql):
            captured["sql"] = sql
            return "101|102|100|1100"

        ind = rt.IncrementalIndicators("aapl")
        with patch.object(rt, "db", side_effect=fake_db):
            loaded = ind.load_history(days="-5")

        normalized = " ".join(captured["sql"].split())
        self.assertTrue(loaded)
        self.assertIn("WHERE symbol='AAPL' AND interval='day'", normalized)
        self.assertIn("LIMIT 100", normalized)

    def test_load_history_skips_invalid_daily_bars(self):
        def fake_db(sql):
            return "\n".join(
                [
                    "101|102|100|1100",
                    "100|99|101|1000",
                    "NaN|103|99|1000",
                    "0|101|99|1000",
                    "99|101|100|1000",
                    "98|100|97|-1",
                    "97|99|96|900",
                ]
            )

        ind = rt.IncrementalIndicators("AAPL")
        with patch.object(rt, "db", side_effect=fake_db):
            loaded = ind.load_history(days=10)

        self.assertTrue(loaded)
        self.assertEqual(ind.closes, [97.0, 101.0])
        self.assertEqual(ind.highs, [99.0, 102.0])
        self.assertEqual(ind.lows, [96.0, 100.0])
        self.assertEqual(ind.volumes, [900.0, 1100.0])

    def test_load_state_returns_default_for_missing_or_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "rt_signal_state.json")
            with patch.object(rt, "STATE_FILE", state_file):
                self.assertEqual(rt.load_state(), {"cooldowns": {}, "date": ""})

                Path(state_file).write_text("{bad json", encoding="utf-8")
                self.assertEqual(rt.load_state(), {"cooldowns": {}, "date": ""})

    def test_load_state_sanitizes_cooldown_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "rt_signal_state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "date": "2026-06-13",
                        "cooldowns": {
                            "AAPL:BUY:RSI": 1000,
                            " 0700:SELL:MA5 ": "2000.5",
                            "bad:none": None,
                            "bad:negative": -1,
                            "bad:nan": float("nan"),
                            "": 123,
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(rt, "STATE_FILE", str(state_file)):
                loaded = rt.load_state()

        self.assertEqual(
            loaded,
            {
                "date": "2026-06-13",
                "cooldowns": {
                    "AAPL:BUY:RSI": 1000.0,
                    "0700:SELL:MA5": 2000.5,
                },
            },
        )

    def test_save_state_writes_sanitized_json_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "rt_signal_state.json"
            with patch.object(rt, "STATE_FILE", str(state_file)):
                rt.save_state(
                    {
                        "date": "2026-06-13",
                        "cooldowns": {
                            "AAPL:BUY:RSI": 1000,
                            "bad:nan": float("nan"),
                            "bad:negative": -1,
                        },
                    }
                )

            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            tmp_files = list(Path(tmpdir).glob("rt_signal_state.json.*.tmp"))

        self.assertEqual(loaded, {"cooldowns": {"AAPL:BUY:RSI": 1000.0}, "date": "2026-06-13"})
        self.assertEqual(tmp_files, [])

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

    def test_signal_readiness_requires_full_multifactor_history(self):
        indicators = FakeIndicators(score=0.8)
        indicators.closes = [100] * (rt.MIN_SIGNAL_HISTORY_BARS - 1)
        indicators.highs = [101] * (rt.MIN_SIGNAL_HISTORY_BARS - 1)
        indicators.lows = [99] * (rt.MIN_SIGNAL_HISTORY_BARS - 1)
        indicators.volumes = [1000] * (rt.MIN_SIGNAL_HISTORY_BARS - 1)

        self.assertFalse(rt.indicator_signal_ready(indicators))

        indicators.closes.append(100)
        indicators.highs.append(101)
        indicators.lows.append(99)
        indicators.volumes.append(1000)
        self.assertTrue(rt.indicator_signal_ready(indicators))

    def test_signal_readiness_rejects_misaligned_daily_ohlcv_history(self):
        indicators = FakeIndicators(score=0.8)
        indicators.lows = indicators.lows[:-1]

        self.assertEqual(rt.indicator_history_bar_count(indicators), rt.MIN_SIGNAL_HISTORY_BARS - 1)
        self.assertFalse(rt.indicator_signal_ready(indicators))

    def test_trigger_check_ignores_misaligned_daily_ohlcv_history(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20
        indicators.highs = indicators.highs[:-1]

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

        self.assertEqual(engine.alerts, [])

    def test_trigger_check_ignores_insufficient_multifactor_history(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.8)
        indicators.closes = [100] * (rt.MIN_SIGNAL_HISTORY_BARS - 1)
        indicators.highs = [101] * (rt.MIN_SIGNAL_HISTORY_BARS - 1)
        indicators.lows = [99] * (rt.MIN_SIGNAL_HISTORY_BARS - 1)
        indicators.volumes = [1000] * (rt.MIN_SIGNAL_HISTORY_BARS - 1)
        indicators.rsi_14 = 20

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

        self.assertEqual(engine.alerts, [])

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
                "prev_close": "nan",
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
        self.assertEqual(quote["prev_close"], 0)
        self.assertEqual(quote["volume"], 0)
        self.assertEqual(quote["amount"], 0)
        self.assertEqual(quote["change_pct"], 0)

    def test_quote_normalization_derives_change_pct_from_prev_close(self):
        quote, reason = rt.normalize_quote(
            {
                "price": "110",
                "high": "111",
                "low": "109",
                "prev_close": "100",
                "change_pct": "",
                "market": "US",
            }
        )

        self.assertIsNone(reason)
        self.assertEqual(quote["prev_close"], 100)
        self.assertAlmostEqual(quote["change_pct"], 10.0)

    def test_trigger_check_uses_derived_change_pct_for_large_move(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.0)
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 110,
                "high": 111,
                "low": 109,
                "prev_close": 100,
                "volume": 0,
                "market": "US",
                "time": "2026-06-11 10:00:00",
            },
        )

        self.assertEqual(len(engine.alerts), 1)
        self.assertEqual(engine.alerts[0]["trigger"], "急漲")
        self.assertAlmostEqual(engine.alerts[0]["change_pct"], 10.0)

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

    def test_send_alert_rejects_non_standard_nan_json(self):
        with tempfile.TemporaryDirectory() as td:
            latest = str(Path(td) / "latest.json")
            queue = str(Path(td) / "queue.jsonl")

            with patch.object(rt, "ALERT_FILE", latest), patch.object(rt, "ALERT_QUEUE_FILE", queue):
                with self.assertRaises(ValueError):
                    rt.send_alert([{"signal_id": "bad", "price": float("nan")}])

            self.assertFalse(Path(latest).exists())
            self.assertFalse(Path(queue).exists())

    def test_send_alert_does_not_update_latest_when_queue_append_fails(self):
        with tempfile.TemporaryDirectory() as td:
            latest_path = Path(td) / "latest.json"
            queue_path = Path(td) / "queue.jsonl"
            latest_path.write_text(json.dumps([{"signal_id": "old"}]), encoding="utf-8")
            original_open = builtins.open

            def fail_queue_open(path, mode="r", *args, **kwargs):
                if str(path) == str(queue_path) and "a" in mode:
                    raise OSError("queue unavailable")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(rt, "ALERT_FILE", str(latest_path)), patch.object(
                rt, "ALERT_QUEUE_FILE", str(queue_path)
            ), patch("builtins.open", side_effect=fail_queue_open):
                with self.assertRaises(OSError):
                    rt.send_alert([{"signal_id": "new"}])

            latest_payload = json.loads(latest_path.read_text(encoding="utf-8"))
            self.assertEqual(latest_payload, [{"signal_id": "old"}])
            self.assertFalse(queue_path.exists())

    def test_cumulative_volume_ratio_uses_elapsed_session_fraction(self):
        ratio = rt.cumulative_volume_ratio(
            quote_volume=700,
            avg_daily_volume=1000,
            market="US",
            quote_time="2026-06-11 14:00:00",
        )

        self.assertAlmostEqual(ratio, 700 / (1000 * (270 / 390)), places=4)
        self.assertLess(ratio, 2)

    def test_parse_quote_datetime_accepts_compact_vendor_timestamps(self):
        self.assertEqual(
            rt.parse_quote_datetime("20260611140000"),
            datetime(2026, 6, 11, 14, 0, 0),
        )
        self.assertEqual(
            rt.parse_quote_datetime("202606111400"),
            datetime(2026, 6, 11, 14, 0, 0),
        )

    def test_cumulative_volume_ratio_uses_compact_quote_timestamp(self):
        ratio = rt.cumulative_volume_ratio(
            quote_volume=700,
            avg_daily_volume=1000,
            market="US",
            quote_time="20260611140000",
        )

        self.assertAlmostEqual(ratio, 700 / (1000 * (270 / 390)), places=4)
        self.assertLess(ratio, 2)

    def test_cumulative_volume_ratio_requires_parseable_quote_timestamp(self):
        self.assertIsNone(
            rt.cumulative_volume_ratio(
                quote_volume=700,
                avg_daily_volume=1000,
                market="US",
                quote_time=None,
            )
        )
        self.assertIsNone(
            rt.cumulative_volume_ratio(
                quote_volume=700,
                avg_daily_volume=1000,
                market="US",
                quote_time="bad-vendor-time",
            )
        )

    def test_alert_signal_date_prefers_quote_timestamp(self):
        self.assertEqual(
            rt.alert_signal_date(
                "20260611140000",
                generated_at=datetime(2026, 6, 12, 1, 0, 0),
            ),
            "20260611",
        )
        self.assertEqual(
            rt.alert_signal_date(None, generated_at=datetime(2026, 6, 12, 1, 0, 0)),
            "20260612",
        )

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

    def test_us_market_open_flags_follow_dst_and_standard_time_in_hkt(self):
        # June is US daylight time: 09:30-16:00 ET maps to 21:30-04:00 HKT.
        self.assertFalse(rt.market_open_flags_hkt(datetime(2026, 6, 15, 21, 29))[1])
        self.assertTrue(rt.market_open_flags_hkt(datetime(2026, 6, 15, 21, 30))[1])
        self.assertTrue(rt.market_open_flags_hkt(datetime(2026, 6, 16, 3, 59))[1])
        self.assertFalse(rt.market_open_flags_hkt(datetime(2026, 6, 16, 4, 1))[1])

        # January is US standard time: 09:30-16:00 ET maps to 22:30-05:00 HKT.
        self.assertFalse(rt.market_open_flags_hkt(datetime(2026, 1, 5, 22, 29))[1])
        self.assertTrue(rt.market_open_flags_hkt(datetime(2026, 1, 5, 22, 30))[1])
        self.assertTrue(rt.market_open_flags_hkt(datetime(2026, 1, 6, 4, 59))[1])
        self.assertFalse(rt.market_open_flags_hkt(datetime(2026, 1, 6, 5, 1))[1])

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

    def test_volume_watch_not_triggered_without_parseable_quote_timestamp(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(avg_volume=1000)

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 100,
                "volume": 4000,
                "market": "US",
                "time": "bad-vendor-time",
                "change_pct": 0,
            },
        )

        self.assertEqual(engine.alerts, [])

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

    def test_ma_death_cross_trigger_emits_sell_from_latest_historical_state(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=-0.8)
        indicators.closes = [100] * 30
        indicators.ma5 = 100
        indicators.ma10 = 99
        indicators.ma20 = 100

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

        death_cross_alerts = [item for item in engine.alerts if item["trigger"] == "MA死叉"]
        self.assertEqual(len(death_cross_alerts), 1)
        self.assertEqual(death_cross_alerts[0]["signal_type"], "SELL")
        self.assertEqual(death_cross_alerts[0]["candidate_signal_type"], "SELL")
        self.assertTrue(death_cross_alerts[0]["confirmed"])
        self.assertTrue(death_cross_alerts[0]["execution_candidate"])

    def test_ma_death_cross_does_not_reemit_already_crossed_state(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=-0.8)
        indicators.closes = [100] * 20 + [90] * 10
        indicators.ma5 = 100
        indicators.ma10 = 90
        indicators.ma20 = 95

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

        self.assertNotIn("MA死叉", [item["trigger"] for item in engine.alerts])

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

    def test_load_watchlists_filters_invalid_file_symbols_by_market(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "watchlist.json"
            path.write_text(
                json.dumps(
                    {
                        "markets": {
                            "HK": {"symbols": ["00700", "hkHSI", "1234", "00005"]},
                            "US": {"symbols": ["AAPL", "BRK.B", "BAD$", "TOO_LONG_SYMBOL"]},
                        }
                    }
                ),
                encoding="utf-8",
            )

            hk, us, context = rt.load_watchlists(env={}, file_path=str(path))

        self.assertEqual(hk, ["00700", "00005"])
        self.assertEqual(us, ["AAPL", "BRK.B"])
        self.assertTrue(any("watchlist_file_invalid_symbols:HK:" in warning for warning in context["warnings"]))
        self.assertTrue(any("watchlist_file_invalid_symbols:US:" in warning for warning in context["warnings"]))

    def test_env_watchlist_filters_invalid_symbols_by_market(self):
        hk, us, context = rt.load_watchlists(
            env={
                "RT_SIGNAL_HK_WATCHLIST": "00700, hkHSI, 00005",
                "RT_SIGNAL_US_WATCHLIST": "AAPL, BRK.B, BAD$",
            },
            file_path="",
        )

        self.assertEqual(hk, ["00700", "00005"])
        self.assertEqual(us, ["AAPL", "BRK.B"])
        self.assertTrue(
            any("watchlist_env_invalid_symbols:RT_SIGNAL_HK_WATCHLIST:" in warning for warning in context["warnings"])
        )
        self.assertTrue(
            any("watchlist_env_invalid_symbols:RT_SIGNAL_US_WATCHLIST:" in warning for warning in context["warnings"])
        )

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

    def test_strategy_config_normalizes_min_rr_ratio(self):
        config, warnings = rt.normalize_strategy_config({"risk_model": {"min_rr_ratio": -1}})

        self.assertEqual(config["risk_model"]["min_rr_ratio"], 1.2)
        self.assertIn("invalid_min_rr_ratio_using_default", warnings)

    def test_strategy_config_normalizes_confirmation_threshold_bounds(self):
        config, warnings = rt.normalize_strategy_config(
            {
                "confirmation_thresholds": {
                    "BUY": {"min_full_score": -2},
                    "SELL": {"max_full_score": 2},
                }
            }
        )

        self.assertEqual(config["confirmation_thresholds"]["BUY"]["min_full_score"], 0.25)
        self.assertEqual(config["confirmation_thresholds"]["SELL"]["max_full_score"], -0.25)
        self.assertIn("invalid_buy_min_full_score_using_default", warnings)
        self.assertIn("invalid_sell_max_full_score_using_default", warnings)

    def test_strategy_config_drops_out_of_range_trigger_threshold_override(self):
        config, warnings = rt.normalize_strategy_config(
            {
                "trigger_overrides": {
                    "BUY:站上MA5": {"min_full_score": -2},
                    "SELL:跌破MA5": {"max_full_score": 2},
                }
            }
        )

        self.assertNotIn("min_full_score", config["trigger_overrides"]["BUY:站上MA5"])
        self.assertNotIn("max_full_score", config["trigger_overrides"]["SELL:跌破MA5"])
        self.assertIn("invalid_trigger_min_full_score:BUY:站上MA5", warnings)
        self.assertIn("invalid_trigger_max_full_score:SELL:跌破MA5", warnings)

    def test_strategy_config_normalizes_trigger_enabled_string_and_bad_cooldown(self):
        config, warnings = rt.normalize_strategy_config(
            {
                "trigger_overrides": {
                    "BUY:RSI超賣": {"enabled": "false", "cooldown_seconds": -5},
                }
            }
        )

        override = config["trigger_overrides"]["BUY:RSI超賣"]
        self.assertIs(override["enabled"], False)
        self.assertNotIn("cooldown_seconds", override)
        self.assertIn("invalid_trigger_cooldown_seconds:BUY:RSI超賣", warnings)

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

    def test_strategy_config_string_false_can_disable_trigger(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "trigger_overrides": {
                    "WATCH:成交量異動": {"enabled": "false"},
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

    def test_invalid_trigger_threshold_override_does_not_confirm_weak_signal(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "trigger_overrides": {
                    "BUY:站上MA5": {"min_full_score": -2},
                }
            }
        )
        indicators = FakeIndicators(score=0.0)

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
        self.assertEqual(ma5_alert["suppressed_directional_reason"], "unconfirmed_directional")
        self.assertFalse(ma5_alert["execution_candidate"])

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

    def test_risk_reward_ratio_uses_actual_price_geometry(self):
        self.assertEqual(rt.TriggerEngine.risk_reward_ratio("BUY", 10.0, 9.97, 10.04), 1.33)
        self.assertEqual(rt.TriggerEngine.risk_reward_ratio("SELL", 10.0, 10.03, 9.96), 1.33)
        self.assertIsNone(rt.TriggerEngine.risk_reward_ratio("BUY", 10.0, 10.0, 10.04))

    def test_risk_price_rounding_preserves_low_price_precision(self):
        self.assertEqual(rt.TriggerEngine.round_risk_price(10.1234), 10.12)
        self.assertEqual(rt.TriggerEngine.round_risk_price(0.1354), 0.135)
        self.assertEqual(rt.TriggerEngine.round_risk_price(0.05456), 0.0546)

    def test_unconfirmed_watch_does_not_cool_down_later_confirmed_directional(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "trigger_overrides": {
                    "BUY:站上MA5": {"min_full_score": 0.6},
                },
            }
        )
        indicators = FakeIndicators(score=0.3)
        quote = {
            "price": 101,
            "volume": 700,
            "market": "US",
            "time": "2026-06-11 14:00:00",
            "change_pct": 0,
        }

        with patch.object(rt.time, "time", side_effect=[1_000_000, 1_000_060]):
            engine.check("AAPL", indicators, quote)
            indicators.score = 0.8
            engine.check("AAPL", indicators, quote)

        ma5_alerts = [item for item in engine.alerts if item["trigger"] == "站上MA5"]
        self.assertEqual(len(ma5_alerts), 2)
        self.assertEqual(ma5_alerts[0]["signal_type"], "WATCH")
        self.assertEqual(ma5_alerts[0]["suppressed_directional_reason"], "unconfirmed_directional")
        self.assertFalse(ma5_alerts[0]["execution_candidate"])
        self.assertEqual(ma5_alerts[1]["signal_type"], "BUY")
        self.assertTrue(ma5_alerts[1]["confirmed"])
        self.assertTrue(ma5_alerts[1]["execution_candidate"])

    def test_low_rr_directional_is_downgraded_to_watch(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "emission": {"emit_unconfirmed_directional_as_watch": False},
                "risk_model": {
                    "atr_stop_multiple": 3.0,
                    "atr_take_profit_multiple": 1.0,
                    "min_rr_ratio": 1.2,
                },
            }
        )
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None
        indicators.atr_14 = 2

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

        alert = [item for item in engine.alerts if item["trigger"] == "RSI超賣"][0]
        self.assertTrue(alert["confirmed"])
        self.assertEqual(alert["signal_type"], "WATCH")
        self.assertEqual(alert["candidate_signal_type"], "BUY")
        self.assertFalse(alert["execution_candidate"])
        self.assertFalse(alert["risk_geometry_valid"])
        self.assertEqual(alert["risk_geometry_reason"], "rr_ratio_below_minimum")
        self.assertEqual(alert["candidate_rr_ratio"], 0.33)
        self.assertEqual(alert["min_rr_ratio"], 1.2)
        self.assertIsNone(alert["stop_loss"])
        self.assertIsNone(alert["take_profit"])

    def test_rounded_price_rr_below_minimum_is_downgraded_to_watch(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "emission": {"emit_unconfirmed_directional_as_watch": False},
                "risk_model": {
                    "atr_stop_multiple": 2.0,
                    "atr_take_profit_multiple": 3.0,
                    "min_rr_ratio": 1.4,
                },
            }
        )
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None
        indicators.atr_14 = 0.014

        engine.check(
            "AAPL",
            indicators,
            {
                "price": 10,
                "volume": 0,
                "market": "US",
                "time": "2026-06-11 10:00:00",
                "change_pct": 0,
            },
        )

        alert = [item for item in engine.alerts if item["trigger"] == "RSI超賣"][0]
        self.assertTrue(alert["confirmed"])
        self.assertEqual(alert["candidate_entry_price"], 10)
        self.assertEqual(alert["candidate_stop_loss"], 9.97)
        self.assertEqual(alert["candidate_take_profit"], 10.04)
        self.assertEqual(alert["candidate_rr_ratio"], 1.33)
        self.assertEqual(alert["signal_type"], "WATCH")
        self.assertEqual(alert["risk_geometry_reason"], "rr_ratio_below_minimum")
        self.assertFalse(alert["execution_candidate"])

    def test_low_price_directional_uses_dynamic_precision_for_rr(self):
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
        indicators.atr_14 = 0.004

        engine.check(
            "00929",
            indicators,
            {
                "price": 0.135,
                "volume": 0,
                "market": "HK",
                "time": "2026-06-11 10:00:00",
                "change_pct": 0,
            },
        )

        alert = [item for item in engine.alerts if item["trigger"] == "RSI超賣"][0]
        self.assertEqual(alert["signal_type"], "BUY")
        self.assertTrue(alert["execution_candidate"])
        self.assertEqual(alert["candidate_entry_price"], 0.135)
        self.assertEqual(alert["candidate_stop_loss"], 0.127)
        self.assertEqual(alert["candidate_take_profit"], 0.147)
        self.assertEqual(alert["candidate_rr_ratio"], 1.5)
        self.assertEqual(alert["entry_price"], 0.135)
        self.assertEqual(alert["stop_loss"], 0.127)
        self.assertEqual(alert["take_profit"], 0.147)

    def test_nonfinite_score_and_atr_do_not_enter_alert_json(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=float("nan"))
        indicators.rsi_14 = 20
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None
        indicators.atr_14 = float("nan")

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

        alert = [item for item in engine.alerts if item["trigger"] == "RSI超賣"][0]
        self.assertEqual(alert["signal_type"], "WATCH")
        self.assertIsNone(alert["full_score"])
        self.assertIsNone(alert["atr"])
        self.assertFalse(alert["risk_geometry_valid"])
        self.assertEqual(alert["risk_geometry_reason"], "missing_or_invalid_atr")
        self.assertIsNone(alert["candidate_stop_loss"])
        self.assertIsNone(alert["candidate_take_profit"])
        json.dumps(alert, allow_nan=False)

    def test_missing_atr_directional_is_downgraded_without_fallback_risk_prices(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None
        indicators.atr_14 = None

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

        alert = [item for item in engine.alerts if item["trigger"] == "RSI超賣"][0]
        self.assertTrue(alert["confirmed"])
        self.assertEqual(alert["signal_type"], "WATCH")
        self.assertEqual(alert["candidate_signal_type"], "BUY")
        self.assertFalse(alert["execution_candidate"])
        self.assertEqual(alert["suppressed_directional_reason"], "missing_or_invalid_atr")
        self.assertFalse(alert["risk_geometry_valid"])
        self.assertEqual(alert["risk_geometry_reason"], "missing_or_invalid_atr")
        self.assertIsNone(alert["stop_loss"])
        self.assertIsNone(alert["take_profit"])
        self.assertIsNone(alert["candidate_stop_loss"])
        self.assertIsNone(alert["candidate_take_profit"])
        self.assertIsNone(alert["candidate_rr_ratio"])
        self.assertIsNone(alert["atr"])

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

    def test_invalid_trigger_cooldown_falls_back_to_global_cooldown(self):
        engine = rt.TriggerEngine(
            strategy_config={
                "signal_cooldown_seconds": 600,
                "trigger_overrides": {
                    "BUY:RSI超賣": {"cooldown_seconds": -5},
                },
            }
        )

        self.assertEqual(engine.trigger_cooldown_seconds("BUY", "RSI超賣"), 600)

    def test_signal_id_date_uses_quote_timestamp_when_available(self):
        engine = rt.TriggerEngine()
        indicators = FakeIndicators(score=0.8)
        indicators.rsi_14 = 20
        indicators.ma5 = None
        indicators.ma10 = None
        indicators.ma20 = None

        with patch.object(rt.time, "time", return_value=1_000_000), patch.object(
            rt, "datetime", wraps=rt.datetime
        ) as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 6, 12, 1, 0, 0)
            engine.check(
                "AAPL",
                indicators,
                {
                    "price": 100,
                    "volume": 0,
                    "market": "US",
                    "time": "20260611140000",
                    "change_pct": 0,
                },
            )

        alert = [item for item in engine.alerts if item["trigger"] == "RSI超賣"][0]
        self.assertTrue(alert["signal_id"].startswith("20260611:AAPL:RSI超賣:BUY:"))
        self.assertEqual(alert["generated_at"], "2026-06-12T01:00:00")

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
