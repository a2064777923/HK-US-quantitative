import unittest
import tempfile
from io import StringIO
from unittest.mock import patch

from scripts import kline_daily_gap_repair as repair


def candidate(symbol="00959"):
    return {
        "market": "HK",
        "exchange": "HKEX",
        "symbol": symbol,
        "day_kline_count": 2000,
        "earliest_daily_date": "2017-05-16",
        "latest_daily_date": "2026-06-10",
        "minute_kline_count": 666,
        "latest_minute_date": "2026-06-12",
        "market_latest_daily_date": "2026-06-12",
        "target_end_date": "2026-06-12",
    }


def source_rows():
    return [
        {"date": "2026-06-10", "open": 1, "close": 1.1, "high": 1.2, "low": 0.9, "volume": 100},
        {"date": "2026-06-11", "open": 1.1, "close": 1.2, "high": 1.25, "low": 1.05, "volume": 110},
        {"date": "2026-06-12", "open": 1.2, "close": 1.3, "high": 1.35, "low": 1.15, "volume": 120},
    ]


class KlineDailyGapRepairTests(unittest.TestCase):
    def test_build_report_plans_only_missing_daily_gap_rows(self):
        with patch.object(repair, "fetch_tencent_day_rows", return_value=(source_rows(), [])):
            payload = repair.build_report([candidate()])

        self.assertEqual(payload["schema"], "kline_daily_gap_repair_report_v1")
        self.assertEqual(payload["status"], "ACTIONABLE")
        self.assertEqual(payload["summary"]["candidate_count"], 1)
        self.assertEqual(payload["summary"]["repair_action_count"], 1)
        self.assertEqual(payload["summary"]["planned_row_count"], 2)
        self.assertIn(
            "operator_may_apply_hash_confirmed_daily_gap_plan_after_review",
            payload["recommendations"],
        )
        action = payload["actions"][0]
        self.assertEqual(action["symbol"], "00959")
        self.assertEqual(action["target_end_date"], "2026-06-12")
        self.assertEqual([row["date"] for row in action["rows"]], ["2026-06-11", "2026-06-12"])
        self.assertTrue(payload["apply_contract"]["dry_run_default"])
        self.assertTrue(payload["apply_contract"]["does_not_submit_orders"])
        self.assertTrue(payload["apply_contract"]["does_not_change_crontab"])
        self.assertTrue(payload["apply_contract"]["does_not_change_watchlists"])
        self.assertTrue(payload["apply_contract"]["does_not_change_strategy"])
        self.assertIn(payload["plan_hash"], payload["apply_contract"]["manual_apply_command"])
        self.assertIn("--apply", payload["apply_contract"]["manual_apply_command"])

    def test_source_that_does_not_reach_target_is_unresolved(self):
        rows = source_rows()[:2]
        attempts = [
            {
                "source_code": "hk00959",
                "status": "has_rows",
                "row_count": 2,
                "earliest_source_date": "2026-06-10",
                "latest_source_date": "2026-06-11",
            }
        ]
        with patch.object(repair, "fetch_tencent_day_rows", return_value=(rows, [], attempts)):
            payload = repair.build_report([candidate()])

        self.assertEqual(payload["summary"]["repair_action_count"], 0)
        self.assertEqual(payload["status"], "UNRESOLVED")
        self.assertIn(
            "investigate_unresolved_daily_gap_symbols_before_trusting_outcome_evidence",
            payload["recommendations"],
        )
        self.assertIn("do_not_patch_unresolved_symbols_from_minute_bars", payload["recommendations"])
        self.assertEqual(payload["summary"]["unresolved_count"], 1)
        unresolved = payload["unresolved"][0]
        self.assertEqual(unresolved["reason"], "source_does_not_reach_target_end")
        self.assertEqual(unresolved["latest_valid_gap_row_date"], "2026-06-11")
        self.assertEqual(unresolved["latest_source_date"], "2026-06-11")
        self.assertFalse(unresolved["source_reaches_target_end"])
        self.assertTrue(unresolved["source_after_latest_daily"])
        self.assertEqual(unresolved["source_attempts"][0]["source_code"], "hk00959")

    def test_invalid_source_rows_are_unresolved(self):
        rows = source_rows()
        rows[-1]["high"] = 1.0
        with patch.object(repair, "fetch_tencent_day_rows", return_value=(rows, [])):
            payload = repair.build_report([candidate()])

        self.assertEqual(payload["summary"]["repair_action_count"], 0)
        self.assertEqual(payload["summary"]["unresolved_count"], 1)
        self.assertEqual(payload["unresolved"][0]["reason"], "source_does_not_reach_target_end")
        self.assertIn("invalid_source_rows", payload["unresolved"][0])

    def test_empty_source_attempt_is_preserved_in_unresolved(self):
        attempts = [
            {
                "source_code": "hk00959",
                "status": "empty",
                "row_count": 0,
                "earliest_source_date": None,
                "latest_source_date": None,
            }
        ]
        with patch.object(repair, "fetch_tencent_day_rows", return_value=([], [], attempts)):
            payload = repair.build_report([candidate()])

        self.assertEqual(payload["status"], "UNRESOLVED")
        unresolved = payload["unresolved"][0]
        self.assertEqual(unresolved["reason"], "source_gap_rows_missing")
        self.assertIn(
            "review_source_coverage_or_symbol_mapping_for_unresolved_gap_symbols",
            payload["recommendations"],
        )
        self.assertEqual(unresolved["source_attempts"], attempts)
        self.assertFalse(unresolved["source_reaches_target_end"])
        self.assertFalse(unresolved["source_after_latest_daily"])

    def test_mixed_actions_and_unresolved_are_partial(self):
        with patch.object(
            repair,
            "fetch_tencent_day_rows",
            side_effect=[(source_rows(), []), ([], [], [])],
        ):
            payload = repair.build_report([candidate("01918"), candidate("00959")])

        self.assertEqual(payload["status"], "PARTIAL")
        self.assertEqual(payload["summary"]["repair_action_count"], 1)
        self.assertEqual(payload["summary"]["unresolved_count"], 1)
        self.assertIn("operator_may_apply_hash_confirmed_daily_gap_plan_after_review", payload["recommendations"])
        self.assertIn(
            "investigate_unresolved_daily_gap_symbols_before_trusting_outcome_evidence",
            payload["recommendations"],
        )

    def test_no_candidates_is_ok(self):
        payload = repair.build_report([])

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["candidate_count"], 0)
        self.assertEqual(payload["recommendations"], ["daily_gap_repair_not_required"])
        self.assertIsNone(payload["apply_contract"]["manual_apply_command"])

    def test_plan_hash_is_stable_for_same_actions(self):
        with patch.object(repair, "fetch_tencent_day_rows", return_value=(source_rows(), [])):
            first = repair.build_report([candidate()])
            second = repair.build_report([candidate()])

        self.assertEqual(first["plan_hash"], second["plan_hash"])

    def test_parallel_fetch_preserves_candidate_order_and_single_fetch(self):
        calls = []

        def fake_fetch(row, count=repair.FETCH_COUNT):
            calls.append(row["symbol"])
            rows = source_rows()
            for item in rows:
                item["source_code"] = f"hk{row['symbol']}"
            return rows, [], [
                {
                    "source_code": f"hk{row['symbol']}",
                    "status": "has_rows",
                    "row_count": len(rows),
                    "earliest_source_date": "2026-06-10",
                    "latest_source_date": "2026-06-12",
                }
            ]

        candidates = [candidate("00001"), candidate("00002"), candidate("00003")]
        with patch.object(repair, "FETCH_WORKERS", 3), patch.object(
            repair,
            "fetch_tencent_day_rows",
            side_effect=fake_fetch,
        ):
            payload = repair.build_report(candidates)

        self.assertCountEqual(calls, ["00001", "00002", "00003"])
        self.assertEqual(len(calls), 3)
        self.assertEqual([action["symbol"] for action in payload["actions"]], ["00001", "00002", "00003"])

    def test_sql_for_action_upserts_only_day_interval_target_rows(self):
        with patch.object(repair, "fetch_tencent_day_rows", return_value=(source_rows(), [])):
            action = repair.build_report([candidate()])["actions"][0]

        sql = repair.sql_for_action(action)

        self.assertIn("INSERT INTO klines", sql)
        self.assertIn("'00959','day','2026-06-11'", sql)
        self.assertIn("'00959','day','2026-06-12'", sql)
        self.assertIn("ON CONFLICT (symbol, interval, timestamp) DO UPDATE", sql)
        self.assertIn("tencent_day_repair", sql)
        self.assertLessEqual(len(repair.DATA_SOURCE), 20)

    def test_main_apply_requires_matching_plan_hash(self):
        with patch.object(repair, "fetch_tencent_day_rows", return_value=(source_rows(), [])), patch.object(
            repair,
            "fetch_gap_candidates",
            return_value=[candidate()],
        ), patch.object(repair, "apply_actions", return_value={"status": "applied"}) as apply_mock:
            with patch.object(
                repair.sys,
                "argv",
                ["kline_daily_gap_repair.py", "--output", "", "--apply", "--confirm-plan-hash", "bad"],
            ), patch("sys.stdout", new_callable=StringIO):
                code = repair.main()

        apply_mock.assert_not_called()
        self.assertEqual(code, 2)

    def test_backup_current_rows_writes_empty_backup_for_empty_actions(self):
        with tempfile.TemporaryDirectory() as td:
            path = repair.backup_current_rows([], backup_dir=td)

        self.assertTrue(path.endswith(".json"))


if __name__ == "__main__":
    unittest.main()
