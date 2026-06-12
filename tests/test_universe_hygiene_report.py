import unittest

from scripts import universe_hygiene_report as report


def row(symbol, latest_date, history=80, market="HK", exchange="HKEX", volume=1000):
    return {
        "market": market,
        "symbol": symbol,
        "name": symbol,
        "exchange": exchange,
        "list_date": "2020-01-01",
        "latest_date": latest_date,
        "latest_close": 10,
        "latest_volume": volume,
        "data_source": "tencent",
        "history_rows_120d": history,
        "zero_volume_rows_20d": 0,
    }


class UniverseHygieneReportTests(unittest.TestCase):
    def test_clean_universe_is_read_only_and_has_clean_recommendation(self):
        payload = report.build_report(
            [
                row("00700", "2026-06-12"),
                row("09988", "2026-06-12"),
            ]
        )
        hk = payload["markets"]["HK"]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["problem_symbol_count"], 0)
        self.assertEqual(hk["problem_symbol_count"], 0)
        self.assertEqual([item["symbol"] for item in hk["active_symbols"]], ["00700", "09988"])
        self.assertEqual(hk["active_symbols"][0]["recommended_action"], "keep_active")
        self.assertEqual(payload["recommendations"], ["active_universe_hygiene_clean"])
        self.assertFalse(payload["source"]["auto_applies_stock_changes"])
        self.assertFalse(payload["proposal"]["source"]["auto_applied"])
        self.assertTrue(payload["proposal"]["source"]["manual_review_required"])

    def test_severely_stale_symbol_becomes_deactivate_or_mapping_candidate(self):
        payload = report.build_report(
            [
                row("00700", "2026-06-12"),
                row("03333", "2024-01-29", history=0),
            ]
        )
        hk = payload["markets"]["HK"]
        candidate = hk["high_priority_candidates"][0]

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["summary"]["high_priority_count"], 1)
        self.assertEqual(candidate["symbol"], "03333")
        self.assertEqual(candidate["recommended_action"], "candidate_deactivate_or_symbol_mapping")
        self.assertIn("latest_kline_stale_ge_30d", candidate["issues"])
        self.assertIn("03333", payload["proposal"]["candidate_deactivate_or_remap"]["HK"])

    def test_one_day_stale_symbol_is_refetch_or_monitor_not_deactivate(self):
        payload = report.build_report(
            [
                row("00700", "2026-06-12"),
                row("00066", "2026-06-11"),
            ]
        )
        hk = payload["markets"]["HK"]
        candidate = hk["refetch_candidates"][0]

        self.assertEqual(candidate["symbol"], "00066")
        self.assertEqual(candidate["recommended_action"], "monitor_or_refetch_after_close")
        self.assertIn("latest_kline_one_day_behind_market", candidate["issues"])
        self.assertIn("00066", payload["proposal"]["candidate_refetch_or_monitor"]["HK"])

    def test_missing_klines_candidate_requires_manual_review(self):
        payload = report.build_report(
            [
                row("00700", "2026-06-12"),
                row("HKHSI", None, history=0),
            ]
        )
        hk = payload["markets"]["HK"]
        candidate = hk["high_priority_candidates"][0]

        self.assertEqual(candidate["symbol"], "HKHSI")
        self.assertIn("symbol_format_unusual_for_exchange", candidate["issues"])
        self.assertIn("missing_daily_klines", candidate["issues"])
        self.assertEqual(candidate["recommended_action"], "candidate_remove_from_stock_universe")


if __name__ == "__main__":
    unittest.main()
