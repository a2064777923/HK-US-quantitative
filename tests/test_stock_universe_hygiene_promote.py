import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import stock_universe_hygiene_promote as promote


def hygiene_report():
    return {
        "schema": "universe_hygiene_report_v1",
        "source": {"auto_applies_stock_changes": False},
        "proposal": {
            "schema": "stock_universe_hygiene_proposal_v1",
            "source": {
                "manual_review_required": True,
                "auto_applied": False,
            },
        },
        "markets": {
            "HK": {
                "high_priority_candidates": [
                    {
                        "market": "HK",
                        "symbol": "hkHSI",
                        "exchange": "HKEX",
                        "name": "hkHSI",
                        "recommended_action": "candidate_remove_from_stock_universe",
                        "issues": ["symbol_format_unusual_for_exchange", "missing_daily_klines"],
                        "latest_date": None,
                        "history_rows_120d": 0,
                    },
                    {
                        "market": "HK",
                        "symbol": "00011",
                        "exchange": "HKEX",
                        "name": "恒生銀行",
                        "recommended_action": "candidate_deactivate_or_symbol_mapping",
                        "issues": ["latest_kline_stale_ge_30d"],
                        "latest_date": "2026-01-14",
                        "history_rows_120d": 0,
                    },
                ]
            }
        },
    }


def write_report(path, payload=None):
    payload = payload or hygiene_report()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


class StockUniverseHygienePromoteTests(unittest.TestCase):
    def test_dry_run_reports_hash_and_selected_safe_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "hygiene.json"
            write_report(report_file)

            payload = promote.build_report(str(report_file), symbols=["hkHSI"])

        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["selected_count"], 1)
        self.assertEqual(payload["selected_candidates"][0]["symbol"], "hkHSI")
        self.assertTrue(payload["safety"]["requires_confirm_proposal_hash"])
        self.assertTrue(payload["safety"]["requires_explicit_symbol_selection"])

    def test_apply_requires_hash_and_symbol_selection(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "hygiene.json"
            write_report(report_file)

            payload = promote.build_report(str(report_file), apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_proposal_hash_required", payload["validation_reasons"])
        self.assertIn("symbol_selection_required", payload["validation_reasons"])

    def test_rejects_disallowed_stale_symbol_without_extra_allow_action(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "hygiene.json"
            write_report(report_file)

            payload = promote.build_report(str(report_file), symbols=["00011"])

        self.assertEqual(payload["status"], "invalid_selection")
        self.assertEqual(payload["selected_count"], 0)
        self.assertIn("one_or_more_symbols_rejected", payload["validation_reasons"])
        self.assertEqual(payload["rejected_symbols"][0]["reason"], "recommended_action_not_allowed")

    def test_apply_deactivates_selected_symbol_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "hygiene.json"
            write_report(report_file)
            dry = promote.build_report(str(report_file), symbols=["hkHSI"])
            calls = []

            def fake_apply(candidates, backup_dir=promote.BACKUP_DIR):
                calls.append(candidates)
                return {"status": "applied", "backup_file": str(Path(td) / "backup.json")}

            with patch.object(promote, "fetch_open_position_symbols", return_value=([], [])), patch.object(
                promote,
                "apply_deactivations",
                side_effect=fake_apply,
            ):
                payload = promote.build_report(
                    str(report_file),
                    symbols=["hkHSI"],
                    apply=True,
                    confirm_proposal_hash=dry["proposal_hash"],
                )

        self.assertEqual(payload["status"], "applied")
        self.assertTrue(payload["applied"])
        self.assertEqual(calls[0][0]["symbol"], "hkHSI")

    def test_apply_blocks_selected_symbol_with_open_position(self):
        with tempfile.TemporaryDirectory() as td:
            report_file = Path(td) / "hygiene.json"
            write_report(report_file)
            dry = promote.build_report(str(report_file), symbols=["hkHSI"])
            protected = [{"symbol": "HKHSI", "portfolio_id": "8", "status": "holding", "quantity": "100"}]

            with patch.object(promote, "fetch_open_position_symbols", return_value=(protected, [])), patch.object(
                promote,
                "apply_deactivations",
            ) as apply_mock:
                payload = promote.build_report(
                    str(report_file),
                    symbols=["hkHSI"],
                    apply=True,
                    confirm_proposal_hash=dry["proposal_hash"],
                )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("selected_symbol_has_open_position", payload["validation_reasons"])
        self.assertEqual(payload["protected_positions"], protected)
        apply_mock.assert_not_called()

    def test_sql_for_deactivate_is_scoped_to_symbol_and_active_rows(self):
        sql = promote.sql_for_deactivate({"symbol": "hkHSI"})

        self.assertIn("UPDATE stocks SET", sql)
        self.assertIn("is_active = false", sql)
        self.assertIn("WHERE upper(symbol) = upper('hkHSI')", sql)
        self.assertIn("AND is_active = true", sql)


if __name__ == "__main__":
    unittest.main()
