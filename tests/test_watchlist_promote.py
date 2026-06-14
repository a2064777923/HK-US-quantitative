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


def ready_report(proposal_hash=None):
    warning_gates = []
    status = "READY"
    ready = True
    if proposal_hash:
        status = "WARN"
        ready = False
        warning_gates = [
            {
                "gate": "watchlist_proposal",
                "status": "WARN",
                "data": {"proposal_hash": proposal_hash},
            }
        ]
    return {
        "schema": "execution_readiness_report_v1",
        "status": status,
        "ready_for_execute": ready,
        "blocking_gates": [],
        "warning_gates": warning_gates,
    }


def source_reliability(status="OK"):
    return {
        "schema": "source_reliability_report_v1",
        "status": status,
        "recommendations": [] if status == "OK" else ["refresh_source_reliability_before_watchlist_promotion"],
    }


def simulation_performance(status="OK"):
    return {
        "schema": "simulation_performance_report_v1",
        "status": status,
        "reason_codes": [] if status == "OK" else ["simulation_total_return_not_positive"],
        "summary": {"return_pct_vs_initial": 2.0 if status == "OK" else -3.0},
    }


def strategy_learning(proposal_hash=None, audit_pass=True, approved=5, rejected=5, fail=0, missing=0):
    payload = {
        "schema": "strategy_learning_report_v1",
        "judgment_audit_coverage": {
            "audit_report_available": True,
            "audit_report_truncated": False,
            "audit_fail_count": fail,
            "audit_missing_count": missing,
        },
        "sizing_blocker_remediation": {
            "sizing_blocker_count": 0,
            "covered_by_watchlist_removal_count": 0,
            "uncovered_count": 0,
            "watchlist_proposal_hash": proposal_hash,
        },
    }
    if audit_pass:
        payload["audit_pass_judgment_effect"] = {
            "sample_filter": "judgment_audit_status_PASS",
            "approved_or_reduced": {"resolved_count": approved},
            "rejected_or_held": {"resolved_count": rejected},
        }
    return payload


def write_guard_reports(root, proposal_hash=None, readiness=None, source=None, simulation=None, learning=None):
    files = {
        "readiness": root / "readiness.json",
        "source": root / "source.json",
        "simulation": root / "simulation.json",
        "learning": root / "learning.json",
    }
    write_json(files["readiness"], readiness if readiness is not None else ready_report(proposal_hash))
    write_json(files["source"], source if source is not None else source_reliability())
    write_json(files["simulation"], simulation if simulation is not None else simulation_performance())
    write_json(files["learning"], learning if learning is not None else strategy_learning(proposal_hash))
    return files


def build_promote_report(report, target, guard_files, **kwargs):
    return promote.build_report(
        str(report),
        str(target),
        execution_readiness_file=str(guard_files["readiness"]),
        source_reliability_file=str(guard_files["source"]),
        simulation_performance_file=str(guard_files["simulation"]),
        strategy_learning_file=str(guard_files["learning"]),
        **kwargs,
    )


