import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import rt_alert_event_store as store


def sample_alert(signal_id="sig-1", side="BUY"):
    return {
        "signal_id": signal_id,
        "source": "rt_signal_engine_v5",
        "symbol": "00700",
        "market": "HK",
        "signal_type": side,
        "candidate_signal_type": side,
        "trigger": "unit-test",
        "confirmed": True,
        "execution_candidate": True,
        "full_score": 0.72 if side == "BUY" else -0.72,
        "price": 300,
        "entry_price": 300,
        "stop_loss": 290,
        "take_profit": 330,
        "rr_ratio": 3,
        "strategy_config_id": "cfg",
        "watchlist_id": "wl",
        "generated_at": "2026-06-12T10:00:00",
    }


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class RtAlertEventStoreTests(unittest.TestCase):
    def test_dry_run_dedupes_alerts_and_reports_schema_hash(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            write_jsonl(queue, [sample_alert("sig-1"), sample_alert("sig-1"), sample_alert("sig-2", "SELL")])

            payload = store.build_report(str(queue), table_name="rt_signal_alert_events")

        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["raw_alert_count"], 3)
        self.assertEqual(payload["event_count"], 2)
        self.assertEqual(payload["duplicate_count"], 1)
        self.assertEqual(payload["event_summary"]["by_signal_type"], {"BUY": 1, "SELL": 1})
        self.assertTrue(payload["schema_hash"])
        self.assertTrue(payload["safety"]["requires_confirm_schema_hash"])
        self.assertTrue(payload["safety"]["does_not_submit_orders"])

    def test_apply_requires_schema_hash(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            write_jsonl(queue, [sample_alert()])

            with patch.object(store, "psql_script") as psql_mock:
                payload = store.build_report(str(queue), apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_schema_hash_required", payload["validation_reasons"])
        psql_mock.assert_not_called()

    def test_apply_runs_idempotent_sql_when_schema_hash_matches(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            write_jsonl(queue, [sample_alert()])
            dry = store.build_report(str(queue))
            calls = []

            def fake_psql(sql, timeout=120):
                calls.append(sql)
                return type("Result", (), {"returncode": 0, "stdout": "INSERT 0 1", "stderr": ""})()

            with patch.object(store, "psql_script", side_effect=fake_psql):
                payload = store.build_report(
                    str(queue),
                    apply=True,
                    confirm_schema_hash=dry["schema_hash"],
                )

        self.assertEqual(payload["status"], "applied")
        self.assertTrue(payload["applied"])
        self.assertEqual(len(calls), 1)
        self.assertIn("CREATE TABLE IF NOT EXISTS rt_signal_alert_events", calls[0])
        self.assertIn("ON CONFLICT (signal_id) DO UPDATE", calls[0])

    def test_invalid_table_name_is_rejected_before_apply(self):
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            write_jsonl(queue, [sample_alert()])

            with patch.object(store, "psql_script") as psql_mock:
                payload = store.build_report(str(queue), table_name="bad;drop", apply=True, confirm_schema_hash="x")

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("invalid_table_name", payload["validation_reasons"])
        psql_mock.assert_not_called()

    def test_sql_escapes_strings_and_keeps_raw_alert_json(self):
        alert = sample_alert("sig-quote")
        alert["trigger"] = "O'Hare breakout"
        alert["symbol"] = "aapl"

        sql = store.upsert_sql(alert)

        self.assertIn("'O''Hare breakout'", sql)
        self.assertIn("'AAPL'", sql)
        self.assertIn("'rt_signal_engine_v5'", sql)
        self.assertIn("::jsonb", sql)


if __name__ == "__main__":
    unittest.main()
