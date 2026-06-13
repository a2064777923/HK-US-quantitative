import unittest

from scripts import cron_audit_report as report


FULL_CRON = "\n".join(
    f"*/5 * * * * /usr/bin/python3 /root/{script} --output {output} --text"
    for script, output in [
        ("system_health_check.py", "/tmp/quantmind_system_health.json"),
        ("data_health_report.py", "/tmp/data_health_report.json"),
        ("data_source_inventory_report.py", "/tmp/data_source_inventory_report.json"),
        ("kline_source_granularity_report.py", "/tmp/kline_source_granularity_report.json"),
        ("intraday_kline_batch.py", "/tmp/intraday_kline_batch.json"),
        ("intraday_context_report.py", "/tmp/intraday_context_report.json"),
        ("intraday_timeframe_quality_report.py", "/tmp/intraday_timeframe_quality_report.json"),
        ("intraday_market_session_overrides_report.py", "/tmp/intraday_market_session_overrides_report.json"),
        ("source_reliability_report.py", "/tmp/source_reliability_report.json"),
        ("trusted_source_preflight.py", "/tmp/trusted_source_preflight_report.json"),
        ("trusted_source_discovery_report.py", "/tmp/trusted_source_discovery_report.json"),
        ("market_context_report.py", "/tmp/market_context_report.json"),
        ("portfolio_report.py", "/tmp/portfolio_report.json"),
        ("watchlist_diff_report.py", "/tmp/watchlist_diff_report.json"),
        ("alert_quality_report.py", "/tmp/rt_alert_quality_report.json"),
        ("rt_signal_outcome_report.py", "/tmp/rt_signal_outcome_report.json"),
        ("rt_alert_event_store.py", "/tmp/rt_alert_event_store_report.json"),
        ("rt_signal_outcome_event_store.py", "/tmp/rt_signal_outcome_event_store_report.json"),
        ("kline_daily_gap_repair.py", "/tmp/kline_daily_gap_repair.json"),
        ("market_index_context_producer.py", "/tmp/market_index_context_inputs.json"),
        ("universe_hygiene_report.py", "/tmp/universe_hygiene_report.json"),
        ("kline_gap_source_diagnostic_report.py", "/tmp/kline_gap_source_diagnostic_report.json"),
        ("kline_gap_alternate_provider_probe.py", "/tmp/kline_gap_alternate_provider_probe.json"),
        ("kline_gap_alternate_provider_repair_plan.py", "/tmp/kline_gap_alternate_provider_repair_plan.json"),
        ("strategy_learning_report.py", "/tmp/strategy_learning_report.json"),
        ("simulation_performance_report.py", "/tmp/simulation_performance_report.json"),
        ("simulation_postmortem_audit_report.py", "/tmp/simulation_postmortem_audit_report.json"),
        ("simulation_postmortem_note_draft_report.py", "/tmp/simulation_postmortem_note_draft_report.json"),
        ("execution_readiness_report.py", "/tmp/execution_readiness_report.json"),
        ("operator_action_queue_report.py", "/tmp/operator_action_queue_report.json"),
        ("hermes_judgment_audit_report.py", "/tmp/hermes_judgment_audit_report.json"),
        ("hermes_judgment_event_store.py", "/tmp/hermes_judgment_event_store_report.json"),
        ("rt_order_intake_event_store.py", "/tmp/rt_order_intake_event_store_report.json"),
        ("hermes_position_judgment_audit_report.py", "/tmp/hermes_position_judgment_audit_report.json"),
        ("hermes_review_packet.py", "/tmp/hermes_signal_review_packet.json"),
    ]
) + """
* * * * * RT_ALERT_REMOTE=local RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1
*/5 * * * * /usr/bin/python3 /root/external_market_context_producer.py --include-infohub --infohub-url http://127.0.0.1:8899 --output /tmp/external_market_context_inputs.json --text && /usr/bin/python3 /root/external_market_context_report.py --output /tmp/external_market_context_report.json --text
*/5 * * * * /usr/bin/python3 /root/event_catalyst_report.py --output /tmp/event_catalyst_report.json --text
*/5 * * * * /usr/bin/python3 /root/event_catalyst_signal_report.py --output /tmp/event_catalyst_signal_report.json --text
*/5 * * * * /usr/bin/python3 /root/market_sentiment_producer.py --output /tmp/market_sentiment_inputs.json --text && /usr/bin/python3 /root/market_sentiment_report.py --output /tmp/market_sentiment_report.json --text
"""


