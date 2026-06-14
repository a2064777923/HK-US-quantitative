import tempfile
import unittest
from datetime import datetime
from io import StringIO
from unittest.mock import patch

from scripts import intraday_kline_batch as batch


def provider_payload(code="hk00700"):
    return {
        "code": 0,
        "data": {
            code: {
                "data": {
                    "data": [
                        "0930 466.000 1000 466000.0",
                        "0931 467.000 1300 606100.0",
                        "0932 466.500 1500 699750.0",
                    ]
                }
            }
        },
    }


class IntradayKlineBatchTests(unittest.TestCase):
    def test_parse_tencent_minute_response_builds_minute_snapshot_rows(self):
        rows, warnings = batch.parse_tencent_minute_response(
            batch.json.dumps(provider_payload()),
            source_code="hk00700",
            symbol="00700",
            market="HK",
            observed_at=datetime(2026, 6, 12, 10, 0),
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["timestamp"], "2026-06-12 09:30:00")
        self.assertEqual(rows[0]["open"], 466.0)
        self.assertEqual(rows[0]["high"], 466.0)
        self.assertEqual(rows[0]["low"], 466.0)
        self.assertEqual(rows[0]["close"], 466.0)
        self.assertEqual(rows[0]["volume"], 1000.0)
        self.assertEqual(rows[1]["volume"], 300.0)
        self.assertEqual(rows[1]["amount"], 140100.0)
        self.assertEqual(rows[0]["data_source"], "tencent_minute_query")
        self.assertEqual(rows[0]["source_granularity"], "minute_snapshot_price")

    def test_weekend_provider_rows_are_not_dated_to_closed_market_day(self):
        rows, warnings = batch.parse_tencent_minute_response(
            batch.json.dumps(provider_payload()),
            source_code="hk00700",
            symbol="00700",
            market="HK",
            observed_at=datetime(2026, 6, 13, 10, 0),
        )

        self.assertEqual(rows, [])
        self.assertTrue(any("invalid_minute_timestamp" in warning for warning in warnings))

    def test_build_report_skips_fetch_when_market_date_is_weekend(self):
        calls = []

        def fetcher(symbol, market, observed_at=None):
            calls.append((symbol, market))
            return [], [], []

        payload = batch.build_report(
            symbols_by_market={"HK": ["00700"], "US": ["AAPL"]},
            fetcher=fetcher,
            observed_at=datetime(2026, 6, 13, 10, 0),
        )

        self.assertEqual(calls, [])
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["action_count"], 0)
        self.assertEqual(payload["summary"]["skipped_symbol_count"], 2)
        self.assertIsNone(payload["apply_contract"]["manual_apply_command"])
        self.assertEqual(
            {item["reason"] for item in payload["skipped"]},
            {"market_closed_weekend"},
        )

    def test_invalid_provider_rows_are_rejected_in_plan(self):
        source_rows = [
            {
                "symbol": "00700",
                "market": "HK",
                "timestamp": "2026-06-12 09:30:00",
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 10,
                "amount": 1000,
            },
            {
                "symbol": "00700",
                "market": "HK",
                "timestamp": "bad",
                "open": 101,
                "high": 101,
                "low": 101,
                "close": 101,
                "volume": 10,
                "amount": 1010,
            },
            {
                "symbol": "00700",
                "market": "HK",
                "timestamp": "2026-06-12 09:31:00",
                "open": 101,
                "high": 99,
                "low": 100,
                "close": 101,
                "volume": 10,
                "amount": 1010,
            },
        ]

        action, issue = batch.plan_action("00700", "HK", source_rows)

        self.assertIsNone(issue)
        self.assertEqual(action["interval"], "min")
        self.assertEqual(action["row_count"], 1)
        self.assertEqual(len(action["invalid_source_rows"]), 2)
        self.assertEqual(action["source_limitation"], "provider returns one price point per minute; OHLC high/low are not independently observed")

    def test_build_report_is_dry_run_and_does_not_apply_actions(self):
        calls = []

        def fetcher(symbol, market, observed_at=None):
            calls.append((symbol, market))
            return (
                [
                    {
                        "symbol": symbol,
                        "market": market,
                        "timestamp": "2026-06-12 09:30:00",
                        "open": 100,
                        "high": 100,
                        "low": 100,
                        "close": 100,
                        "volume": 10,
                        "amount": 1000,
                        "data_source": "tencent_minute_query",
                        "source_code": "hk00700",
                    }
                ],
                [],
                [{"source_code": "hk00700", "status": "has_rows", "row_count": 1}],
            )

        with patch.object(batch, "apply_actions") as apply_mock:
            payload = batch.build_report(
                symbols_by_market={"HK": ["00700"], "US": []},
                fetcher=fetcher,
                observed_at=datetime(2026, 6, 12, 10, 0),
                fetch_sleep_seconds=0,
            )

        apply_mock.assert_not_called()
        self.assertEqual(calls, [("00700", "HK")])
        self.assertEqual(payload["schema"], "intraday_kline_batch_report_v1")
        self.assertEqual(payload["status"], "ACTIONABLE")
        self.assertEqual(payload["mode"], "dry-run")
        self.assertTrue(payload["apply_contract"]["dry_run_default"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_strategy"])
        self.assertFalse(payload["source"]["changes_alert_queue"])
        self.assertFalse(payload["source"]["changes_crontab"])
        self.assertFalse(payload["source"]["repairs_daily_klines"])
        self.assertIn("--confirm-plan-hash", payload["apply_contract"]["manual_apply_command"])

    def test_sql_for_action_upserts_only_min_interval_rows(self):
        action, issue = batch.plan_action(
            "00700",
            "HK",
            [
                {
                    "symbol": "00700",
                    "market": "HK",
                    "timestamp": "2026-06-12 09:30:00",
                    "open": 100,
                    "high": 100,
                    "low": 100,
                    "close": 100,
                    "volume": 10,
                    "amount": 1000,
                    "data_source": "tencent_minute_query",
                }
            ],
        )

        self.assertIsNone(issue)
        sql = batch.sql_for_action(action)

        self.assertIn("INSERT INTO klines", sql)
        self.assertIn("'00700','min','2026-06-12 09:30:00'", sql)
        self.assertNotIn("'00700','day'", sql)
        self.assertIn("ON CONFLICT (symbol, interval, timestamp) DO UPDATE", sql)
        self.assertIn("tencent_minute_query", sql)
        self.assertNotIn("source_granularity", sql)

    def test_sql_for_action_persists_source_granularity_when_column_exists(self):
        action, issue = batch.plan_action(
            "00700",
            "HK",
            [
                {
                    "symbol": "00700",
                    "market": "HK",
                    "timestamp": "2026-06-12 09:30:00",
                    "open": 100,
                    "high": 100,
                    "low": 100,
                    "close": 100,
                    "volume": 10,
                    "amount": 1000,
                    "data_source": "tencent_minute_query",
                    "source_granularity": "minute_snapshot_price",
                }
            ],
        )

        self.assertIsNone(issue)
        sql = batch.sql_for_action(action, kline_columns={"source_granularity"})

        self.assertIn("data_source, source_granularity, created_at", sql)
        self.assertIn("'tencent_minute_query','minute_snapshot_price',NOW()", sql)
        self.assertIn("source_granularity = EXCLUDED.source_granularity", sql)

    def test_main_apply_requires_matching_plan_hash(self):
        def fetcher(symbol, market, observed_at=None):
            return (
                [
                    {
                        "symbol": symbol,
                        "market": market,
                        "timestamp": "2026-06-12 09:30:00",
                        "open": 100,
                        "high": 100,
                        "low": 100,
                        "close": 100,
                        "volume": 10,
                        "amount": 1000,
                    }
                ],
                [],
                [],
            )

        with tempfile.TemporaryDirectory() as td:
            output = f"{td}/report.json"
            with patch.object(batch, "fetch_tencent_minute_rows", side_effect=fetcher), patch.object(
                batch,
                "apply_actions",
                return_value={"status": "applied"},
            ) as apply_mock, patch(
                "sys.stdout",
                new_callable=StringIO,
            ):
                code = batch.main(
                    [
                        "--output",
                        output,
                        "--symbol",
                        "HK:00700",
                        "--apply",
                        "--confirm-plan-hash",
                        "bad",
                    ]
                )

        apply_mock.assert_not_called()
        self.assertEqual(code, 2)

    def test_fetcher_provider_attempts_us_variants_until_rows_exist(self):
        payload_empty = batch.json.dumps({"code": 0, "data": {"usAAPL": {"data": {"data": ["  0"]}}}})
        payload_ok = batch.json.dumps(
            {
                "code": 0,
                "data": {
                    "usAAPL.OQ": {
                        "data": {
                            "data": [
                                "0930 200.00 1000",
                                "0931 201.00 1100",
                            ]
                        }
                    }
                },
            }
        )

        class Response:
            def __init__(self, text):
                self.text = text

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return self.text.encode()

        with patch.object(
            batch.urllib.request,
            "urlopen",
            side_effect=[Response(payload_empty), Response(payload_ok)],
        ):
            rows, warnings, attempts = batch.fetch_tencent_minute_rows(
                "AAPL",
                "US",
                observed_at=datetime(2026, 6, 12, 10, 0),
            )

        self.assertEqual(len(rows), 2)
        self.assertEqual(attempts[0]["source_code"], "usAAPL")
        self.assertEqual(attempts[0]["status"], "empty")
        self.assertEqual(attempts[1]["source_code"], "usAAPL.OQ")
        self.assertEqual(attempts[1]["status"], "has_rows")
        self.assertTrue(any("invalid_minute_line" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
