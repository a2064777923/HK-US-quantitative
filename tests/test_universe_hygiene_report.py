import unittest
from unittest.mock import patch

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
    def test_fetch_universe_rows_reads_canonical_daily_bars(self):
        captured = {}

        def fake_table_columns(table):
            return {"data_source"} if table == "klines" else set()

        def fake_psql(sql):
            captured["sql"] = sql
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": "US\tAAPL\tApple\tNASDAQ\t2020-01-01\t2026-06-12\t100\t1000\ttencent\t80\t0\n",
                    "stderr": "",
                },
            )()

        with patch.object(report, "table_columns", side_effect=fake_table_columns), patch.object(
            report, "psql", side_effect=fake_psql
        ):
            rows, warnings = report.fetch_universe_rows()

        sql = captured["sql"]
        normalized = " ".join(sql.split())
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertIn("daily_bar AS", sql)
        self.assertIn("SELECT DISTINCT ON (k.symbol, k.timestamp::date)", sql)
        self.assertIn("ORDER BY k.symbol, k.timestamp::date, k.timestamp DESC", normalized)
        self.assertIn("count(d.*) FILTER", sql)

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

    def test_us_non_common_equity_instruments_are_remove_candidates(self):
        payload = report.build_report(
            [
                row("MSFT", "2026-06-12", market="US", exchange="NASDAQ"),
                row(
                    "XYZW",
                    "2026-06-12",
                    market="US",
                    exchange="NASDAQ",
                    history=80,
                )
                | {"name": "Example Acquisition Corp. Warrant"},
                row(
                    "ABCU",
                    "2026-06-12",
                    market="US",
                    exchange="NASDAQ",
                    history=80,
                )
                | {"name": "Example Acquisition Corp Units"},
                row(
                    "ABCN",
                    "2026-06-12",
                    market="US",
                    exchange="NYSE",
                    history=80,
                )
                | {"name": "Example 9.875% Senior Notes Due 2030"},
            ]
        )
        us = payload["markets"]["US"]
        candidates = {item["symbol"]: item for item in us["high_priority_candidates"]}

        self.assertEqual(us["problem_symbol_count"], 3)
        self.assertNotIn("MSFT", candidates)
        for symbol in ("XYZW", "ABCU", "ABCN"):
            self.assertEqual(candidates[symbol]["recommended_action"], "candidate_remove_from_stock_universe")
            self.assertIn("unsupported_us_equity_instrument", candidates[symbol]["issues"])
            self.assertIn(symbol, payload["proposal"]["candidate_deactivate_or_remap"]["US"])

    def test_us_adr_common_equities_remain_supported(self):
        payload = report.build_report(
            [
                row("BABA", "2026-06-12", market="US", exchange="NYSE", history=80) | {"name": "Alibaba Group Holding Limited American Depositary Shares"},
                row("NOK", "2026-06-12", market="US", exchange="NYSE", history=80) | {"name": "Nokia Corporation Sponsored American Depositary Shares"},
                row("UNH", "2026-06-12", market="US", exchange="NYSE", history=80) | {"name": "UnitedHealth Group Incorporated Common Stock"},
            ]
        )
        us = payload["markets"]["US"]
        symbols = {item["symbol"]: item for item in us["active_symbols"]}

        self.assertEqual(us["problem_symbol_count"], 0)
        self.assertEqual(symbols["BABA"]["recommended_action"], "keep_active")
        self.assertEqual(symbols["NOK"]["recommended_action"], "keep_active")
        self.assertEqual(symbols["UNH"]["recommended_action"], "keep_active")

    def test_problem_lists_are_complete_for_bulk_universe_cleanup(self):
        rows = [row("AAPL", "2026-06-12", market="US", exchange="NASDAQ")]
        rows.extend(
            row(f"BAD{i:03d}^A", None, history=0, market="US", exchange="NYSE")
            for i in range(150)
        )

        payload = report.build_report(rows)
        us = payload["markets"]["US"]

        self.assertEqual(us["problem_symbol_count"], 150)
        self.assertEqual(len(us["high_priority_candidates"]), 150)
        self.assertEqual(len(us["all_problem_symbols"]), 150)
        self.assertEqual(len(payload["proposal"]["candidate_deactivate_or_remap"]["US"]), 150)


if __name__ == "__main__":
    unittest.main()
