import unittest

from scripts import intraday_timeframe_quality_report as report


def window(minutes, status="OK", rows=None):
    rows = minutes if rows is None else rows
    return {
        "schema": "intraday_rolling_window_v1",
        "coverage_status": status,
        "row_count": rows,
        "expected_minute_count": minutes,
        "change_pct": 0.5,
        "momentum": "up",
    }


def symbol_row(
    symbol="00700",
    market="HK",
    status="OK",
    alignment="bullish_aligned",
    contradictions=None,
    quality=None,
    windows=None,
):
    windows = windows or {
        "5m": window(5),
        "15m": window(15),
        "30m": window(30),
        "60m": window(60),
    }
    return {
        "symbol": symbol,
        "market": market,
        "status": status,
        "rolling_windows": windows,
        "multi_timeframe_confirmation": {
            "schema": "intraday_multi_timeframe_confirmation_v1",
            "alignment": alignment,
            "dominant_direction": "up",
            "contradictions": contradictions or [],
        },
        "quality": quality
        or {
            "schema": "intraday_symbol_quality_v1",
            "status": "OK",
            "valid_point_count": 60,
            "full_ohlc_row_count": 60,
            "low_fidelity_point_count": 0,
            "snapshot_like_row_count": 0,
            "missing_source_granularity_count": 0,
        },
    }


def context(symbols):
    return {
        "schema": "intraday_context_report_v1",
        "status": "OK",
        "generated_at": "2026-06-14T10:00:00",
        "granularity_policy": {
            "schema": "intraday_granularity_usage_policy_v1",
            "daily_forward_outcomes_remain_authority": True,
        },
        "markets": {
            "HK": {
                "market": "HK",
                "status": "OK",
                "market_session": {"is_regular_session_open": True},
                "symbols": symbols,
            }
        },
    }


