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
        self.assertIn("updated_at = NOW()", joined)
        self.assertNotIn("expected_count", joined)
        self.assertNotIn("'running'", joined)
        self.assertIn("'feature_ready'", joined)
        self.assertIn("'signal_ready'", joined)

    def test_feature_run_timestamps_refresh_on_same_day_rerun(self):
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
                "updated_at",
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
            engine.finalize_feature_run("signal_v4_20260612", 285, 285, {"HOLD": 285})

        insert_sql = executed[0]
        finalize_sql = executed[1]
        self.assertIn("updated_at = NOW()", insert_sql)
        self.assertIn("updated_at = NOW()", finalize_sql)

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
            normalized = " ".join(sql.split())
            if "information_schema.columns" in sql and "engine_feature_runs" in sql:
                return feature_columns
            if "information_schema.columns" in sql and "engine_signal_scores" in sql:
                return signal_columns
            if "SELECT max(daily_bar.trade_date)" in sql:
                self.assertIn("SELECT DISTINCT ON (k.symbol, k.timestamp::date)", sql)
                self.assertIn("ORDER BY k.symbol, k.timestamp::date, k.timestamp DESC", normalized)
                return "2026-06-12"
            if "SELECT d.symbol, s.exchange" in sql:
                self.assertIn("WITH daily_bar AS", sql)
                self.assertIn("HAVING count(*) >= 30 AND max(d.trade_date)", normalized)
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

    def test_analyze_stock_reads_canonical_daily_bars_before_limit(self):
        captured = {}

        def fake_db(sql, timeout=30):
            captured["sql"] = sql
            return "\n".join(
                [
                    f"{100 + idx}|{101 + idx}|{99 + idx}|{100 + idx}|{1000 + idx}"
                    for idx in range(35)
                ]
            )

        with patch.object(engine, "db", side_effect=fake_db):
            result = engine.analyze_stock("AAPL", "NASDAQ")

        self.assertIsNotNone(result)
        sql = captured["sql"]
        normalized = " ".join(sql.split())
        self.assertIn("WITH daily_bar AS", sql)
        self.assertIn("SELECT DISTINCT ON (timestamp::date)", sql)
        self.assertIn("ORDER BY timestamp::date, timestamp DESC", normalized)
        self.assertIn("FROM daily_bar ORDER BY trade_date DESC LIMIT 120", normalized)

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
