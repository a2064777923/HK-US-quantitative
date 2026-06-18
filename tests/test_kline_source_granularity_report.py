import unittest
from unittest import mock

from scripts import kline_source_granularity_report as report


def source_row(interval, data_source, source_granularity="missing", row_count=10, symbol_count=2):
    return {
        "interval": interval,
        "data_source": data_source,
        "source_granularity": source_granularity,
        "row_count": row_count,
        "symbol_count": symbol_count,
        "min_timestamp": "2026-06-12 09:30:00",
        "max_timestamp": "2026-06-12 16:00:00",
    }


class KlineSourceGranularityReportTests(unittest.TestCase):
    def test_missing_column_plans_schema_add_and_safe_backfill(self):
        payload = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source"],
            source_rows=[
                source_row("min", "tencent_min", row_count=100),
                source_row("day", "tencent", row_count=200),
            ],
        )

        self.assertEqual(payload["schema"], "kline_source_granularity_report_v1")
        self.assertEqual(payload["status"], "ACTION_REQUIRED")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["writes_database"])
        self.assertFalse(payload["source"]["changes_schema"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["does_not_change_ohlcv_prices_or_volumes"] is False)
        self.assertFalse(payload["summary"]["source_granularity_column_exists"])

        actions = {row["id"]: row for row in payload["proposal"]["actions"]}
        self.assertIn("add_klines_source_granularity_column", actions)
        self.assertIn("backfill_tencent_minute_snapshot", actions)
        self.assertIn("backfill_daily_ohlcv", actions)
        self.assertEqual(actions["backfill_tencent_minute_snapshot"]["source_granularity"], "minute_snapshot_price")
        self.assertIn("ALTER TABLE klines ADD COLUMN IF NOT EXISTS source_granularity", payload["proposal"]["sql_script"])
        self.assertIn("UPDATE klines", payload["proposal"]["sql_script"])
        self.assertIn("--confirm-proposal-hash", payload["proposal"]["apply_command"])

    def test_existing_column_only_backfills_missing_granularity(self):
        payload = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source", "source_granularity"],
            source_rows=[
                source_row("min", "tencent_minute_query", row_count=25),
                source_row("min", "broker_minute_ohlcv", "minute_ohlcv", row_count=25),
            ],
        )

        actions = {row["id"]: row for row in payload["proposal"]["actions"]}
        self.assertEqual(payload["status"], "ACTION_REQUIRED")
        self.assertNotIn("add_klines_source_granularity_column", actions)
        self.assertIn("backfill_tencent_minute_snapshot", actions)
        self.assertNotIn("backfill_trusted_minute_ohlcv", actions)
        self.assertEqual(payload["summary"]["estimated_backfill_row_count"], 25)

    def test_unmapped_missing_granularity_is_review_only(self):
        payload = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source", "source_granularity"],
            source_rows=[source_row("min", "unknown_vendor", row_count=7)],
        )

        self.assertEqual(payload["status"], "REVIEW")
        self.assertEqual(payload["proposal"]["action_count"], 0)
        self.assertEqual(payload["issues"][0]["reason"], "no_safe_granularity_mapping_for_source")
        self.assertIn("do_not_infer_source_granularity_for_unmapped_sources", payload["recommendations"])

    def test_clean_contract_is_ok(self):
        payload = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source", "source_granularity"],
            source_rows=[
                source_row("min", "tencent_min", "minute_snapshot_price", row_count=100),
                source_row("day", "tencent", "daily_ohlcv", row_count=200),
            ],
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["proposal"]["action_count"], 0)
        self.assertIn("kline_source_granularity_contract_clean", payload["recommendations"])

    def test_apply_requires_matching_hash(self):
        payload = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source"],
            source_rows=[source_row("min", "tencent_min", row_count=100)],
        )

        result = report.apply_payload(payload, confirm_proposal_hash="bad")

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["applied"])
        self.assertIn("confirm_proposal_hash_mismatch", result["validation_reasons"])

    def test_proposal_hash_is_stable_when_only_estimated_row_count_changes(self):
        one = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source"],
            source_rows=[source_row("min", "tencent_min", row_count=100)],
        )
        two = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source"],
            source_rows=[source_row("min", "tencent_min", row_count=200)],
        )

        self.assertEqual(one["proposal"]["proposal_hash"], two["proposal"]["proposal_hash"])

    def test_apply_runs_only_after_hash_confirmation(self):
        payload = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source"],
            source_rows=[source_row("min", "tencent_min", row_count=100)],
        )
        digest = payload["proposal"]["proposal_hash"]

        query_results = [
            type("Result", (), {"returncode": 0, "stdout": "100\t150\n", "stderr": ""})(),
            type("Result", (), {"returncode": 0, "stdout": "0\t150\n", "stderr": ""})(),
        ]
        with mock.patch.object(report, "backup_metadata", return_value="/tmp/backup.json"), mock.patch.object(
            report,
            "execute_sql_script",
            return_value=type("Result", (), {"returncode": 0, "stdout": "ALTER TABLE\nCOMMENT\n", "stderr": ""})(),
        ) as execute_script, mock.patch.object(report, "execute_sql_query", side_effect=query_results) as execute_query:
            result = report.apply_payload(payload, confirm_proposal_hash=digest, batch_size=50)

        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["applied"])
        execute_script.assert_called_once()
        self.assertEqual(execute_query.call_count, 2)
        self.assertEqual(result["rows_updated"], 100)
        self.assertEqual(result["action_results"][1]["batches"], 2)
        self.assertIn("id > 0", execute_query.call_args_list[0].args[0])
        self.assertIn("ORDER BY id", execute_query.call_args_list[0].args[0])
        self.assertIn("LIMIT 50", execute_query.call_args_list[0].args[0])

    def test_apply_can_stop_after_max_batches_for_controlled_backfill(self):
        payload = report.build_report(
            columns=["symbol", "interval", "timestamp", "data_source", "source_granularity"],
            source_rows=[source_row("day", "tencent", row_count=100)],
        )
        digest = payload["proposal"]["proposal_hash"]

        with mock.patch.object(report, "backup_metadata", return_value="/tmp/backup.json"), mock.patch.object(
            report,
            "execute_sql_query",
            return_value=type("Result", (), {"returncode": 0, "stdout": "10\t100\n", "stderr": ""})(),
        ) as execute:
            result = report.apply_payload(payload, confirm_proposal_hash=digest, batch_size=10, max_batches=1)

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["applied"])
        self.assertIn("max_batches_reached", result["validation_reasons"])
        self.assertEqual(execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
