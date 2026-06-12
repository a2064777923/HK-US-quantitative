import unittest

from scripts import rt_signal_outcome_report as report


def alert(
    signal_id,
    symbol,
    side,
    entry=100,
    trigger="MA",
    confirmed=True,
    strategy_config_id=None,
    watchlist_id=None,
):
    item = {
        "signal_id": signal_id,
        "symbol": symbol,
        "market": "US",
        "signal_type": side,
        "trigger": trigger,
        "confirmed": confirmed,
        "full_score": 0.7 if confirmed else 0.1,
        "entry_price": entry,
        "stop_loss": entry * 0.95 if side == "BUY" else entry * 1.05,
        "take_profit": entry * 1.10 if side == "BUY" else entry * 0.90,
        "rr_ratio": 2.0,
        "quote_time": "2026-06-10 14:30:00",
        "generated_at": "2026-06-11T02:30:00",
    }
    if strategy_config_id:
        item.update(
            {
                "strategy_config_id": strategy_config_id,
                "strategy_config_source": "file",
                "strategy_config_version": f"{strategy_config_id}-version",
            }
        )
    if watchlist_id:
        item.update(
            {
                "watchlist_id": watchlist_id,
                "watchlist_source": "file",
                "watchlist_count": 10,
            }
        )
    return item


