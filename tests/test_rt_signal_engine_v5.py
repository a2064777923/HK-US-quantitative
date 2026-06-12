import json
import tempfile
import unittest
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

    def get_score(self):
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


if __name__ == "__main__":
    unittest.main()
