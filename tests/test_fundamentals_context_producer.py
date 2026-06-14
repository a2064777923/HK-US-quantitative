import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts import fundamentals_context_producer as producer


NOW = datetime(2026, 6, 12, 10, 30, 0)


def yahoo_result():
    return {
        "price": {
            "shortName": "Tencent",
            "currency": "HKD",
            "marketCap": {"raw": 3200000000000},
        },
        "summaryDetail": {
            "trailingPE": {"raw": 28.5},
            "dividendYield": {"raw": 0.007},
        },
        "defaultKeyStatistics": {
            "priceToBook": {"raw": 4.2},
            "priceToSalesTrailing12Months": {"raw": 7.1},
        },
        "financialData": {
            "returnOnEquity": {"raw": 0.18},
            "revenueGrowth": {"raw": 0.095},
            "earningsGrowth": {"raw": 0.12},
            "debtToEquity": {"raw": 40},
        },
    }


def tencent_quote_text():
    return "\n".join(
        [
            (
                'v_hk00700="100~腾讯控股~00700~463.600~457.200~466.000~22334646.0~0~0~'
                '463.600~0~0~0~0~0~0~0~0~0~463.600~0~0~0~0~0~0~0~0~0~22334646.0~'
                '2026/06/12 16:08:26~6.400~1.40~467.000~459.400~463.600~22334646.0~'
                '10346698122.244~0~16.96~~0~0~1.66~42224.8430~42224.8430~TENCENT";'
            ),
            (
                'v_usAAPL="200~苹果~AAPL.OQ~291.53~295.63~296.03~9797292~0~0~0~0~0~'
                '0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~~2026-06-12 10:40:39~-4.10~'
                '-1.39~297.14~290.61~USD~9797292~2870706984~0.07~35.29~~39.08~~'
                '2.21~42791.60390~42818.04895~Apple Inc.";'
            ),
        ]
    )


class FundamentalsContextProducerTests(unittest.TestCase):
    def test_provider_symbol_converts_hk_code_to_yahoo_symbol(self):
        self.assertEqual(producer.provider_symbol("00700"), "0700.HK")
        self.assertEqual(producer.provider_symbol("AAPL"), "AAPL")

    def test_tencent_provider_symbol_converts_codes(self):
        self.assertEqual(producer.tencent_provider_symbol("00700"), "hk00700")
        self.assertEqual(producer.tencent_provider_symbol("AAPL"), "usAAPL")

    def test_build_snapshot_maps_yahoo_fields_to_context_contract(self):
        calls = []

        def fake_fetch(symbol):
            calls.append(symbol)
            return yahoo_result()

        payload = producer.build_snapshot(
            symbols=["00700"],
            watchlist_file="",
            fetch_summary=fake_fetch,
            now=NOW,
        )

        self.assertEqual(payload["schema"], "fundamentals_context_producer_v1")
        self.assertEqual(calls, ["0700.HK"])
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        item = payload["items"][0]
        self.assertEqual(item["symbol"], "00700")
        self.assertEqual(item["market"], "HK")
        self.assertEqual(item["pe_ttm"], 28.5)
        self.assertEqual(item["roe_pct"], 18.0)
        self.assertEqual(item["dividend_yield_pct"], 0.7)
        self.assertEqual(item["debt_to_equity"], 0.4)

    def test_fetch_failures_are_warnings_not_exceptions(self):
        def fake_fetch(symbol):
            raise ValueError("provider down")

        payload = producer.build_snapshot(
            symbols=["AAPL"],
            watchlist_file="",
            fetch_summary=fake_fetch,
            fetch_tencent_quotes=None,
            now=NOW,
        )

        self.assertEqual(payload["items"], [])
        self.assertIn("fetch_failed:AAPL:AAPL:provider down", payload["warnings"])

    def test_tencent_quote_parser_maps_conservative_partial_fields(self):
        items, warnings = producer.parse_tencent_quote_text(
            tencent_quote_text(),
            requested_symbols=["00700", "AAPL"],
            observed_at=NOW.isoformat(timespec="seconds"),
        )

        self.assertEqual(warnings, [])
        hk = items["00700"]
        us = items["AAPL"]
        self.assertEqual(hk["source"], "tencent_quote_snapshot")
        self.assertEqual(hk["market"], "HK")
        self.assertEqual(hk["currency"], "HKD")
        self.assertEqual(hk["pe_ttm"], 16.96)
        self.assertIsNone(hk["market_cap"])
        self.assertIsNone(hk["pb"])
        self.assertEqual(us["market"], "US")
        self.assertEqual(us["currency"], "USD")
        self.assertEqual(us["pe_ttm"], 35.29)

    def test_yahoo_failure_falls_back_to_tencent_partial_snapshot(self):
        def fake_fetch(symbol):
            raise ValueError("provider down")

        def fake_tencent(symbols, observed_at):
            self.assertEqual(symbols, ["00700", "AAPL"])
            return producer.parse_tencent_quote_text(
                tencent_quote_text(),
                requested_symbols=symbols,
                observed_at=observed_at,
            )

        payload = producer.build_snapshot(
            symbols=["00700", "AAPL"],
            watchlist_file="",
            fetch_summary=fake_fetch,
            fetch_tencent_quotes=fake_tencent,
            now=NOW,
        )

        self.assertEqual([item["source"] for item in payload["items"]], ["tencent_quote_snapshot", "tencent_quote_snapshot"])
        self.assertIn("fallback_provider_used:00700:tencent_quote_snapshot_partial", payload["warnings"])
        self.assertIn("fallback_provider_used:AAPL:tencent_quote_snapshot_partial", payload["warnings"])
        self.assertEqual(payload["source"]["provider"], "yahoo_quote_summary+tencent_quote_snapshot_fallback")

    def test_tencent_failure_is_warning_not_exception(self):
        def fake_fetch(symbol):
            raise ValueError("provider down")

        def fake_tencent(symbols, observed_at):
            raise RuntimeError("tencent down")

        payload = producer.build_snapshot(
            symbols=["AAPL"],
            watchlist_file="",
            fetch_summary=fake_fetch,
            fetch_tencent_quotes=fake_tencent,
            now=NOW,
        )

        self.assertEqual(payload["items"], [])
        self.assertIn("tencent_fetch_failed:tencent down", payload["warnings"])

    def test_watchlist_symbols_are_loaded_when_no_cli_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "watchlist.json"
            path.write_text('{"markets":{"HK":{"symbols":["00700"]},"US":{"symbols":["AAPL"]}}}', encoding="utf-8")

            payload = producer.build_snapshot(
                watchlist_file=str(path),
                fetch_summary=lambda _symbol: yahoo_result(),
                now=NOW,
            )

        self.assertEqual([item["symbol"] for item in payload["items"]], ["00700", "AAPL"])
        self.assertEqual(payload["source"]["symbol_count"], 2)


if __name__ == "__main__":
    unittest.main()
