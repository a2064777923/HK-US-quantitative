import unittest

from scripts import kline_gap_alternate_provider_probe as probe


def unresolved(symbol="00959", market="HK", latest_source_date="2025-06-25"):
    return {
        "symbol": symbol,
        "market": market,
        "latest_daily_date": latest_source_date,
        "target_end_date": "2026-06-12",
        "latest_source_date": latest_source_date,
        "source_attempts": [
            {
                "source_code": f"hk{symbol}" if market == "HK" else f"us{symbol}.N",
                "status": "has_rows",
                "row_count": 800,
                "latest_source_date": latest_source_date,
            }
        ],
    }


def chart_rows(*dates):
    rows = []
    for idx, day in enumerate(dates, start=1):
        rows.append(
            {
                "date": day,
                "open": 10 + idx,
                "high": 11 + idx,
                "low": 9 + idx,
                "close": 10.5 + idx,
                "volume": 1000 + idx,
            }
        )
    return rows


class KlineGapAlternateProviderProbeTests(unittest.TestCase):
    def test_yahoo_provider_symbol_converts_hk_and_us_symbols(self):
        self.assertEqual(probe.yahoo_provider_symbol("00959", "HK"), "0959.HK")
        self.assertEqual(probe.yahoo_provider_symbol("SQ", "US"), "SQ")

    def test_alternate_provider_has_current_daily_rows(self):
        payload = probe.build_report(
            {"schema": "kline_daily_gap_repair_report_v1", "status": "PARTIAL", "unresolved": [unresolved()]},
            fetch_chart=lambda _provider: chart_rows("2025-06-25", "2026-06-11", "2026-06-12"),
        )

        item = payload["probes"][0]
        self.assertEqual(payload["status"], "ACTION_REQUIRED")
        self.assertEqual(item["provider_symbol"], "0959.HK")
        self.assertEqual(item["category"], "alternate_provider_has_current_daily_rows")
        self.assertEqual(item["alternate_latest_date"], "2026-06-12")
        self.assertTrue(item["alternate_reaches_target_end"])
        self.assertEqual(item["alternate_gap_row_count"], 2)
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["auto_uses_alternate_provider_for_repairs"])
        self.assertIn(
            "compare_yahoo_daily_rows_against_primary_provider_before_any_manual_repair",
            payload["recommendations"],
        )

    def test_providers_agree_stale_or_suspended_when_latest_dates_match(self):
        payload = probe.build_report(
            {"schema": "kline_daily_gap_repair_report_v1", "status": "UNRESOLVED", "unresolved": [unresolved()]},
            fetch_chart=lambda _provider: chart_rows("2024-01-01", "2025-06-25"),
        )

        item = payload["probes"][0]
        self.assertEqual(item["category"], "providers_agree_symbol_stale_or_suspended")
        self.assertEqual(item["confidence"], "high")
        self.assertEqual(payload["summary"]["providers_agree_stale_count"], 1)
        self.assertIn(
            "prioritize_listing_status_or_deactivation_review_for_symbols_stale_across_providers",
            payload["recommendations"],
        )

    def test_fetch_failure_is_warning_context_not_exception(self):
        def fail(_provider):
            raise ValueError("provider down")

        payload = probe.build_report(
            {"schema": "kline_daily_gap_repair_report_v1", "status": "UNRESOLVED", "unresolved": [unresolved("SQ", "US")]},
            fetch_chart=fail,
        )

        item = payload["probes"][0]
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(item["category"], "alternate_provider_fetch_failed")
        self.assertEqual(item["status"], "fetch_failed")
        self.assertIn("provider down", item["error"])
        self.assertEqual(payload["summary"]["fetch_failed_count"], 1)

    def test_invalid_alternate_gap_rows_are_high_confidence_blocker(self):
        payload = probe.build_report(
            {"schema": "kline_daily_gap_repair_report_v1", "status": "UNRESOLVED", "unresolved": [unresolved()]},
            fetch_chart=lambda _provider: [
                {
                    "date": "2026-06-12",
                    "open": 10,
                    "high": 8,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                }
            ],
        )

        item = payload["probes"][0]
        self.assertEqual(item["category"], "alternate_provider_rows_invalid")
        self.assertEqual(item["confidence"], "high")
        self.assertEqual(item["alternate_gap_row_count"], 0)
        self.assertEqual(item["invalid_alternate_gap_row_count"], 1)
        self.assertIn("block_alternate_provider_repair_until_rows_validate", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
