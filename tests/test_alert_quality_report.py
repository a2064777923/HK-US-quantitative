import unittest

from scripts import alert_quality_report as report


def alert(signal_id, symbol, side, price, trigger="MA", confirmed=True, score=0.7, rr=2.0):
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "signal_type": side,
        "trigger": trigger,
        "confirmed": confirmed,
        "full_score": score,
        "price": price,
        "entry_price": price,
        "stop_loss": price * 0.95 if side == "BUY" else price * 1.05,
        "take_profit": price * 1.10 if side == "BUY" else price * 0.90,
        "rr_ratio": rr,
        "generated_at": "2026-06-12T10:00:00",
        "market": "US",
        "watchlist_id": "watchlist-test",
        "watchlist_source": "file",
        "watchlist_count": 10,
        "strategy_config_id": "strategy-test",
        "strategy_config_source": "file",
        "strategy_config_version": "unit-test",
    }


def watch(signal_id, symbol, price):
    item = alert(signal_id, symbol, "WATCH", price, trigger="volume", rr=None)
    item["stop_loss"] = None
    item["take_profit"] = None
    item["rr_ratio"] = None
    return item


class AlertQualityReportTests(unittest.TestCase):
    def test_signed_move_uses_direction(self):
        buy = alert("b1", "AAPL", "BUY", 100)
        sell = alert("s1", "TSLA", "SELL", 100)

        self.assertAlmostEqual(report.signed_move_pct(buy, 110), 10)
        self.assertAlmostEqual(report.signed_move_pct(sell, 90), 10)
        self.assertAlmostEqual(report.signed_move_pct(sell, 110), -10)

    def test_build_report_counts_watch_but_summarizes_directional_quality(self):
        alerts = [
            alert("b1", "AAPL", "BUY", 100, trigger="breakout"),
            watch("w1", "AAPL", 103),
            alert("s1", "TSLA", "SELL", 200, trigger="breakdown"),
            alert("m1", "AAPL", "BUY", 110, trigger="breakout", confirmed=False, score=0.1),
            alert("s2", "TSLA", "SELL", 180, trigger="breakdown"),
        ]
        packet = {
            "generated_at": "2026-06-12T10:05:00",
            "review_items": [
                {"signal_id": "b1", "eligible_for_approval": True, "blocking_reasons": [], "intake": {"status": "dry_run"}},
                {
                    "signal_id": "m1",
                    "eligible_for_approval": False,
                    "blocking_reasons": ["buy_score_below_threshold"],
                    "intake": {"status": "rejected"},
                },
            ],
        }

        payload = report.build_report(alerts, packet)

        self.assertEqual(payload["schema"], "alert_quality_report_v1")
        self.assertIn(payload["status"], ("OK", "WARN"))
        self.assertEqual(payload["total_alert_count"], 5)
        self.assertEqual(payload["directional_alert_count"], 4)
        self.assertEqual(payload["watch_alert_count"], 1)
        self.assertEqual(payload["confirmed_directional_count"], 3)
        self.assertEqual(payload["packet_review_item_count"], 2)
        self.assertEqual(payload["packet_eligible_count"], 1)
        self.assertEqual(payload["symbol_conflict_count"], 0)
        self.assertTrue(payload["primary_recommendation"])
        self.assertEqual(payload["counts"]["total_alerts"], 5)
        self.assertEqual(payload["counts"]["directional_alerts"], 4)
        self.assertEqual(payload["counts"]["by_signal_type"]["WATCH"], 1)
        self.assertEqual(payload["counts"]["by_watchlist_source"]["file"], 5)
        self.assertEqual(payload["counts"]["by_strategy_config_source"]["file"], 5)
        self.assertEqual(payload["directional_quality"]["missing_watchlist_metadata_count"], 0)
        self.assertEqual(payload["directional_quality"]["missing_strategy_config_metadata_count"], 0)
        self.assertEqual(payload["packet_review"]["review_item_count"], 2)
        self.assertEqual(payload["packet_review"]["eligible_count"], 1)
        self.assertEqual(payload["packet_review"]["blocking_reasons"]["buy_score_below_threshold"], 1)
        self.assertEqual(len(payload["symbol_conflicts"]), 0)

        breakout = [row for row in payload["trigger_quality"] if row["trigger"] == "breakout"][0]
        self.assertEqual(breakout["count"], 2)
        self.assertEqual(breakout["packet_eligible_count"], 1)
        self.assertEqual(breakout["marked_count"], 1)
        self.assertEqual(breakout["avg_signed_move_pct"], 10.0)

    def test_symbol_conflicts_detects_buy_and_sell_same_symbol(self):
        payload = report.build_report(
            [
                alert("b1", "AAPL", "BUY", 100),
                alert("s1", "AAPL", "SELL", 99),
            ],
            {},
        )

        self.assertEqual(payload["symbol_conflicts"], [{"symbol": "AAPL", "buy_count": 1, "sell_count": 1}])
        self.assertEqual(payload["symbol_conflict_count"], 1)

    def test_missing_watchlist_metadata_is_reported(self):
        item = alert("b1", "AAPL", "BUY", 100)
        item.pop("watchlist_id")
        item.pop("watchlist_source")
        item.pop("strategy_config_id")
        item.pop("strategy_config_source")

        payload = report.build_report([item], {})

        self.assertEqual(payload["directional_quality"]["missing_watchlist_metadata_count"], 1)
        self.assertEqual(payload["directional_quality"]["missing_strategy_config_metadata_count"], 1)
        self.assertIn(
            "directional_alerts_missing_watchlist_metadata_restart_v5_with_configured_watchlist",
            payload["recommendations"],
        )
        self.assertIn(
            "directional_alerts_missing_strategy_config_metadata_restart_v5_with_configured_strategy",
            payload["recommendations"],
        )

    def test_current_sample_scope_excludes_legacy_missing_metadata(self):
        legacy = alert("old", "AAPL", "BUY", 100)
        for key in ("watchlist_id", "watchlist_source", "strategy_config_id", "strategy_config_source"):
            legacy.pop(key)
        current = alert("new", "MSFT", "BUY", 100)

        payload = report.build_report([legacy, current], {})

        self.assertEqual(payload["sample_scope"]["mode"], "latest_strategy_config_and_watchlist")
        self.assertEqual(payload["sample_scope"]["excluded_alert_count"], 1)
        self.assertEqual(payload["sample_scope"]["excluded_directional_alert_count"], 1)
        self.assertEqual(payload["total_alert_count"], 1)
        self.assertEqual(payload["directional_alert_count"], 1)
        self.assertEqual(payload["directional_quality"]["missing_watchlist_metadata_count"], 0)
        self.assertEqual(payload["directional_quality"]["missing_strategy_config_metadata_count"], 0)
        self.assertNotIn(
            "directional_alerts_missing_watchlist_metadata_restart_v5_with_configured_watchlist",
            payload["recommendations"],
        )
        self.assertNotIn(
            "directional_alerts_missing_strategy_config_metadata_restart_v5_with_configured_strategy",
            payload["recommendations"],
        )


if __name__ == "__main__":
    unittest.main()
