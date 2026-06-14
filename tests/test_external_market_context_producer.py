import unittest
from datetime import datetime

from scripts import external_market_context_producer as producer
from scripts import external_market_context_report as report


NOW = datetime(2026, 6, 12, 10, 30, 0)


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Fed signals dovish rate cut path as Nasdaq rallies</title>
      <link>https://example.com/fed-rally</link>
      <description>Risk-on tone improves after cooling inflation data.</description>
      <pubDate>Fri, 12 Jun 2026 10:00:00 +0800</pubDate>
    </item>
    <item>
      <title>Tesla tumbles as tariff conflict hits China demand</title>
      <link>https://example.com/tesla-tariff</link>
      <description>Investors turn risk-off after new sanction headlines.</description>
      <pubDate>Fri, 12 Jun 2026 10:05:00 +0800</pubDate>
    </item>
  </channel>
</rss>
"""


class ExternalMarketContextProducerTests(unittest.TestCase):
    def test_build_snapshot_creates_context_consumed_by_report(self):
        feed = {
            "source": "unit_feed",
            "url": "https://example.com/rss",
            "markets": ["US"],
            "category": "macro",
        }

        payload = producer.build_snapshot(
            fetch_feed_func=lambda _url: RSS,
            feeds=[feed],
            now=NOW,
            limit_per_feed=10,
        )
        context = report.build_report(payload["items"], now=NOW)

        self.assertEqual(payload["schema"], "external_market_context_producer_v1")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_alert_queue"])
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(context["status"], "RISK")
        self.assertEqual(context["summary"]["fresh_item_count"], 2)
        self.assertGreaterEqual(context["summary"]["high_impact_count"], 2)

    def test_negative_company_headline_matches_symbol_and_risk(self):
        feed = {"source": "unit_feed", "url": "https://example.com/rss", "markets": ["US"], "category": "news"}

        payload = producer.build_snapshot(fetch_feed_func=lambda _url: RSS, feeds=[feed], now=NOW)
        tesla = [item for item in payload["items"] if "Tesla" in item["title"]][0]

        self.assertEqual(tesla["sentiment"], "negative")
        self.assertIn("TSLA", tesla["symbols"])
        self.assertIn("US", tesla["markets"])
        self.assertGreaterEqual(tesla["impact_score"], 0.7)

    def test_fetch_failure_is_warning_not_exception(self):
        def fetch(_url):
            raise ValueError("provider unavailable")

        payload = producer.build_snapshot(
            fetch_feed_func=fetch,
            feeds=[{"source": "broken", "url": "https://example.com/rss", "markets": ["GLOBAL"], "category": "news"}],
            now=NOW,
        )

        self.assertEqual(payload["items"], [])
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertIn("fetch_failed:broken", payload["warnings"][0])

    def test_infohub_items_are_normalized_without_replacing_rss_contract(self):
        feed = {
            "source": "unit_feed",
            "url": "https://example.com/rss",
            "markets": ["US"],
            "category": "macro",
        }

        def fetch_json(url):
            self.assertIn("127.0.0.1:8899", url)
            return {
                "items": [
                    {
                        "source": "cnbc",
                        "title": "Hong Kong stocks rally as China stimulus supports Tencent",
                        "link": "https://example.com/infohub-hk",
                        "published": "Fri, 12 Jun 2026 10:10:00 +0800",
                        "summary": "Risk-on tone improves for China internet shares.",
                    }
                ]
            }

        payload = producer.build_snapshot(
            fetch_feed_func=lambda _url: RSS,
            fetch_json_func=fetch_json,
            feeds=[feed],
            now=NOW,
            include_infohub=True,
        )
        context = report.build_report(payload["items"], now=NOW)

        self.assertEqual(payload["source"]["provider"], "rss+infohub")
        self.assertTrue(payload["source"]["include_infohub"])
        self.assertEqual(len(payload["items"]), 3)
        infohub = [item for item in payload["items"] if "producer:infohub" in item["tags"]][0]
        self.assertEqual(infohub["provider"], "infohub_public_rss_bridge")
        self.assertIn("HK", infohub["markets"])
        self.assertIn("00700", infohub["symbols"])
        self.assertIn("infohub_public_rss_bridge", context["summary"]["by_provider"])

    def test_infohub_fetch_failure_is_warning_not_exception(self):
        def fetch_json(_url):
            raise ValueError("infohub down")

        payload = producer.build_snapshot(
            fetch_feed_func=lambda _url: RSS,
            fetch_json_func=fetch_json,
            feeds=[],
            now=NOW,
            include_infohub=True,
            include_rss=False,
        )

        self.assertEqual(payload["items"], [])
        self.assertGreaterEqual(len(payload["warnings"]), 1)
        self.assertIn("fetch_failed:infohub_macro_global", payload["warnings"][0])


if __name__ == "__main__":
    unittest.main()
