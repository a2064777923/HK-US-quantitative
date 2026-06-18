import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from scripts import data_health_report as report


def stock(market, symbol):
    return {"market": market, "symbol": symbol}


def kline(
    market,
    symbol,
    day,
    close=100,
    high=105,
    low=95,
    open_price=100,
    data_source="tencent",
    latest_minute_date=None,
    latest_minute_data_source=None,
):
    return {
        "market": market,
        "symbol": symbol,
        "date": day,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "data_source": data_source,
        "latest_minute_date": latest_minute_date,
        "latest_minute_data_source": latest_minute_data_source,
    }


def history(market, symbol, latest_day, days=70, close=100, latest_minute_date=None):
    start = date.fromisoformat(latest_day) - timedelta(days=days - 1)
    rows = []
    for idx in range(days):
        current_close = close + idx
        is_latest = idx == days - 1
        rows.append(
            kline(
                market,
                symbol,
                (start + timedelta(days=idx)).isoformat(),
                close=current_close,
                high=current_close + 5,
                low=current_close - 5,
                open_price=current_close,
                latest_minute_date=latest_minute_date if is_latest else None,
                latest_minute_data_source="tencent" if is_latest and latest_minute_date else None,
            )
        )
    return rows


def signal(market, latest_day, count=2):
    return {
        "market": market,
        "latest_signal_date": latest_day,
        "signal_count": count,
        "buy_count": 1,
        "hold_count": 1,
        "sell_count": 0,
    }


def feature(status="signal_ready", ready=2, expected=2):
    return {
        "run_id": "signal_v4_20260612",
        "trade_date": "2026-06-12",
        "status": status,
        "expected_count": expected,
        "ready_count": ready,
        "missing_count": max(expected - ready, 0),
        "created_at": "2026-06-12 16:30:00",
        "updated_at": "2026-06-12 16:30:00",
    }


