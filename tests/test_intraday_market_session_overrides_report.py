import unittest
from datetime import datetime
from pathlib import Path

from scripts import intraday_market_session_overrides_report as report


VALID_PAYLOAD = {
    "schema": "intraday_market_sessions_v1",
    "markets": {
        "HK": {
            "closed_dates": {"2026-07-01": "hkex_public_holiday"},
            "half_days": {
                "2026-12-24": {
                    "reason": "hkex_half_day",
                    "session_windows": [{"open": "09:30", "close": "12:00"}],
                }
            },
        },
        "US": {
            "closed_dates": {"2026-07-03": "nyse_observed_holiday"},
            "session_overrides": {
                "2026-11-27": {
                    "reason": "nyse_early_close",
                    "session_windows": [{"open": "09:30", "close": "13:00"}],
                }
            },
        },
    },
}


class IntradayMarketSessionOverridesReportTests(unittest.TestCase):
    def test_valid_override_payload_is_ok_and_read_only(self):
        payload = report.build_report(
            overrides_file="/root/intraday_market_sessions.json",
            payload=VALID_PAYLOAD,
            now=datetime(2026, 6, 13, 10, 0),
        )

        self.assertEqual(payload["schema"], "intraday_market_session_overrides_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_strategy"])
        self.assertEqual(payload["markets"]["HK"]["coverage_until"], "2026-12-24")
        self.assertEqual(payload["markets"]["US"]["coverage_until"], "2026-11-27")
        self.assertIn("intraday_market_session_overrides_validated", payload["recommendations"])

    def test_missing_file_warns_without_failing_report_generation(self):
        payload = report.build_report(
            overrides_file="/definitely/missing/intraday_market_sessions.json",
            now=datetime(2026, 6, 13, 10, 0),
        )

        self.assertEqual(payload["status"], "WARN")
        self.assertIn(
            "overrides_file_missing:/definitely/missing/intraday_market_sessions.json",
            payload["warnings"],
        )
        self.assertIn(
            "review_intraday_market_session_override_coverage_for_holidays_and_half_days",
            payload["recommendations"],
        )

    def test_invalid_session_window_fails_validation(self):
        invalid = {
            "markets": {
                "HK": {
                    "half_days": {
                        "2026-12-24": {
                            "reason": "bad_half_day",
                            "session_windows": [{"open": "13:00", "close": "12:00"}],
                        }
                    }
                },
                "US": VALID_PAYLOAD["markets"]["US"],
            }
        }

        payload = report.build_report(
            overrides_file="/root/intraday_market_sessions.json",
            payload=invalid,
            now=datetime(2026, 6, 13, 10, 0),
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("HK:half_days:2026-12-24:invalid_session_windows", payload["errors"])
        self.assertIn(
            "fix_intraday_market_session_override_schema_before_trusting_calendar",
            payload["recommendations"],
        )

    def test_repository_2026_override_config_validates_after_june_13(self):
        config_path = Path(__file__).resolve().parents[1] / "config" / "intraday_market_sessions.json"
        payload = report.build_report(
            overrides_file=str(config_path),
            now=datetime(2026, 6, 13, 10, 0),
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["markets"]["HK"]["coverage_until"], "2026-12-31")
        self.assertEqual(payload["markets"]["US"]["coverage_until"], "2026-12-25")
        self.assertEqual(payload["summary"]["error_count"], 0)
        self.assertEqual(payload["summary"]["warning_count"], 0)


if __name__ == "__main__":
    unittest.main()
