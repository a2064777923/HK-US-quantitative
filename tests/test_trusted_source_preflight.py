import unittest
from datetime import datetime

from scripts import trusted_source_preflight as preflight


NOW = datetime(2026, 6, 12, 10, 30, 0)


def external_item(**extra):
    payload = {
        "id": "wudao-1",
        "category": "event",
        "source": "wudao_mcp_flash_news",
        "provider": "wudao_mcp_flash_news",
        "producer": "wudao_mcp",
        "title": "Ceasefire boosts risk appetite",
        "summary": "High impact geopolitical event.",
        "published_at": "2026-06-12T10:00:00",
        "sentiment": "positive",
        "impact_score": 0.85,
        "markets": ["US", "HK"],
        "symbols": ["00700"],
        "tags": ["provider:wudao_mcp_flash_news"],
    }
    payload.update(extra)
    return payload


def sentiment_indicator(**extra):
    payload = {
        "id": "northbound-1",
        "indicator_type": "capital_flow",
        "name": "Northbound capital flow",
        "source": "northbound_flow_snapshot",
        "provider": "capital_flow_snapshot",
        "observed_at": "2026-06-12T10:00:00",
        "markets": ["HK", "CN"],
        "direction": "risk_on",
        "score": 0.45,
        "value": 2000000000,
        "unit": "HKD",
        "summary": "Northbound net inflow.",
        "tags": ["provider:capital_flow_snapshot"],
    }
    payload.update(extra)
    return payload


def volatility_indicator(**extra):
    payload = {
        "id": "vix-1",
        "indicator_type": "volatility",
        "name": "VIX",
        "source": "cboe_vix_snapshot",
        "provider": "cboe_vix",
        "observed_at": "2026-06-12T10:00:00",
        "markets": ["US"],
        "direction": "risk_on",
        "score": 0.35,
        "value": 16.2,
        "unit": "index",
        "summary": "VIX eased.",
    }
    payload.update(extra)
    return payload


def fundamentals_item(**extra):
    payload = {
        "symbol": "00700",
        "market": "HK",
        "name": "Tencent",
        "source": "broker_fundamentals_snapshot",
        "provider": "broker_fundamentals_snapshot",
        "provider_symbol": "00700.HK",
        "as_of": "2026-06-12T10:00:00",
        "currency": "HKD",
        "market_cap": 3000000000000,
        "pe_ttm": 18.2,
        "pb": 3.1,
        "ps": 5.0,
        "roe_pct": 18.0,
        "revenue_growth_pct": 8.5,
        "earnings_growth_pct": 10.2,
        "dividend_yield_pct": 1.0,
        "debt_to_equity": 0.4,
        "summary": "Broker full fundamentals snapshot.",
    }
    payload.update(extra)
    return payload


class TrustedSourcePreflightTests(unittest.TestCase):
    def test_full_trusted_payload_passes_without_writing_contracts(self):
        payload = preflight.build_report(
            external_items=[
                external_item(category="macro", id="macro-1", symbols=[]),
                external_item(category="capital_flow", id="flow-1", provider="capital_flow_snapshot"),
            ],
            sentiment_indicators=[volatility_indicator(), sentiment_indicator()],
            fundamentals_items=[fundamentals_item()],
            now=NOW,
        )

        self.assertEqual(payload["schema"], "trusted_source_preflight_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_strategy"])
        self.assertFalse(payload["source"]["changes_alert_queue"])
        self.assertFalse(payload["source"]["writes_ingest_files"])
        self.assertEqual(payload["summary"]["failed_component_count"], 0)
        self.assertIn("external_trusted_source_preflight_passed", payload["recommendations"])
        self.assertIn("market_sentiment_trusted_source_preflight_passed", payload["recommendations"])
        self.assertIn("fundamentals_trusted_source_preflight_passed", payload["recommendations"])

    def test_public_fallback_and_partial_fundamentals_warn(self):
        payload = preflight.build_report(
            external_items=[
                external_item(
                    id="rss-1",
                    category="macro",
                    source="google_news_us_market",
                    provider="google_news_us_market",
                    producer="rss",
                    symbols=[],
                    tags=["producer:rss", "source:google_news_us_market"],
                )
            ],
            sentiment_indicators=[
                volatility_indicator(source="yahoo_chart", provider="yahoo_chart", tags=["producer:yahoo_chart"])
            ],
            fundamentals_items=[
                fundamentals_item(
                    source="tencent_quote_snapshot",
                    provider="tencent_quote_snapshot",
                    market_cap=None,
                    pb=None,
                    ps=None,
                    roe_pct=None,
                    revenue_growth_pct=None,
                    earnings_growth_pct=None,
                    dividend_yield_pct=None,
                    debt_to_equity=None,
                )
            ],
            now=NOW,
        )

        self.assertEqual(payload["status"], "WARN")
        by_name = {component["name"]: component for component in payload["components"]}
        self.assertIn("external_context_only_public_fallback_sources", by_name["external_market_context_inputs"]["reasons"])
        self.assertIn("capital_flow_context_missing", by_name["external_market_context_inputs"]["reasons"])
        self.assertIn("sentiment_only_public_fallback_sources", by_name["market_sentiment_inputs"]["reasons"])
        self.assertIn("capital_flow_sentiment_missing", by_name["market_sentiment_inputs"]["reasons"])
        self.assertIn("partial_fundamentals_present", by_name["fundamentals_context_inputs"]["reasons"])
        self.assertIn("fundamentals_public_or_partial_fallback_sources_present", by_name["fundamentals_context_inputs"]["reasons"])
        self.assertIn("wire_wudao_mcp_broker_or_official_macro_provider_before_claiming_trusted_event_awareness", payload["recommendations"])

    def test_invalid_payload_fails_preflight(self):
        payload = preflight.build_report(
            external_items=[external_item(published_at="not-a-date", title="")],
            sentiment_indicators=[sentiment_indicator(indicator_type="bad", observed_at="bad", direction="panic", score=2)],
            fundamentals_items=[fundamentals_item(symbol="", as_of="bad")],
            now=NOW,
        )

        self.assertEqual(payload["status"], "FAIL")
        by_name = {component["name"]: component for component in payload["components"]}
        self.assertIn("invalid_external_items", by_name["external_market_context_inputs"]["reasons"])
        self.assertIn("invalid_market_sentiment_indicators", by_name["market_sentiment_inputs"]["reasons"])
        self.assertIn("invalid_fundamentals_items", by_name["fundamentals_context_inputs"]["reasons"])
        self.assertIn("fix_external_context_payload_schema_before_ingest", payload["recommendations"])
        self.assertIn("fix_market_sentiment_payload_schema_before_ingest", payload["recommendations"])
        self.assertIn("fix_fundamentals_payload_schema_before_ingest", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