class CronAuditReportTests(unittest.TestCase):
    def test_ok_when_required_read_only_jobs_are_present(self):
        payload = report.build_report(FULL_CRON)

        self.assertEqual(payload["schema"], "cron_audit_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_crontab"])
        self.assertEqual(payload["summary"]["missing_required_job_count"], 0)
        self.assertEqual(payload["summary"]["dangerous_enabled_count"], 0)
        self.assertEqual(payload["installation_plan"]["status"], "not_required")
        self.assertFalse(payload["installation_plan"]["operator_contract"]["submits_orders"])
        self.assertTrue(payload["installation_plan"]["operator_contract"]["does_not_edit_crontab"])

    def test_missing_read_only_jobs_warn(self):
        payload = report.build_report("*/5 * * * * /usr/bin/python3 /root/data_health_report.py --output /tmp/data_health_report.json")

        self.assertEqual(payload["status"], "WARN")
        missing = {job["name"] for job in payload["missing_required_jobs"]}
        self.assertIn("hermes_review_packet", missing)
        self.assertIn("rt_signal_outcome", missing)
        self.assertIn("kline_gap_source_diagnostic", missing)
        self.assertIn("kline_gap_alternate_provider_probe", missing)
        self.assertIn("kline_gap_alternate_provider_repair_plan", missing)
        self.assertIn("source_reliability", missing)
        self.assertIn("data_source_inventory", missing)
        self.assertIn("kline_source_granularity", missing)
        self.assertIn("intraday_kline_batch", missing)
        self.assertIn("intraday_context", missing)
        self.assertIn("intraday_timeframe_quality", missing)
        self.assertIn("intraday_market_session_overrides", missing)
        self.assertIn("trusted_source_preflight", missing)
        self.assertIn("trusted_source_discovery", missing)
        self.assertIn("rt_alert_event_store", missing)
        self.assertIn("hermes_judgment_event_store", missing)
        self.assertIn("rt_order_intake_event_store", missing)
        self.assertIn("rt_signal_outcome_event_store", missing)
        self.assertIn("simulation_postmortem_audit", missing)
        self.assertIn("simulation_postmortem_note_draft", missing)
        self.assertIn("operator_action_queue", missing)
        self.assertIn("rt_alert_bridge_notify", missing)
        packet_job = [job for job in payload["missing_required_jobs"] if job["name"] == "hermes_review_packet"][0]
        self.assertIn("hermes_review_packet.py", packet_job["recommended_cron"])
        plan = payload["installation_plan"]
        self.assertEqual(plan["schema"], "read_only_cron_installation_plan_v1")
        self.assertEqual(plan["status"], "operator_review_required")
        self.assertEqual(len(plan["proposal_hash"]), 16)
        self.assertTrue(plan["manual_review_required"])
        self.assertFalse(plan["auto_applied"])
        self.assertGreater(plan["install_line_count"], 0)
        self.assertEqual(plan["rejected_line_count"], 0)
        self.assertFalse(plan["operator_contract"]["submits_orders"])
        self.assertFalse(plan["operator_contract"]["uses_execute_mode"])
        self.assertFalse(plan["operator_contract"]["uses_apply_flags"])
        self.assertTrue(plan["operator_contract"]["requires_operator_manual_install"])
        install_text = "\n".join(row["recommended_cron"] for row in plan["install_lines"])
        external_job = [row for row in plan["install_lines"] if row["name"] == "external_market_context"][0]
        intraday_job = [row for row in plan["install_lines"] if row["name"] == "intraday_kline_batch"][0]
        intraday_context_job = [row for row in plan["install_lines"] if row["name"] == "intraday_context"][0]
        intraday_quality_job = [
            row for row in plan["install_lines"] if row["name"] == "intraday_timeframe_quality"
        ][0]
        calendar_job = [
            row for row in plan["install_lines"] if row["name"] == "intraday_market_session_overrides"
        ][0]
        self.assertIn("--include-infohub", external_job["recommended_cron"])
        self.assertIn("--infohub-url http://127.0.0.1:8899", external_job["recommended_cron"])
        self.assertIn("intraday_kline_batch.py", intraday_job["recommended_cron"])
        self.assertIn("/tmp/intraday_kline_batch.json", intraday_job["recommended_cron"])
        self.assertIn("intraday_context_report.py", intraday_context_job["recommended_cron"])
        self.assertIn("/tmp/intraday_context_report.json", intraday_context_job["recommended_cron"])
        self.assertIn("intraday_timeframe_quality_report.py", intraday_quality_job["recommended_cron"])
        self.assertIn("/tmp/intraday_timeframe_quality_report.json", intraday_quality_job["recommended_cron"])
        self.assertIn("intraday_market_session_overrides_report.py", calendar_job["recommended_cron"])
        self.assertIn("/tmp/intraday_market_session_overrides_report.json", calendar_job["recommended_cron"])
        bridge_job = [row for row in plan["install_lines"] if row["name"] == "rt_alert_bridge_notify"][0]
        operator_queue_job = [row for row in plan["install_lines"] if row["name"] == "operator_action_queue"][0]
        self.assertIn("RT_ALERT_REMOTE=local", bridge_job["recommended_cron"])
        self.assertIn("RT_ALERT_EXECUTION_MODE=notify", bridge_job["recommended_cron"])
        self.assertIn("operator_action_queue_report.py", operator_queue_job["recommended_cron"])
        self.assertIn("/tmp/operator_action_queue_report.json", operator_queue_job["recommended_cron"])
        self.assertNotIn("alert-sim", install_text)
        self.assertNotIn("--mode execute", install_text)
        self.assertNotIn(" --apply", install_text)
        text = report.build_text_report(payload)
        self.assertIn("Installation plan:", text)
        self.assertIn("Recommended read-only cron for missing jobs:", text)
        self.assertIn("install_missing_read_only_cron_jobs_from_config_hermes_v5_crontab", payload["recommendations"])

    def test_external_context_without_infohub_is_missing_required_job(self):
        weak_external_cron = "\n".join(
            line
            for line in FULL_CRON.splitlines()
            if "external_market_context_producer.py" not in line
        ) + "\n*/5 * * * * /usr/bin/python3 /root/external_market_context_producer.py --output /tmp/external_market_context_inputs.json --text && /usr/bin/python3 /root/external_market_context_report.py --output /tmp/external_market_context_report.json --text"

        payload = report.build_report(weak_external_cron)

        self.assertEqual(payload["status"], "WARN")
        missing = {job["name"] for job in payload["missing_required_jobs"]}
        self.assertIn("external_market_context", missing)
        external_job = [
            row for row in payload["installation_plan"]["install_lines"] if row["name"] == "external_market_context"
        ][0]
        self.assertIn("--include-infohub", external_job["recommended_cron"])

    def test_bridge_notify_without_local_mode_is_missing_required_job(self):
        old_bridge_cron = "\n".join(
            line
            for line in FULL_CRON.splitlines()
            if "rt_alert_bridge.py" not in line
        ) + "\n* * * * * RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py"

        payload = report.build_report(old_bridge_cron)

        self.assertEqual(payload["status"], "WARN")
        missing = {job["name"] for job in payload["missing_required_jobs"]}
        self.assertIn("rt_alert_bridge_notify", missing)
        bridge_job = [
            row for row in payload["installation_plan"]["install_lines"] if row["name"] == "rt_alert_bridge_notify"
        ][0]
        self.assertIn("RT_ALERT_REMOTE=local", bridge_job["recommended_cron"])

    def test_alert_delivery_warns_when_feishu_enabled_without_credentials(self):
        feishu_cron = (
            FULL_CRON
            + "\n* * * * * RT_ALERT_REMOTE=local RT_ALERT_SEND_FEISHU=1 "
            "RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 "
            "/usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1\n"
        )

        payload = report.build_report(
            feishu_cron,
            env={},
            env_file_text="",
            sent_file_texts={"alert_sent": "[]", "position_review_sent": "[]"},
        )

        self.assertEqual(payload["status"], "WARN")
        delivery = payload["alert_delivery"]
        self.assertEqual(delivery["status"], "WARN")
        self.assertTrue(delivery["feishu_delivery_enabled"])
        self.assertEqual(
            delivery["feishu_config"]["missing_keys"],
            ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"],
        )
        self.assertIn("feishu_delivery_enabled_but_credentials_missing", delivery["warnings"])
        self.assertIn("feishu_delivery_enabled_without_explicit_env_source", delivery["warnings"])
        self.assertIn(
            "configure_feishu_env_before_enabling_rt_alert_send_feishu",
            payload["recommendations"],
        )

    def test_alert_delivery_accepts_feishu_env_file_source_without_exposing_values(self):
        base_without_bridge = "\n".join(
            line for line in FULL_CRON.splitlines() if "rt_alert_bridge.py" not in line
        )
        feishu_cron = (
            base_without_bridge
            + '\n* * * * * /bin/bash -lc "cd /root && [ -f /root/.quantmind_env ] && . /root/.quantmind_env; '
            'RT_ALERT_REMOTE=local RT_ALERT_SEND_FEISHU=1 RT_ALERT_EXECUTION_MODE=notify '
            'RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1"\n'
        )
        env_file = "\n".join(
            [
                'export FEISHU_APP_ID="app-id-secret-value"',
                "FEISHU_APP_SECRET='app-secret-value'",
                "FEISHU_CHAT_ID=chat-id-value",
            ]
        )

        payload = report.build_report(
            feishu_cron,
            env={},
            env_file_text=env_file,
            sent_file_texts={"alert_sent": "[]", "position_review_sent": "[]"},
        )

        self.assertEqual(payload["status"], "OK")
        delivery = payload["alert_delivery"]
        self.assertEqual(delivery["status"], "OK")
        self.assertTrue(delivery["bridge_notify_present"])
        self.assertTrue(delivery["bridge_notify_local_mode"])
        self.assertTrue(delivery["feishu_delivery_enabled"])
        self.assertEqual(delivery["feishu_config"]["missing_keys"], [])
        self.assertEqual(
            delivery["feishu_config"]["present_keys"],
            ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"],
        )
        self.assertTrue(delivery["feishu_config"]["values_redacted"])
        self.assertNotIn("app-secret-value", report.build_text_report(payload))

    def test_alert_delivery_warns_on_invalid_sent_state_json(self):
        payload = report.build_report(
            FULL_CRON,
            env={},
            env_file_text="",
            sent_file_texts={"alert_sent": "{broken", "position_review_sent": "[]"},
        )

        self.assertEqual(payload["status"], "WARN")
        delivery = payload["alert_delivery"]
        self.assertEqual(delivery["sent_state"]["status"], "WARN")
        self.assertIn("alert_delivery_sent_state_invalid", delivery["warnings"])
        self.assertIn("repair_or_rotate_invalid_rt_alert_sent_state_files", payload["recommendations"])

    def test_dangerous_execution_cron_fails(self):
        payload = report.build_report(
            FULL_CRON + "\n* * * * * RT_ALERT_EXECUTION_MODE=alert-sim /usr/bin/python3 /root/rt_alert_bridge.py\n"
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["summary"]["dangerous_enabled_count"], 1)
        self.assertEqual(payload["dangerous_enabled_jobs"][0]["pattern"], "RT_ALERT_EXECUTION_MODE=alert-sim")
        self.assertIn("disable_dangerous_execution_cron_before_any_review", payload["recommendations"])

    def test_commented_dangerous_cron_is_ignored(self):
        payload = report.build_report(
            FULL_CRON + "\n# * * * * * RT_ALERT_EXECUTION_MODE=alert-sim /usr/bin/python3 /root/rt_alert_bridge.py\n"
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["dangerous_enabled_count"], 0)

    def test_unsafe_recommended_cron_line_is_rejected_from_installation_plan(self):
        original = report.REQUIRED_READ_ONLY_JOBS
        try:
            report.REQUIRED_READ_ONLY_JOBS = [
                {
                    "name": "unsafe",
                    "tokens": ["unsafe.py"],
                    "why": "test unsafe line",
                    "recommended_cron": "* * * * * /usr/bin/python3 /root/rt_order_intake.py --mode execute",
                }
            ]
            payload = report.build_report("")
        finally:
            report.REQUIRED_READ_ONLY_JOBS = original

        self.assertEqual(payload["status"], "WARN")
        plan = payload["installation_plan"]
        self.assertEqual(plan["status"], "blocked_unsafe_recommended_lines")
        self.assertEqual(plan["install_line_count"], 0)
        self.assertEqual(plan["rejected_line_count"], 1)
        self.assertIn("--mode execute", plan["rejected_lines"][0]["unsafe_tokens"])


if __name__ == "__main__":
    unittest.main()
