import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import trusted_source_preflight as preflight
from scripts import fundamentals_context_ingest as ingest


def item(**extra):
    payload = {
        "symbol": "00700",
        "market": "HK",
        "name": "Tencent",
        "source": "broker_fundamentals_snapshot",
        "provider": "broker_fundamentals_snapshot",
        "provider_symbol": "00700.HK",
        "as_of": "2026-06-12T10:00:00",
        "currency": "HKD",
        "market_cap": 3_000_000_000_000,
        "pe_ttm": 18.2,
        "pb": 3.1,
        "ps": 5.0,
        "roe_pct": 18.0,
        "revenue_growth_pct": 8.5,
        "earnings_growth_pct": 10.2,
        "dividend_yield_pct": 1.0,
        "debt_to_equity": 0.4,
        "summary": "Broker full fundamentals snapshot.",
    }
    payload.update(extra)
    return payload


class FundamentalsContextIngestTests(unittest.TestCase):
    def test_valid_item_is_accepted_and_appended(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fundamentals.jsonl"
            payload = ingest.build_ingest([item()], output_file=str(path))
            ingest.append_jsonl(str(path), payload["accepted"])

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(payload["schema"], "fundamentals_context_ingest_v1")
        self.assertEqual(payload["accepted_count"], 1)
        self.assertEqual(payload["rejected_count"], 0)
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_strategy"])
        self.assertFalse(payload["source"]["changes_alert_queue"])
        self.assertFalse(payload["source"]["changes_crontab"])
        self.assertFalse(payload["source"]["repairs_data"])
        self.assertEqual(rows[0]["symbol"], "00700")
        self.assertEqual(rows[0]["fundamental_completeness"]["level"], "full")

    def test_duplicate_item_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fundamentals.jsonl"
            first = ingest.build_ingest([item()], output_file=str(path))
            ingest.append_jsonl(str(path), first["accepted"])
            second = ingest.build_ingest([item()], output_file=str(path))

        self.assertEqual(second["accepted_count"], 0)
        self.assertEqual(second["duplicate_count"], 1)

    def test_invalid_item_is_rejected_by_default(self):
        payload = ingest.build_ingest(
            [item(symbol="", as_of="not-a-date", source="", provider="", pe_ttm="not-a-number")],
            output_file="",
        )

        self.assertEqual(payload["accepted_count"], 0)
        self.assertEqual(payload["rejected_count"], 1)
        self.assertIn("missing_symbol", payload["rejected"][0]["reasons"])
        self.assertIn("invalid_as_of", payload["rejected"][0]["reasons"])
        self.assertIn("missing_source", payload["rejected"][0]["reasons"])
        self.assertIn("invalid_metric:pe_ttm", payload["rejected"][0]["reasons"])

    def test_allow_invalid_keeps_item_with_warnings(self):
        payload = ingest.build_ingest(
            [item(as_of="not-a-date", pb="bad")],
            output_file="",
            allow_invalid=True,
        )

        self.assertEqual(payload["accepted_count"], 1)
        self.assertEqual(payload["rejected_count"], 0)
        self.assertIn("invalid_as_of", payload["accepted"][0]["ingest_warnings"])
        self.assertIn("invalid_metric:pb", payload["accepted"][0]["ingest_warnings"])

    def test_cli_defaults_to_dry_run_without_writing_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "fundamentals.jsonl"
            item_json = json.dumps(item())
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/fundamentals_context_ingest.py",
                    "--item-json",
                    item_json,
                    "--output-jsonl-file",
                    str(output),
                    "--json",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)

        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["appended"])
        self.assertEqual(payload["accepted_count"], 1)
        self.assertFalse(output.exists())

    def test_cli_append_writes_jsonl_only_when_requested(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "fundamentals.jsonl"
            item_json = json.dumps(item())
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/fundamentals_context_ingest.py",
                    "--item-json",
                    item_json,
                    "--output-jsonl-file",
                    str(output),
                    "--append",
                    "--json",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertFalse(payload["dry_run"])
        self.assertTrue(payload["appended"])
        self.assertEqual(rows[0]["source"], "broker_fundamentals_snapshot")

    def test_appended_broker_payload_satisfies_preflight_fundamentals_component(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fundamentals.jsonl"
            payload = ingest.build_ingest([item()], output_file=str(path))
            ingest.append_jsonl(str(path), payload["accepted"])

            report = preflight.build_report(
                external_items=[],
                sentiment_indicators=[],
                fundamentals_items=None,
                now=preflight.datetime(2026, 6, 12, 10, 30, 0),
                fundamentals_input_file="",
                fundamentals_input_jsonl_file=str(path),
            )

        fundamentals = [row for row in report["components"] if row["name"] == "fundamentals_context_inputs"][0]
        self.assertEqual(fundamentals["status"], "OK")
        self.assertEqual(fundamentals["trusted_full_item_count"], 1)
        self.assertNotIn("partial_fundamentals_present", fundamentals["reasons"])
        self.assertIn("fundamentals_trusted_source_preflight_passed", fundamentals["recommendations"])


if __name__ == "__main__":
    unittest.main()
