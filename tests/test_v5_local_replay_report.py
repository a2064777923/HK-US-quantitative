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
            self.assertEqual(payload["replay_quality"]["schema"], "v5_local_replay_quality_v1")
            self.assertIn(payload["replay_quality"]["status"], {"OK", "WARN"})
            self.assertIn("alert_rate_per_100_bars", payload["replay_quality"]["metrics"])
            self.assertEqual(payload["replay_breakdown"]["schema"], "v5_local_replay_breakdown_v1")
            self.assertGreaterEqual(payload["replay_breakdown"]["summary"]["trigger_group_count"], 1)
            self.assertGreaterEqual(payload["replay_breakdown"]["summary"]["market_count"], 1)
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
            self.assertEqual(payload["replay_quality"]["status"], "FAIL")
            self.assertIn("replay_quality_no_evaluated_bars", [item["code"] for item in payload["checks"]])
            self.assertFalse(payload["summary"]["promotion_ready"])

    def test_replay_quality_flags_high_noise_metrics(self):
        alert_summary = {
            "alert_count": 80,
            "execution_candidate_count": 20,
            "confirmed_directional_count": 20,
            "downgraded_directional_count": 65,
            "by_candidate_signal_type": {"BUY": 40, "SELL": 40},
            "by_trigger": {"站上MA5": 50},
            "alerted_symbol_day_count": 50,
            "multi_alert_symbol_day_count": 25,
            "max_alerts_per_symbol_day": 4,
            "avg_alerts_per_alerted_symbol_day": 1.6,
        }

        quality = report.replay_quality_assessment(100, 2, alert_summary)

        self.assertEqual(quality["status"], "WARN")
        codes = [item["code"] for item in quality["checks"]]
        self.assertIn("replay_alert_density_high", codes)
        self.assertIn("execution_candidate_density_high", codes)
        self.assertIn("directional_confirmation_ratio_low", codes)
        self.assertIn("directional_downgrade_ratio_high", codes)
        self.assertIn("multi_trigger_symbol_day_ratio_high", codes)
        self.assertEqual(quality["thresholds"]["alert_density_warn_per_100_bars"], 50.0)
        self.assertEqual(quality["metrics"]["alert_rate_per_100_bars"], 80.0)
        self.assertEqual(quality["metrics"]["directional_confirmation_ratio_pct"], 25.0)

    def test_replay_breakdown_flags_noisy_trigger_groups_by_market(self):
        symbol_reports = [
            {
                "symbol": "00700",
                "market": "HK",
                "evaluated_bars": 100,
                "alerts": [
                    (
                        "2026-02-01",
                        {
                            "market": "HK",
                            "symbol": "00700",
                            "trigger": "站上MA5",
                            "signal_type": "WATCH",
                            "candidate_signal_type": "BUY",
                            "confirmed": False,
                            "execution_candidate": False,
                        },
                    )
                    for _ in range(8)
                ]
                + [
                    (
                        "2026-02-02",
                        {
                            "market": "HK",
                            "symbol": "00700",
                            "trigger": "布林下軌突破",
                            "signal_type": "BUY",
                            "candidate_signal_type": "BUY",
                            "confirmed": True,
                            "execution_candidate": True,
                        },
                    )
                    for _ in range(3)
                ],
            }
        ]
        alert_summary = report.summarize_alerts(symbol_reports)

        breakdown = report.replay_breakdown(symbol_reports, 100, alert_summary)

        self.assertEqual(breakdown["schema"], "v5_local_replay_breakdown_v1")
        self.assertEqual(breakdown["summary"]["trigger_group_count"], 2)
        noisy = breakdown["top_noisy_triggers"][0]
        self.assertEqual(noisy["key"], "HK:BUY:站上MA5")
        self.assertIn("trigger_replay_alert_density_high", noisy["reasons"])
        self.assertIn("trigger_directional_confirmation_ratio_low", noisy["reasons"])
        self.assertIn("trigger_directional_downgrade_ratio_high", noisy["reasons"])
        self.assertEqual(noisy["metrics"]["alert_rate_per_100_bars"], 8.0)
        self.assertEqual(breakdown["market_quality"][0]["market"], "HK")

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