class IntradayTimeframeQualityReportTests(unittest.TestCase):
    def test_full_timeframe_quality_is_ok_and_read_only(self):
        payload = report.build_report(context([symbol_row()]))

        self.assertEqual(payload["schema"], "intraday_timeframe_quality_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["queries_database"])
        self.assertFalse(payload["source"]["writes_database"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertEqual(payload["summary"]["symbol_count"], 1)
        self.assertEqual(payload["summary"]["timeframes"]["60m"]["ok_symbol_count"], 1)
        self.assertEqual(payload["summary"]["soft_confirmation_eligible_symbol_count"], 1)
        self.assertEqual(payload["summary"]["cap_or_challenge_only_symbol_count"], 0)
        self.assertEqual(payload["summary"]["diagnostic_only_symbol_count"], 0)
        self.assertIn("intraday_timeframe_quality_clean", payload["recommendations"])
        symbol = payload["markets"]["HK"]["symbols"][0]
        self.assertEqual(symbol["decision_use"], "soft_confirmation_eligible")
        self.assertEqual(symbol["allowed_effects"], ["soft_confirm_signal", "cap_confidence", "challenge_signal"])
        self.assertIn("decision_use=soft=1", report.build_text_report(payload))
        policy = payload["decision_policy"]
        self.assertEqual(policy["schema"], "intraday_timeframe_decision_policy_v1")
        self.assertEqual(policy["confidence_use"], "soft_confirmation_eligible")
        self.assertFalse(policy["may_raise_confidence"])
        self.assertTrue(policy["requires_forward_evidence_before_confidence_raise"])
        self.assertFalse(policy["can_override_daily_gates"])
        self.assertFalse(policy["execution_permission"])
        self.assertEqual(policy["timeframe_roles"]["5m"], "entry_timing_noise_check")
        self.assertIn("soft_confirm_signal", policy["allowed_effects"])

    def test_limited_hourly_coverage_degrades_and_caps_confidence(self):
        windows = {
            "5m": window(5),
            "15m": window(15),
            "30m": window(30, "LIMITED", rows=20),
            "60m": window(60, "LIMITED", rows=20),
        }
        payload = report.build_report(context([symbol_row(windows=windows)]))

        self.assertEqual(payload["status"], "DEGRADED")
        self.assertEqual(payload["summary"]["limited_timeframe_symbol_count"], 1)
        self.assertEqual(payload["summary"]["timeframes"]["30m"]["limited_symbol_count"], 1)
        self.assertEqual(payload["summary"]["timeframes"]["60m"]["limited_symbol_count"], 1)
        symbol = payload["markets"]["HK"]["symbols"][0]
        self.assertEqual(symbol["limited_timeframes"], ["30m", "60m"])
        self.assertEqual(symbol["decision_use"], "cap_or_challenge_only")
        self.assertEqual(symbol["allowed_effects"], ["cap_confidence", "challenge_signal"])
        self.assertEqual(payload["summary"]["cap_or_challenge_only_symbol_count"], 1)
        self.assertIn("timeframe_coverage_limited", symbol["reasons"])
        self.assertIn("do_not_raise_confidence_from_limited_30m_60m_coverage", payload["recommendations"])
        policy = payload["decision_policy"]
        self.assertEqual(policy["confidence_use"], "cap_or_challenge_only")
        self.assertFalse(policy["may_raise_confidence"])
        self.assertEqual(policy["allowed_effects"], ["cap_confidence", "challenge_signal"])
        self.assertIn("timeframe_coverage_limited", policy["reason_codes"])

    def test_underfilled_windows_cannot_be_promoted_to_ok(self):
        inferred_limited = window(60, rows=20)
        inferred_limited.pop("coverage_status")
        windows = {
            "5m": window(5),
            "15m": window(15),
            "30m": window(30, "OK", rows=12),
            "60m": inferred_limited,
        }
        payload = report.build_report(context([symbol_row(windows=windows)]))

        symbol = payload["markets"]["HK"]["symbols"][0]

        self.assertEqual(payload["status"], "DEGRADED")
        self.assertEqual(payload["summary"]["limited_timeframe_symbol_count"], 1)
        self.assertEqual(symbol["limited_timeframes"], ["30m", "60m"])
        self.assertEqual(symbol["timeframes"]["30m"]["status"], "LIMITED")
        self.assertEqual(symbol["timeframes"]["60m"]["status"], "LIMITED")
        self.assertEqual(symbol["timeframes"]["60m"]["coverage_pct"], 33.33)
        self.assertEqual(symbol["decision_use"], "cap_or_challenge_only")
        self.assertIn("timeframe_coverage_limited", symbol["reasons"])

    def test_snapshot_low_fidelity_timeframes_are_advisory_only(self):
        quality = {
            "schema": "intraday_symbol_quality_v1",
            "status": "WARN",
            "valid_point_count": 60,
            "full_ohlc_row_count": 0,
            "low_fidelity_point_count": 60,
            "snapshot_like_row_count": 60,
            "missing_source_granularity_count": 0,
        }
        payload = report.build_report(context([symbol_row(quality=quality)]))

        self.assertEqual(payload["status"], "DEGRADED")
        self.assertEqual(payload["summary"]["low_fidelity_symbol_count"], 1)
        self.assertEqual(payload["summary"]["snapshot_like_symbol_count"], 1)
        self.assertEqual(payload["summary"]["cap_or_challenge_only_symbol_count"], 1)
        self.assertEqual(payload["markets"]["HK"]["symbols"][0]["decision_use"], "cap_or_challenge_only")
        self.assertIn(
            "treat_snapshot_minute_timeframes_as_advisory_until_full_ohlcv",
            payload["recommendations"],
        )

    def test_missing_all_timeframes_are_diagnostic_only_for_symbol(self):
        windows = {
            "5m": window(5, "MISSING", rows=0),
            "15m": window(15, "MISSING", rows=0),
            "30m": window(30, "MISSING", rows=0),
            "60m": window(60, "MISSING", rows=0),
        }
        quality = {
            "schema": "intraday_symbol_quality_v1",
            "status": "MISSING",
            "valid_point_count": 0,
            "full_ohlc_row_count": 0,
            "low_fidelity_point_count": 0,
            "snapshot_like_row_count": 0,
            "missing_source_granularity_count": 0,
        }
        payload = report.build_report(context([symbol_row(status="MISSING", windows=windows, quality=quality)]))

        symbol = payload["markets"]["HK"]["symbols"][0]

        self.assertEqual(symbol["status"], "MISSING")
        self.assertEqual(symbol["decision_use"], "diagnostic_only")
        self.assertEqual(symbol["allowed_effects"], [])
        self.assertEqual(payload["summary"]["diagnostic_only_symbol_count"], 1)

    def test_conflicting_timeframes_require_hermes_disclosure(self):
        payload = report.build_report(
            context(
                [
                    symbol_row(
                        alignment="conflicting_timeframes",
                        contradictions=["latest_5m_contradicts_latest_60m"],
                    )
                ]
            )
        )

        self.assertEqual(payload["status"], "DEGRADED")
        self.assertEqual(payload["summary"]["conflict_symbol_count"], 1)
        self.assertEqual(payload["markets"]["HK"]["symbols"][0]["decision_use"], "cap_or_challenge_only")
        self.assertIn("require_hermes_to_discuss_intraday_timeframe_conflicts", payload["recommendations"])

    def test_invalid_upstream_schema_fails(self):
        payload = report.build_report({"schema": "wrong", "status": "OK"})

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("fix_intraday_context_report_before_timeframe_quality_review", payload["recommendations"])
        self.assertEqual(payload["decision_policy"]["confidence_use"], "diagnostic_only")
        self.assertEqual(payload["decision_policy"]["allowed_effects"], [])


if __name__ == "__main__":
    unittest.main()
