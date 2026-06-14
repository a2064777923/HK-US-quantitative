import unittest
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from scripts import external_market_context_report as report


NOW = datetime(2026, 6, 12, 10, 30, 0)


def item(**extra):
    payload = {
        "id": "news-1",
        "category": "news",
        "source": "wudao",
        "title": "Ceasefire headline",
        "summary": "Risk appetite improves after ceasefire news.",
        "published_at": "2026-06-12T10:00:00",
        "sentiment": "positive",
        "impact_score": 0.8,
        "markets": ["US", "HK"],
        "symbols": ["TSLA"],
        "tags": ["geopolitics"],
    }
    payload.update(extra)
    return payload


class ExternalMarketContextReportTests(unittest.TestCase):
    def test_missing_inputs_report_missing_status(self):
        payload = report.build_report([], now=NOW, warnings=["json_input_missing:/tmp/x"])

        self.assertEqual(payload["schema"], "external_market_context_report_v1")
        self.assertEqual(payload["status"], "MISSING")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertIn("hermes_should_not_claim_news_macro_awareness_without_external_context", payload["recommendations"])

    def test_positive_fresh_context_is_ok_but_reports_missing_categories(self):
        payload = report.build_report(
            [item(), item(id="macro-1", category="macro", symbols=[], sentiment="neutral", impact_score=0.3)],
            now=NOW,
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["fresh_item_count"], 2)
        self.assertEqual(payload["summary"]["positive_high_impact_count"], 1)
        self.assertIn("capital_flow_context_missing_add_northbound_or_market_flow_summary", payload["recommendations"])

    def test_negative_high_impact_context_sets_risk_status(self):
        payload = report.build_report(
            [
                item(sentiment="negative", impact_score=0.91, title="Unexpected sanction headline"),
                item(id="flow-1", category="capital_flow", sentiment="negative", impact_score=0.6),
                item(id="macro-1", category="macro", sentiment="mixed", impact_score=0.5),
            ],
            now=NOW,
        )

        self.assertEqual(payload["status"], "RISK")
        self.assertEqual(payload["summary"]["negative_high_impact_count"], 1)
        self.assertIn("require_hermes_explicit_risk_note_for_negative_high_impact_events", payload["recommendations"])

    def test_all_stale_context_sets_stale_status(self):
        old_time = (NOW - timedelta(hours=6)).isoformat(timespec="seconds")
        payload = report.build_report([item(published_at=old_time)], now=NOW)

        self.assertEqual(payload["status"], "STALE")
        self.assertEqual(payload["summary"]["fresh_item_count"], 0)
        self.assertIn("refresh_external_context_before_trade_judgment", payload["recommendations"])

    def test_build_report_reads_explicit_jsonl_path(self):
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / "external.jsonl"
            jsonl.write_text(json.dumps(item()) + "\n", encoding="utf-8")

            payload = report.build_report(input_file="", input_jsonl_file=str(jsonl), now=NOW)

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["item_count"], 1)
        self.assertEqual(payload["source"]["input_jsonl_file"], str(jsonl))

    def test_missing_optional_jsonl_does_not_warn_when_json_input_exists(self):
        with tempfile.TemporaryDirectory() as td:
            json_input = Path(td) / "external.json"
            missing_jsonl = Path(td) / "external.jsonl"
            json_input.write_text(json.dumps({"items": [item()]}), encoding="utf-8")

            payload = report.build_report(
                input_file=str(json_input),
                input_jsonl_file=str(missing_jsonl),
                now=NOW,
            )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["item_count"], 1)
        self.assertNotIn(f"jsonl_input_missing:{missing_jsonl}", payload["warnings"])

    def test_json_input_producer_warnings_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            json_input = Path(td) / "external.json"
            warning = "fetch_failed:broken:provider unavailable"
            json_input.write_text(json.dumps({"items": [item()], "warnings": [warning]}), encoding="utf-8")

            payload = report.build_report(input_file=str(json_input), input_jsonl_file="", now=NOW)

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["item_count"], 1)
        self.assertIn(f"producer_warning:{warning}", payload["warnings"])
        self.assertEqual(payload["summary"]["producer_fetch_failed_count"], 1)
        self.assertIn("fix_external_context_provider_fetch_failures", payload["recommendations"])

    def test_source_coverage_separates_public_fallback_from_trusted_providers(self):
        payload = report.build_report(
            [
                item(
                    id="rss-1",
                    source="google_news_us_market",
                    tags=["producer:rss", "source:google_news_us_market"],
                    category="macro",
                    symbols=[],
                ),
                item(
                    id="broker-flow-1",
                    source="broker_feed",
                    provider="broker_feed",
                    producer="broker",
                    category="capital_flow",
                    symbols=["00700"],
                    sentiment="neutral",
                    impact_score=0.4,
                ),
            ],
            now=NOW,
        )

        self.assertEqual(payload["summary"]["by_provider"]["google_news_us_market"], 1)
        rss_item = [row for row in payload["items"] if row["id"] == "rss-1"][0]
        broker_item = [row for row in payload["items"] if row["id"] == "broker-flow-1"][0]
        self.assertEqual(rss_item["provider_grade"], "public_fallback")
        self.assertEqual(broker_item["provider_grade"], "trusted")
        self.assertEqual(payload["summary"]["trusted_provider_item_count"], 1)
        self.assertEqual(payload["summary"]["fallback_rss_item_count"], 1)
        self.assertEqual(payload["summary"]["fallback_positive_high_impact_count"], 1)
        self.assertEqual(payload["summary"]["capital_flow_item_count"], 1)
        self.assertEqual(payload["summary"]["watchlist_symbol_item_count"], 1)
        self.assertIn(
            "positive_high_impact_public_fallback_requires_source_limit_acknowledgement",
            payload["recommendations"],
        )

    def test_structured_provider_prefixes_count_as_trusted(self):
        payload = report.build_report(
            [
                item(
                    id="wudao-1",
                    source="wudao_mcp_flash_news",
                    provider="wudao_mcp_flash_news",
                    producer="wudao_mcp",
                    category="event",
                    symbols=["00700"],
                    sentiment="negative",
                    impact_score=0.91,
                    tags=["provider:wudao_mcp_flash_news"],
                ),
                item(
                    id="northbound-1",
                    source="northbound_flow_snapshot",
                    provider="capital_flow_snapshot",
                    producer="broker_feed",
                    category="capital_flow",
                    symbols=[],
                    sentiment="positive",
                    impact_score=0.55,
                    tags=["source:northbound_flow_snapshot"],
                ),
                item(
                    id="macro-1",
                    source="official_macro_calendar",
                    provider="official_macro_calendar",
                    producer="official_macro",
                    category="macro",
                    symbols=[],
                    sentiment="neutral",
                    impact_score=0.45,
                ),
            ],
            now=NOW,
        )

        self.assertEqual(payload["summary"]["trusted_provider_item_count"], 3)
        self.assertEqual(payload["summary"]["fallback_rss_item_count"], 0)
        self.assertEqual(payload["summary"]["capital_flow_item_count"], 1)
        self.assertNotIn(
            "external_context_only_public_fallback_wire_wudao_infohub_or_broker_structured_feed",
            payload["recommendations"],
        )

    def test_public_fallback_only_recommends_structured_provider(self):
        payload = report.build_report(
            [
                item(
                    id="rss-1",
                    source="google_news_us_market",
                    tags=["producer:rss", "source:google_news_us_market"],
                    category="macro",
                    symbols=[],
                    sentiment="neutral",
                    impact_score=0.4,
                )
            ],
            now=NOW,
        )

        self.assertEqual(payload["summary"]["trusted_provider_item_count"], 0)
        self.assertEqual(payload["summary"]["fallback_rss_item_count"], 1)
        self.assertIn(
            "external_context_only_public_fallback_wire_wudao_infohub_or_broker_structured_feed",
            payload["recommendations"],
        )


if __name__ == "__main__":
    unittest.main()
