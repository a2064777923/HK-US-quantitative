import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from scripts import market_sentiment_report as report


NOW = datetime(2026, 6, 12, 10, 30, 0)


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


class MarketSentimentReportTests(unittest.TestCase):
    def test_missing_inputs_report_missing_status(self):
        payload = report.build_report([], now=NOW, warnings=["json_input_missing:/tmp/x"])

        self.assertEqual(payload["schema"], "market_sentiment_report_v1")
        self.assertEqual(payload["status"], "MISSING")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertIn(
            "hermes_should_not_claim_quantified_sentiment_awareness_without_market_sentiment",
            payload["recommendations"],
        )

    def test_fresh_risk_on_indicators_are_ok(self):
        payload = report.build_report(
            [
                indicator(),
                indicator(
                    id="flow-1",
                    indicator_type="capital_flow",
                    name="Northbound flow",
                    markets=["HK"],
                    direction="risk_on",
                    score=0.4,
                    value=1200,
                    unit="HKD_mn",
                ),
            ],
            now=NOW,
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["fresh_indicator_count"], 2)
        self.assertGreater(payload["summary"]["overall_score"], 0)
        self.assertEqual(payload["summary"]["market_scores"]["US"], 0.35)

    def test_fresh_risk_off_indicators_set_risk_status(self):
        payload = report.build_report(
            [
                indicator(direction="risk_off", score=-0.8, value=35.0, previous_value=24.0),
                indicator(
                    id="flow-1",
                    indicator_type="capital_flow",
                    markets=["HK"],
                    direction="risk_off",
                    score=-0.5,
                    value=-900,
                    unit="HKD_mn",
                ),
            ],
            now=NOW,
        )

        self.assertEqual(payload["status"], "RISK")
        self.assertEqual(payload["summary"]["risk_off_count"], 2)
        self.assertIn("tighten_new_buy_review_when_sentiment_is_risk_off", payload["recommendations"])

    def test_all_stale_indicators_set_stale_status(self):
        old_time = (NOW - timedelta(hours=6)).isoformat(timespec="seconds")
        payload = report.build_report([indicator(observed_at=old_time)], now=NOW)

        self.assertEqual(payload["status"], "STALE")
        self.assertEqual(payload["summary"]["fresh_indicator_count"], 0)
        self.assertIn("refresh_market_sentiment_before_trade_judgment", payload["recommendations"])

    def test_build_report_reads_explicit_jsonl_path(self):
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / "sentiment.jsonl"
            jsonl.write_text(json.dumps(indicator()) + "\n", encoding="utf-8")

            payload = report.build_report(input_file="", input_jsonl_file=str(jsonl), now=NOW)

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["indicator_count"], 1)
        self.assertEqual(payload["source"]["input_jsonl_file"], str(jsonl))

    def test_missing_optional_jsonl_does_not_warn_when_json_input_exists(self):
        with tempfile.TemporaryDirectory() as td:
            json_input = Path(td) / "sentiment.json"
            missing_jsonl = Path(td) / "sentiment.jsonl"
            json_input.write_text(json.dumps({"indicators": [indicator()]}), encoding="utf-8")

            payload = report.build_report(
                input_file=str(json_input),
                input_jsonl_file=str(missing_jsonl),
                now=NOW,
            )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["indicator_count"], 1)
        self.assertNotIn(f"jsonl_input_missing:{missing_jsonl}", payload["warnings"])

    def test_json_input_producer_warnings_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            json_input = Path(td) / "sentiment.json"
            warning = "fetch_failed:^VIX:provider unavailable"
            json_input.write_text(json.dumps({"indicators": [indicator()], "warnings": [warning]}), encoding="utf-8")

            payload = report.build_report(input_file=str(json_input), input_jsonl_file="", now=NOW)

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["indicator_count"], 1)
        self.assertIn(f"producer_warning:{warning}", payload["warnings"])


if __name__ == "__main__":
    unittest.main()
