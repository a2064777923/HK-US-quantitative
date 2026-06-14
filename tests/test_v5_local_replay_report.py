import argparse
import csv
import json
import os
import tempfile
import unittest

from scripts import v5_local_replay_report as report


FIELDS = ["symbol", "dt", "open_price", "high_price", "low_price", "close_price", "volume"]


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def trend_rows(symbol, start=100.0, count=45):
    rows = []
    price = start
    for index in range(count):
        price += 1.0
        date = f"2026-01-{index + 1:02d}" if index < 31 else f"2026-02-{index - 30:02d}"
        rows.append(
            {
                "symbol": symbol,
                "dt": date,
                "open_price": round(price - 0.5, 2),
                "high_price": round(price + 1.0, 2),
                "low_price": round(price - 1.0, 2),
                "close_price": round(price, 2),
                "volume": 1000000 + index * 1000,
            }
        )
    return rows


def replay_args(tmp, **overrides):
    values = {
        "hk_csv": os.path.join(tmp, "hk_klines_v2.csv"),
        "us_csv": os.path.join(tmp, "us_klines.csv"),
        "output": os.path.join(tmp, "v5_local_replay_report.json"),
        "strategy_config_file": "",
        "market": [],
        "start_date": None,
        "end_date": None,
        "min_history_bars": 30,
        "max_symbols": 0,
        "max_bars_per_symbol": 0,
        "alert_sample_limit": 10,
        "respect_cooldown": False,
        "text": False,
        "json": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class V5LocalReplayReportTests(unittest.TestCase):
    def test_build_report_is_local_read_only_and_replays_v5(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_rows(os.path.join(tmp, "hk_klines_v2.csv"), trend_rows("00700"))
            write_rows(os.path.join(tmp, "us_klines.csv"), trend_rows("AAPL", start=200.0))

            payload = report.build_report(replay_args(tmp))

            self.assertEqual(payload["schema"], "v5_local_replay_report_v1")
            self.assertEqual(payload["summary"]["overall_status"], "V5_REPLAY_RESEARCH_ONLY")
            self.assertEqual(payload["summary"]["hermes_use"], "v5_replay_research_context_only")
            self.assertFalse(payload["summary"]["promotion_ready"])
            self.assertTrue(payload["source"]["read_only_inputs"])
            self.assertTrue(payload["source"]["local_only"])
            self.assertFalse(payload["source"]["writes_alert_queue"])
            self.assertFalse(payload["source"]["submits_orders"])
            self.assertEqual(payload["summary"]["symbol_count"], 2)
            self.assertGreater(payload["summary"]["evaluated_bars"], 0)
            self.assertEqual(payload["storage_policy"]["commit_raw_csv_to_git"], False)
            self.assertEqual(payload["hermes_contract"]["contract"], "v5_replay_research_context_only")
            self.assertIn(
                "daily_close_synthetic_quote_not_intraday_path",
                [item["code"] for item in payload["checks"]],
            )

    def test_missing_csv_makes_evidence_insufficient_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_rows(os.path.join(tmp, "hk_klines_v2.csv"), [])

            payload = report.build_report(replay_args(tmp))

            self.assertEqual(payload["summary"]["overall_status"], "INSUFFICIENT_REPLAY_DATA")
            self.assertIn("us_csv_missing", [item["code"] for item in payload["checks"]])
            self.assertFalse(payload["summary"]["promotion_ready"])

    def test_main_writes_report_and_text_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            hk = os.path.join(tmp, "hk_klines_v2.csv")
            us = os.path.join(tmp, "us_klines.csv")
            output = os.path.join(tmp, "report.json")
            write_rows(hk, trend_rows("00700"))
            write_rows(us, trend_rows("AAPL", start=200.0))

            rc = report.main(["--hk-csv", hk, "--us-csv", us, "--output", output, "--text"])

            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(output))
            with open(output, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["source"]["source_files"]["hk_csv"], os.path.abspath(hk))
            self.assertEqual(payload["summary"]["overall_status"], "V5_REPLAY_RESEARCH_ONLY")


if __name__ == "__main__":
    unittest.main()
