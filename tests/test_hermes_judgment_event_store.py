import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import hermes_judgment_event_store as store


def judgment(signal_id="sig-1", decision="approve"):
    return {
        "schema": "hermes_trade_judgment_v1",
        "packet_id": "packet-1",
        "signal_id": signal_id,
        "decision": decision,
        "confidence": 0.82,
        "reviewed_at": "2026-06-12T10:00:00",
        "reviewer": "hermes",
        "supporting_factors": ["support"],
        "opposing_factors": ["opposition"],
        "risk_notes": ["risk"],
    }


def audit_payload(status="PASS", signal_id="sig-1"):
    return {
        "schema": "hermes_judgment_audit_report_v1",
        "judgments": [
            {
                "packet_id": "packet-1",
                "signal_id": signal_id,
                "decision": "approve",
                "reviewed_at": "2026-06-12T10:00:00",
                "status": status,
                "reasons": [] if status == "PASS" else ["approval_for_ineligible_review_item"],
                "packet_source": "packet_archive",
            }
        ],
    }


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class HermesJudgmentEventStoreTests(unittest.TestCase):
    def test_dry_run_reports_events_and_audit_status(self):
        with tempfile.TemporaryDirectory() as td:
            judgments = Path(td) / "judgments.jsonl"
            audit = Path(td) / "audit.json"
            write_jsonl(judgments, [judgment(), judgment("sig-2", "hold")])
            audit.write_text(json.dumps(audit_payload()), encoding="utf-8")

            payload = store.build_report(str(judgments), str(audit))

        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["judgment_count"], 2)
        self.assertEqual(payload["event_count"], 2)
        self.assertEqual(payload["event_summary"]["by_decision"], {"approve": 1, "hold": 1})
        self.assertEqual(payload["event_summary"]["by_audit_status"], {"PASS": 1, "missing": 1})
        self.assertTrue(payload["schema_hash"])
        self.assertTrue(payload["safety"]["does_not_submit_orders"])
        self.assertTrue(payload["safety"]["does_not_change_intake_state"])

    def test_apply_requires_schema_hash(self):
        with tempfile.TemporaryDirectory() as td:
            judgments = Path(td) / "judgments.jsonl"
            write_jsonl(judgments, [judgment()])

            with patch.object(store, "psql_script") as psql_mock:
                payload = store.build_report(str(judgments), apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_schema_hash_required", payload["validation_reasons"])
        psql_mock.assert_not_called()

    def test_apply_runs_idempotent_sql_when_schema_hash_matches(self):
        with tempfile.TemporaryDirectory() as td:
            judgments = Path(td) / "judgments.jsonl"
            audit = Path(td) / "audit.json"
            write_jsonl(judgments, [judgment()])
            audit.write_text(json.dumps(audit_payload(status="FAIL")), encoding="utf-8")
            dry = store.build_report(str(judgments), str(audit))
            calls = []

            def fake_psql(sql, timeout=120):
                calls.append(sql)
                return type("Result", (), {"returncode": 0, "stdout": "INSERT 0 1", "stderr": ""})()

            with patch.object(store, "psql_script", side_effect=fake_psql):
                payload = store.build_report(
                    str(judgments),
                    str(audit),
                    apply=True,
                    confirm_schema_hash=dry["schema_hash"],
                )

        self.assertEqual(payload["status"], "applied")
        self.assertTrue(payload["applied"])
        self.assertEqual(len(calls), 1)
        self.assertIn("CREATE TABLE IF NOT EXISTS hermes_trade_judgment_events", calls[0])
        self.assertIn("ON CONFLICT (judgment_key) DO UPDATE", calls[0])
        self.assertIn("'FAIL'", calls[0])
        self.assertIn("approval_for_ineligible_review_item", calls[0])

    def test_invalid_table_name_blocks_apply(self):
        with tempfile.TemporaryDirectory() as td:
            judgments = Path(td) / "judgments.jsonl"
            write_jsonl(judgments, [judgment()])

            with patch.object(store, "psql_script") as psql_mock:
                payload = store.build_report(
                    str(judgments),
                    table_name="bad;drop",
                    apply=True,
                    confirm_schema_hash="x",
                )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("invalid_table_name", payload["validation_reasons"])
        psql_mock.assert_not_called()

    def test_sql_escapes_raw_judgment_json(self):
        item = judgment()
        item["supporting_factors"] = ["O'Hare support"]
        event = {"judgment_key": store.judgment_key(item), "judgment": item, "audit": {}}

        sql = store.upsert_sql(event)

        self.assertIn("O''Hare support", sql)
        self.assertIn("'approve'", sql)
        self.assertIn("::jsonb", sql)


if __name__ == "__main__":
    unittest.main()
