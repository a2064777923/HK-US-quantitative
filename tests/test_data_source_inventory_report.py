import unittest
from datetime import datetime

from scripts import data_source_inventory_report as report


NOW = datetime(2026, 6, 14, 10, 0, 0)


def table_rows():
    return [{"table": table, "exists": True, "row_count": 1} for table in report.CORE_TABLES]


def context_row(name, exists=True, required=True, stale=False, schema_valid=True):
    return {
        "name": name,
        "path": f"/tmp/{name}.json",
        "exists": exists,
        "stale": stale,
        "schema_valid": schema_valid,
        "required_for_live_review": required,
    }


class DataSourceInventoryReportTests(unittest.TestCase):
    def test_optional_local_backtest_reliability_missing_does_not_degrade_live_inventory(self):
        payload = report.build_report(
            table_summaries=table_rows(),
            kline_source_rows=[{"data_source": "tencent", "row_count": 100, "interval": "day"}],
            signal_source_rows=[],
            portfolio_rows=[],
            context_file_rows=[
                context_row("data_health"),
                context_row("execution_readiness"),
                context_row("local_backtest_reliability", exists=False, required=False),
            ],
            input_file_rows=[{"name": "external_market_context_json", "exists": True}],
            warnings=[],
            now=NOW,
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["context_file_status_counts"], {"present": 2, "missing": 1})
        self.assertEqual(payload["summary"]["required_context_file_status_counts"], {"present": 2})
        self.assertEqual(payload["summary"]["optional_context_file_status_counts"], {"missing": 1})
        self.assertIn("optional_context_reports_not_ready", [row["code"] for row in payload["weaknesses"]])
        self.assertEqual(payload["summary"]["error_weakness_count"], 0)
        self.assertEqual(payload["summary"]["warning_weakness_count"], 0)
        self.assertEqual(payload["summary"]["info_weakness_count"], 1)
        self.assertIn(
            "treat_optional_research_context_as_unavailable_until_refreshed",
            payload["recommendations"],
        )

    def test_missing_required_context_still_degrades_inventory(self):
        payload = report.build_report(
            table_summaries=table_rows(),
            kline_source_rows=[{"data_source": "tencent", "row_count": 100, "interval": "day"}],
            signal_source_rows=[],
            portfolio_rows=[],
            context_file_rows=[
                context_row("data_health", exists=False, required=True),
                context_row("local_backtest_reliability", exists=False, required=False),
            ],
            input_file_rows=[{"name": "external_market_context_json", "exists": True}],
            warnings=[],
            now=NOW,
        )

        codes = [row["code"] for row in payload["weaknesses"]]
        self.assertEqual(payload["status"], "DEGRADED")
        self.assertEqual(payload["summary"]["context_file_status_counts"], {"missing": 2})
        self.assertEqual(payload["summary"]["required_context_file_status_counts"], {"missing": 1})
        self.assertEqual(payload["summary"]["optional_context_file_status_counts"], {"missing": 1})
        self.assertIn("context_reports_missing", codes)
        self.assertIn("optional_context_reports_not_ready", codes)
        self.assertIn("refresh_missing_context_reports_before_hermes_review", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