class WatchlistPromoteTests(unittest.TestCase):
    def test_dry_run_reports_changes_without_writing_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00700", "03988"], us=["AAPL"])
            write_json(target, live)
            report_payload = diff_report(live, {"HK": {"add_symbols": ["06690"], "remove_symbols": ["00700"]}})
            write_json(report, report_payload)
            guard_files = write_guard_reports(root, proposal_hash=report_payload["proposal"]["proposal_hash"])

            payload = build_promote_report(report, target, guard_files)
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
            report_payload = diff_report(live, {"HK": {"add_symbols": [], "remove_symbols": ["00700"]}})
            write_json(report, report_payload)
            guard_files = write_guard_reports(root, proposal_hash=report_payload["proposal"]["proposal_hash"])

            payload = build_promote_report(report, target, guard_files, apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_proposal_hash_required", payload["validation_reasons"])

    def test_apply_blocks_when_target_hash_changed_since_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00700"], us=[])
            report_payload = diff_report(live, {"HK": {"add_symbols": [], "remove_symbols": ["00700"]}})
            write_json(report, report_payload)
            changed_live = watchlist(hk=["00700", "03988"], us=[])
            write_json(target, changed_live)
            proposal_hash = json.loads(report.read_text(encoding="utf-8"))["proposal"]["proposal_hash"]
            guard_files = write_guard_reports(root, proposal_hash=proposal_hash)

            payload = build_promote_report(
                report,
                target,
                guard_files,
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
            guard_files = write_guard_reports(root, proposal_hash=proposal["proposal_hash"])

            payload = build_promote_report(report, target, guard_files)

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
            guard_files = write_guard_reports(root, proposal_hash=report_payload["proposal"]["proposal_hash"])
            old_backup_dir = promote.BACKUP_DIR
            promote.BACKUP_DIR = str(backup_dir)
            try:
                payload = build_promote_report(
                    report,
                    target,
                    guard_files,
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

    def test_apply_allows_matching_watchlist_readiness_warning_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            backup_dir = root / "backups"
            live = watchlist(hk=["00700"], us=[])
            report_payload = diff_report(live, {"HK": {"add_symbols": ["06690"], "remove_symbols": ["00700"]}})
            proposal_hash = report_payload["proposal"]["proposal_hash"]
            write_json(target, live)
            write_json(report, report_payload)
            guard_files = write_guard_reports(root, proposal_hash=proposal_hash)
            old_backup_dir = promote.BACKUP_DIR
            promote.BACKUP_DIR = str(backup_dir)
            try:
                payload = build_promote_report(
                    report,
                    target,
                    guard_files,
                    apply=True,
                    confirm_proposal_hash=proposal_hash,
                )
            finally:
                promote.BACKUP_DIR = old_backup_dir

        self.assertEqual(payload["status"], "applied_restart_required")
        self.assertEqual(payload["promotion_blockers"], [])
        self.assertTrue(payload["promotion_context"]["execution_readiness"]["matching_watchlist_warning"])

    def test_apply_blocks_when_readiness_has_other_warning_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00700"], us=[])
            report_payload = diff_report(live, {"HK": {"add_symbols": ["06690"], "remove_symbols": []}})
            write_json(target, live)
            write_json(report, report_payload)
            readiness = ready_report(report_payload["proposal"]["proposal_hash"])
            readiness["warning_gates"].append({"gate": "source_reliability", "status": "WARN"})
            guard_files = write_guard_reports(root, report_payload["proposal"]["proposal_hash"], readiness=readiness)

            payload = build_promote_report(
                report,
                target,
                guard_files,
                apply=True,
                confirm_proposal_hash=report_payload["proposal"]["proposal_hash"],
            )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn(
            "proposal_promotion_blocker:execution_readiness_not_clean_blocks_watchlist_promotion",
            payload["validation_reasons"],
        )

    def test_apply_blocks_when_source_reliability_or_simulation_not_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00700"], us=[])
            report_payload = diff_report(live, {"HK": {"add_symbols": ["06690"], "remove_symbols": []}})
            write_json(target, live)
            write_json(report, report_payload)
            guard_files = write_guard_reports(
                root,
                report_payload["proposal"]["proposal_hash"],
                source=source_reliability("DEGRADED"),
                simulation=simulation_performance("FAIL"),
            )

            payload = build_promote_report(
                report,
                target,
                guard_files,
                apply=True,
                confirm_proposal_hash=report_payload["proposal"]["proposal_hash"],
            )

        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("source_reliability_not_ok_blocks_watchlist_promotion", codes)
        self.assertIn("simulation_performance_not_ok_blocks_watchlist_promotion", codes)

    def test_apply_blocks_when_strategy_learning_audit_evidence_not_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "watchlist.json"
            report = root / "diff.json"
            live = watchlist(hk=["00700"], us=[])
            report_payload = diff_report(live, {"HK": {"add_symbols": ["06690"], "remove_symbols": []}})
            proposal_hash = report_payload["proposal"]["proposal_hash"]
            write_json(target, live)
            write_json(report, report_payload)
            guard_files = write_guard_reports(
                root,
                proposal_hash,
                learning=strategy_learning(proposal_hash, approved=4, rejected=5, fail=1),
            )

            payload = build_promote_report(
                report,
                target,
                guard_files,
                apply=True,
                confirm_proposal_hash=proposal_hash,
            )

        codes = [row["code"] for row in payload["promotion_blockers"]]
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("strategy_learning_judgment_audit_gaps_block_watchlist_promotion", codes)
        self.assertIn("strategy_learning_audit_pass_sample_too_small_blocks_watchlist_promotion", codes)


if __name__ == "__main__":
    unittest.main()
