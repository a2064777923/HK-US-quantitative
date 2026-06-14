import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from scripts import rt_signal_engine_v5 as rt
from scripts import strategy_config_promote as promote


def proposal_payload(config):
    normalized, _ = rt.normalize_strategy_config(config)
    return {
        "schema": "rt_signal_strategy_config_proposal_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "manual_review_required": True,
            "auto_applied": False,
        },
        "proposal_hash": normalized["config_id"],
        "proposed_config": normalized,
    }


def blocked_proposal_payload(config):
    payload = proposal_payload(config)
    payload["promotion_blockers"] = [
        {
            "code": "simulation_performance_fail_blocks_strategy_promotion",
            "simulation_status": "FAIL",
            "remediation_hash": "feedface12345678",
        }
    ]
    payload["promotion"] = {
        "blocked": True,
        "promotion_blockers": payload["promotion_blockers"],
    }
    return payload


def readiness_blocked_proposal_payload(config):
    payload = proposal_payload(config)
    payload["promotion_blockers"] = [
        {
            "code": "execution_readiness_not_ready_blocks_strategy_promotion",
            "readiness_status": "BLOCKED",
            "blocking_gate_names": ["hermes_judgment_effect"],
        }
    ]
    payload["promotion"] = {
        "blocked": True,
        "promotion_blockers": payload["promotion_blockers"],
    }
    return payload


