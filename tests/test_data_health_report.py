import unittest
from datetime import date, datetime, timedelta

from scripts import data_health_report as report


def stock(market, symbol):
    return {"market": market, "symbol": symbol}


def kline(market, symbol, day, close=100, high=105, low=95, open_price=100, data_source="tencent"):
    return {
        "market": market,
        "symbol": symbol,
        "date": day,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "data_source": data_source,
    }


def history(market, symbol, latest_day, days=70, close=100):
    start = date.fromisoformat(latest_day) - timedelta(days=days - 1)
    rows = []
    for idx in range(days):
        current_close = close + idx
        rows.append(
            kline(
                market,
                symbol,
                (start + timedelta(days=idx)).isoformat(),
                close=current_close,
                high=current_close + 5,
                low=current_close - 5,
                open_price=current_close,
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
            current_dt=datetime(2026, 6, 12, 17, 0),
        )

        us = payload["markets"]["US"]
        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("invalid_latest_ohlc", us["failures"])
        self.assertEqual(us["integrity"]["invalid_latest_ohlc_count"], 1)
        self.assertIn("high_below_low", us["integrity"]["invalid_latest_ohlc_examples"][0]["errors"])

    def test_feature_run_not_ready_warns_without_data_failure(self):
        stocks = [stock("US", "AAPL")]
        rows = history("US", "AAPL", "2026-06-12")

        payload = report.build_report(
            stock_rows=stocks,
            kline_rows=rows,
            signal_rows=[signal("US", "2026-06-12", count=1)],
            feature_run_rows=[feature(status="feature_ready", ready=0, expected=1)],
            current_dt=datetime(2026, 6, 12, 17, 0),
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


if __name__ == "__main__":
    unittest.main()
