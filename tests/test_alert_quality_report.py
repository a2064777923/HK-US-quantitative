import unittest
from unittest.mock import patch

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


def stale_alert(signal_id, symbol, side, price, trigger="MA", confirmed=True, score=0.7, rr=2.0):
    item = alert(signal_id, symbol, side, price, trigger=trigger, confirmed=confirmed, score=score, rr=rr)
    item["generated_at"] = "2020-01-01T10:00:00"
    item["execution_candidate"] = True
    return item


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

    def test_stale_alerts_do_not_trigger_strategy_threshold_warning(self):
        payload = report.build_report(
            [
                stale_alert("s1", "TSLA", "SELL", 100, trigger="跌破MA5", score=-0.8),
                stale_alert("s2", "AAPL", "BUY", 100, trigger="急漲", score=0.9),
            ],
            {},
        )

        self.assertEqual(payload["directional_quality"]["live_validation_pass_count"], 0)
        self.assertEqual(payload["directional_quality"]["validation_pass_count"], 2)
        self.assertEqual(payload["directional_quality"]["live_validation_fail_reasons"], {"alert_too_old": 2})
        self.assertEqual(payload["directional_quality"]["quality_validation_fail_reasons"], {})
        self.assertNotIn(
            "directional_validation_pass_rate_below_50pct_review_v5_confirmation_thresholds",
            payload["recommendations"],
        )
        self.assertIn(
            "live_intake_validation_low_due_to_stale_alerts_do_not_change_strategy_thresholds",
            payload["recommendations"],
        )

    def test_watch_dominated_diagnostic_queue_is_note_not_recommendation_when_packet_filters_watch(self):
        packet = {
            "generated_at": "2026-06-12T10:05:00",
            "review_items": [
                {"signal_id": "b1", "signal_type": "BUY", "eligible_for_approval": True, "intake": {"status": "dry_run"}},
            ],
        }
        payload = report.build_report(
            [
                alert("b1", "AAPL", "BUY", 100, trigger="breakout"),
                watch("w1", "AAPL", 101),
                watch("w2", "MSFT", 102),
                watch("w3", "TSLA", 103),
            ],
            packet,
        )

        self.assertNotIn(
            "watch_alerts_dominate_queue_review_packet_should_filter_directional_alerts",
            payload["recommendations"],
        )
        self.assertIn("watch_alerts_dominate_diagnostic_queue_but_packet_filters_watch", payload["diagnostic_notes"])
        self.assertEqual(payload["packet_review"]["watch_review_item_count"], 0)

    def test_watch_quality_splits_pure_watch_from_downgraded_directional_candidates(self):
        downgraded = []
        for idx in range(10):
            item = watch(f"dw{idx}", f"SYM{idx}", 100 + idx)
            item["candidate_signal_type"] = "BUY"
            item["suppressed_directional_reason"] = "unconfirmed_directional"
            downgraded.append(item)
        pure = watch("pure", "VOL", 50)
        pure["candidate_signal_type"] = "WATCH"

        payload = report.build_report(downgraded + [pure], {})

        self.assertEqual(payload["watch_quality"]["watch_count"], 11)
        self.assertEqual(payload["watch_quality"]["downgraded_directional_watch_count"], 10)
        self.assertEqual(payload["watch_quality"]["pure_watch_count"], 1)
        self.assertEqual(payload["watch_quality"]["downgraded_directional_watch_rate_pct"], 90.91)
        self.assertEqual(payload["watch_quality"]["downgrade_reasons"], {"unconfirmed_directional": 10})
        self.assertIn("watch_alerts_include_downgraded_directional_candidates", payload["diagnostic_notes"])
        self.assertIn(
            "downgraded_directional_watch_alerts_dominate_review_v5_emission_policy",
            payload["recommendations"],
        )

    def test_watch_quality_includes_summary_only_suppressed_watch_count(self):
        payload = report.build_report(
            [watch("w1", "AAPL", 100)],
            {},
            watch_summary={
                "schema": "rt_signal_watch_summary_v1",
                "generated_at": "2026-06-12T10:05:00",
                "suppressed_watch_count": 32,
                "counts_by_trigger": {"成交量異動": 32},
            },
        )
        text = report.build_text_report(payload)

        self.assertEqual(payload["watch_quality"]["summary_only_suppressed_count"], 32)
        self.assertEqual(payload["watch_quality"]["summary_only_counts_by_trigger"], {"成交量異動": 32})
        self.assertIn("summary_only=32", text)

    def test_watch_packet_review_items_still_trigger_filter_recommendation(self):
        packet = {
            "generated_at": "2026-06-12T10:05:00",
            "review_items": [
                {"signal_id": "w1", "signal_type": "WATCH", "eligible_for_approval": False, "intake": {"status": "rejected"}},
            ],
        }
        payload = report.build_report(
            [
                alert("b1", "AAPL", "BUY", 100, trigger="breakout"),
                watch("w1", "AAPL", 101),
                watch("w2", "MSFT", 102),
                watch("w3", "TSLA", 103),
            ],
            packet,
        )

        self.assertIn(
            "watch_alerts_dominate_queue_review_packet_should_filter_directional_alerts",
            payload["recommendations"],
        )
        self.assertIn("watch_alerts_dominate_packet_review_items", payload["diagnostic_notes"])
        self.assertEqual(payload["packet_review"]["watch_review_item_count"], 1)

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

        with patch.object(report.rt_runtime_scope, "current_runtime_sample_scope", return_value={"mode": "runtime_scope_unavailable"}):
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

    def test_current_sample_scope_uses_directional_candidates_not_only_emitted_directionals(self):
        legacy = alert("old", "AAPL", "BUY", 100)
        current = watch("new", "MSFT", 100)
        current["candidate_signal_type"] = "BUY"
        current["watchlist_id"] = "new-watchlist"
        current["strategy_config_id"] = "new-strategy"

        with patch.object(report.rt_runtime_scope, "current_runtime_sample_scope", return_value={"mode": "runtime_scope_unavailable"}):
            payload = report.build_report([legacy, current], {})

        self.assertEqual(payload["sample_scope"]["mode"], "latest_strategy_config_and_watchlist")
        self.assertEqual(payload["sample_scope"]["strategy_config_id"], "new-strategy")
        self.assertEqual(payload["sample_scope"]["watchlist_id"], "new-watchlist")
        self.assertEqual(payload["sample_scope"]["excluded_alert_count"], 1)
        self.assertEqual(payload["sample_scope"]["directional_candidate_count"], 1)
        self.assertEqual(payload["total_alert_count"], 1)
        self.assertEqual(payload["directional_alert_count"], 0)

    def test_current_sample_scope_prefers_runtime_strategy_watchlist_over_latest_old_alert(self):
        old = alert("old", "AAPL", "BUY", 100)
        old["strategy_config_id"] = "cfg-old"
        old["watchlist_id"] = "wl-current"
        runtime_watch = watch("runtime-watch", "MSFT", 100)
        runtime_watch["candidate_signal_type"] = "BUY"
        runtime_watch["strategy_config_id"] = "cfg-runtime"
        runtime_watch["watchlist_id"] = "wl-current"
        runtime_scope = {
            "mode": "runtime_strategy_config_and_watchlist",
            "strategy_config_id": "cfg-runtime",
            "watchlist_id": "wl-current",
            "strategy_config_version": "v5.5-test",
        }

        with patch.object(report.rt_runtime_scope, "current_runtime_sample_scope", return_value=runtime_scope):
            payload = report.build_report([runtime_watch, old], {})

        self.assertEqual(payload["sample_scope"]["mode"], "runtime_strategy_config_and_watchlist")
        self.assertEqual(payload["sample_scope"]["strategy_config_id"], "cfg-runtime")
        self.assertEqual(payload["sample_scope"]["strategy_config_version"], "v5.5-test")
        self.assertEqual(payload["sample_scope"]["excluded_alert_count"], 1)
        self.assertEqual(payload["total_alert_count"], 1)


if __name__ == "__main__":
    unittest.main()
