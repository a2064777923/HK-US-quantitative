import unittest
from datetime import datetime

from scripts import market_sentiment_producer as producer
from scripts import market_sentiment_report as report


NOW = datetime(2026, 6, 12, 10, 30, 0)


def ts(day):
    return int(datetime(2026, 6, day, 16, 0, 0).timestamp())


class MarketSentimentProducerTests(unittest.TestCase):
    def test_build_snapshot_creates_read_only_indicators_consumed_by_report(self):
        values = {
            "^VIX": [(ts(11), 22.0), (ts(12), 18.0)],
            "SPY": [(ts(11), 100.0), (ts(12), 102.0)],
            "QQQ": [(ts(11), 100.0), (ts(12), 103.0)],
            "^HSI": [(ts(11), 20000.0), (ts(12), 20200.0)],
            "2800.HK": [(ts(11), 20.0), (ts(12), 20.2)],
        }

        payload = producer.build_snapshot(fetch_chart=lambda symbol: values[symbol], now=NOW)
        sentiment = report.build_report(payload["indicators"], now=NOW)

        self.assertEqual(payload["schema"], "market_sentiment_producer_v1")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_alert_queue"])
        self.assertEqual(len(payload["indicators"]), 5)
        self.assertEqual(sentiment["status"], "OK")
        self.assertGreater(sentiment["summary"]["overall_score"], 0)

    def test_vix_spike_and_index_drop_produces_risk_status(self):
        values = {
            "^VIX": [(ts(11), 18.0), (ts(12), 32.0)],
            "SPY": [(ts(11), 100.0), (ts(12), 97.0)],
            "QQQ": [(ts(11), 100.0), (ts(12), 96.0)],
            "^HSI": [(ts(11), 20000.0), (ts(12), 19200.0)],
            "2800.HK": [(ts(11), 20.0), (ts(12), 19.3)],
        }

        payload = producer.build_snapshot(fetch_chart=lambda symbol: values[symbol], now=NOW)
        sentiment = report.build_report(payload["indicators"], now=NOW)

        self.assertEqual(sentiment["status"], "RISK")
        self.assertGreaterEqual(sentiment["summary"]["risk_off_count"], 3)
        vix = [item for item in payload["indicators"] if item["name"] == "VIX"][0]
        self.assertEqual(vix["direction"], "risk_off")
        self.assertLess(vix["score"], 0)

    def test_fetch_failures_are_warnings_not_exceptions(self):
        def fetch(symbol):
            if symbol == "^VIX":
                raise ValueError("provider unavailable")
            return [(ts(11), 100.0), (ts(12), 101.0)]

        payload = producer.build_snapshot(fetch_chart=fetch, now=NOW)

        self.assertEqual(len(payload["indicators"]), 4)
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertIn("fetch_failed:^VIX", payload["warnings"][0])


if __name__ == "__main__":
    unittest.main()
