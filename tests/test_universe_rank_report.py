import unittest
from datetime import date, timedelta

from scripts import universe_rank_report as report


def kline_series(symbol, market="US", exchange="NASDAQ", start_close=100, amount=1_000_000, days=100, end=None):
    end = end or date(2026, 6, 12)
    rows = []
    for idx in range(days):
        day = end - timedelta(days=days - idx - 1)
        close = start_close + idx * 0.4
        rows.append(
            {
                "market": market,
                "exchange": exchange,
                "symbol": symbol,
                "date": day.isoformat(),
                "close": close,
                "high": close * 1.02,
                "low": close * 0.98,
                "volume": 100_000 + idx,
                "amount": amount,
            }
        )
    return rows


class UniverseRankReportTests(unittest.TestCase):
    def test_ranked_universe_prefers_fresh_liquid_buy_supported_symbol(self):
        fresh = kline_series("AAA", amount=2_000_000)
        stale = kline_series("BBB", amount=100_000, end=date(2026, 6, 9))
        rows = fresh + stale
        signals = [
            {"market": "US", "symbol": "AAA", "trade_date": "2026-06-12", "signal_side": "BUY", "fusion_score": 0.8},
            {"market": "US", "symbol": "BBB", "trade_date": "2026-06-12", "signal_side": "SELL", "fusion_score": -0.7},
        ]

        payload = report.build_report(rows, signals, top_us=5)
        market = payload["markets"]["US"]

        self.assertEqual(market["top_ranked"][0]["symbol"], "AAA")
        self.assertIn("AAA", market["selected_symbols"])
        self.assertNotIn("BBB", market["selected_symbols"])
        bbb = [item for item in market["top_ranked"] if item["symbol"] == "BBB"][0]
        self.assertIn("stale_latest_kline", bbb["blockers"])
        self.assertIn("latest_v4_sell_pressure", bbb["blockers"])

    def test_watchlist_candidate_is_manual_review_only(self):
        rows = kline_series("00700", market="HK", exchange="HKEX", start_close=50, amount=10_000_000)
        payload = report.build_report(
            rows,
            [{"market": "HK", "symbol": "00700", "trade_date": "2026-06-12", "signal_side": "BUY", "fusion_score": 0.6}],
            top_hk=1,
        )
        candidate = payload["watchlist_candidate"]

        self.assertEqual(candidate["schema"], "rt_signal_watchlist_v1")
        self.assertFalse(candidate["source"]["auto_applied"])
        self.assertTrue(candidate["source"]["manual_review_required"])
        self.assertEqual(candidate["markets"]["HK"]["symbols"], ["00700"])
        self.assertFalse(payload["source"]["auto_applies_watchlist"])

    def test_hk_symbol_above_sim_allocation_is_visible_but_not_candidate(self):
        rows = kline_series(
            "00700",
            market="HK",
            exchange="HKEX",
            start_close=460,
            amount=20_000_000,
        )
        payload = report.build_report(
            rows,
            [{"market": "HK", "symbol": "00700", "trade_date": "2026-06-12", "signal_side": "BUY", "fusion_score": 0.9}],
            top_hk=5,
        )
        market = payload["markets"]["HK"]
        ranked = market["top_ranked"][0]

        self.assertEqual(ranked["symbol"], "00700")
        self.assertEqual(ranked["sim_tradability"], "allocation_below_one_lot")
        self.assertEqual(ranked["lot_size"], 100)
        self.assertGreater(ranked["min_lot_notional_hkd"], ranked["sim_max_alloc_hkd"])
        self.assertIn("sim_allocation_below_one_lot", ranked["blockers"])
        self.assertEqual(market["ranked_symbol_count"], 1)
        self.assertEqual(market["ranked_symbols"][0]["symbol"], "00700")
        self.assertIn("sim_allocation_below_one_lot", market["ranked_symbols"][0]["blockers"])
        self.assertNotIn("00700", market["selected_symbols"])
        self.assertEqual(market["blocker_counts"]["sim_allocation_below_one_lot"], 1)
        self.assertEqual(market["sim_tradability_counts"]["allocation_below_one_lot"], 1)
        self.assertIn(
            "HK:sim_allocation_below_one_lot_review_watchlist_or_position_size",
            payload["recommendations"],
        )

    def test_low_candidate_coverage_recommends_review(self):
        short = kline_series("SHORT", days=20, amount=100)
        payload = report.build_report(short, [], top_us=5)

        self.assertIn(
            "US:candidate_coverage_below_25pct_review_data_liquidity_filters",
            payload["recommendations"],
        )
        self.assertEqual(payload["markets"]["US"]["candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
