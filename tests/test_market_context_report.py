import unittest
from datetime import date, timedelta

from scripts import market_context_report as report


def kline_rows(market, symbol, closes):
    start = date(2026, 1, 1)
    rows = []
    for idx, close in enumerate(closes):
        rows.append(
            {
                "market": market,
                "symbol": symbol,
                "date": (start + timedelta(days=idx)).isoformat(),
                "close": close,
            }
        )
    return rows


class MarketContextReportTests(unittest.TestCase):
    def test_risk_on_when_breadth_and_returns_are_positive(self):
        rows = []
        rows.extend(kline_rows("US", "AAPL", list(range(100, 160))))
        rows.extend(kline_rows("US", "MSFT", list(range(50, 110))))
        signals = [
            {"market": "US", "symbol": "AAPL", "trade_date": "2026-03-01", "signal_side": "BUY", "fusion_score": 0.8},
            {"market": "US", "symbol": "MSFT", "trade_date": "2026-03-01", "signal_side": "HOLD", "fusion_score": 0.5},
        ]

        payload = report.build_report(rows, signals)
        us = payload["markets"]["US"]

        self.assertEqual(us["regime"], "risk_on")
        self.assertEqual(us["breadth"]["above_ma20_pct"], 100.0)
        self.assertEqual(us["v4_signal_summary"]["by_side"]["BUY"], 1)
        self.assertIn("market_context_supports_normal_review_discipline", payload["recommendations"])

    def test_risk_off_when_breadth_is_weak(self):
        rows = []
        rows.extend(kline_rows("HK", "00700", list(range(160, 100, -1))))
        rows.extend(kline_rows("HK", "09988", list(range(110, 50, -1))))
        signals = [
            {"market": "HK", "symbol": "00700", "trade_date": "2026-03-01", "signal_side": "BUY", "fusion_score": 0.75},
        ]

        payload = report.build_report(rows, signals)
        hk = payload["markets"]["HK"]

        self.assertEqual(hk["regime"], "risk_off")
        self.assertEqual(hk["breadth"]["above_ma20_pct"], 0.0)
        self.assertIn("tighten_new_buy_approval_or_reduce_size", hk["notes"])
        self.assertIn("HK:risk_off_require_reduced_or_rejected_new_buys", payload["recommendations"])
        self.assertIn("HK:buy_signals_against_weak_breadth", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
