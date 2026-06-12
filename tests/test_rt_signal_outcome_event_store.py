import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import rt_signal_outcome_event_store as store


def evaluation(signal_id="sig-1", status="resolved"):
    item = {
        "signal_id": signal_id,
        "symbol": "00700",
        "market": "HK",
        "signal_type": "BUY",
        "trigger": "MA",
        "confirmed": True,
        "status": status,
        "strategy_config_id": "cfg",
        "watchlist_id": "wl",
        "signal_date": "2026-06-12",
        "generated_at": "2026-06-12T10:00:00",
        "latest_kline_date": "2026-06-15",
        "available_future_days": 1 if status == "resolved" else 0,
        "outcomes": {},
    }
    if status == "resolved":
        item["outcomes"]["1d"] = {
            "status": "resolved",
            "mark_date": "2026-06-15",
            "signed_close_return_pct": 1.25,
            "win": True,
            "target_hit": False,
            "stop_hit": False,
            "first_hit": None,
        }
    else:
        item["reason"] = "no_future_daily_klines"
        item["outcomes"]["1d"] = {
            "status": "pending",
            "available_future_days": 0,
            "needed_future_days": 1,
        }
    return item


def outcome_report(evaluations=None):
    evaluations = evaluations if evaluations is not None else [evaluation()]
    return {
        "schema": "rt_signal_outcome_report_v1",
        "generated_at": "2026-06-12T11:00:00",
        "status": "INSUFFICIENT_SAMPLE",
        "sample_scope": {"mode": "latest_strategy_config_and_watchlist", "strategy_config_id": "cfg", "watchlist_id": "wl"},
        "evaluated_signal_count": len(evaluations),
        "resolved_signal_count": len([item for item in evaluations if item.get("status") == "resolved"]),
        "pending_signal_count": len([item for item in evaluations if item.get("status") != "resolved"]),
        "primary_horizon": "1d",
        "primary_recommendation": "outcome_sample_below_30_keep_shadow_mode",
        "evaluations": evaluations,
        "recent_evaluations": evaluations[-50:],
    }


class RtSignalOutcomeEventStoreTests(unittest.TestCase):
    def test_dry_run_persists_full_evaluations_summary(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "outcome.json"
            report_file.write_text(
                json.dumps(outcome_report([evaluation("sig-1"), evaluation("sig-2", "pending")])),
                encoding="utf-8",
            )

            payload = store.build_report(str(report_file))

        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["event_count"], 2)
        self.assertEqual(payload["event_summary"]["by_status"], {"pending": 1, "resolved": 1})
        self.assertEqual(payload["event_summary"]["by_primary_status"], {"pending": 1, "resolved": 1})
        self.assertEqual(payload["event_summary"]["resolved_count"], 1)
        self.assertEqual(payload["source_report"]["sample_scope"]["strategy_config_id"], "cfg")
        self.assertTrue(payload["schema_hash"])
        self.assertTrue(payload["safety"]["does_not_submit_orders"])
        self.assertTrue(payload["safety"]["does_not_change_strategy_config"])

    def test_recent_evaluations_fallback_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "outcome.json"
            payload = outcome_report([evaluation("sig-1")])
            payload.pop("evaluations")
            report_file.write_text(json.dumps(payload), encoding="utf-8")

            result = store.build_report(str(report_file))

        self.assertEqual(result["event_count"], 1)
        self.assertIn("outcome_report_missing_full_evaluations_using_recent_only", result["warnings"])

    def test_pending_without_horizon_outcome_uses_evaluation_status_as_primary_status(self):
        item = evaluation("sig-pending", "pending")
        item["outcomes"] = {}
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "outcome.json"
            report_file.write_text(json.dumps(outcome_report([item])), encoding="utf-8")

            payload = store.build_report(str(report_file))

        self.assertEqual(payload["event_summary"]["by_status"], {"pending": 1})
        self.assertEqual(payload["event_summary"]["by_primary_status"], {"pending": 1})

    def test_apply_requires_schema_hash(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "outcome.json"
            report_file.write_text(json.dumps(outcome_report()), encoding="utf-8")

            with patch.object(store, "psql_script") as psql_mock:
                payload = store.build_report(str(report_file), apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_schema_hash_required", payload["validation_reasons"])
        psql_mock.assert_not_called()

    def test_apply_runs_idempotent_sql_when_schema_hash_matches(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "outcome.json"
            report_file.write_text(json.dumps(outcome_report()), encoding="utf-8")
            dry = store.build_report(str(report_file))
            calls = []

            def fake_psql(sql, timeout=120):
                calls.append(sql)
                return type("Result", (), {"returncode": 0, "stdout": "INSERT 0 1", "stderr": ""})()

            with patch.object(store, "psql_script", side_effect=fake_psql):
                payload = store.build_report(
                    str(report_file),
                    apply=True,
                    confirm_schema_hash=dry["schema_hash"],
                )

        self.assertEqual(payload["status"], "applied")
        self.assertTrue(payload["applied"])
        self.assertEqual(len(calls), 1)
        self.assertIn("CREATE TABLE IF NOT EXISTS rt_signal_outcome_events", calls[0])
        self.assertIn("ON CONFLICT (signal_id) DO UPDATE", calls[0])

    def test_invalid_table_name_blocks_apply(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "outcome.json"
            report_file.write_text(json.dumps(outcome_report()), encoding="utf-8")

            with patch.object(store, "psql_script") as psql_mock:
                payload = store.build_report(
                    str(report_file),
                    table_name="bad;drop",
                    apply=True,
                    confirm_schema_hash="x",
                )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("invalid_table_name", payload["validation_reasons"])
        psql_mock.assert_not_called()

    def test_sql_escapes_raw_evaluation_json(self):
        item = evaluation("sig-quote")
        item["trigger"] = "O'Hare MA"
        event = {"signal_id": "sig-quote", "evaluation": item, "primary_horizon": "1d", "primary_outcome": item["outcomes"]["1d"]}

        sql = store.upsert_sql(event, outcome_report([item]))

        self.assertIn("O''Hare MA", sql)
        self.assertIn("'sig-quote'", sql)
        self.assertIn("::jsonb", sql)


if __name__ == "__main__":
    unittest.main()
