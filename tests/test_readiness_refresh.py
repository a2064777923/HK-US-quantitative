import unittest

from scripts import readiness_refresh as refresh


class ReadinessRefreshTests(unittest.TestCase):
    def test_selected_steps_can_skip_network_producers(self):
        steps = refresh.selected_steps(skip_network_producers=True)
        names = [step["name"] for step in steps]

        self.assertNotIn("external_market_context_producer", names)
        self.assertNotIn("market_sentiment_producer", names)
        self.assertNotIn("intraday_kline_batch", names)
        self.assertNotIn("kline_daily_gap_repair", names)
        self.assertNotIn("kline_gap_alternate_provider_probe", names)
        self.assertNotIn("kline_gap_alternate_provider_repair_plan", names)
        self.assertIn("intraday_timeframe_quality", names)
        self.assertIn("intraday_market_session_overrides", names)
        self.assertIn("kline_source_granularity", names)
        self.assertIn("source_reliability", names)
        self.assertIn("execution_readiness", names)
        self.assertIn("hermes_review_packet_seed", names)
        self.assertIn("operator_action_queue", names)
        self.assertIn("hermes_review_packet", names)
        self.assertLess(names.index("rt_signal_outcome"), names.index("source_reliability"))
        self.assertLess(names.index("source_reliability"), names.index("execution_readiness"))
        self.assertLess(names.index("execution_readiness"), names.index("hermes_review_packet_seed"))
        self.assertLess(names.index("hermes_review_packet_seed"), names.index("operator_action_queue"))
        self.assertLess(names.index("operator_action_queue"), names.index("hermes_review_packet"))

    def test_default_steps_include_read_only_daily_gap_repair_plan(self):
        names = [step["name"] for step in refresh.selected_steps()]

        self.assertIn("source_reliability", names)
        self.assertIn("kline_source_granularity", names)
        self.assertIn("kline_daily_gap_repair", names)
        self.assertIn("universe_hygiene", names)
        self.assertIn("kline_gap_source_diagnostic", names)
        self.assertIn("kline_gap_alternate_provider_probe", names)
        self.assertIn("kline_gap_alternate_provider_repair_plan", names)
        self.assertIn("trusted_source_discovery", names)
        self.assertIn("trusted_source_preflight", names)
        self.assertIn("intraday_kline_batch", names)
        self.assertIn("intraday_timeframe_quality", names)
        self.assertIn("intraday_market_session_overrides", names)
        self.assertIn("rt_alert_event_store", names)
        self.assertIn("rt_signal_outcome_event_store", names)
        self.assertIn("hermes_judgment_event_store", names)
        self.assertIn("rt_order_intake_event_store", names)
        self.assertIn("operator_action_queue", names)
        self.assertIn("hermes_review_packet_seed", names)
        self.assertIn("simulation_postmortem_audit", names)
        self.assertIn("simulation_postmortem_note_draft", names)

    def test_source_reliability_runs_after_its_context_inputs(self):
        names = [step["name"] for step in refresh.selected_steps()]
        source_idx = names.index("source_reliability")

        for dependency in (
            "cron_audit",
            "data_health",
            "data_source_inventory",
            "kline_source_granularity",
            "external_market_context",
            "event_catalysts",
            "event_catalyst_signals",
            "market_sentiment",
            "fundamentals_context",
            "trusted_source_discovery",
            "trusted_source_preflight",
            "market_context",
            "intraday_context",
            "intraday_timeframe_quality",
            "intraday_market_session_overrides",
            "rt_signal_outcome",
        ):
            self.assertLess(names.index(dependency), source_idx, dependency)
        self.assertLess(names.index("intraday_kline_batch"), names.index("intraday_context"))
        self.assertLess(names.index("intraday_context"), names.index("intraday_timeframe_quality"))
        self.assertLess(names.index("intraday_timeframe_quality"), names.index("source_reliability"))
        self.assertLess(names.index("kline_daily_gap_repair"), names.index("rt_signal_outcome"))
        self.assertLess(names.index("kline_gap_source_diagnostic"), names.index("rt_signal_outcome"))
        self.assertLess(names.index("kline_gap_alternate_provider_probe"), names.index("rt_signal_outcome"))
        self.assertLess(names.index("kline_gap_alternate_provider_repair_plan"), names.index("rt_signal_outcome"))
        self.assertLess(names.index("fundamentals_context"), names.index("trusted_source_preflight"))
        self.assertLess(names.index("market_sentiment"), names.index("trusted_source_preflight"))
        self.assertLess(names.index("external_market_context"), names.index("trusted_source_preflight"))
        self.assertLess(names.index("trusted_source_discovery"), names.index("trusted_source_preflight"))
        self.assertLess(source_idx, names.index("execution_readiness"))
        self.assertLess(names.index("execution_readiness"), names.index("hermes_review_packet_seed"))
        self.assertLess(names.index("hermes_review_packet_seed"), names.index("operator_action_queue"))
        self.assertLess(names.index("operator_action_queue"), names.index("hermes_review_packet"))
        self.assertLess(source_idx, names.index("hermes_review_packet"))

    def test_strategy_learning_runs_after_judgment_audit(self):
        names = [step["name"] for step in refresh.selected_steps()]

        self.assertLess(names.index("hermes_judgment_audit"), names.index("strategy_learning"))
        self.assertLess(names.index("strategy_learning"), names.index("execution_readiness"))

    def test_simulation_postmortem_audit_runs_after_performance_before_operator_queue(self):
        names = [step["name"] for step in refresh.selected_steps()]

        self.assertLess(names.index("simulation_performance"), names.index("simulation_postmortem_audit"))
        self.assertLess(names.index("simulation_postmortem_audit"), names.index("simulation_postmortem_note_draft"))
        self.assertLess(names.index("simulation_postmortem_note_draft"), names.index("operator_action_queue"))
        self.assertLess(names.index("simulation_postmortem_audit"), names.index("execution_readiness"))
        self.assertLess(names.index("simulation_postmortem_audit"), names.index("operator_action_queue"))

    def test_full_refresh_external_context_uses_infohub_bridge(self):
        step = [step for step in refresh.selected_steps() if step["name"] == "external_market_context_producer"][0]

        self.assertIn("--include-infohub", step["cmd"])
        self.assertIn("--infohub-url", step["cmd"])
        self.assertIn("http://127.0.0.1:8899", step["cmd"])

    def test_selected_steps_accept_comma_delimited_only_list(self):
        steps = refresh.selected_steps(only=["data_health,rt_alert_event_store"])
        names = [step["name"] for step in steps]

        self.assertEqual(names, ["data_health", "rt_alert_event_store"])

    def test_dry_run_builds_read_only_plan_without_running(self):
        steps = refresh.selected_steps(only=["cron_audit", "execution_readiness"])

        payload = refresh.build_report(steps=steps, scripts_dir="/root", dry_run=True)

        self.assertEqual(payload["schema"], "readiness_refresh_report_v1")
        self.assertEqual(payload["status"], "DRY_RUN")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_crontab"])
        self.assertFalse(payload["source"]["uses_apply_flags"])
        self.assertFalse(payload["source"]["uses_execute_mode"])
        self.assertEqual(payload["summary"]["dry_run_count"], 2)
        self.assertIn("python", payload["steps"][0]["cmd"][0].lower())
        self.assertIn("cron_audit_report.py", payload["steps"][0]["cmd"][1])

    def test_failed_step_marks_report_failed(self):
        steps = [{"name": "bad", "cmd": ["missing_script.py"], "network": False}]

        payload = refresh.build_report(steps=steps, scripts_dir="/definitely/missing", timeout_seconds=1)

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["summary"]["failed_count"], 1)
        self.assertIn("inspect_failed_refresh_step:bad", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
