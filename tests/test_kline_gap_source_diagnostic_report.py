import unittest

from scripts import kline_gap_source_diagnostic_report as report


def unresolved(symbol="00959", reason="source_gap_rows_missing", attempts=None, **extra):
    payload = {
        "symbol": symbol,
        "market": "HK",
        "reason": reason,
        "latest_daily_date": "2025-06-25",
        "target_end_date": "2026-06-12",
        "latest_source_date": "2025-06-25",
        "source_reaches_target_end": False,
        "source_after_latest_daily": False,
        "source_attempts": attempts
        if attempts is not None
        else [
            {
                "source_code": f"hk{symbol}",
                "status": "has_rows",
                "row_count": 800,
                "earliest_source_date": "2022-03-24",
                "latest_source_date": "2025-06-25",
            }
        ],
    }
    payload.update(extra)
    return payload


def gap_payload(items):
    return {
        "schema": "kline_daily_gap_repair_report_v1",
        "status": "PARTIAL" if items else "OK",
        "plan_hash": "abc123",
        "unresolved": items,
    }


def hygiene_payload(*items):
    return {
        "schema": "universe_hygiene_report_v1",
        "status": "WARN" if items else "OK",
        "markets": {
            "HK": {
                "active_symbols": list(items),
                "all_problem_symbols": [item for item in items if item.get("recommended_action") != "keep_active"],
            }
        },
    }


def watchlist_payload(*symbols):
    return {
        "schema": "rt_signal_watchlist_v1",
        "markets": {
            "HK": {"symbols": list(symbols)},
            "US": {"symbols": ["AAPL"]},
        },
    }


def portfolio_payload(*positions):
    return {
        "schema": "portfolio_context_report_v1",
        "portfolio_reports": [
            {
                "portfolio_id": 8,
                "role": "simulation",
                "positions": list(positions),
            }
        ],
    }