class StrategyConfigPromoteTests(unittest.TestCase):
    def test_dry_run_reports_changes_without_writing_target(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "strategy.json"
            proposal = Path(td) / "proposal.json"
            target.write_text(json.dumps(rt.default_strategy_config()), encoding="utf-8")
            proposed = rt.default_strategy_config()
            proposed["confirmation_thresholds"]["BUY"]["min_full_score"] = 0.55
            proposal.write_text(json.dumps(proposal_payload(proposed)), encoding="utf-8")

            payload = promote.build_report(str(proposal), str(target))

            stored = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["change_count"], 1)
        self.assertEqual(stored["confirmation_thresholds"]["BUY"]["min_full_score"], rt.BUY_CONFIRMATION_MIN_SCORE)

    def test_apply_requires_matching_hash(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "strategy.json"
            proposal = Path(td) / "proposal.json"
            target.write_text(json.dumps(rt.default_strategy_config()), encoding="utf-8")
            proposed = rt.default_strategy_config()
            proposed["volume_anomaly_ratio"] = 4.0
            proposal.write_text(json.dumps(proposal_payload(proposed)), encoding="utf-8")

            payload = promote.build_report(str(proposal), str(target), apply=True)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("confirm_proposal_hash_required", payload["validation_reasons"])

    def test_apply_writes_target_and_backup_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "strategy.json"
            proposal = Path(td) / "proposal.json"
            backup_dir = Path(td) / "backups"
            target.write_text(json.dumps(rt.default_strategy_config()), encoding="utf-8")
            proposed = rt.default_strategy_config()
            proposed["trigger_overrides"] = {"BUY:站上MA5": {"min_full_score": 0.45}}
            proposal_payload_obj = proposal_payload(proposed)
            proposal.write_text(json.dumps(proposal_payload_obj), encoding="utf-8")

            old_backup_dir = promote.BACKUP_DIR
            promote.BACKUP_DIR = str(backup_dir)
            try:
                payload = promote.build_report(
                    str(proposal),
                    str(target),
                    apply=True,
                    confirm_proposal_hash=proposal_payload_obj["proposal_hash"],
                )
            finally:
                promote.BACKUP_DIR = old_backup_dir

            stored = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "applied")
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["proposal_freshness"]["status"], "fresh")
        self.assertTrue(payload["safety"]["requires_fresh_proposal"])
        self.assertTrue(Path(payload["backup_file"]).exists())
        self.assertEqual(stored["trigger_overrides"]["BUY:站上MA5"]["min_full_score"], 0.45)

    def test_apply_is_blocked_when_proposal_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "strategy.json"
            proposal = Path(td) / "proposal.json"
            target.write_text(json.dumps(rt.default_strategy_config()), encoding="utf-8")
            proposed = rt.default_strategy_config()
            proposed["confirmation_thresholds"]["BUY"]["min_full_score"] = 0.55
            proposal_payload_obj = proposal_payload(proposed)
            proposal_payload_obj["generated_at"] = (
                datetime.now() - timedelta(minutes=120)
            ).isoformat(timespec="seconds")
            proposal.write_text(json.dumps(proposal_payload_obj), encoding="utf-8")

            payload = promote.build_report(
                str(proposal),
                str(target),
                apply=True,
                confirm_proposal_hash=proposal_payload_obj["proposal_hash"],
                max_proposal_age_minutes=90,
            )
            stored = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["proposal_freshness"]["status"], "stale")
        self.assertIn("proposal_stale", payload["validation_reasons"])
        self.assertEqual(stored["confirmation_thresholds"]["BUY"]["min_full_score"], rt.BUY_CONFIRMATION_MIN_SCORE)

    def test_apply_is_blocked_when_proposal_timestamp_missing(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "strategy.json"
            proposal = Path(td) / "proposal.json"
            target.write_text(json.dumps(rt.default_strategy_config()), encoding="utf-8")
            proposed = rt.default_strategy_config()
            proposed["confirmation_thresholds"]["BUY"]["min_full_score"] = 0.55
            proposal_payload_obj = proposal_payload(proposed)
            proposal_payload_obj.pop("generated_at")
            proposal.write_text(json.dumps(proposal_payload_obj), encoding="utf-8")

            payload = promote.build_report(
                str(proposal),
                str(target),
                apply=True,
                confirm_proposal_hash=proposal_payload_obj["proposal_hash"],
            )

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["proposal_freshness"]["status"], "missing_timestamp")
        self.assertIn("proposal_missing_timestamp", payload["validation_reasons"])

    def test_apply_is_blocked_when_proposal_has_promotion_blocker(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "strategy.json"
            proposal = Path(td) / "proposal.json"
            target.write_text(json.dumps(rt.default_strategy_config()), encoding="utf-8")
            proposed = rt.default_strategy_config()
            proposed["confirmation_thresholds"]["BUY"]["min_full_score"] = 0.55
            proposal_payload_obj = blocked_proposal_payload(proposed)
            proposal.write_text(json.dumps(proposal_payload_obj), encoding="utf-8")

            payload = promote.build_report(
                str(proposal),
                str(target),
                apply=True,
                confirm_proposal_hash=proposal_payload_obj["proposal_hash"],
            )
            stored = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["applied"])
        self.assertIn(
            "proposal_promotion_blocker:simulation_performance_fail_blocks_strategy_promotion",
            payload["validation_reasons"],
        )
        self.assertEqual(stored["confirmation_thresholds"]["BUY"]["min_full_score"], rt.BUY_CONFIRMATION_MIN_SCORE)

    def test_dry_run_reports_blocked_status_for_promotion_blocker(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "strategy.json"
            proposal = Path(td) / "proposal.json"
            target.write_text(json.dumps(rt.default_strategy_config()), encoding="utf-8")
            proposed = rt.default_strategy_config()
            proposed["confirmation_thresholds"]["BUY"]["min_full_score"] = 0.55
            proposal_payload_obj = blocked_proposal_payload(proposed)
            proposal.write_text(json.dumps(proposal_payload_obj), encoding="utf-8")

            payload = promote.build_report(str(proposal), str(target))

        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["applied"])

    def test_apply_is_blocked_when_readiness_promotion_blocker_present(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "strategy.json"
            proposal = Path(td) / "proposal.json"
            target.write_text(json.dumps(rt.default_strategy_config()), encoding="utf-8")
            proposed = rt.default_strategy_config()
            proposed["confirmation_thresholds"]["BUY"]["min_full_score"] = 0.55
            proposal_payload_obj = readiness_blocked_proposal_payload(proposed)
            proposal.write_text(json.dumps(proposal_payload_obj), encoding="utf-8")

            payload = promote.build_report(
                str(proposal),
                str(target),
                apply=True,
                confirm_proposal_hash=proposal_payload_obj["proposal_hash"],
            )

        self.assertEqual(payload["status"], "blocked")
        self.assertIn(
            "proposal_promotion_blocker:execution_readiness_not_ready_blocks_strategy_promotion",
            payload["validation_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
