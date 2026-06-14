import unittest

from scripts import kline_gap_alternate_provider_repair_plan as plan


def unresolved(symbol="00959", market="HK", latest_date="2026-06-10", target="2026-06-12"):
    return {
        "symbol": symbol,
        "market": market,
        "latest_daily_date": latest_date,
        "latest_source_date": latest_date,
        "latest_daily_close": 10.0,
        "target_end_date": target,
    }


def payload(items):
    return {
        "schema": "kline_daily_gap_repair_report_v1",
        "status": "UNRESOLVED" if items else "OK",
        "plan_hash": "daily-gap-hash",
        "unresolved": items,
    }


def rows(*dates, volume=1000, flat=False):
    out = []
    for idx, day in enumerate(dates, start=1):
        if flat:
            open_price = high = low = close = 10.0
        else:
            open_price = 10.0 + idx
            high = 11.0 + idx
            low = 9.0 + idx
            close = 10.5 + idx
        out.append(
            {
                "date": day,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    return out


class KlineGapAlternateProviderRepairPlanTests(unittest.TestCase):
    def test_high_quality_gap_rows_become_manual_repair_candidate(self):
        report = plan.build_report(
            payload([unresolved()]),
            fetch_chart=lambda _provider: rows("2026-06-10", "2026-06-11", "2026-06-12"),
        )

        candidate = report["candidates"][0]
        self.assertEqual(report["schema"], "kline_gap_alternate_provider_repair_plan_v1")
        self.assertEqual(report["status"], "ACTION_REQUIRED")
        self.assertEqual(report["summary"]["manual_repair_candidate_count"], 1)
        self.assertEqual(report["summary"]["planned_row_count"], 2)
        self.assertEqual(candidate["status"], "manual_repair_candidate_after_operator_comparison")
        self.assertEqual(candidate["provider_symbol"], "0959.HK")
        self.assertEqual(candidate["quality"]["status"], "PASS")
        self.assertEqual(candidate["rows"][0]["date"], "2026-06-11")
        self.assertGreater(candidate["rows"][0]["change_percent"], 0)
        self.assertTrue(report["source"]["read_only"])
        self.assertFalse(report["source"]["auto_applies_repairs"])
        self.assertFalse(report["source"]["auto_uses_alternate_provider_for_repairs"])
        self.assertIsNone(report["operator_contract"]["manual_apply_command"])
        self.assertIn(
            "operator_may_design_separate_hash_confirmed_repair_after_row_comparison",
            report["recommendations"],
        )

    def test_zero_volume_flat_rows_are_review_only_not_repair_candidates(self):
        report = plan.build_report(
            payload([unresolved(latest_date="2026-04-01", target="2026-04-03")]),
            fetch_chart=lambda _provider: rows("2026-04-02", "2026-04-03", volume=0, flat=True),
        )

        candidate = report["candidates"][0]
        self.assertEqual(report["status"], "REVIEW")
        self.assertEqual(report["summary"]["manual_repair_candidate_count"], 0)
        self.assertEqual(report["summary"]["review_only_count"], 1)
        self.assertEqual(candidate["status"], "review_only_quality_not_sufficient_for_repair_plan")
        self.assertEqual(candidate["quality"]["zero_volume_pct"], 100.0)
        self.assertEqual(candidate["quality"]["flat_ohlc_pct"], 100.0)
        self.assertIn("zero_volume_gap_rows_above_threshold", candidate["quality"]["reasons"])
        self.assertIn("flat_ohlc_gap_rows_above_threshold", candidate["quality"]["reasons"])
        self.assertIn(
            "do_not_repair_zero_volume_or_flat_ohlc_alternate_rows_without_external_confirmation",
            report["recommendations"],
        )

    def test_fetch_failure_is_issue_and_does_not_throw(self):
        def fail(_provider):
            raise RuntimeError("provider down")

        report = plan.build_report(payload([unresolved("SQ", "US")]), fetch_chart=fail)

        self.assertEqual(report["status"], "WARN")
        self.assertEqual(report["summary"]["candidate_count"], 0)
        self.assertEqual(report["issues"][0]["symbol"], "SQ")
        self.assertEqual(report["issues"][0]["category"], "alternate_provider_fetch_failed")
        self.assertIn("provider down", report["issues"][0]["detail"])

    def test_invalid_gap_rows_are_blocked_candidates(self):
        report = plan.build_report(
            payload([unresolved()]),
            fetch_chart=lambda _provider: [
                {
                    "date": "2026-06-11",
                    "open": 10,
                    "high": 9,
                    "low": 10,
                    "close": 10,
                    "volume": 100,
                }
            ],
        )

        candidate = report["candidates"][0]
        self.assertEqual(candidate["status"], "blocked_invalid_alternate_rows")
        self.assertEqual(candidate["quality"]["status"], "BLOCK")
        self.assertEqual(report["summary"]["blocked_candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
