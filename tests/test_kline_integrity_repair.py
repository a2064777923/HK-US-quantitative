import unittest
import tempfile
from unittest.mock import patch
from datetime import datetime

from scripts import kline_integrity_repair as repair
from scripts import kline_batch
from scripts import quantmind_daily_pipeline


def invalid_row(symbol="00816"):
    return {
        "symbol": symbol,
        "exchange": "HKEX",
        "market": "hk",
        "date": "2026-06-12",
        "open": 2.45,
        "high": 2.45,
        "low": 2.45,
        "close": 2.49,
        "volume": 500,
        "amount": 0.12,
        "change_percent": 0,
    }


def replacement():
    return {
        "date": "2026-06-12",
        "open": 2.45,
        "high": 2.50,
        "low": 2.45,
        "close": 2.49,
        "volume": 500,
        "amount": 1245,
        "change_percent": 1.632653,
        "source_code": "hk00816",
    }


class KlineIntegrityRepairTests(unittest.TestCase):
    def test_build_report_plans_valid_source_repair(self):
        with patch.object(repair, "fetch_tencent_day", return_value=(replacement(), [])):
            payload = repair.build_report([invalid_row()])

        self.assertEqual(payload["schema"], "kline_integrity_repair_report_v1")
        self.assertEqual(payload["summary"]["invalid_latest_count"], 1)
        self.assertEqual(payload["summary"]["repair_action_count"], 1)
        self.assertEqual(payload["summary"]["unresolved_count"], 0)
        action = payload["actions"][0]
        self.assertEqual(action["symbol"], "00816")
        self.assertIn("close_outside_high_low", action["errors_before"])
        self.assertEqual(action["errors_after"], [])

    def test_invalid_replacement_is_unresolved_not_applied(self):
        bad = replacement()
        bad["high"] = 2.40
        row = invalid_row()
        row["date"] = "2026-06-11"
        with patch.object(repair, "fetch_tencent_day", return_value=(bad, [])):
            payload = repair.build_report([row])

        self.assertEqual(payload["summary"]["repair_action_count"], 0)
        self.assertEqual(payload["summary"]["unresolved_count"], 1)
        self.assertEqual(payload["unresolved"][0]["reason"], "source_replacement_invalid")

    def test_invalid_current_day_replacement_plans_provisional_delete(self):
        bad = replacement()
        bad["high"] = 2.40
        row = invalid_row()
        with patch.object(repair, "today_iso", return_value=row["date"]), patch.object(
            repair,
            "fetch_tencent_day",
            return_value=(bad, []),
        ):
            payload = repair.build_report([invalid_row()])

        self.assertEqual(payload["summary"]["repair_action_count"], 1)
        self.assertEqual(payload["summary"]["unresolved_count"], 0)
        action = payload["actions"][0]
        self.assertEqual(action["action"], "delete_provisional_kline")
        self.assertEqual(action["reason"], "source_replacement_invalid_for_current_day")
        self.assertIn("DELETE FROM klines", repair.sql_for_action(action))

    def test_plan_hash_is_stable_for_same_actions(self):
        with patch.object(repair, "fetch_tencent_day", return_value=(replacement(), [])):
            first = repair.build_report([invalid_row()])
            second = repair.build_report([invalid_row()])

        self.assertEqual(first["plan_hash"], second["plan_hash"])

    def test_sql_for_action_updates_only_target_day_row(self):
        with patch.object(repair, "fetch_tencent_day", return_value=(replacement(), [])):
            action = repair.build_report([invalid_row()])["actions"][0]

        sql = repair.sql_for_action(action)

        self.assertIn("UPDATE klines SET", sql)
        self.assertIn("data_source = 'tencent_day_repair'", sql)
        self.assertIn("WHERE symbol = '00816'", sql)
        self.assertIn("AND interval = 'day'", sql)
        self.assertIn("AND timestamp::date = '2026-06-12'::date", sql)

    def test_ingestion_ohlc_guards_reject_inconsistent_rows(self):
        self.assertTrue(kline_batch.valid_ohlc(10, 11, 12, 9))
        self.assertTrue(kline_batch.valid_ohlc(10, 13, 12, 9))
        self.assertFalse(kline_batch.valid_ohlc(10, 11, 8, 9))
        self.assertTrue(quantmind_daily_pipeline.valid_ohlc(10, 11, 12, 9))
        self.assertFalse(quantmind_daily_pipeline.valid_ohlc(10, 13, 12, 9))

    def test_run_update_can_target_us_without_hk_batch(self):
        db_results = {
            "SELECT symbol FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE') ORDER BY symbol": "AAPL\nMSFT\n",
            "SELECT count(DISTINCT symbol) FROM klines WHERE interval='day'": "2",
            "SELECT max(timestamp) FROM klines WHERE interval='day'": "2026-06-12",
        }

        def fake_db(sql):
            if sql in db_results:
                return db_results[sql]
            raise AssertionError(f"unexpected db query: {sql}")

        with patch.object(kline_batch, "db", side_effect=fake_db), patch.object(
            kline_batch,
            "process_batch",
            side_effect=AssertionError("HK batch should not run"),
        ), patch.object(kline_batch, "process_us_symbols", return_value=(2, 0)) as process_us:
            ok = kline_batch.run_update("us", now=datetime(2026, 6, 18, 6, 0))

        self.assertTrue(ok)
        process_us.assert_called_once_with(["AAPL", "MSFT"])

    def test_run_update_allows_partial_symbol_failures_above_success_threshold(self):
        db_results = {
            "SELECT symbol FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE') ORDER BY symbol": "AAPL\nMSFT\nBAD\n",
            "SELECT count(DISTINCT symbol) FROM klines WHERE interval='day'": "3",
            "SELECT max(timestamp) FROM klines WHERE interval='day'": "2026-06-12",
        }

        def fake_db(sql):
            if sql in db_results:
                return db_results[sql]
            raise AssertionError(f"unexpected db query: {sql}")

        with patch.object(kline_batch, "db", side_effect=fake_db), patch.object(
            kline_batch,
            "process_us_symbols",
            return_value=(2, 1),
        ), patch.object(kline_batch, "MIN_SUCCESS_RATE", 0.60):
            ok = kline_batch.run_update("us", now=datetime(2026, 6, 18, 6, 0))

        self.assertTrue(ok)

    def test_run_update_blocks_us_current_session_daily_refresh(self):
        with patch.object(kline_batch, "db") as db_mock, patch.object(
            kline_batch,
            "process_us_symbols",
        ) as process_us:
            ok = kline_batch.run_update("us", now=datetime(2026, 6, 17, 21, 30))

        self.assertFalse(ok)
        process_us.assert_not_called()
        db_mock.assert_any_call("SELECT count(DISTINCT symbol) FROM klines WHERE interval='day'")

    def test_kline_batch_flushes_multiple_statements_individually_by_default(self):
        calls = []

        def result(code, stderr=""):
            return type("Result", (), {"returncode": code, "stdout": "", "stderr": stderr})()

        def fake_db_batch(path):
            calls.append(path)
            return result(0)

        sql_one = "INSERT INTO klines (symbol) VALUES ('00700');"
        sql_two = "INSERT INTO klines (symbol) VALUES ('01810');"
        with patch.object(kline_batch, "db_batch", side_effect=fake_db_batch), patch.object(
            kline_batch,
            "ALLOW_MULTI_SYMBOL_TRANSACTION",
            False,
        ):
            ok, fail = kline_batch._flush_batch([sql_one, sql_two])

        self.assertEqual((ok, fail), (2, 0))
        self.assertEqual(len(calls), 2)

    def test_kline_batch_process_counts_actual_write_results(self):
        kline = [["2026-06-12", "10", "11", "12", "9", "100"]]
        with patch.object(kline_batch, "fetch_kline", return_value=kline), patch.object(
            kline_batch,
            "_flush_batch",
            return_value=(1, 1),
        ), patch.object(kline_batch.time, "sleep", return_value=None):
            ok, fail = kline_batch.process_batch(["00700", "01810"], "hk", "tencent")

        self.assertEqual((ok, fail), (1, 1))

    def test_kline_batch_process_us_counts_write_failures(self):
        kline = [["2026-06-12", "10", "11", "12", "9", "100"]]
        with patch.object(kline_batch, "alpaca_auth_available", return_value=False), patch.object(
            kline_batch,
            "fetch_kline",
            return_value=kline,
        ), patch.object(
            kline_batch,
            "_flush_batch",
            side_effect=[(1, 0), (0, 1)],
        ), patch.object(kline_batch.time, "sleep", return_value=None):
            ok, fail = kline_batch.process_us_symbols(["AAPL", "SQ"])

        self.assertEqual((ok, fail), (1, 1))

    def test_kline_batch_process_us_prefers_alpaca_when_configured(self):
        bars = {
            "AAPL": [["2026-06-12", 10.0, 11.0, 12.0, 9.0, 1000.0]],
            "MSFT": [["2026-06-12", 20.0, 21.0, 22.0, 19.0, 2000.0]],
        }
        flushed = []

        def fake_flush(sql_buf):
            flushed.extend(sql_buf)
            return (1, 0)

        with patch.object(kline_batch, "alpaca_auth_available", return_value=True), patch.object(
            kline_batch,
            "fetch_alpaca_us_daily_rows",
            return_value=bars,
        ) as alpaca_fetch, patch.object(kline_batch, "fetch_kline", side_effect=AssertionError("Tencent should not be used")), patch.object(
            kline_batch,
            "_flush_batch",
            side_effect=fake_flush,
        ), patch.object(kline_batch.time, "sleep", return_value=None):
            ok, fail = kline_batch.process_us_symbols(["AAPL", "MSFT"])

        self.assertEqual((ok, fail), (2, 0))
        alpaca_fetch.assert_called_once_with(["AAPL", "MSFT"])
        self.assertEqual(len(flushed), 2)
        self.assertTrue(all("alpaca_market_data" in sql for sql in flushed))

    def test_kline_batch_alpaca_rows_parse_and_skip_invalid_symbols(self):
        payloads = [
            {
                "bars": {
                    "AAPL": [
                        {"t": "2026-06-11T04:00:00Z", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100},
                        {"t": "2026-06-12T04:00:00Z", "o": 11, "h": 13, "l": 10, "c": 12, "v": 200},
                    ]
                },
                "next_page_token": None,
            }
        ]
        requests_seen = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                import json

                return json.dumps(self.payload).encode()

        def fake_urlopen(req, timeout=60):
            requests_seen.append(req.full_url)
            return FakeResponse(payloads.pop(0))

        with patch.object(kline_batch, "ALPACA_API_KEY_ID", "key"), patch.object(
            kline_batch,
            "ALPACA_API_SECRET_KEY",
            "secret",
        ), patch.object(kline_batch.urllib.request, "urlopen", side_effect=fake_urlopen):
            rows = kline_batch.fetch_alpaca_us_daily_rows(["AAPL", "BAD^A"], count=1, now=datetime(2026, 6, 18, 6, 0))

        self.assertEqual(rows, {"AAPL": [["2026-06-12", 11.0, 12.0, 13.0, 10.0, 200.0]]})
        self.assertIn("symbols=AAPL", requests_seen[0])
        self.assertNotIn("BAD", requests_seen[0])

    def test_kline_batch_lock_raises_when_already_running(self):
        with tempfile.TemporaryDirectory() as td, patch.object(
            kline_batch,
            "lock_handle",
            side_effect=kline_batch.AlreadyRunning("busy"),
        ):
            with self.assertRaises(kline_batch.AlreadyRunning):
                with kline_batch.SingleInstanceLock(f"{td}/kline.lock"):
                    pass


if __name__ == "__main__":
    unittest.main()