class RtSignalOutcomeReportTests(unittest.TestCase):
    def test_buy_outcome_uses_future_daily_klines(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 120, "low": 90, "close": 101},
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
                {"date": "2026-06-12", "open": 110, "high": 113, "low": 108, "close": 111},
                {"date": "2026-06-15", "open": 111, "high": 115, "low": 107, "close": 112},
            ]
        }

        payload = report.build_report([alert("b1", "AAPL", "BUY")], klines_by_symbol=klines, horizons=(1, 3))
        item = payload["recent_evaluations"][0]

        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["available_future_days"], 3)
        self.assertEqual(item["outcomes"]["1d"]["signed_close_return_pct"], 10.0)
        self.assertTrue(item["outcomes"]["1d"]["target_hit"])
        self.assertFalse(item["outcomes"]["1d"]["stop_hit"])
        self.assertEqual(payload["schema"], "rt_signal_outcome_report_v1")
        self.assertEqual(payload["raw_alert_count"], 1)
        self.assertEqual(payload["directional_alert_count"], 1)
        self.assertEqual(payload["evaluated_signal_count"], 1)
        self.assertEqual(payload["resolved_signal_count"], 1)
        self.assertEqual(payload["pending_signal_count"], 0)
        self.assertEqual(payload["primary_horizon"], "1d")
        self.assertEqual(payload["primary_horizon_metric"]["resolved_count"], 1)
        self.assertEqual(payload["primary_horizon_metric"]["avg_max_favorable_pct"], 12.0)
        self.assertEqual(payload["primary_horizon_metric"]["avg_max_adverse_pct"], 1.0)
        self.assertEqual(payload["primary_horizon_metric"]["favorable_to_adverse_ratio"], 12.0)
        self.assertEqual(payload["status"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(payload["overall"]["horizons"]["1d"]["win_rate_pct"], 100.0)
        self.assertEqual(payload["evaluations"], payload["recent_evaluations"])
        self.assertEqual(payload["evaluations"][0]["signal_id"], "b1")

    def test_sell_outcome_inverts_return_direction(self):
        klines = {
            "TSLA": [
                {"date": "2026-06-10", "open": 100, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 99, "high": 100, "low": 89, "close": 90},
            ]
        }

        payload = report.build_report([alert("s1", "TSLA", "SELL")], klines_by_symbol=klines, horizons=(1,))
        outcome = payload["recent_evaluations"][0]["outcomes"]["1d"]

        self.assertEqual(outcome["signed_close_return_pct"], 10.0)
        self.assertTrue(outcome["target_hit"])
        self.assertFalse(outcome["stop_hit"])

    def test_pending_when_no_future_kline_exists(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
            ]
        }

        payload = report.build_report([alert("b1", "AAPL", "BUY")], klines_by_symbol=klines, horizons=(1,))
        item = payload["recent_evaluations"][0]

        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["reason"], "no_future_daily_klines")
        self.assertEqual(payload["overall"]["horizons"]["1d"]["pending_count"], 1)
        self.assertEqual(payload["status"], "PENDING")
        self.assertEqual(payload["resolved_signal_count"], 0)
        self.assertEqual(payload["pending_or_invalid_count"], 1)
        self.assertEqual(payload["pending_reasons"], {"no_future_daily_klines": 1})
        self.assertEqual(payload["primary_recommendation"], "outcome_sample_not_ready_keep_collecting_daily_klines")
        self.assertEqual(payload["recommendations"], ["outcome_sample_not_ready_keep_collecting_daily_klines"])

    def test_dedupes_signal_ids(self):
        alerts = [alert("b1", "AAPL", "BUY"), alert("b1", "AAPL", "BUY")]
        payload = report.build_report(alerts, klines_by_symbol={"AAPL": []}, horizons=(1,))

        self.assertEqual(payload["counts"]["directional_alert_count"], 2)
        self.assertEqual(payload["counts"]["evaluated_signal_count"], 1)
        self.assertEqual(payload["counts"]["duplicate_signal_count"], 1)
        self.assertEqual(payload["directional_alert_count"], 2)
        self.assertEqual(payload["evaluated_signal_count"], 1)
        self.assertEqual(payload["duplicate_signal_count"], 1)

    def test_evaluation_preserves_strategy_and_watchlist_metadata(self):
        item = alert("b1", "AAPL", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-a")
        evaluated = report.evaluate_alert(
            item,
            [
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
            horizons=(1,),
        )

        self.assertEqual(evaluated["strategy_config_id"], "cfg-a")
        self.assertEqual(evaluated["strategy_config_source"], "file")
        self.assertEqual(evaluated["strategy_config_version"], "cfg-a-version")
        self.assertEqual(evaluated["watchlist_id"], "wl-a")
        self.assertEqual(evaluated["watchlist_source"], "file")
        self.assertEqual(evaluated["watchlist_count"], 10)

    def test_groups_outcomes_by_strategy_config_and_watchlist(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
            "MSFT": [
                {"date": "2026-06-11", "open": 101, "high": 102, "low": 94, "close": 95},
            ],
        }
        alerts = [
            alert("b1", "AAPL", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-a"),
            alert("b2", "MSFT", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-b"),
        ]

        payload = report.build_report(alerts, klines_by_symbol=klines, horizons=(1,), sample_scope_mode="all")
        by_config = {row["key"]: row for row in payload["by_strategy_config"]}
        by_watchlist = {row["key"]: row for row in payload["by_watchlist"]}
        by_config_trigger = {row["key"]: row for row in payload["by_strategy_config_trigger"]}

        self.assertEqual(by_config["cfg-a"]["count"], 2)
        self.assertEqual(by_config["cfg-a"]["horizons"]["1d"]["resolved_count"], 2)
        self.assertEqual(by_config["cfg-a"]["horizons"]["1d"]["win_rate_pct"], 50.0)
        self.assertEqual(by_config["cfg-a"]["version_counts"], {"cfg-a-version": 2})
        self.assertEqual(by_watchlist["wl-a"]["count"], 1)
        self.assertEqual(by_watchlist["wl-b"]["count"], 1)
        self.assertEqual(by_config_trigger["cfg-a|BUY:MA"]["strategy_config_id"], "cfg-a")

    def test_missing_metadata_is_grouped_explicitly(self):
        payload = report.build_report(
            [alert("b1", "AAPL", "BUY")],
            klines_by_symbol={
                "AAPL": [
                    {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
                ]
            },
            horizons=(1,),
        )

        self.assertEqual(payload["counts"]["missing_watchlist_metadata_count"], 1)
        self.assertEqual(payload["counts"]["missing_strategy_config_metadata_count"], 1)
        self.assertEqual(payload["by_strategy_config"][0]["key"], "missing")
        self.assertEqual(payload["by_watchlist"][0]["key"], "missing")

    def test_current_sample_scope_excludes_legacy_missing_metadata(self):
        legacy = alert("old", "AAPL", "BUY")
        current = alert("new", "MSFT", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-a")
        klines = {
            "AAPL": [
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
            "MSFT": [
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
        }

        payload = report.build_report([legacy, current], klines_by_symbol=klines, horizons=(1,))

        self.assertEqual(payload["sample_scope"]["mode"], "latest_strategy_config_and_watchlist")
        self.assertEqual(payload["sample_scope"]["excluded_alert_count"], 1)
        self.assertEqual(payload["sample_scope"]["excluded_directional_alert_count"], 1)
        self.assertEqual(payload["raw_alert_count"], 1)
        self.assertEqual(payload["directional_alert_count"], 1)
        self.assertEqual(payload["evaluated_signal_count"], 1)
        self.assertEqual(payload["resolved_signal_count"], 1)
        self.assertEqual(payload["counts"]["missing_watchlist_metadata_count"], 0)
        self.assertEqual(payload["counts"]["missing_strategy_config_metadata_count"], 0)
        self.assertEqual(payload["by_strategy_config"][0]["key"], "cfg-a")
        self.assertEqual(payload["by_watchlist"][0]["key"], "wl-a")


if __name__ == "__main__":
    unittest.main()
