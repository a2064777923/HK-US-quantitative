import unittest

from scripts import operator_action_queue_report as report


def base_payloads():
    return {
        "readiness": {
            "schema": "execution_readiness_report_v1",
            "status": "BLOCKED",
            "ready_for_execute": False,
            "blocking_gates": [
                {"gate": "forward_outcome_evidence", "detail": "resolved outcomes 0 / required 5"},
                {"gate": "hermes_judgment_effect", "detail": "sample 0 / required 5"},
                {"gate": "simulation_portfolio_performance", "detail": "simulation return negative"},
                {"gate": "simulation_trade_review", "detail": "closed PnL negative"},
            ],
        },
        "cron_audit": {
            "schema": "cron_audit_report_v1",
            "status": "WARN",
            "missing_required_jobs": [
                {
                    "name": "data_source_inventory",
                    "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/data_source_inventory_report.py --output /tmp/data_source_inventory_report.json --text >> /tmp/data_source_inventory_report.log 2>&1",
                },
                {
                    "name": "kline_source_granularity",
                    "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/kline_source_granularity_report.py --output /tmp/kline_source_granularity_report.json --text >> /tmp/kline_source_granularity_report.log 2>&1",
                },
                {
                    "name": "intraday_timeframe_quality",
                    "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/intraday_timeframe_quality_report.py --output /tmp/intraday_timeframe_quality_report.json --text >> /tmp/intraday_timeframe_quality_report.log 2>&1",
                },
                {
                    "name": "operator_action_queue",
                    "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/operator_action_queue_report.py --output /tmp/operator_action_queue_report.json --text >> /tmp/operator_action_queue_report.log 2>&1",
                },
                {
                    "name": "rt_alert_bridge_notify",
                    "recommended_cron": "* * * * * RT_ALERT_REMOTE=local RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1",
                },
                {
                    "name": "simulation_postmortem_audit",
                    "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/simulation_postmortem_audit_report.py --output /tmp/simulation_postmortem_audit_report.json --text >> /tmp/simulation_postmortem_audit_report.log 2>&1",
                },
                {
                    "name": "simulation_postmortem_note_draft",
                    "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/simulation_postmortem_note_draft_report.py --output /tmp/simulation_postmortem_note_draft_report.json --text >> /tmp/simulation_postmortem_note_draft_report.log 2>&1",
                },
            ],
            "alert_delivery": {
                "schema": "alert_delivery_audit_v1",
                "status": "WARN",
                "warnings": ["rt_alert_bridge_notify_cron_missing"],
                "feishu_delivery_enabled": False,
                "feishu_config": {
                    "missing_keys": ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"],
                    "env_file_path": "/root/.quantmind_env",
                    "values_redacted": True,
                },
            },
            "installation_plan": {
                "schema": "read_only_cron_installation_plan_v1",
                "status": "operator_review_required",
                "proposal_hash": "abc123def4567890",
                "install_line_count": 7,
            },
        },
        "cron_promotion": {
            "schema": "read_only_cron_install_promotion_report_v1",
            "status": "dry_run",
            "proposal_hash": "abc123def4567890",
            "new_install_lines": [
                "*/10 * * * * /usr/bin/python3 /root/data_source_inventory_report.py --output /tmp/data_source_inventory_report.json --text >> /tmp/data_source_inventory_report.log 2>&1",
                "*/30 * * * * /usr/bin/python3 /root/kline_source_granularity_report.py --output /tmp/kline_source_granularity_report.json --text >> /tmp/kline_source_granularity_report.log 2>&1",
                "*/5 * * * * /usr/bin/python3 /root/intraday_timeframe_quality_report.py --output /tmp/intraday_timeframe_quality_report.json --text >> /tmp/intraday_timeframe_quality_report.log 2>&1",
                "*/5 * * * * /usr/bin/python3 /root/operator_action_queue_report.py --output /tmp/operator_action_queue_report.json --text >> /tmp/operator_action_queue_report.log 2>&1",
                "* * * * * RT_ALERT_REMOTE=local RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1",
                "*/30 * * * * /usr/bin/python3 /root/simulation_postmortem_audit_report.py --output /tmp/simulation_postmortem_audit_report.json --text >> /tmp/simulation_postmortem_audit_report.log 2>&1",
                "*/30 * * * * /usr/bin/python3 /root/simulation_postmortem_note_draft_report.py --output /tmp/simulation_postmortem_note_draft_report.json --text >> /tmp/simulation_postmortem_note_draft_report.log 2>&1",
            ],
            "applied": False,
        },
        "packet": {
            "schema": "hermes_signal_review_packet_v1",
            "packet_id": "packet-1",
            "alert_selection": {
                "review_alert_count": 20,
                "review_signal_ids": ["sig-old-1", "sig-old-2"],
            },
            "review_item_suppression": {
                "schema": "hermes_review_item_suppression_summary_v1",
                "status": "ALL_SELECTED_ALERTS_SUPPRESSED",
                "selected_alert_count": 20,
                "review_item_count": 0,
                "non_actionable_observation_count": 20,
                "reason_counts": [{"key": "alert_too_old", "count": 20}],
                "recommendations": ["wait_for_fresh_confirmed_alerts_or_run_packet_during_market_session"],
            },
            "non_actionable_observation_count": 20,
            "position_review": {
                "position_judgment_template_summary": {
                    "schema": "portfolio_position_judgment_template_summary_v1",
                    "template_count": 2,
                    "template_only": True,
                }
            },
        },
        "position_audit": {
            "schema": "hermes_position_judgment_audit_report_v1",
            "status": "WARN",
            "coverage": {
                "schema": "hermes_position_judgment_coverage_v1",
                "position_review_item_count": 3,
                "judged_review_count": 0,
                "unjudged_review_count": 3,
                "high_urgency_review_count": 2,
                "unjudged_high_urgency_review_count": 2,
                "unjudged_high_urgency_examples": [
                    {
                        "review_id": "simulation:8:00816:2026-06-12:exit_review",
                        "portfolio_id": 8,
                        "role": "simulation",
                        "symbol": "00816",
                        "urgency": "high",
                    }
                ],
            },
        },
        "source_reliability": {
            "schema": "source_reliability_report_v1",
            "status": "STALE",
            "components": [
                {
                    "name": "data_source_inventory",
                    "reliability_status": "DEGRADED",
                    "report_status": "DEGRADED",
                    "reasons": ["data_source_inventory_weaknesses"],
                    "coverage": {
                        "weakness_codes": ["kline_data_source_missing", "context_reports_missing"],
                        "context_file_status_counts": {"present": 10, "missing": 2},
                        "kline_source_counts": {"missing": 2, "tencent": 1000},
                    },
                    "summary": {"weakness_count": 2},
                },
                {
                    "name": "kline_source_granularity",
                    "reliability_status": "DEGRADED",
                    "report_status": "ACTION_REQUIRED",
                    "reasons": [
                        "kline_source_granularity_column_missing",
                        "kline_source_granularity_backfill_proposal_pending",
                    ],
                    "coverage": {
                        "source_granularity_column_exists": False,
                        "proposal_action_count": 2,
                        "estimated_backfill_row_count": 193240,
                        "proposal_hash": "granularity1234",
                    },
                },
                {
                    "name": "fundamentals_context",
                    "reliability_status": "STALE",
                    "report_status": "RISK",
                    "reasons": [
                        "report_stale",
                        "fundamentals_primary_provider_fetch_failed",
                        "fundamentals_partial_metric_coverage",
                    ],
                    "warnings": ["producer_warning:fetch_failed:00700:HTTP Error 401: Unauthorized"],
                    "summary": {"fallback_item_count": 80, "full_item_count": 0},
                },
                {
                    "name": "external_market_context",
                    "reliability_status": "DEGRADED",
                    "report_status": "RISK",
                    "reasons": ["external_context_only_public_fallback_sources"],
                    "summary": {"trusted_provider_item_count": 0, "fallback_rss_item_count": 70},
                },
                {
                    "name": "trusted_source_discovery",
                    "reliability_status": "DEGRADED",
                    "missing_capabilities": ["capital_flow_context", "full_fundamentals_context"],
                    "summary": {"configured_provider_count": 0},
                },
                {
                    "name": "intraday_kline_batch",
                    "reliability_status": "DEGRADED",
                    "reasons": ["intraday_kline_batch_unofficial_public_provider"],
                    "coverage": {"provider_contract": "unofficial_public_web_endpoint_unversioned_best_effort"},
                },
                {
                    "name": "intraday_timeframe_quality",
                    "reliability_status": "DEGRADED",
                    "report_status": "DEGRADED",
                    "reasons": [
                        "intraday_timeframe_coverage_limited",
                        "intraday_timeframe_conflicts",
                        "intraday_timeframe_snapshot_like_minute_rows",
                    ],
                    "coverage": {
                        "limited_timeframe_symbol_count": 2,
                        "conflict_symbol_count": 1,
                        "snapshot_like_symbol_count": 1,
                    },
                    "recommendations": ["do_not_raise_confidence_from_limited_30m_60m_coverage"],
                },
            ],
        },
        "trusted_source_discovery": {
            "schema": "trusted_source_discovery_report_v1",
            "status": "WARN",
            "summary": {
                "provider_count": 5,
                "configured_provider_count": 0,
                "reachable_provider_count": 1,
                "capability_count": 5,
                "missing_capability_count": 4,
            },
            "providers": [
                {
                    "provider": "wudao",
                    "status": "MISSING",
                    "configured": False,
                    "reachable": False,
                    "env": {
                        "present_env_keys": [],
                        "missing_env_keys": ["WUDAO_MCP_URL", "WUDAO_API_KEY"],
                        "secret_values_redacted": True,
                    },
                },
                {
                    "provider": "infohub",
                    "status": "DISCOVERED_ENDPOINT_ONLY",
                    "configured": False,
                    "reachable": True,
                    "env": {
                        "present_env_keys": [],
                        "missing_env_keys": ["EXTERNAL_CONTEXT_INFOHUB_URL"],
                        "secret_values_redacted": True,
                    },
                },
            ],
            "capabilities": [
                {
                    "capability": "trusted_event_context",
                    "status": "MISSING",
                    "candidate_providers": ["wudao", "broker", "official_macro"],
                    "configured_or_reachable_providers": [],
                    "ready_providers": [],
                },
                {
                    "capability": "infohub_public_context",
                    "status": "CONFIGURED_UNVERIFIED",
                    "candidate_providers": ["infohub"],
                    "configured_or_reachable_providers": ["infohub"],
                    "ready_providers": [],
                },
            ],
            "recommendations": ["configure_wudao_broker_or_official_event_source"],
        },
        "trusted_source_preflight": {
            "schema": "trusted_source_preflight_report_v1",
            "status": "WARN",
            "components": [
                {
                    "name": "external_market_context_inputs",
                    "status": "WARN",
                    "item_count": 71,
                    "trusted_item_count": 0,
                    "fallback_item_count": 71,
                    "reasons": ["external_context_only_public_fallback_sources"],
                    "recommendations": [
                        "wire_wudao_mcp_broker_or_official_macro_provider_before_claiming_trusted_event_awareness"
                    ],
                },
                {
                    "name": "fundamentals_context_inputs",
                    "status": "WARN",
                    "item_count": 80,
                    "trusted_item_count": 0,
                    "trusted_full_item_count": 0,
                    "fallback_item_count": 80,
                    "warnings": ["producer_warning:fetch_failed:00700:HTTP Error 401: Unauthorized"],
                    "reasons": ["trusted_full_fundamentals_count_below_minimum"],
                    "recommendations": ["wire_broker_vendor_or_official_fundamentals_provider"],
                },
            ],
            "recommendations": ["wire_broker_vendor_or_official_fundamentals_provider"],
            "ingest_workflow": {
                "external_context_dry_run": "/usr/bin/python3 /root/external_market_context_ingest.py --input-file <trusted_external_payload.json> --dry-run --text",
                "market_sentiment_dry_run": "/usr/bin/python3 /root/market_sentiment_ingest.py --input-file <trusted_sentiment_payload.json> --dry-run --text",
                "fundamentals_context_dry_run": "/usr/bin/python3 /root/fundamentals_context_ingest.py --input-file <trusted_fundamentals_payload.json> --dry-run --text",
                "post_ingest_refresh": [
                    "/usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text"
                ],
            },
        },
        "simulation_performance": {
            "schema": "simulation_performance_report_v1",
            "status": "FAIL",
            "reason_codes": ["simulation_total_return_not_positive"],
            "summary": {
                "portfolio_id": 8,
                "return_pct_vs_initial": -5.9,
                "closed_win_rate_pct": 14.29,
                "closed_pnl_hkd_est": -933.38,
            },
            "worst_closed_symbols": [
                {"symbol": "LI", "pnl_hkd_est": -265.33, "win_rate_pct": 0.0},
            ],
            "open_position_risk": [
                {"symbol": "00929", "priority": "high", "unrealized_pnl_pct": -36.4},
            ],
            "failure_postmortem": {
                "schema": "simulation_failure_postmortem_v1",
                "status": "ACTION_REQUIRED",
                "diagnostics": {"worst_symbol": "LI", "closed_win_rate_pct": 14.29},
                "hypotheses": [
                    {"id": "entry_filter_or_signal_quality_weak", "severity": "high"},
                ],
                "required_learning_record": {
                    "required_fields": ["symbol", "failure_category", "lesson"],
                    "promotion_gate": "manual and hash-confirmed",
                },
            },
            "remediation_plan": {"proposal_hash": "remediate12345678"},
        },
        "simulation_postmortem_audit": {
            "schema": "simulation_postmortem_audit_report_v1",
            "status": "WARN",
            "coverage": {
                "required_target_count": 2,
                "covered_target_count": 0,
                "missing_target_count": 2,
                "note_count": 0,
                "failed_note_count": 0,
            },
            "missing_required_targets": [
                {"target_id": "closed_trade:LI", "symbol": "LI", "target_type": "closed_trade"},
                {"target_id": "open_position:00929", "symbol": "00929", "target_type": "open_position"},
            ],
            "recommendations": ["write_simulation_postmortem_notes:2"],
            "note_contract": {
                "schema": "simulation_postmortem_note_contract_v1",
                "note_file": "/tmp/simulation_postmortem_notes.jsonl",
            },
        },
        "simulation_postmortem_note_draft": {
            "schema": "simulation_postmortem_note_draft_report_v1",
            "status": "ACTION_REQUIRED",
            "summary": {"draft_count": 2, "target_count": 2},
            "append_instructions": {
                "manual_only": True,
                "remove_draft_only_before_append": True,
                "must_replace_all_placeholders": True,
            },
            "drafts": [
                {
                    "schema": "simulation_trade_postmortem_note_v1",
                    "draft_only": True,
                    "symbol": "LI",
                    "target_type": "closed_trade",
                }
            ],
        },
        "outcome": {
            "schema": "rt_signal_outcome_report_v1",
            "status": "PENDING",
            "counts": {"evaluated_signal_count": 156},
            "intraday_signal_context_summary": {"coverage_pct": 59.62},
            "recommendations": ["outcome_sample_not_ready_keep_collecting_daily_klines"],
        },
    }


