import unittest
from datetime import datetime, timedelta

from scripts import market_index_context_producer as producer


NOW = datetime(2026, 6, 12, 10, 30, 0)


def series(start=100.0, days=60, step=1.0):
    base = datetime(2026, 1, 1, 16, 0, 0)
    return [
        {
            "date": (base + timedelta(days=idx)).date().isoformat(),
            "open": start + idx * step - 0.5,
            "high": start + idx * step + 1.0,
            "low": start + idx * step - 1.0,
            "close": start + idx * step,
            "volume": 1000 + idx,
        }
        for idx in range(days)
    ]


class MarketIndexContextProducerTests(unittest.TestCase):
    def test_build_snapshot_creates_read_only_index_history(self):
        payload = producer.build_snapshot(fetch_chart=lambda symbol: series(), now=NOW)

        self.assertEqual(payload["schema"], "market_index_context_producer_v1")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["writes_database"])
        self.assertEqual(payload["source"]["provider_grade"], "public_fallback")
        self.assertEqual(len(payload["indexes"]), 6)
        self.assertGreaterEqual(payload["indexes"][0]["history_days"], 20)
        self.assertEqual(len(payload["indexes"][0]["series"]), 60)

    def test_fetch_failures_are_warnings_not_exceptions(self):
        def fetch(symbol):
            if symbol == "^HSI":
                raise ValueError("provider unavailable")
            return series()

        payload = producer.build_snapshot(fetch_chart=fetch, now=NOW)

        self.assertEqual(len(payload["indexes"]), 5)
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertIn("fetch_failed:^HSI", payload["warnings"][0])


if __name__ == "__main__":
    unittest.main()