class DataHealthReportTests(unittest.TestCase):
    def test_fetch_kline_rows_projects_market_date_for_history_window(self):
        captured = {}

        def fake_psql(sql, timeout=90):
            captured["sql"] = sql
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        original_cache = dict(report._COLUMN_CACHE)
        try:
            report._COLUMN_CACHE["klines"] = {"data_source"}
            with patch.object(report, "psql", side_effect=fake_psql):
                rows, warnings = report.fetch_kline_rows()
        finally:
            report._COLUMN_CACHE.clear()
            report._COLUMN_CACHE.update(original_cache)

        self.assertEqual(rows, [])
        self.assertEqual(warnings, [])
        self.assertIn("market_clock.market_date", captured["sql"])
        self.assertIn("AND k.timestamp >= (a.market_date - INTERVAL '120 days')", captured["sql"])

    def test_ok_when_latest_klines_integrity_and_signals_are_current(self):
        stocks = [stock("HK", "00700"), stock("HK", "09988")]
        rows = history("HK", "00700", "2026-06-12") + history("HK", "09988", "2026-06-12")

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("HK", "2026-06-12")],
            feature_run_rows=[feature()],
            current_dt=datetime(2026, 6, 12, 17, 0),
        )

        hk = payload["markets"]["HK"]
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(hk["status"], "OK")
        self.assertEqual(hk["coverage"]["latest_date_coverage_pct"], 100.0)
        self.assertEqual(hk["coverage"]["history_60d_coverage_pct"], 100.0)
        self.assertEqual(hk["integrity"]["invalid_latest_ohlc_count"], 0)
        self.assertEqual(hk["source_quality"]["schema"], "kline_source_quality_v1")
        self.assertEqual(hk["source_quality"]["daily_latest_source_counts"], {"tencent": 2})
        self.assertEqual(hk["source_quality"]["repair_daily_latest_count"], 0)
        self.assertEqual(hk["source_quality"]["missing_daily_latest_source_count"], 0)
        self.assertEqual(hk["source_quality"]["daily_minute_source_mismatch_count"], 0)
        self.assertEqual(payload["recommendations"], ["data_health_ok_for_review_context"])

    def test_warns_when_active_symbol_is_stale_and_signal_lags(self):
        stocks = [stock("HK", "00700"), stock("HK", "09988")]
        rows = history("HK", "00700", "2026-06-12") + history("HK", "09988", "2026-06-10")

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("HK", "2026-06-10")],
            feature_run_rows=[feature()],
            current_dt=datetime(2026, 6, 12, 17, 0),
        )

        hk = payload["markets"]["HK"]
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(hk["coverage"]["stale_vs_market_latest_count"], 1)
        self.assertIn("active_symbols_stale_vs_market_latest", hk["warnings"])
        self.assertIn("signal_rows_lag_latest_klines", hk["warnings"])
        self.assertIn("HK:review_data_warning:active_symbols_stale_vs_market_latest", payload["recommendations"])

    def test_flags_minute_fresh_daily_stale_symbols(self):
        stocks = [stock("HK", "00700"), stock("HK", "00959")]
        rows = history("HK", "00700", "2026-06-12") + history(
            "HK",
            "00959",
            "2025-06-25",
            latest_minute_date="2026-06-12",
        )

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("HK", "2026-06-12")],
            feature_run_rows=[feature()],
            current_dt=datetime(2026, 6, 12, 17, 0),
        )

        hk = payload["markets"]["HK"]
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(hk["coverage"]["stale_vs_market_latest_count"], 1)
        self.assertEqual(hk["coverage"]["minute_fresh_daily_stale_count"], 1)
        self.assertIn("minute_fresh_daily_stale_symbols", hk["warnings"])
        self.assertEqual(hk["sample_minute_fresh_daily_stale_symbols"][0]["symbol"], "00959")
        self.assertEqual(hk["sample_minute_fresh_daily_stale_symbols"][0]["latest_minute_date"], "2026-06-12")
        self.assertIn("HK:review_data_warning:minute_fresh_daily_stale_symbols", payload["recommendations"])
        remediation = payload["daily_gap_remediation"]
        self.assertEqual(remediation["schema"], "kline_daily_gap_remediation_v1")
        self.assertEqual(remediation["status"], "operator_action_required")
        self.assertEqual(remediation["gap_symbol_count"], 1)
        self.assertFalse(remediation["submits_orders"])
        self.assertFalse(remediation["changes_crontab"])
        self.assertTrue(remediation["write_command_requires_operator"])
        self.assertIn("kline_daily_gap_repair.py", remediation["dry_run_command"])
        self.assertIn("--confirm-plan-hash <plan_hash>", remediation["apply_command_template"])
        text = report.build_text_report(payload)
        self.assertIn("daily_gap_remediation:", text)

    def test_flags_repair_latest_data_sources_as_warning_context(self):
        stocks = [stock("HK", "00700"), stock("HK", "01918")]
        rows = history("HK", "00700", "2026-06-12", latest_minute_date="2026-06-12") + history(
            "HK",
            "01918",
            "2026-06-12",
            latest_minute_date="2026-06-12",
        )
        rows[-1]["data_source"] = "tencent_day_repair"

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("HK", "2026-06-12")],
            feature_run_rows=[feature()],
            current_dt=datetime(2026, 6, 12, 17, 0),
        )

        hk = payload["markets"]["HK"]
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("daily_latest_contains_repair_sources", hk["warnings"])
        self.assertEqual(hk["source_quality"]["repair_daily_latest_count"], 1)
        self.assertEqual(hk["source_quality"]["repair_daily_latest_pct"], 50.0)
        self.assertEqual(hk["source_quality"]["sample_repair_daily_latest_symbols"][0]["symbol"], "01918")
        self.assertEqual(hk["source_quality"]["daily_minute_source_mismatch_count"], 1)
        self.assertEqual(
            hk["source_quality"]["sample_daily_minute_source_mismatches"][0]["daily_source_family"],
            "repair",
        )
        self.assertIn("HK:review_data_warning:daily_latest_contains_repair_sources", payload["recommendations"])

    def test_flags_missing_latest_data_source_as_warning_context(self):
        stocks = [stock("US", "AAPL")]
        rows = history("US", "AAPL", "2026-06-12")
        rows[-1]["data_source"] = None

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("US", "2026-06-12", count=1)],
            feature_run_rows=[feature(expected=1, ready=1)],
            current_dt=datetime(2026, 6, 13, 6, 0),
        )

        us = payload["markets"]["US"]
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("daily_latest_data_source_missing", us["warnings"])
        self.assertEqual(us["source_quality"]["daily_latest_source_counts"], {"missing": 1})
        self.assertEqual(us["source_quality"]["daily_latest_source_coverage_pct"], 0.0)
        self.assertEqual(us["source_quality"]["sample_missing_daily_latest_source_symbols"], ["AAPL"])

    def test_us_session_ignores_same_day_provisional_daily_bar_for_signal_freshness(self):
        stocks = [stock("US", "AAPL")]
        rows = history("US", "AAPL", "2026-06-16")
        rows.append(
            kline(
                "US",
                "AAPL",
                "2026-06-17",
                close=185,
                high=186,
                low=180,
                open_price=181,
                data_source="alpaca_provisional",
            )
        )

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("US", "2026-06-16", count=1)],
            feature_run_rows=[feature(expected=1, ready=1)],
            current_dt=datetime(2026, 6, 17, 22, 0),
        )

        us = payload["markets"]["US"]
        self.assertEqual(us["expected_completed_date"], "2026-06-16")
        self.assertEqual(us["latest_date"], "2026-06-16")
        self.assertEqual(us["newest_daily_date"], "2026-06-17")
        self.assertEqual(us["signals"]["status"], "OK")
        self.assertNotIn("signal_rows_lag_latest_klines", us["warnings"])

    def test_open_close_outside_high_low_are_soft_warnings_not_failures(self):
        stocks = [stock("US", "AAPL")]
        rows = history("US", "AAPL", "2026-06-12")
        rows[-1]["close"] = 110
        rows[-1]["high"] = 105
        rows[-1]["low"] = 95
        rows[-1]["open"] = 94

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("US", "2026-06-12", count=1)],
            feature_run_rows=[feature(expected=1, ready=1)],
            current_dt=datetime(2026, 6, 13, 6, 0),
        )

        us = payload["markets"]["US"]
        self.assertEqual(payload["status"], "WARN")
        self.assertNotIn("invalid_latest_ohlc", us["failures"])
        self.assertIn("latest_ohlc_range_soft_warnings", us["warnings"])
        self.assertEqual(us["integrity"]["invalid_latest_ohlc_count"], 0)
        self.assertEqual(us["integrity"]["latest_ohlc_warning_count"], 1)
        self.assertIn("close_outside_high_low", us["integrity"]["latest_ohlc_warning_examples"][0]["warnings"])
        self.assertIn("open_outside_high_low", us["integrity"]["latest_ohlc_warning_examples"][0]["warnings"])

    def test_invalid_latest_ohlc_fails_report(self):
        stocks = [stock("US", "AAPL")]
        rows = history("US", "AAPL", "2026-06-12")
        rows[-1]["high"] = 90
        rows[-1]["low"] = 95

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("US", "2026-06-12", count=1)],
            feature_run_rows=[feature(expected=1, ready=1)],
            current_dt=datetime(2026, 6, 13, 6, 0),
        )

        us = payload["markets"]["US"]
        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("invalid_latest_ohlc", us["failures"])
        self.assertEqual(us["integrity"]["invalid_latest_ohlc_count"], 1)
        self.assertIn("high_below_low", us["integrity"]["invalid_latest_ohlc_examples"][0]["errors"])

    def test_duplicate_daily_symbol_dates_fail_report(self):
        stocks = [stock("US", "AAPL")]
        rows = history("US", "AAPL", "2026-06-12")
        rows[-1]["duplicate_symbol_day_count"] = 2
        rows[-1]["raw_symbol_day_row_count"] = 3

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("US", "2026-06-12", count=1)],
            feature_run_rows=[feature(expected=1, ready=1)],
            current_dt=datetime(2026, 6, 13, 6, 0),
        )

        us = payload["markets"]["US"]
        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("duplicate_daily_symbol_dates", us["failures"])
        self.assertEqual(us["integrity"]["duplicate_daily_symbol_date_count"], 2)
        self.assertEqual(us["integrity"]["duplicate_daily_symbol_date_counts_by_date"], {"2026-06-12": 2})
        self.assertEqual(us["integrity"]["duplicate_daily_symbol_date_examples"][0]["symbol"], "AAPL")
        self.assertEqual(us["integrity"]["duplicate_daily_symbol_date_examples"][0]["raw_symbol_day_row_count"], 3)
        self.assertIn(
            "US:block_execution_until_data_failure_fixed:duplicate_daily_symbol_dates",
            payload["recommendations"],
        )
        self.assertIn("duplicate_day_rows=2", report.build_text_report(payload))

    def test_feature_run_not_ready_warns_without_data_failure(self):
        stocks = [stock("US", "AAPL")]
        rows = history("US", "AAPL", "2026-06-12")

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("US", "2026-06-12", count=1)],
            feature_run_rows=[feature(status="feature_ready", ready=0, expected=1)],
            current_dt=datetime(2026, 6, 13, 6, 0),
        )

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["feature_run"]["status"], "WARN")
        self.assertIn("review_signal_v4_feature_run_before_trusting_new_daily_signals", payload["recommendations"])

    def test_intraday_current_day_signal_run_fails_until_full_day_ready(self):
        stocks = [stock("HK", "00700")]
        rows = history("HK", "00700", "2026-06-12")
        intraday_feature = feature(expected=1, ready=1)
        intraday_feature["created_at"] = "2026-06-12 09:34:00"
        intraday_feature["updated_at"] = "2026-06-12 09:34:00"

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("HK", "2026-06-12", count=1)],
            feature_run_rows=[intraday_feature],
            current_dt=datetime(2026, 6, 12, 11, 30),
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["feature_run"]["status"], "FAIL")
        self.assertIn("current_session_before_daily_signal_ready_time", payload["feature_run"]["notes"])
        self.assertIn("latest_daily_signal_run_generated_before_full_day_cutoff", payload["feature_run"]["notes"])
        self.assertIn("block_execution_until_signal_v4_full_day_run_ready", payload["recommendations"])

        text = report.build_text_report(payload)
        self.assertIn("feature_run_notes:", text)
        self.assertIn("current_session_before_daily_signal_ready_time", text)

    def test_post_cutoff_intraday_signal_run_recommends_operator_rerun(self):
        stocks = [stock("HK", "00700")]
        rows = history("HK", "00700", "2026-06-12")
        intraday_feature = feature(expected=1, ready=1)
        intraday_feature["created_at"] = "2026-06-12 09:34:00"
        intraday_feature["updated_at"] = "2026-06-12 09:34:00"

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("HK", "2026-06-12", count=1)],
            feature_run_rows=[intraday_feature],
            current_dt=datetime(2026, 6, 12, 17, 0),
        )

        remediation = payload["feature_run"]["remediation"]
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(remediation["schema"], "signal_v4_daily_run_remediation_v1")
        self.assertEqual(remediation["status"], "operator_action_required")
        self.assertEqual(remediation["latest_run_id"], "signal_v4_20260612")
        self.assertTrue(remediation["current_after_cutoff"])
        self.assertTrue(remediation["latest_generated_before_cutoff"])
        self.assertFalse(remediation["submits_orders"])
        self.assertEqual(
            remediation["required_action"],
            "run_signal_engine_v4_post_close_under_operator_control",
        )
        self.assertIn("--preflight --json", remediation["safe_preflight_command"])
        self.assertIn("signal_engine_v4.py", remediation["manual_write_command"])

        text = report.build_text_report(payload)
        self.assertIn("feature_run_remediation:", text)
        self.assertIn("run_signal_engine_v4_post_close_under_operator_control", text)

    def test_quality_generated_at_can_prove_post_cutoff_rerun_when_metadata_lags(self):
        stocks = [stock("HK", "00700")]
        rows = history("HK", "00700", "2026-06-12")
        lagged_feature = feature(expected=1, ready=1)
        lagged_feature["created_at"] = "2026-06-12 09:34:00"
        lagged_feature["updated_at"] = "2026-06-12 09:34:00"
        lagged_feature["quality_generated_at"] = "2026-06-12T21:34:49"

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("HK", "2026-06-12", count=1)],
            feature_run_rows=[lagged_feature],
            current_dt=datetime(2026, 6, 12, 23, 0),
        )

        feature_run = payload["feature_run"]
        latest = feature_run["latest"]
        remediation = feature_run["remediation"]
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(feature_run["status"], "OK")
        self.assertEqual(latest["effective_generated_at"], "2026-06-12T21:34:49")
        self.assertEqual(latest["effective_generated_at_source"], "quality_generated_at")
        self.assertIn("feature_run_metadata_timestamp_lagged_quality_generated_at", feature_run["notes"])
        self.assertNotIn("latest_daily_signal_run_generated_before_full_day_cutoff", feature_run["notes"])
        self.assertFalse(remediation["latest_generated_before_cutoff"])
        self.assertEqual(remediation["required_action"], "none")

        text = report.build_text_report(payload)
        self.assertIn("effective_generated_at=2026-06-12T21:34:49", text)

    def test_quality_generated_at_before_cutoff_still_blocks_when_metadata_lags(self):
        stocks = [stock("HK", "00700")]
        rows = history("HK", "00700", "2026-06-12")
        lagged_feature = feature(expected=1, ready=1)
        lagged_feature["created_at"] = "2026-06-12 09:34:00"
        lagged_feature["updated_at"] = "2026-06-12 09:34:00"
        lagged_feature["quality_generated_at"] = "2026-06-12T10:15:00"

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("HK", "2026-06-12", count=1)],
            feature_run_rows=[lagged_feature],
            current_dt=datetime(2026, 6, 12, 23, 0),
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["feature_run"]["status"], "FAIL")
        self.assertIn(
            "latest_daily_signal_run_generated_before_full_day_cutoff",
            payload["feature_run"]["notes"],
        )


if __name__ == "__main__":
    unittest.main()