class OperatorActionQueueReportTests(unittest.TestCase):
    def test_build_report_prioritizes_safe_operator_actions(self):
        payload = report.build_report(base_payloads())
        actions = {item["id"]: item for item in payload["actions"]}

        self.assertEqual(payload["schema"], "operator_action_queue_report_v1")
        self.assertEqual(payload["status"], "ACTION_REQUIRED")
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["writes_judgments"])
        self.assertFalse(payload["source"]["changes_crontab"])
        self.assertIn("write_high_urgency_position_judgments", actions)
        self.assertIn("keep_simulation_execution_disabled_until_recovery", actions)
        self.assertIn("install_data_source_inventory_cron", actions)
        self.assertIn("install_kline_source_granularity_cron", actions)
        self.assertIn("install_intraday_timeframe_quality_cron", actions)
        self.assertIn("install_operator_action_queue_cron", actions)
        self.assertIn("install_simulation_postmortem_review_crons", actions)
        self.assertIn("install_rt_alert_bridge_notify_cron", actions)
        self.assertIn("refresh_stale_alert_review_packet", actions)
        self.assertIn("configure_trusted_fundamentals_provider", actions)
        self.assertIn("review_data_source_inventory_weaknesses", actions)
        self.assertIn("review_kline_source_granularity_proposal", actions)
        self.assertIn("review_intraday_timeframe_quality_limits", actions)
        self.assertIn("onboard_trusted_source_payloads", actions)
        self.assertIn("write_or_repair_simulation_postmortem_notes", actions)
        self.assertEqual(actions["write_high_urgency_position_judgments"]["priority"], "P0")
        self.assertTrue(actions["write_high_urgency_position_judgments"]["operator_effect"]["writes_judgments"])
        self.assertTrue(actions["write_high_urgency_position_judgments"]["operator_effect"]["advisory_only"])
        self.assertFalse(actions["write_high_urgency_position_judgments"]["operator_effect"]["submits_orders"])
        self.assertIn("template_summary", actions["write_high_urgency_position_judgments"]["evidence"])

    def test_stale_alert_packet_action_is_observation_only(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["refresh_stale_alert_review_packet"]

        self.assertEqual(item["priority"], "P1")
        self.assertEqual(item["category"], "operator_wiring")
        self.assertEqual(item["evidence"]["packet_id"], "packet-1")
        self.assertEqual(item["evidence"]["review_item_suppression"]["status"], "ALL_SELECTED_ALERTS_SUPPRESSED")
        self.assertEqual(item["evidence"]["review_item_suppression"]["reason_counts"][0]["key"], "alert_too_old")
        self.assertTrue(item["operator_effect"]["refreshes_reports"])
        self.assertFalse(item["operator_effect"]["restarts_services"])
        self.assertFalse(item["operator_effect"]["writes_judgments"])
        self.assertFalse(item["operator_effect"]["submits_orders"])
        self.assertIn("Do not write trade judgments", item["recommended_next_step"])

    def test_data_source_inventory_cron_action_is_hash_gated(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["install_data_source_inventory_cron"]

        self.assertIn("--confirm-proposal-hash abc123def4567890", item["operator_command"])
        self.assertEqual(len(item["evidence"]["install_lines"]), 1)
        self.assertIn("data_source_inventory_report.py", item["evidence"]["install_lines"][0])
        self.assertTrue(item["evidence"]["promotion_usable"])
        self.assertTrue(item["operator_effect"]["changes_crontab"])
        self.assertFalse(item["operator_effect"]["uses_execute_mode"])
        self.assertFalse(item["operator_effect"]["enables_alert_sim"])
        self.assertFalse(item["operator_effect"]["enables_legacy_sim"])

    def test_kline_source_granularity_cron_action_is_hash_gated(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["install_kline_source_granularity_cron"]

        self.assertIn("--confirm-proposal-hash abc123def4567890", item["operator_command"])
        self.assertEqual(len(item["evidence"]["install_lines"]), 1)
        self.assertIn("kline_source_granularity_report.py", item["evidence"]["install_lines"][0])
        self.assertTrue(item["evidence"]["promotion_usable"])
        self.assertTrue(item["operator_effect"]["changes_crontab"])
        self.assertFalse(item["operator_effect"]["uses_execute_mode"])
        self.assertFalse(item["operator_effect"]["enables_alert_sim"])
        self.assertFalse(item["operator_effect"]["enables_legacy_sim"])

    def test_intraday_timeframe_quality_cron_action_is_hash_gated(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["install_intraday_timeframe_quality_cron"]

        self.assertIn("--confirm-proposal-hash abc123def4567890", item["operator_command"])
        self.assertEqual(len(item["evidence"]["install_lines"]), 1)
        self.assertIn("intraday_timeframe_quality_report.py", item["evidence"]["install_lines"][0])
        self.assertTrue(item["evidence"]["promotion_usable"])
        self.assertTrue(item["operator_effect"]["changes_crontab"])
        self.assertFalse(item["operator_effect"]["uses_execute_mode"])
        self.assertFalse(item["operator_effect"]["enables_alert_sim"])
        self.assertFalse(item["operator_effect"]["enables_legacy_sim"])

    def test_notify_cron_action_is_hash_gated_and_non_execution(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["install_rt_alert_bridge_notify_cron"]

        self.assertIn("--confirm-proposal-hash abc123def4567890", item["operator_command"])
        self.assertEqual(len(item["evidence"]["install_lines"]), 1)
        self.assertIn("rt_alert_bridge.py", item["evidence"]["install_lines"][0])
        self.assertNotIn("operator_action_queue_report.py", item["evidence"]["install_lines"][0])
        self.assertTrue(item["operator_effect"]["changes_crontab"])
        self.assertTrue(item["operator_effect"]["backs_up_crontab_before_apply"])
        self.assertFalse(item["operator_effect"]["uses_execute_mode"])
        self.assertFalse(item["operator_effect"]["enables_alert_sim"])
        self.assertFalse(item["operator_effect"]["enables_legacy_sim"])
        self.assertFalse(item["operator_effect"]["sends_feishu"])

    def test_stale_cron_promotion_hash_does_not_offer_apply_command(self):
        payloads = base_payloads()
        payloads["cron_audit"]["installation_plan"]["proposal_hash"] = "newhash123456789"
        payload = report.build_report(payloads)
        item = {row["id"]: row for row in payload["actions"]}["install_operator_action_queue_cron"]

        self.assertNotIn("--apply", item["operator_command"])
        self.assertNotIn("--confirm-proposal-hash", item["operator_command"])
        self.assertEqual(item["evidence"]["proposal_hash"], "abc123def4567890")
        self.assertEqual(item["evidence"]["cron_audit_proposal_hash"], "newhash123456789")
        self.assertFalse(item["evidence"]["promotion_usable"])
        self.assertIn("cron_promotion_hash_mismatch", item["evidence"]["promotion_blockers"])
        self.assertIn("cron_promotion_report_stale_or_mismatched", item["blockers"])
        self.assertFalse(item["operator_effect"]["changes_crontab"])
        self.assertIn("Regenerate", item["recommended_next_step"])

    def test_operator_action_queue_cron_action_is_separate_and_hash_gated(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["install_operator_action_queue_cron"]

        self.assertIn("--confirm-proposal-hash abc123def4567890", item["operator_command"])
        self.assertEqual(len(item["evidence"]["install_lines"]), 1)
        self.assertIn("operator_action_queue_report.py", item["evidence"]["install_lines"][0])
        self.assertNotIn("rt_alert_bridge.py", item["evidence"]["install_lines"][0])
        self.assertTrue(item["operator_effect"]["changes_crontab"])
        self.assertTrue(item["operator_effect"]["backs_up_crontab_before_apply"])
        self.assertFalse(item["operator_effect"]["uses_execute_mode"])
        self.assertFalse(item["operator_effect"]["enables_alert_sim"])
        self.assertFalse(item["operator_effect"]["enables_legacy_sim"])

    def test_simulation_postmortem_cron_action_groups_audit_and_draft_jobs(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["install_simulation_postmortem_review_crons"]

        self.assertIn("--confirm-proposal-hash abc123def4567890", item["operator_command"])
        self.assertEqual(
            {row["name"] for row in item["evidence"]["missing_jobs"]},
            {"simulation_postmortem_audit", "simulation_postmortem_note_draft"},
        )
        self.assertEqual(len(item["evidence"]["install_lines"]), 2)
        install_text = "\n".join(item["evidence"]["install_lines"])
        self.assertIn("simulation_postmortem_audit_report.py", install_text)
        self.assertIn("simulation_postmortem_note_draft_report.py", install_text)
        self.assertNotIn("--apply", install_text)
        self.assertTrue(item["operator_effect"]["changes_crontab"])
        self.assertTrue(item["operator_effect"]["backs_up_crontab_before_apply"])
        self.assertFalse(item["operator_effect"]["uses_execute_mode"])
        self.assertFalse(item["operator_effect"]["enables_alert_sim"])
        self.assertFalse(item["operator_effect"]["enables_legacy_sim"])
        self.assertFalse(item["operator_effect"]["sends_feishu"])

    def test_trusted_source_onboarding_action_redacts_secrets_and_uses_dry_run(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["onboard_trusted_source_payloads"]

        self.assertEqual(item["priority"], "P1")
        self.assertIn("trusted_event_context", [
            row["capability"] for row in item["evidence"]["missing_or_unverified_capabilities"]
        ])
        providers = {row["provider"]: row for row in item["evidence"]["provider_env_requirements"]}
        self.assertEqual(providers["wudao"]["missing_env_keys"], ["WUDAO_MCP_URL", "WUDAO_API_KEY"])
        self.assertTrue(providers["wudao"]["secret_values_redacted"])
        self.assertIn("external_context_dry_run", item["evidence"]["dry_run_commands"])
        self.assertIn("--dry-run", item["evidence"]["dry_run_commands"]["external_context_dry_run"])
        self.assertTrue(item["operator_effect"]["changes_secret_file"])
        self.assertFalse(item["operator_effect"]["writes_ingest_files"])
        self.assertFalse(item["operator_effect"]["submits_orders"])
        self.assertFalse(item["operator_effect"]["changes_crontab"])
        self.assertFalse(item["operator_effect"]["prints_secret_values"])

    def test_data_source_inventory_action_is_review_only(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["review_data_source_inventory_weaknesses"]

        self.assertEqual(item["priority"], "P2")
        self.assertEqual(item["category"], "source_provider")
        self.assertEqual(item["evidence"]["coverage"]["weakness_codes"], ["kline_data_source_missing", "context_reports_missing"])
        self.assertFalse(item["operator_effect"]["submits_orders"])
        self.assertFalse(item["operator_effect"]["changes_strategy"])
        self.assertFalse(item["operator_effect"]["changes_portfolio"])
        self.assertFalse(item["operator_effect"]["changes_crontab"])
        self.assertIn("/tmp/data_source_inventory_report.json", item["recommended_next_step"])

    def test_kline_source_granularity_action_is_hash_gated_and_provenance_only(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["review_kline_source_granularity_proposal"]

        self.assertEqual(item["priority"], "P2")
        self.assertEqual(item["category"], "source_provider")
        self.assertIn("--confirm-proposal-hash granularity1234", item["operator_command"])
        self.assertTrue(item["operator_effect"]["writes_database"])
        self.assertTrue(item["operator_effect"]["changes_schema"])
        self.assertTrue(item["operator_effect"]["does_not_change_ohlcv_prices_or_volumes"])
        self.assertFalse(item["operator_effect"]["submits_orders"])
        self.assertFalse(item["operator_effect"]["changes_strategy"])
        self.assertFalse(item["operator_effect"]["changes_portfolio"])
        self.assertFalse(item["operator_effect"]["changes_crontab"])
        self.assertIn("source_granularity_provenance_review_required", item["blockers"])

    def test_intraday_timeframe_quality_action_is_review_only(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["review_intraday_timeframe_quality_limits"]

        self.assertEqual(item["priority"], "P2")
        self.assertEqual(item["category"], "source_provider")
        self.assertEqual(item["evidence"]["coverage"]["conflict_symbol_count"], 1)
        self.assertIn("do_not_raise_confidence_from_limited_30m_60m_coverage", item["evidence"]["recommendations"])
        self.assertFalse(item["operator_effect"]["submits_orders"])
        self.assertFalse(item["operator_effect"]["changes_strategy"])
        self.assertFalse(item["operator_effect"]["changes_portfolio"])
        self.assertFalse(item["operator_effect"]["changes_crontab"])
        self.assertIn("/tmp/intraday_timeframe_quality_report.json", item["recommended_next_step"])
        self.assertIn("intraday_timeframe_quality_review_required", item["blockers"])

    def test_simulation_postmortem_note_action_is_not_strategy_change(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["write_or_repair_simulation_postmortem_notes"]

        self.assertEqual(item["priority"], "P1")
        self.assertEqual(item["category"], "simulation_recovery")
        self.assertEqual(item["evidence"]["coverage"]["missing_target_count"], 2)
        self.assertEqual(item["evidence"]["missing_required_targets"][0]["target_id"], "closed_trade:LI")
        self.assertEqual(item["evidence"]["draft_report"]["schema"], "simulation_postmortem_note_draft_report_v1")
        self.assertEqual(item["evidence"]["draft_report"]["summary"]["draft_count"], 2)
        self.assertTrue(item["evidence"]["draft_report"]["sample_drafts"][0]["draft_only"])
        self.assertTrue(item["operator_effect"]["writes_postmortem_notes"])
        self.assertTrue(item["operator_effect"]["draft_helper_read_only"])
        self.assertFalse(item["operator_effect"]["writes_judgments"])
        self.assertFalse(item["operator_effect"]["submits_orders"])
        self.assertFalse(item["operator_effect"]["changes_strategy"])
        self.assertFalse(item["operator_effect"]["changes_portfolio"])
        self.assertIn("simulation_trade_postmortem_note_v1", item["recommended_next_step"])
        self.assertIn("remove draft_only", item["recommended_next_step"])

    def test_simulation_failure_action_includes_postmortem_context(self):
        payload = report.build_report(base_payloads())
        item = {row["id"]: row for row in payload["actions"]}["review_simulation_performance_failure"]

        self.assertEqual(item["priority"], "P0")
        self.assertEqual(item["evidence"]["summary"]["portfolio_id"], 8)
        self.assertEqual(item["evidence"]["worst_closed_symbols"][0]["symbol"], "LI")
        self.assertEqual(item["evidence"]["failure_postmortem"]["status"], "ACTION_REQUIRED")
        self.assertEqual(
            item["evidence"]["failure_postmortem"]["hypotheses"][0]["id"],
            "entry_filter_or_signal_quality_weak",
        )
        self.assertIn(
            "failure_category",
            item["evidence"]["failure_postmortem"]["required_learning_record"]["required_fields"],
        )
        self.assertEqual(item["evidence"]["remediation_proposal_hash"], "remediate12345678")
        self.assertFalse(item["operator_effect"]["submits_orders"])
        self.assertFalse(item["operator_effect"]["changes_strategy"])

    def test_no_actions_is_ok(self):
        payload = report.build_report(
            {
                "readiness": {"schema": "execution_readiness_report_v1", "status": "READY", "blocking_gates": []},
                "cron_audit": {"schema": "cron_audit_report_v1", "status": "OK", "missing_required_jobs": []},
                "cron_promotion": {},
                "packet": {"schema": "hermes_signal_review_packet_v1", "packet_id": "packet-ok"},
                "position_audit": {
                    "schema": "hermes_position_judgment_audit_report_v1",
                    "status": "OK",
                    "coverage": {"unjudged_high_urgency_review_count": 0},
                },
                "source_reliability": {
                    "schema": "source_reliability_report_v1",
                    "status": "OK",
                    "components": [],
                },
                "trusted_source_discovery": {
                    "schema": "trusted_source_discovery_report_v1",
                    "status": "OK",
                    "providers": [],
                    "capabilities": [],
                },
                "trusted_source_preflight": {
                    "schema": "trusted_source_preflight_report_v1",
                    "status": "OK",
                    "components": [],
                },
                "simulation_performance": {
                    "schema": "simulation_performance_report_v1",
                    "status": "OK",
                },
                "simulation_postmortem_audit": {
                    "schema": "simulation_postmortem_audit_report_v1",
                    "status": "OK",
                    "coverage": {"missing_target_count": 0, "failed_note_count": 0},
                },
                "outcome": {
                    "schema": "rt_signal_outcome_report_v1",
                    "status": "OK",
                    "counts": {"evaluated_signal_count": 10},
                },
            }
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["actions"], [])
        self.assertEqual(payload["summary"]["action_count"], 0)


if __name__ == "__main__":
    unittest.main()