class KlineGapSourceDiagnosticReportTests(unittest.TestCase):
    def test_no_unresolved_symbols_is_ok_and_read_only(self):
        payload = report.build_report(gap_payload([]), hygiene_payload())

        self.assertEqual(payload["schema"], "kline_gap_source_diagnostic_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["unresolved_count"], 0)
        self.assertEqual(payload["recommendations"], ["no_unresolved_daily_gap_source_issues"])
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["applies_kline_repairs"])
        self.assertFalse(payload["source"]["auto_excludes_from_evidence"])

    def test_universe_hygiene_problem_overrides_provider_category(self):
        payload = report.build_report(
            gap_payload([unresolved("03333")]),
            hygiene_payload(
                {
                    "symbol": "03333",
                    "recommended_action": "candidate_deactivate_or_symbol_mapping",
                    "issues": ["latest_kline_stale_ge_30d", "no_history_rows_120d"],
                    "severity": "high",
                    "lag_days_vs_market_latest": 865,
                }
            ),
        )

        item = payload["classifications"][0]
        self.assertEqual(payload["status"], "ACTION_REQUIRED")
        self.assertEqual(item["category"], "active_universe_or_symbol_mapping_issue")
        self.assertEqual(item["confidence"], "high")
        self.assertEqual(item["hygiene"]["recommended_action"], "candidate_deactivate_or_symbol_mapping")
        self.assertIn("review_active_universe_or_symbol_mapping_for_unresolved_gap_symbols", payload["recommendations"])

    def test_empty_provider_attempts_classify_as_symbol_mapping_unavailable(self):
        attempts = [
            {
                "source_code": "usSQ.OQ",
                "status": "empty",
                "row_count": 0,
                "earliest_source_date": None,
                "latest_source_date": None,
            }
        ]

        payload = report.build_report(
            gap_payload([unresolved("SQ", attempts=attempts, latest_source_date=None)]),
            hygiene_payload({"symbol": "SQ", "recommended_action": "keep_active", "issues": ["healthy_active_symbol"]}),
        )

        item = payload["classifications"][0]
        self.assertEqual(payload["status"], "REVIEW")
        self.assertEqual(item["category"], "provider_symbol_mapping_unavailable")
        self.assertEqual(item["recommended_action"], "try_alternate_provider_or_symbol_code_then_review_active_universe")
        self.assertIn("try_alternate_provider_or_symbol_code_for_unresolved_gap_symbols", payload["recommendations"])

    def test_partial_provider_lag_keeps_do_not_patch_from_minute_guidance(self):
        payload = report.build_report(
            gap_payload(
                [
                    unresolved(
                        "00959",
                        reason="source_does_not_reach_target_end",
                        latest_daily_date="2026-06-10",
                        latest_source_date="2026-06-11",
                        latest_valid_gap_row_date="2026-06-11",
                        source_after_latest_daily=True,
                    )
                ]
            ),
            hygiene_payload({"symbol": "00959", "recommended_action": "keep_active", "issues": ["healthy_active_symbol"]}),
        )

        item = payload["classifications"][0]
        self.assertEqual(item["category"], "provider_lag_or_partial_gap")
        self.assertEqual(item["source_lag_days_vs_target"], 1)
        self.assertIn("do_not_patch_provider_lag_symbols_from_minute_bars", payload["recommendations"])

    def test_invalid_source_rows_are_high_confidence_blocker(self):
        payload = report.build_report(
            gap_payload(
                [
                    unresolved(
                        "01918",
                        invalid_source_rows=[{"date": "2026-06-12", "errors": ["high_below_low"]}],
                    )
                ]
            ),
            hygiene_payload({"symbol": "01918", "recommended_action": "keep_active", "issues": ["healthy_active_symbol"]}),
        )

        item = payload["classifications"][0]
        self.assertEqual(payload["status"], "ACTION_REQUIRED")
        self.assertEqual(item["category"], "source_rows_invalid")
        self.assertEqual(item["confidence"], "high")
        self.assertIn("block_manual_repair_until_provider_rows_validate", payload["recommendations"])

    def test_unresolved_gap_exposure_marks_watchlist_and_position_blockers(self):
        payload = report.build_report(
            gap_payload([unresolved("00959"), unresolved("00011"), unresolved("03333")]),
            hygiene_payload(
                {
                    "symbol": "00959",
                    "recommended_action": "candidate_deactivate_or_symbol_mapping",
                    "issues": ["latest_kline_stale_ge_30d"],
                },
                {
                    "symbol": "00011",
                    "recommended_action": "candidate_deactivate_or_symbol_mapping",
                    "issues": ["latest_kline_stale_ge_30d"],
                },
                {
                    "symbol": "03333",
                    "recommended_action": "candidate_deactivate_or_symbol_mapping",
                    "issues": ["latest_kline_stale_ge_30d"],
                },
            ),
            watchlist=watchlist_payload("00959"),
            portfolio_report=portfolio_payload(
                {
                    "symbol": "00011",
                    "quantity": 100,
                    "market_value_hkd": 10500,
                    "unrealized_pnl_hkd": -200,
                }
            ),
        )

        by_symbol = {item["symbol"]: item for item in payload["classifications"]}

        self.assertEqual(payload["summary"]["current_v5_watchlist_exposed_count"], 1)
        self.assertEqual(payload["summary"]["open_position_exposed_count"], 1)
        self.assertEqual(payload["summary"]["sample_current_v5_watchlist_exposed_symbols"], ["00959"])
        self.assertEqual(payload["summary"]["sample_open_position_exposed_symbols"], ["00011"])
        self.assertTrue(by_symbol["00959"]["exposure"]["in_current_v5_watchlist"])
        self.assertIn("current_v5_watchlist_member", by_symbol["00959"]["exposure"]["deactivation_blockers"])
        self.assertTrue(by_symbol["00011"]["exposure"]["has_open_position"])
        self.assertIn("open_position_in_positions_table", by_symbol["00011"]["exposure"]["deactivation_blockers"])
        self.assertFalse(by_symbol["03333"]["exposure"]["safe_to_deactivate_without_manual_review"])
        self.assertIn(
            "still_requires_symbol_mapping_or_refetch_review_before_deactivation",
            by_symbol["03333"]["exposure"]["notes"],
        )
        self.assertIn("review_watchlist_membership_for_unresolved_gap_symbols:1", payload["recommendations"])
        self.assertIn("block_deactivation_until_position_review_for_unresolved_gap_symbols:1", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
