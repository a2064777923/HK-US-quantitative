import json
import tempfile
import unittest
from pathlib import Path

from scripts import external_market_context_ingest as ingest


def item(**extra):
    payload = {
        "id": "wudao-1",
        "category": "news",
        "source": "wudao",
        "title": "Ceasefire headline",
        "summary": "Risk appetite improves.",
        "published_at": "2026-06-12T10:00:00",
        "sentiment": "positive",
        "impact_score": 0.8,
        "markets": ["US", "HK"],
        "symbols": ["TSLA"],
    }
    payload.update(extra)
    return payload


class ExternalMarketContextIngestTests(unittest.TestCase):
    def test_valid_item_is_accepted_and_appended(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "external.jsonl"
            payload = ingest.build_ingest([item()], output_file=str(path))
            ingest.append_jsonl(str(path), payload["accepted"])

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(payload["accepted_count"], 1)
        self.assertEqual(payload["rejected_count"], 0)
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertEqual(rows[0]["category"], "news")
        self.assertEqual(rows[0]["symbols"], ["TSLA"])

    def test_duplicate_item_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "external.jsonl"
            first = ingest.build_ingest([item()], output_file=str(path))
            ingest.append_jsonl(str(path), first["accepted"])
            second = ingest.build_ingest([item()], output_file=str(path))

        self.assertEqual(second["accepted_count"], 0)
        self.assertEqual(second["duplicate_count"], 1)

    def test_invalid_item_is_rejected_by_default(self):
        payload = ingest.build_ingest([item(title="", summary="", published_at="not-a-date")], output_file="")

        self.assertEqual(payload["accepted_count"], 0)
        self.assertEqual(payload["rejected_count"], 1)
        self.assertIn("missing_title", payload["rejected"][0]["reasons"])
        self.assertIn("invalid_published_at", payload["rejected"][0]["reasons"])

    def test_allow_invalid_keeps_item_with_warnings(self):
        payload = ingest.build_ingest(
            [item(title="", summary="", published_at="not-a-date")],
            output_file="",
            allow_invalid=True,
        )

        self.assertEqual(payload["accepted_count"], 1)
        self.assertEqual(payload["rejected_count"], 0)
        self.assertIn("missing_title", payload["accepted"][0]["ingest_warnings"])


if __name__ == "__main__":
    unittest.main()
