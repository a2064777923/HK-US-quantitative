import json
import tempfile
import unittest
from pathlib import Path

from scripts import market_sentiment_ingest as ingest


def indicator(**extra):
    payload = {
        "id": "vix-1",
        "indicator_type": "volatility",
        "name": "VIX",
        "source": "producer",
        "observed_at": "2026-06-12T10:00:00",
        "markets": ["US"],
        "direction": "risk_on",
        "score": 0.35,
        "value": 16.2,
        "previous_value": 18.4,
        "unit": "index",
        "summary": "VIX eased.",
    }
    payload.update(extra)
    return payload


class MarketSentimentIngestTests(unittest.TestCase):
    def test_valid_indicator_is_accepted_and_appended(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sentiment.jsonl"
            payload = ingest.build_ingest([indicator()], output_file=str(path))
            ingest.append_jsonl(str(path), payload["accepted"])

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(payload["accepted_count"], 1)
        self.assertEqual(payload["rejected_count"], 0)
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_alert_queue"])
        self.assertEqual(rows[0]["indicator_type"], "volatility")
        self.assertEqual(rows[0]["markets"], ["US"])

    def test_duplicate_indicator_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sentiment.jsonl"
            first = ingest.build_ingest([indicator()], output_file=str(path))
            ingest.append_jsonl(str(path), first["accepted"])
            second = ingest.build_ingest([indicator()], output_file=str(path))

        self.assertEqual(second["accepted_count"], 0)
        self.assertEqual(second["duplicate_count"], 1)

    def test_invalid_indicator_is_rejected_by_default(self):
        payload = ingest.build_ingest(
            [
                indicator(
                    indicator_type="bad_type",
                    observed_at="not-a-date",
                    direction="panic",
                    score=2.0,
                )
            ],
            output_file="",
        )

        self.assertEqual(payload["accepted_count"], 0)
        self.assertEqual(payload["rejected_count"], 1)
        self.assertIn("invalid_indicator_type", payload["rejected"][0]["reasons"])
        self.assertIn("invalid_observed_at", payload["rejected"][0]["reasons"])
        self.assertIn("invalid_direction", payload["rejected"][0]["reasons"])
        self.assertIn("score_out_of_range", payload["rejected"][0]["reasons"])

    def test_allow_invalid_keeps_indicator_with_warnings(self):
        payload = ingest.build_ingest(
            [indicator(observed_at="not-a-date", score=2.0)],
            output_file="",
            allow_invalid=True,
        )

        self.assertEqual(payload["accepted_count"], 1)
        self.assertEqual(payload["rejected_count"], 0)
        self.assertIn("invalid_observed_at", payload["accepted"][0]["ingest_warnings"])
        self.assertIn("score_out_of_range", payload["accepted"][0]["ingest_warnings"])


if __name__ == "__main__":
    unittest.main()
