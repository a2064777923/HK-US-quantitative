import unittest
from datetime import datetime
from unittest.mock import patch

from scripts import signal_engine_v4 as engine


class SignalEngineV4SchemaTests(unittest.TestCase):
    def test_feature_run_writes_remote_symbol_count_columns(self):
        executed = []
        remote_columns = "\n".join(
            [
                "run_id",
                "tenant_id",
                "user_id",
                "trade_date",
                "model_name",
                "model_version",
                "feature_version",
                "feature_dim",
                "status",
                "expected_symbols",
                "ready_symbols",
                "missing_symbols",
                "source",
                "quality",
            ]
        )

        def fake_db(sql, timeout=30):
            if "information_schema.columns" in sql:
                return remote_columns
            executed.append(" ".join(sql.split()))
            return ""

        engine._COLUMN_CACHE.clear()
        with patch.object(engine, "db", side_effect=fake_db):
            engine.ensure_feature_run("signal_v4_20260612", "2026-06-12", 285)
            engine.finalize_feature_run("signal_v4_20260612", 285, 280, {"BUY": 10})

        joined = "\n".join(executed)
        self.assertIn("expected_symbols", joined)
        self.assertIn("ready_symbols", joined)
        self.assertIn("missing_symbols", joined)
        self.assertNotIn("expected_count", joined)
        self.assertNotIn("'running'", joined)
        self.assertIn("'feature_ready'", joined)
        self.assertIn("'signal_ready'", joined)

    def test_preflight_reports_schema_and_candidates_without_writes(self):
        feature_columns = "\n".join(
            [
                "run_id",
                "tenant_id",
                "user_id",
                "trade_date",
                "model_name",
                "model_version",
                "feature_version",
                "feature_dim",
                "status",
                "expected_symbols",
                "ready_symbols",
                "missing_symbols",
                "source",
                "quality",
            ]
        )
        signal_columns = "\n".join(["run_id", "quality", "fusion_score", "signal_side"])

        def fake_db(sql, timeout=30):
            if "information_schema.columns" in sql and "engine_feature_runs" in sql:
                return feature_columns
            if "information_schema.columns" in sql and "engine_signal_scores" in sql:
                return signal_columns
            if "SELECT max(k.timestamp::date)" in sql:
                return "2026-06-12"
            if "SELECT k.symbol, s.exchange" in sql:
                return "00700|HKEX\nAAPL|NASDAQ"
            raise AssertionError(f"unexpected SQL: {sql}")

        engine._COLUMN_CACHE.clear()
        with patch.object(engine, "db", side_effect=fake_db), patch.object(
            engine, "daily_signal_write_block", return_value=(False, "")
        ):
            payload = engine.build_preflight_payload()

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["trade_date"], "2026-06-12")
        self.assertEqual(payload["run_id"], "signal_v4_20260612")
        self.assertEqual(payload["candidate_count"], 2)
        self.assertFalse(payload["writes_database"])
        self.assertFalse(payload["write_blocked"])
        self.assertEqual(payload["feature_run_count_columns"]["expected"], "expected_symbols")
        self.assertTrue(payload["schema_checks"]["engine_signal_scores_has_run_id"])

    def test_daily_signal_write_block_before_ready_time(self):
        with patch.object(engine, "ALLOW_INTRADAY_DAILY_SIGNAL", False):
            blocked, reason = engine.daily_signal_write_block(
                "2026-06-12",
                now=datetime(2026, 6, 12, 9, 34),
            )

        self.assertTrue(blocked)
        self.assertIn("current_session_before_daily_signal_ready_time", reason)

    def test_daily_signal_write_allowed_after_ready_time(self):
        with patch.object(engine, "ALLOW_INTRADAY_DAILY_SIGNAL", False):
            blocked, reason = engine.daily_signal_write_block(
                "2026-06-12",
                now=datetime(2026, 6, 12, 16, 30),
            )

        self.assertFalse(blocked)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
