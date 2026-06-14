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


def index_rows(market, symbol, closes, name=None):
    start = date(2026, 1, 1)
    rows = []
    for idx, close in enumerate(closes):
        rows.append(
            {
                "market": market,
                "symbol": symbol,
                "name": name or symbol,
                "date": (start + timedelta(days=idx)).isoformat(),
                "close": close,
                "source_table": "index_ohlcv_daily",
                "source": "unit_test",
            }
        )
    return rows


def index_snapshot(market, symbol, closes, name=None):
    return {
        "schema": "market_index_context_producer_v1",
        "generated_at": "2026-03-01T10:00:00",
        "source": {"provider_grade": "public_fallback"},
        "warnings": [],
        "indexes": [
            {
                "symbol": symbol,
                "provider_symbol": symbol,
                "name": name or symbol,
                "market": market,
                "source": "yahoo_chart_snapshot",
                "series": [
                    {
                        "date": (date(2026, 1, 1) + timedelta(days=idx)).isoformat(),
                        "close": close,
                    }
                    for idx, close in enumerate(closes)
                ],
            }
        ],
    }


def sentiment(score=0.7, direction="risk_on", market="US"):
    return {
        "schema": "market_sentiment_report_v1",
        "status": "OK",
        "summary": {
            "fresh_indicator_count": 2,
            "overall_score": score,
            "market_scores": {market: score, "GLOBAL": score},
            "risk_off_count": 0 if direction != "risk_off" else 2,
            "risk_on_count": 2 if direction == "risk_on" else 0,
        },
        "indicators": [
            {
                "id": f"{market}-index",
                "name": f"{market} index daily return",
                "indicator_type": "risk_appetite",
                "source": "yahoo_chart",
                "markets": [market],
                "direction": direction,
                "score": score,
                "value": 1.5 if direction == "risk_on" else -1.5,
                "change": 1.5 if direction == "risk_on" else -1.5,
                "unit": "pct",
                "summary": "index proxy moved with risk appetite",
                "stale": False,
            }
        ],
    }


