import json
import tempfile
import unittest
from pathlib import Path

from scripts import watchlist_diff_report as diff
from scripts import watchlist_promote as promote
from scripts import rt_signal_engine_v5 as rt


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def watchlist(hk=None, us=None):
    return {
        "schema": "rt_signal_watchlist_v1",
        "markets": {
            "HK": {"symbols": hk or []},
            "US": {"symbols": us or []},
        },
    }


def diff_report(live, proposal_markets):
    markets = {}
    for market, changes in proposal_markets.items():
        add_symbols = changes.get("add_symbols") or []
        remove_symbols = changes.get("remove_symbols") or []
        markets[market] = {
            "add_symbols": add_symbols,
            "remove_symbols": remove_symbols,
            "add_count": len(add_symbols),
            "remove_count": len(remove_symbols),
            "remove_context": [
                {"symbol": symbol, "blockers": ["sim_allocation_below_one_lot"]}
                for symbol in remove_symbols
            ],
        }
    proposal = diff.build_proposal(markets, "2026-06-12T10:00:00")
    return {
        "schema": "watchlist_diff_report_v1",
        "source": {
            "read_only": True,
            "auto_applies_watchlist": False,
            "submits_orders": False,
            "live_watchlist_hash": diff.stable_hash(promote.live_symbols(live)),
        },
        "markets": markets,
        "proposal": proposal,
    }


class WatchlistPromoteTests(unittest.TestCase):
    def test_dry_run_reports_changes_without_writing_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00700", "03988"], us=["AAPL"])
            write_json(target, live)
            write_json(report, diff_report(live, {"HK": {"add_symbols": ["06690"], "remove_symbols": ["00700"]}}))

            payload = promote.build_report(str(report), str(target))
            stored = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["change_count"], 2)
        self.assertEqual(stored["markets"]["HK"]["symbols"], ["00700", "03988"])
        self.assertEqual(payload["proposed_watchlist"]["markets"]["HK"]["symbols"], ["03988", "06690"])
        self.assertEqual(
            payload["proposed_watchlist_id"],
            rt.watchlist_digest({"HK": ["03988", "06690"], "US": ["AAPL"]}),
        )

    def test_apply_requires_matching_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00700"], us=[])
            write_json(target, live)
            write_json(report, diff_report(live, {"HK": {"add_symbols": [], "remove_symbols": ["00700"]}}))

            payload = promote.build_report(str(report), str(target), apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_proposal_hash_required", payload["validation_reasons"])

    def test_apply_blocks_when_target_hash_changed_since_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00700"], us=[])
            write_json(report, diff_report(live, {"HK": {"add_symbols": [], "remove_symbols": ["00700"]}}))
            changed_live = watchlist(hk=["00700", "03988"], us=[])
            write_json(target, changed_live)
            proposal_hash = json.loads(report.read_text(encoding="utf-8"))["proposal"]["proposal_hash"]

            payload = promote.build_report(
                str(report),
                str(target),
                apply=True,
                confirm_proposal_hash=proposal_hash,
            )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("target_watchlist_hash_changed_since_report", payload["validation_reasons"])

    def test_validate_hash_uses_proposal_payload_not_truncated_report_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00001", "00002"], us=[])
            write_json(target, live)
            proposal = {
                "schema": "rt_signal_watchlist_change_proposal_v1",
                "generated_at": "2026-06-12T10:00:00",
                "source": {
                    "manual_review_required": True,
                    "auto_applied": False,
                    "does_not_restart_services": True,
                    "does_not_submit_orders": True,
                },
                "markets": {
                    "HK": {
                        "add_symbols": [],
                        "remove_symbols": ["00001", "00002"],
                        "remove_symbols_missing_active_universe": ["00001", "00002"],
                        "review_required": True,
                    }
                },
            }
            proposal["proposal_hash"] = diff.proposal_hash_for_payload(proposal)
            write_json(
                report,
                {
                    "schema": "watchlist_diff_report_v1",
                    "source": {
                        "read_only": True,
                        "auto_applies_watchlist": False,
                        "submits_orders": False,
                        "live_watchlist_hash": diff.stable_hash(promote.live_symbols(live)),
                    },
                    "markets": {
                        "HK": {
                            "add_symbols": [],
                            "remove_symbols": ["00001", "00002"],
                            "add_count": 0,
                            "remove_count": 2,
                            "remove_context": [
                                {
                                    "symbol": "00001",
                                    "blockers": ["not_in_active_or_ranked_universe"],
                                }
                            ],
                        }
                    },
                    "proposal": proposal,
                },
            )

            payload = promote.build_report(str(report), str(target))

        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["validation_reasons"], [])

    def test_apply_writes_target_and_backup_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            backup_dir = root / "backups"
            live = watchlist(hk=["00700", "03988"], us=["AAPL"])
            report_payload = diff_report(
                live,
                {
                    "HK": {"add_symbols": ["06690"], "remove_symbols": ["00700"]},
                    "US": {"add_symbols": ["MSFT"], "remove_symbols": []},
                },
            )
            write_json(target, live)
            write_json(report, report_payload)
            old_backup_dir = promote.BACKUP_DIR
            promote.BACKUP_DIR = str(backup_dir)
            try:
                payload = promote.build_report(
                    str(report),
                    str(target),
                    apply=True,
                    confirm_proposal_hash=report_payload["proposal"]["proposal_hash"],
                )
            finally:
                promote.BACKUP_DIR = old_backup_dir
            stored = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "applied_restart_required")
        self.assertTrue(payload["applied"])
        self.assertTrue(Path(payload["backup_file"]).exists())
        self.assertEqual(stored["markets"]["HK"]["symbols"], ["03988", "06690"])
        self.assertEqual(stored["markets"]["US"]["symbols"], ["AAPL", "MSFT"])
        self.assertTrue(payload["safety"]["does_not_restart_services"])


if __name__ == "__main__":
    unittest.main()
