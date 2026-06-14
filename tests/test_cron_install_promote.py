import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import cron_audit_report as audit
from scripts import cron_install_promote as promote


def audit_payload(crontab_text=""):
    return audit.build_report(crontab_text)


class CronInstallPromoteTests(unittest.TestCase):
    def test_dry_run_reports_missing_read_only_lines_without_writing_crontab(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cron_audit.json"
            path.write_text(json.dumps(audit_payload("")), encoding="utf-8")

            payload = promote.build_report(
                cron_audit_file=str(path),
                current_crontab_text="* * * * * /usr/bin/python3 /root/existing.py\n",
            )

        self.assertEqual(payload["schema"], "read_only_cron_install_promotion_report_v1")
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertGreater(payload["new_install_line_count"], 0)
        self.assertTrue(payload["safety"]["dry_run_by_default"])
        self.assertTrue(payload["safety"]["requires_confirm_proposal_hash"])
        self.assertTrue(payload["safety"]["rejects_execute_mode"])
        self.assertTrue(payload["safety"]["rejects_apply_flags"])
        self.assertTrue(payload["safety"]["rejects_alert_sim"])
        self.assertTrue(payload["safety"]["does_not_submit_orders"])
        install_text = "\n".join(payload["new_install_lines"])
        self.assertNotIn("--mode execute", install_text)
        self.assertNotIn(" --apply", install_text)
        self.assertNotIn("alert-sim", install_text)

    def test_apply_requires_matching_hash(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cron_audit.json"
            path.write_text(json.dumps(audit_payload("")), encoding="utf-8")

            payload = promote.build_report(
                cron_audit_file=str(path),
                apply=True,
                current_crontab_text="",
            )

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["applied"])
        self.assertIn("confirm_proposal_hash_required", payload["validation_reasons"])

    def test_apply_installs_only_after_hash_and_backup(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cron_audit.json"
            cron_payload = audit_payload("")
            path.write_text(json.dumps(cron_payload), encoding="utf-8")
            calls = {}

            def fake_install(text):
                calls["crontab"] = text
                return {"status": "installed", "returncode": 0, "stdout": "", "stderr": ""}

            with mock.patch.object(promote, "install_crontab", side_effect=fake_install), mock.patch.object(
                promote,
                "backup_crontab",
                return_value="/tmp/crontab.bak",
            ):
                payload = promote.build_report(
                    cron_audit_file=str(path),
                    apply=True,
                    confirm_proposal_hash=cron_payload["installation_plan"]["proposal_hash"],
                    current_crontab_text="# current\n",
                )

        self.assertEqual(payload["status"], "applied")
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["backup_file"], "/tmp/crontab.bak")
        self.assertIn("Hermes v5 read-only evidence jobs", calls["crontab"])
        self.assertIn("simulation_performance_report.py", calls["crontab"])
        self.assertIn("--include-infohub", calls["crontab"])

    def test_unsafe_plan_is_blocked(self):
        unsafe_payload = audit.build_report("")
        unsafe_payload["installation_plan"]["install_lines"] = [
            {
                "name": "unsafe",
                "why": "test",
                "recommended_cron": "* * * * * /usr/bin/python3 /root/rt_order_intake.py --mode execute",
            }
        ]
        unsafe_payload["installation_plan"]["install_line_count"] = 1
        unsafe_payload["installation_plan"]["proposal_hash"] = audit.stable_hash(
            {"install_lines": unsafe_payload["installation_plan"]["install_lines"], "rejected_lines": []}
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cron_audit.json"
            path.write_text(json.dumps(unsafe_payload), encoding="utf-8")

            payload = promote.build_report(
                cron_audit_file=str(path),
                apply=True,
                confirm_proposal_hash=unsafe_payload["installation_plan"]["proposal_hash"],
                current_crontab_text="",
            )

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["applied"])
        self.assertTrue(payload["unsafe_lines"])
        self.assertTrue(any(reason.startswith("unsafe_install_line") for reason in payload["validation_reasons"]))

    def test_tampered_plan_hash_is_blocked(self):
        tampered_payload = audit.build_report("")
        tampered_payload["installation_plan"]["install_lines"].append(
            {
                "name": "extra",
                "why": "tampered after hash",
                "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/portfolio_report.py --output /tmp/portfolio_report.json --text >> /tmp/portfolio_report.log 2>&1",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cron_audit.json"
            path.write_text(json.dumps(tampered_payload), encoding="utf-8")

            payload = promote.build_report(
                cron_audit_file=str(path),
                apply=True,
                confirm_proposal_hash=tampered_payload["installation_plan"]["proposal_hash"],
                current_crontab_text="",
            )

        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["applied"])
        self.assertIn("installation_plan_hash_mismatch", payload["validation_reasons"])

    def test_not_required_plan_is_blocked(self):
        full_cron = "\n".join(row["recommended_cron"] for row in audit.REQUIRED_READ_ONLY_JOBS)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cron_audit.json"
            path.write_text(json.dumps(audit_payload(full_cron)), encoding="utf-8")

            payload = promote.build_report(cron_audit_file=str(path), current_crontab_text=full_cron)

        self.assertEqual(payload["status"], "blocked")
        self.assertIn("installation_plan_not_required", payload["validation_reasons"])


if __name__ == "__main__":
    unittest.main()