class MarketContextReportTests(unittest.TestCase):
    def test_risk_on_when_breadth_and_returns_are_positive(self):
        rows = []
        rows.extend(kline_rows("US", "AAPL", list(range(100, 160))))
        rows.extend(kline_rows("US", "MSFT", list(range(50, 110))))
        signals = [
            {"market": "US", "symbol": "AAPL", "trade_date": "2026-03-01", "signal_side": "BUY", "fusion_score": 0.8},
            {"market": "US", "symbol": "MSFT", "trade_date": "2026-03-01", "signal_side": "HOLD", "fusion_score": 0.5},
        ]

        payload = report.build_report(rows, signals, sentiment_payload=sentiment(score=0.6, direction="risk_on"))
        us = payload["markets"]["US"]

        self.assertEqual(us["regime"], "risk_on")
        self.assertEqual(us["breadth"]["above_ma20_pct"], 100.0)
        self.assertEqual(us["v4_signal_summary"]["by_side"]["BUY"], 1)
        self.assertEqual(us["cross_market"]["schema"], "market_context_cross_market_v1")
        self.assertEqual(us["cross_market"]["sentiment_direction"], "risk_on")
        self.assertEqual(us["cross_market"]["alignment"], "confirms_breadth")
        self.assertIn("real_index_or_volatility_sentiment_confirms_breadth_proxy", us["cross_market"]["notes"])
        self.assertIn("market_context_supports_normal_review_discipline", payload["recommendations"])

    def test_risk_off_when_breadth_is_weak(self):
        rows = []
        rows.extend(kline_rows("HK", "00700", list(range(160, 100, -1))))
        rows.extend(kline_rows("HK", "09988", list(range(110, 50, -1))))
        signals = [
            {"market": "HK", "symbol": "00700", "trade_date": "2026-03-01", "signal_side": "BUY", "fusion_score": 0.75},
        ]

        payload = report.build_report(rows, signals, sentiment_payload=sentiment(score=-0.7, direction="risk_off", market="HK"))
        hk = payload["markets"]["HK"]

        self.assertEqual(hk["regime"], "risk_off")
        self.assertEqual(hk["breadth"]["above_ma20_pct"], 0.0)
        self.assertEqual(hk["cross_market"]["sentiment_direction"], "risk_off")
        self.assertEqual(hk["cross_market"]["alignment"], "confirms_breadth")
        self.assertIn("tighten_new_buy_approval_or_reduce_size", hk["notes"])
        self.assertIn("HK:risk_off_require_reduced_or_rejected_new_buys", payload["recommendations"])
        self.assertIn("HK:buy_signals_against_weak_breadth", payload["recommendations"])

    def test_cross_market_flags_real_index_sentiment_conflict_with_breadth_proxy(self):
        rows = []
        rows.extend(kline_rows("HK", "00700", list(range(160, 100, -1))))
        rows.extend(kline_rows("HK", "09988", list(range(110, 50, -1))))

        payload = report.build_report(
            rows,
            [{"market": "HK", "symbol": "00700", "trade_date": "2026-03-01", "signal_side": "BUY", "fusion_score": 0.75}],
            sentiment_payload=sentiment(score=0.8, direction="risk_on", market="HK"),
        )
        hk = payload["markets"]["HK"]

        self.assertEqual(hk["regime"], "risk_off")
        self.assertEqual(hk["cross_market"]["sentiment_direction"], "risk_on")
        self.assertEqual(hk["cross_market"]["alignment"], "conflicts_with_breadth")
        self.assertIn("real_index_or_volatility_sentiment_conflicts_with_breadth_proxy", hk["cross_market"]["notes"])
        self.assertEqual(hk["cross_market"]["indicators"][0]["source"], "yahoo_chart")

    def test_cross_market_incomplete_when_sentiment_missing(self):
        rows = kline_rows("US", "AAPL", list(range(100, 160)))

        payload = report.build_report(rows, [], sentiment_payload={"status": "missing", "path": "/tmp/missing"})
        us = payload["markets"]["US"]

        self.assertEqual(us["cross_market"]["status"], "incomplete")
        self.assertEqual(us["cross_market"]["alignment"], "incomplete")
        self.assertIn("market_sentiment_missing_for_cross_market_confirmation", us["cross_market"]["notes"])

    def test_native_index_context_confirms_breadth_when_real_index_is_positive(self):
        rows = []
        rows.extend(kline_rows("US", "AAPL", list(range(100, 160))))
        rows.extend(kline_rows("US", "MSFT", list(range(50, 110))))
        indexes = index_rows("US", "^GSPC", list(range(4000, 4060)), name="S&P 500")

        payload = report.build_report(
            rows,
            [],
            sentiment_payload=sentiment(score=0.6, direction="risk_on"),
            index_rows=indexes,
        )
        us = payload["markets"]["US"]

        self.assertEqual(us["regime"], "risk_on")
        self.assertEqual(us["native_index_context"]["schema"], "market_context_native_index_v1")
        self.assertEqual(us["native_index_context"]["status"], "OK")
        self.assertEqual(us["native_index_context"]["index_direction"], "risk_on")
        self.assertEqual(us["native_index_context"]["alignment"], "confirms_breadth")
        self.assertEqual(us["native_index_context"]["primary_index"]["symbol"], "^GSPC")

    def test_native_index_context_flags_conflict_with_breadth_proxy(self):
        rows = []
        rows.extend(kline_rows("HK", "00700", list(range(160, 100, -1))))
        rows.extend(kline_rows("HK", "09988", list(range(110, 50, -1))))
        indexes = index_rows("HK", "^HSI", list(range(18000, 18060)), name="Hang Seng Index")

        payload = report.build_report(
            rows,
            [],
            sentiment_payload=sentiment(score=-0.7, direction="risk_off", market="HK"),
            index_rows=indexes,
        )
        hk = payload["markets"]["HK"]

        self.assertEqual(hk["regime"], "risk_off")
        self.assertEqual(hk["native_index_context"]["status"], "OK")
        self.assertEqual(hk["native_index_context"]["index_direction"], "risk_on")
        self.assertEqual(hk["native_index_context"]["alignment"], "conflicts_with_breadth")
        self.assertIn("native_index_conflicts_with_stock_pool_breadth", hk["native_index_context"]["notes"])

    def test_native_index_context_is_incomplete_when_native_index_rows_missing(self):
        rows = kline_rows("US", "AAPL", list(range(100, 160)))

        payload = report.build_report(rows, [], sentiment_payload=sentiment(), index_rows=[])
        us = payload["markets"]["US"]

        self.assertEqual(us["native_index_context"]["status"], "MISSING")
        self.assertEqual(us["native_index_context"]["alignment"], "incomplete")
        self.assertIn("native_index_series_missing", us["native_index_context"]["notes"])
        self.assertIn("native_index_context_missing:US", payload["warnings"])

    def test_native_index_context_can_use_public_snapshot_input(self):
        rows = []
        rows.extend(kline_rows("US", "AAPL", list(range(100, 160))))
        rows.extend(kline_rows("US", "MSFT", list(range(50, 110))))
        snapshot = index_snapshot("US", "^GSPC", list(range(4000, 4060)), name="S&P 500")

        payload = report.build_report(
            rows,
            [],
            sentiment_payload=sentiment(score=0.6, direction="risk_on"),
            index_rows=[],
            index_snapshot_payload=snapshot,
        )
        us = payload["markets"]["US"]

        self.assertEqual(us["native_index_context"]["status"], "OK")
        self.assertEqual(us["native_index_context"]["alignment"], "confirms_breadth")
        self.assertEqual(us["native_index_context"]["primary_index"]["source_table"], "market_index_context_inputs")
        self.assertEqual(us["native_index_context"]["primary_index"]["provider_grade"], "public_fallback")


if __name__ == "__main__":
    unittest.main()
