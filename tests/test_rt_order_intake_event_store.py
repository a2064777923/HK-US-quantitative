import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import rt_order_intake_event_store as store


def intake_decision(signal_id="sig-1", status="dry_run", mode="dry-run"):
    return {
        "signal_id": signal_id,
        "status": status,
        "mode": mode,
        "plan": {
            "symbol": "00700",
            "side": "buy",
            "quantity": 100,
            "price_reference": 300,
            "notional_hkd": 30000,
            "risk_hkd": 1000,
        },
        "strategy_evidence": {"status": "DRY_RUN_ONLY", "would_block_execute": True},
        "symbol_conflict": {"status": "DRY_RUN_ONLY"},
        "market_context": {"status": "PASS"},
        "hermes": {"status": "DRY_RUN_ONLY"},
        "checked_at": "2026-06-12T10:00:00",
    }


def write_state(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class RtOrderIntakeEventStoreTests(unittest.TestCase):
    def test_dry_run_reads_processed_and_dry_run_ledgers(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            write_state(
                state_file,
                {
                    "dry_runs": {"sig-1": intake_decision("sig-1")},
                    "processed": {
                        "sig-2": {
                            **intake_decision("sig-2", status="submitted", mode="execute"),
                            "submitted_at": "2026-06-12T10:05:00",
                            "order_result": {"order_id": "ok"},
                        }
                    },
                },
            )

            payload = store.build_report(str(state_file))

        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["state_stats"]["dry_run_count"], 1)
        self.assertEqual(payload["state_stats"]["processed_count"], 1)
        self.assertEqual(payload["event_count"], 2)
        self.assertEqual(payload["event_summary"]["by_ledger"], {"dry_runs": 1, "processed": 1})
        self.assertEqual(payload["event_summary"]["by_status"], {"dry_run": 1, "submitted": 1})
        self.assertEqual(payload["event_summary"]["submitted_count"], 1)
        self.assertTrue(payload["schema_hash"])
        self.assertTrue(payload["safety"]["does_not_submit_orders"])
        self.assertTrue(payload["safety"]["does_not_change_intake_state"])

    def test_apply_requires_schema_hash(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            write_state(state_file, {"dry_runs": {"sig-1": intake_decision("sig-1")}, "processed": {}})

            with patch.object(store, "psql_script") as psql_mock:
                payload = store.build_report(str(state_file), apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_schema_hash_required", payload["validation_reasons"])
        psql_mock.assert_not_called()

    def test_apply_runs_idempotent_sql_when_schema_hash_matches(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            write_state(state_file, {"dry_runs": {"sig-1": intake_decision("sig-1")}, "processed": {}})
            dry = store.build_report(str(state_file))
            calls = []

            def fake_psql(sql, timeout=120):
                calls.append(sql)
                return type("Result", (), {"returncode": 0, "stdout": "INSERT 0 1", "stderr": ""})()

            with patch.object(store, "psql_script", side_effect=fake_psql):
                payload = store.build_report(
                    str(state_file),
                    apply=True,
                    confirm_schema_hash=dry["schema_hash"],
                )

        self.assertEqual(payload["status"], "applied")
        self.assertTrue(payload["applied"])
        self.assertEqual(len(calls), 1)
        self.assertIn("CREATE TABLE IF NOT EXISTS rt_order_intake_events", calls[0])
        self.assertIn("ON CONFLICT (event_key) DO UPDATE", calls[0])

    def test_invalid_table_name_blocks_apply(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            write_state(state_file, {"dry_runs": {"sig-1": intake_decision("sig-1")}, "processed": {}})

            with patch.object(store, "psql_script") as psql_mock:
                payload = store.build_report(
                    str(state_file),
                    table_name="bad;drop",
                    apply=True,
                    confirm_schema_hash="x",
                )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("invalid_table_name", payload["validation_reasons"])
        psql_mock.assert_not_called()

    def test_sql_escapes_raw_decision_json(self):
        decision = intake_decision("sig-quote")
        decision["reasons"] = ["O'Hare rejection"]
        event = {
            "event_key": store.decision_event_key("dry_runs", "sig-quote", decision),
            "ledger": "dry_runs",
            "signal_id": "sig-quote",
            "decision": decision,
        }

        sql = store.upsert_sql(event)

        self.assertIn("O''Hare rejection", sql)
        self.assertIn("'sig-quote'", sql)
        self.assertIn("::jsonb", sql)


if __name__ == "__main__":
    unittest.main()
