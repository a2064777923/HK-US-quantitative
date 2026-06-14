import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from scripts import fundamentals_context_report as report


NOW = datetime(2026, 6, 12, 10, 30, 0)


def fundamental(**extra):
    payload = {
        "symbol": "00700",
        "market": "HK",
        "name": "Tencent",
        "source": "fixture",
        "as_of": "2026-06-01T00:00:00",
        "currency": "HKD",
        "market_cap": 3200000000000,
        "pe_ttm": 28.5,
        "pb": 4.2,
        "ps": 7.1,
        "roe_pct": 18.0,
        "revenue_growth_pct": 9.5,
        "earnings_growth_pct": 12.0,
        "dividend_yield_pct": 0.7,
        "debt_to_equity": 0.4,
    }
    payload.update(extra)
    return payload


class FundamentalsContextReportTests(unittest.TestCase):
    def test_missing_inputs_report_missing_status(self):
        payload = report.build_report([], now=NOW, warnings=["json_input_missing:/tmp/x"])

        self.assertEqual(payload["schema"], "fundamentals_context_report_v1")
        self.assertEqual(payload["status"], "MISSING")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertIn(
            "hermes_should_not_claim_fundamental_or_valuation_awareness_without_fundamentals_context",
            payload["recommendations"],
        )

    def test_fresh_reasonable_fundamentals_are_ok(self):
        payload = report.build_report([fundamental()], now=NOW)

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["fresh_item_count"], 1)
        self.assertEqual(payload["summary"]["full_item_count"], 1)
        self.assertEqual(payload["summary"]["partial_item_count"], 0)
        self.assertEqual(payload["items"][0]["symbol"], "00700")
        self.assertEqual(payload["items"][0]["valuation_flags"], [])
        self.assertEqual(payload["items"][0]["fundamental_completeness"]["level"], "full")

    def test_risky_valuation_profitability_and_leverage_flags(self):
        payload = report.build_report(
            [
                fundamental(
                    symbol="NVDA",
                    market="US",
                    pe_ttm=85,
                    pb=12,
                    ps=22,
                    roe_pct=3,
                    earnings_growth_pct=-8,
                    debt_to_equity=3.1,
                )
            ],
            now=NOW,
        )

        flags = payload["items"][0]["valuation_flags"]
        self.assertEqual(payload["status"], "RISK")
        self.assertIn("overvalued", flags)
        self.assertIn("weak_profitability", flags)
        self.assertIn("earnings_decline", flags)
        self.assertIn("high_leverage", flags)
        self.assertIn("require_hermes_explicit_valuation_risk_note_for_overvalued_buy_candidates", payload["recommendations"])

    def test_tencent_fallback_is_fresh_but_partial_and_risky(self):
        payload = report.build_report(
            [
                fundamental(
                    source="tencent_quote_snapshot",
                    provider_symbol="hk00700",
                    market_cap=None,
                    pe_ttm=28.5,
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
            warnings=[
                "producer_warning:fetch_failed:00700:0700.HK:HTTP Error 401: Unauthorized",
                "producer_warning:fallback_provider_used:00700:tencent_quote_snapshot_partial",
            ],
        )

        item = payload["items"][0]
        self.assertEqual(payload["status"], "RISK")
        self.assertEqual(payload["summary"]["fresh_item_count"], 1)
        self.assertEqual(payload["summary"]["by_source"], {"tencent_quote_snapshot": 1})
        self.assertEqual(payload["summary"]["fallback_item_count"], 1)
        self.assertEqual(payload["summary"]["partial_item_count"], 1)
        self.assertEqual(payload["summary"]["producer_fetch_failed_count"], 1)
        self.assertEqual(payload["summary"]["fallback_provider_used_count"], 1)
        self.assertEqual(item["provider_symbol"], "hk00700")
        self.assertEqual(item["fundamental_completeness"]["level"], "partial")
        self.assertEqual(item["fundamental_completeness"]["available_metric_count"], 1)
        self.assertIn("partial_fundamentals", item["valuation_flags"])
        self.assertIn("require_hermes_partial_fundamentals_disclosure_for_buy_candidates", payload["recommendations"])
        self.assertIn("treat_fallback_provider_fundamentals_as_partial_context_only", payload["recommendations"])
        self.assertIn("investigate_fundamentals_provider_fetch_failures_before_trusting_full_coverage", payload["recommendations"])

    def test_negative_pe_flags_negative_earnings(self):
        payload = report.build_report([fundamental(pe_ttm=-4.2)], now=NOW)

        self.assertEqual(payload["status"], "RISK")
        self.assertIn("negative_earnings", payload["items"][0]["valuation_flags"])

    def test_all_stale_items_set_stale_status(self):
        old_time = (NOW - timedelta(days=200)).isoformat(timespec="seconds")
        payload = report.build_report([fundamental(as_of=old_time)], now=NOW)

        self.assertEqual(payload["status"], "STALE")
        self.assertIn("stale_fundamentals", payload["items"][0]["valuation_flags"])
        self.assertIn("refresh_fundamentals_context_before_buy_judgment", payload["recommendations"])

    def test_build_report_reads_json_and_jsonl_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            json_input = Path(td) / "fundamentals.json"
            jsonl_input = Path(td) / "fundamentals.jsonl"
            json_input.write_text(json.dumps({"items": [fundamental(symbol="00700")]}), encoding="utf-8")
            jsonl_input.write_text(json.dumps(fundamental(symbol="AAPL", market="US")) + "\n", encoding="utf-8")

            payload = report.build_report(
                input_file=str(json_input),
                input_jsonl_file=str(jsonl_input),
                now=NOW,
            )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["item_count"], 2)
        self.assertEqual(payload["summary"]["by_market"], {"HK": 1, "US": 1})


if __name__ == "__main__":
    unittest.main()
