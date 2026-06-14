import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import intraday_context_report as report


def minute_row(
    symbol,
    timestamp,
    close,
    market="HK",
    open_price=None,
    high=None,
    low=None,
    volume=100,
    data_source="tencent_min",
    source_granularity="missing",
):
    open_price = close if open_price is None else open_price
    high = max(open_price, close) if high is None else high
    low = min(open_price, close) if low is None else low
    return {
        "market": market,
        "symbol": symbol,
        "timestamp": timestamp.isoformat(timespec="seconds"),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": close * volume,
        "data_source": data_source,
        "source_granularity": source_granularity,
    }


class IntradayContextReportTests(unittest.TestCase):
    def test_build_report_summarizes_5m_15m_and_60m_intraday_context(self):
        now = datetime(2026, 6, 12, 10, 9)
        start = datetime(2026, 6, 12, 9, 30)
        rows = []
        for idx in range(40):
            ts = start + timedelta(minutes=idx)
            close = 100 + idx * 0.2
            rows.append(minute_row("00700", ts, close, open_price=close - 0.05, volume=100 + idx))

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
        )

        hk = payload["markets"]["HK"]
        item = hk["symbols"][0]
        self.assertEqual(payload["schema"], "intraday_context_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertEqual(payload["granularity_policy"]["schema"], "intraday_granularity_usage_policy_v1")
        self.assertTrue(payload["granularity_policy"]["daily_forward_outcomes_remain_authority"])
        self.assertEqual(
            payload["granularity_policy"]["timeframes"]["60m"]["role"],
            "intraday_regime_confirmation_or_challenge",
        )
        self.assertIn(
            "core_alpha_generation",
            payload["granularity_policy"]["timeframes"]["1m"]["forbidden_uses"],
        )
        self.assertEqual(item["status"], "OK")
        self.assertGreater(item["session"]["change_pct"], 3.0)
        self.assertEqual(item["session"]["momentum"], "strong_up")
        self.assertEqual(item["latest_5m"]["momentum"], "up")
        self.assertEqual(item["latest_15m"]["momentum"], "strong_up")
        self.assertEqual(item["multi_timeframe_confirmation"]["alignment"], "bullish_aligned")
        self.assertTrue(item["multi_timeframe_confirmation"]["buy_confirmation"])
        self.assertFalse(item["multi_timeframe_confirmation"]["sell_confirmation"])
        self.assertTrue(item["recent_5m_bars"])
        self.assertTrue(item["recent_60m_bars"])
        self.assertIn("intraday_session_up_supports_buy_review", item["hermes_notes"])
        self.assertIn("intraday_multi_timeframe_bullish_challenges_sell_review", item["hermes_notes"])

    def test_multi_timeframe_conflict_is_explicit_for_hermes(self):
        now = datetime(2026, 6, 12, 10, 9)
        start = datetime(2026, 6, 12, 9, 30)
        rows = []
        for idx in range(30):
            ts = start + timedelta(minutes=idx)
            close = 100 - idx * 0.03
            rows.append(minute_row("00700", ts, close, open_price=close + 0.02, volume=100))
        for idx, close in enumerate([98.5, 98.0, 97.2, 96.4, 95.8], start=30):
            ts = start + timedelta(minutes=idx)
            rows.append(minute_row("00700", ts, close, open_price=close + 0.05, volume=160))
        for idx in range(35, 40):
            ts = start + timedelta(minutes=idx)
            close = 96.0 + (idx - 35) * 0.25
            rows.append(minute_row("00700", ts, close, open_price=close - 0.05, volume=200))

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
        )

        item = payload["markets"]["HK"]["symbols"][0]
        mtf = item["multi_timeframe_confirmation"]
        self.assertEqual(mtf["schema"], "intraday_multi_timeframe_confirmation_v1")
        self.assertEqual(mtf["alignment"], "mixed_bearish")
        self.assertIn("latest_5m_contradicts_session", mtf["contradictions"])
        self.assertIn("latest_5m_contradicts_latest_60m", mtf["contradictions"])
        self.assertIn("intraday_timeframes_conflicting_requires_disclosure", item["hermes_notes"])

    def test_stale_intraday_context_is_visible_but_read_only(self):
        now = datetime(2026, 6, 12, 11, 0)
        rows = [
            minute_row("AAPL", datetime(2026, 6, 12, 10, 0), 100, market="US"),
            minute_row("AAPL", datetime(2026, 6, 12, 10, 1), 99, market="US"),
        ]

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": [], "US": ["AAPL"]},
            now=now,
        )

        us = payload["markets"]["US"]
        item = us["symbols"][0]
        self.assertEqual(payload["status"], "STALE")
        self.assertEqual(item["status"], "STALE")
        self.assertIn("intraday_context_stale_for_symbol", item["hermes_notes"])
        self.assertIn("US:refresh_intraday_context_before_trade_judgment", payload["recommendations"])

    def test_closed_market_intraday_context_is_not_misclassified_as_fetch_failure(self):
        now = datetime(2026, 6, 13, 10, 0)
        rows = [
            minute_row("00700", datetime(2026, 6, 12, 15, 58), 100),
            minute_row("00700", datetime(2026, 6, 12, 15, 59), 100.2),
        ]

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
        )

        hk = payload["markets"]["HK"]
        item = hk["symbols"][0]

        self.assertEqual(payload["status"], "CLOSED")
        self.assertEqual(hk["status"], "CLOSED")
        self.assertEqual(item["status"], "CLOSED")
        self.assertEqual(payload["summary"]["closed_symbol_count"], 1)
        self.assertEqual(payload["summary"]["stale_symbol_count"], 0)
        self.assertEqual(item["market_session"]["phase"], "CLOSED_WEEKEND")
        self.assertFalse(item["market_session"]["holiday_calendar_applied"])
        self.assertIn("intraday_market_not_open_requires_session_context", item["hermes_notes"])
        self.assertNotIn("intraday_context_stale_for_symbol", item["hermes_notes"])
        self.assertIn("HK:intraday_market_closed_use_last_session_context_only", payload["recommendations"])

    def test_closed_market_without_rows_is_closed_not_fetch_failure(self):
        now = datetime(2026, 6, 13, 10, 0)

        payload = report.build_report(
            intraday_rows=[],
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
        )

        hk = payload["markets"]["HK"]
        item = hk["symbols"][0]

        self.assertEqual(payload["status"], "CLOSED")
        self.assertEqual(hk["status"], "CLOSED")
        self.assertEqual(item["status"], "CLOSED")
        self.assertEqual(payload["summary"]["closed_symbol_count"], 1)
        self.assertEqual(payload["summary"]["missing_symbol_count"], 0)
        self.assertIn("missing_intraday_rows", item["warnings"])
        self.assertIn("intraday_context_missing_for_symbol", item["hermes_notes"])
        self.assertIn("intraday_market_not_open_requires_session_context", item["hermes_notes"])
        self.assertIn("HK:intraday_market_closed_use_last_session_context_only", payload["recommendations"])

    def test_us_intraday_age_uses_market_local_time_for_aware_now(self):
        now = datetime(2026, 6, 12, 14, 5, tzinfo=timezone.utc)
        rows = [
            minute_row("AAPL", datetime(2026, 6, 12, 10, 0), 100, market="US"),
        ]

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": [], "US": ["AAPL"]},
            now=now,
        )

        item = payload["markets"]["US"]["symbols"][0]

        self.assertEqual(item["market_session"]["phase"], "REGULAR_OPEN")
        self.assertEqual(item["latest_age_minutes"], 5.0)
        self.assertEqual(item["status"], "OK")

    def test_market_session_closed_date_override_prevents_false_regular_open(self):
        now = datetime(2026, 6, 12, 10, 5)
        rows = [
            minute_row("00700", datetime(2026, 6, 11, 15, 59), 100),
        ]

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
            market_session_overrides={
                "HK": {
                    "closed_dates": {"2026-06-12": "hkex_holiday_unit_test"},
                }
            },
            market_session_overrides_file="/root/intraday_market_sessions.json",
        )

        session = payload["markets"]["HK"]["market_session"]
        item = payload["markets"]["HK"]["symbols"][0]

        self.assertEqual(payload["status"], "CLOSED")
        self.assertEqual(item["status"], "CLOSED")
        self.assertEqual(session["phase"], "CLOSED_HOLIDAY")
        self.assertTrue(session["holiday_calendar_applied"])
        self.assertTrue(session["override_applied"])
        self.assertEqual(session["override_reason"], "hkex_holiday_unit_test")
        self.assertIn("intraday_market_not_open_requires_session_context", item["hermes_notes"])

    def test_market_session_half_day_override_prevents_after_close_false_open(self):
        now = datetime(2026, 6, 12, 13, 0)
        rows = [
            minute_row("00700", datetime(2026, 6, 12, 12, 0), 100),
        ]

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
            market_session_overrides={
                "HK": {
                    "half_days": {
                        "2026-06-12": {
                            "reason": "hkex_half_day_unit_test",
                            "session_windows": [{"open": "09:30", "close": "12:00"}],
                        }
                    }
                }
            },
            market_session_overrides_file="/root/intraday_market_sessions.json",
        )

        session = payload["markets"]["HK"]["market_session"]
        item = payload["markets"]["HK"]["symbols"][0]

        self.assertEqual(payload["status"], "CLOSED")
        self.assertEqual(item["status"], "CLOSED")
        self.assertEqual(session["phase"], "AFTER_CLOSE")
        self.assertTrue(session["holiday_calendar_applied"])
        self.assertEqual(session["regular_session"], "09:30-12:00")
        self.assertEqual(session["override_reason"], "hkex_half_day_unit_test")
        self.assertIn("HK:intraday_market_closed_use_last_session_context_only", payload["recommendations"])

    def test_missing_intraday_rows_are_reported_per_symbol(self):
        payload = report.build_report(
            intraday_rows=[],
            symbols_by_market={"HK": ["00700"], "US": []},
            now=datetime(2026, 6, 12, 10, 0),
        )

        item = payload["markets"]["HK"]["symbols"][0]
        self.assertEqual(payload["status"], "MISSING")
        self.assertEqual(item["status"], "MISSING")
        self.assertIn("intraday_context_missing_for_symbol", item["hermes_notes"])

    def test_degraded_minute_quality_is_visible_without_becoming_execution_signal(self):
        now = datetime(2026, 6, 12, 10, 30)
        rows = [
            minute_row("00700", datetime(2026, 6, 12, 9, 30), 100),
            minute_row("00700", datetime(2026, 6, 12, 9, 31), 100.2),
            minute_row("00700", datetime(2026, 6, 12, 9, 31), 100.25),
            minute_row("00700", datetime(2026, 6, 12, 9, 45), 100.5, high=100.1, low=100.2),
            {
                **minute_row("00700", datetime(2026, 6, 12, 9, 46), 100.6),
                "data_source": "missing",
            },
            minute_row("00700", datetime(2026, 6, 12, 9, 47), 100.7),
        ]

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
        )

        item = payload["markets"]["HK"]["symbols"][0]
        quality = item["quality"]
        summary = payload["summary"]

        self.assertEqual(quality["schema"], "intraday_symbol_quality_v1")
        self.assertEqual(quality["status"], "WARN")
        self.assertEqual(quality["invalid_ohlc_count"], 1)
        self.assertEqual(quality["duplicate_timestamp_count"], 1)
        self.assertEqual(quality["large_gap_count"], 1)
        self.assertEqual(quality["missing_data_source_count"], 1)
        self.assertEqual(summary["quality_degraded_symbol_count"], 1)
        self.assertEqual(summary["large_gap_symbol_count"], 1)
        self.assertEqual(summary["invalid_ohlc_symbol_count"], 1)
        self.assertIn("intraday_context_quality_degraded_requires_disclosure", item["hermes_notes"])
        self.assertIn("HK:review_intraday_quality_before_trade_judgment", payload["recommendations"])
        self.assertIn("HK:refresh_or_repair_minute_kline_gap_coverage", payload["recommendations"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_strategy"])

    def test_low_fidelity_snapshot_minute_provenance_is_visible_for_hermes(self):
        now = datetime(2026, 6, 12, 10, 9)
        start = datetime(2026, 6, 12, 9, 30)
        rows = [
            minute_row(
                "00700",
                start + timedelta(minutes=idx),
                100 + idx * 0.1,
                data_source="tencent_minute_query",
                source_granularity="minute_snapshot_price",
            )
            for idx in range(40)
        ]

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
        )

        item = payload["markets"]["HK"]["symbols"][0]
        quality = item["quality"]

        self.assertEqual(quality["status"], "WARN")
        self.assertEqual(quality["low_fidelity_point_count"], 40)
        self.assertEqual(quality["snapshot_like_row_count"], 40)
        self.assertEqual(quality["full_ohlc_row_count"], 0)
        self.assertEqual(item["source_granularities"], ["minute_snapshot_price"])
        self.assertIn("low_fidelity_intraday_source", quality["notes"])
        self.assertIn("snapshot_like_intraday_rows", quality["notes"])
        self.assertIn("intraday_context_quality_degraded_requires_disclosure", item["hermes_notes"])
        self.assertEqual(payload["summary"]["low_fidelity_source_symbol_count"], 1)
        self.assertEqual(payload["summary"]["snapshot_like_symbol_count"], 1)
        self.assertIn(
            "HK:treat_public_snapshot_minute_rows_as_advisory_until_full_ohlcv_source",
            payload["recommendations"],
        )

    def test_rolling_windows_expose_15m_30m_and_60m_coverage_limits(self):
        now = datetime(2026, 6, 12, 9, 49)
        start = datetime(2026, 6, 12, 9, 30)
        rows = [
            minute_row(
                "00700",
                start + timedelta(minutes=idx),
                100 + idx * 0.1,
                open_price=100 + idx * 0.1 - 0.02,
                data_source="broker_minute_ohlcv",
                source_granularity="minute_ohlcv",
            )
            for idx in range(20)
        ]

        payload = report.build_report(
            intraday_rows=rows,
            symbols_by_market={"HK": ["00700"], "US": []},
            now=now,
        )

        item = payload["markets"]["HK"]["symbols"][0]

        self.assertEqual(item["latest_5m"]["coverage_status"], "OK")
        self.assertEqual(item["latest_5m"]["row_count"], 5)
        self.assertEqual(item["latest_15m"]["coverage_status"], "OK")
        self.assertEqual(item["latest_15m"]["row_count"], 15)
        self.assertEqual(item["latest_30m"]["coverage_status"], "LIMITED")
        self.assertEqual(item["latest_30m"]["row_count"], 20)
        self.assertEqual(item["latest_60m"]["coverage_status"], "LIMITED")
        self.assertEqual(item["latest_60m"]["row_count"], 20)
        self.assertEqual(item["rolling_windows"]["15m"]["expected_minute_count"], 15)
        self.assertEqual(item["rolling_windows"]["30m"]["expected_minute_count"], 30)
        self.assertEqual(item["rolling_windows"]["60m"]["expected_minute_count"], 60)
        self.assertIn(
            "intraday_30m_window_coverage_limited_requires_disclosure",
            item["hermes_notes"],
        )
        self.assertIn(
            "intraday_60m_window_coverage_limited_requires_disclosure",
            item["hermes_notes"],
        )

    def test_fetch_intraday_rows_reads_source_granularity_when_schema_supports_it(self):
        captured = {}

        def fake_psql(sql, timeout=45):
            if "information_schema.columns" in sql:
                return type("Result", (), {"returncode": 0, "stdout": "source_granularity\n", "stderr": ""})()
            captured["sql"] = sql
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": (
                        "HK\t00700\t2026-06-12 09:30:00\t100\t101\t99\t100.5\t1000\t100500\t"
                        "broker_minute_ohlcv\tminute_ohlcv\n"
                    ),
                    "stderr": "",
                },
            )()

        with patch.object(report, "psql", side_effect=fake_psql):
            rows, warnings = report.fetch_intraday_rows({"HK": ["00700"], "US": []})

        self.assertEqual(warnings, [])
        self.assertIn("COALESCE(k.source_granularity, 'missing')", captured["sql"])
        self.assertEqual(rows[0]["source_granularity"], "minute_ohlcv")

    def test_fetch_intraday_rows_is_compatible_without_source_granularity_column(self):
        captured = {}

        def fake_psql(sql, timeout=45):
            if "information_schema.columns" in sql:
                return type("Result", (), {"returncode": 0, "stdout": "data_source\n", "stderr": ""})()
            captured["sql"] = sql
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": "HK\t00700\t2026-06-12 09:30:00\t100\t101\t99\t100.5\t1000\t100500\ttencent_min\n",
                    "stderr": "",
                },
            )()

        with patch.object(report, "psql", side_effect=fake_psql):
            rows, warnings = report.fetch_intraday_rows({"HK": ["00700"], "US": []})

        self.assertEqual(warnings, [])
        self.assertIn("'missing'", captured["sql"])
        self.assertEqual(rows[0]["source_granularity"], "missing")


if __name__ == "__main__":
    unittest.main()
