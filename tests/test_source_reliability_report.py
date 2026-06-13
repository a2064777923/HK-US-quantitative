import unittest
from datetime import datetime, timedelta

from scripts import source_reliability_report as report


NOW = datetime(2026, 6, 13, 10, 0, 0)
FRESH = "2026-06-13T09:45:00"


def payload(schema, status="OK", **extra):
    item = {
        "schema": schema,
        "status": status,
        "generated_at": FRESH,
        "source": {"read_only": True, "submits_orders": False},
        "summary": {},
        "warnings": [],
        "recommendations": [],
    }
    item.update(extra)
    return item


def ok_payloads():
    return {
        "data_source_inventory": payload(
            "data_source_inventory_report_v1",
            summary={
                "table_status_counts": {"present": 7},
                "context_file_status_counts": {"present": 12},
                "present_input_payload_file_count": 4,
                "kline_source_counts": {"tencent": 1000},
                "weakness_count": 0,
                "error_weakness_count": 0,
                "warning_weakness_count": 0,
            },
            weaknesses=[],
        ),
        "data_health": payload("data_health_report_v1"),
        "kline_source_granularity": payload(
            "kline_source_granularity_report_v1",
            summary={
                "source_granularity_column_exists": True,
                "proposal_action_count": 0,
                "estimated_backfill_row_count": 0,
                "unmapped_missing_granularity_group_count": 0,
            },
            proposal={
                "schema": "kline_source_granularity_proposal_v1",
                "proposal_hash": "clean123",
                "action_count": 0,
                "actions": [],
            },
        ),
        "market_context": payload("market_context_report_v1"),
        "intraday_kline_batch": payload(
            "intraday_kline_batch_report_v1",
            status="APPLIED",
            source={
                "dry_run_default": True,
                "provider": "tencent_minute_query",
                "provider_contract": "vendor_feed_verified",
                "submits_orders": False,
                "changes_strategy": False,
                "changes_alert_queue": False,
                "changes_crontab": False,
                "repairs_daily_klines": False,
            },
            summary={
                "requested_symbol_count": 4,
                "action_count": 0,
                "planned_row_count": 0,
                "unresolved_count": 0,
                "sparse_us_action_count": 0,
                "invalid_source_row_count": 0,
            },
        ),
        "intraday_context": payload(
            "intraday_context_report_v1",
            summary={
                "market_count": 2,
                "symbol_count": 4,
                "ok_symbol_count": 4,
                "stale_symbol_count": 0,
                "missing_symbol_count": 0,
            },
        ),
        "intraday_timeframe_quality": payload(
            "intraday_timeframe_quality_report_v1",
            summary={
                "market_count": 2,
                "symbol_count": 4,
                "degraded_symbol_count": 0,
                "missing_symbol_count": 0,
                "limited_timeframe_symbol_count": 0,
                "missing_timeframe_symbol_count": 0,
                "conflict_symbol_count": 0,
                "low_fidelity_symbol_count": 0,
                "snapshot_like_symbol_count": 0,
                "missing_source_granularity_symbol_count": 0,
                "closed_symbol_count": 0,
                "stale_symbol_count": 0,
                "timeframes": {
                    "5m": {"ok_symbol_count": 4, "limited_symbol_count": 0, "missing_symbol_count": 0},
                    "15m": {"ok_symbol_count": 4, "limited_symbol_count": 0, "missing_symbol_count": 0},
                    "30m": {"ok_symbol_count": 4, "limited_symbol_count": 0, "missing_symbol_count": 0},
                    "60m": {"ok_symbol_count": 4, "limited_symbol_count": 0, "missing_symbol_count": 0},
                },
            },
            source={
                "read_only": True,
                "input_file": "/tmp/intraday_context_report.json",
                "queries_database": False,
                "submits_orders": False,
                "writes_database": False,
                "changes_strategy": False,
                "changes_crontab": False,
            },
        ),
        "intraday_market_session_overrides": payload(
            "intraday_market_session_overrides_report_v1",
            summary={
                "market_count": 2,
                "ok_market_count": 2,
                "warning_market_count": 0,
                "failed_market_count": 0,
                "warning_count": 0,
                "error_count": 0,
            },
        ),
        "external_market_context": payload("external_market_context_report_v1"),
        "event_catalysts": payload("event_catalyst_report_v1"),
        "event_catalyst_signals": payload("event_catalyst_signal_report_v1"),
        "market_sentiment": payload("market_sentiment_report_v1"),
        "fundamentals_context": payload("fundamentals_context_report_v1"),
        "trusted_source_preflight": payload(
            "trusted_source_preflight_report_v1",
            summary={"failed_component_count": 0, "warning_or_missing_component_count": 0},
        ),
        "trusted_source_discovery": payload(
            "trusted_source_discovery_report_v1",
            summary={"missing_capability_count": 0},
            capabilities=[
                {
                    "capability": "trusted_event_context",
                    "status": "READY_TO_VALIDATE_PAYLOAD",
                    "configured_or_reachable_providers": ["wudao"],
                },
                {
                    "capability": "capital_flow_context",
                    "status": "READY_TO_VALIDATE_PAYLOAD",
                    "configured_or_reachable_providers": ["broker"],
                },
                {
                    "capability": "market_sentiment_context",
                    "status": "READY_TO_VALIDATE_PAYLOAD",
                    "configured_or_reachable_providers": ["broker"],
                },
                {
                    "capability": "full_fundamentals_context",
                    "status": "READY_TO_VALIDATE_PAYLOAD",
                    "configured_or_reachable_providers": ["fundamentals_vendor"],
                },
            ],
        ),
        "cron_audit": payload(
            "cron_audit_report_v1",
            summary={"missing_required_job_count": 0, "dangerous_enabled_count": 0},
        ),
        "rt_signal_outcome": payload(
            "rt_signal_outcome_report_v1",
            status="OK",
            summary={"resolved_signal_count": 8},
            primary_horizon=1,
            primary_horizon_metric={
                "horizon_days": 1,
                "resolved_count": 8,
                "effective_unresolved_first_hit_rate_pct": 0.0,
                "effective_first_hit_counts": {"intraday_target": 4, "intraday_stop": 2, "none": 2},
            },
            intraday_sequence_summary={
                "schema": "intraday_sequence_summary_v1",
                "ambiguous_daily_count": 2,
                "resolved_count": 2,
                "missing_count": 0,
                "ambiguous_count": 0,
                "unresolved_count": 0,
                "low_fidelity_count": 0,
                "status_counts": {"RESOLVED": 2},
                "first_hit_counts": {"intraday_target": 1, "intraday_stop": 1},
            },
        ),
    }


class SourceReliabilityReportTests(unittest.TestCase):
    def test_all_sources_ok(self):
        result = report.build_report(ok_payloads(), now=NOW)

        self.assertEqual(result["schema"], "source_reliability_report_v1")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["summary"]["degraded_or_worse_count"], 0)
        self.assertTrue(result["source"]["read_only"])
        self.assertFalse(result["source"]["submits_orders"])

    def test_partial_fundamentals_degrade_source_reliability(self):
        inputs = ok_payloads()
        inputs["fundamentals_context"] = payload(
            "fundamentals_context_report_v1",
            status="RISK",
            summary={
                "fresh_item_count": 80,
                "partial_item_count": 80,
                "fallback_item_count": 80,
                "producer_fetch_failed_count": 80,
                "by_source": {"tencent_quote_snapshot": 80},
            },
            warnings=["producer_warning:fetch_failed:00700:0700.HK:HTTP Error 401: Unauthorized"],
        )

        result = report.build_report(inputs, now=NOW)
        fundamentals = [row for row in result["components"] if row["name"] == "fundamentals_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(fundamentals["reliability_status"], "DEGRADED")
        self.assertIn("fundamentals_fallback_provider_used", fundamentals["reasons"])
        self.assertIn("fundamentals_partial_metric_coverage", fundamentals["reasons"])
        self.assertIn("fundamentals_primary_provider_fetch_failed", fundamentals["reasons"])
        self.assertIn(
            "fix_fundamentals_primary_provider_or_replace_with_broker_fundamentals",
            result["recommendations"],
        )

    def test_data_source_inventory_weakness_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["data_source_inventory"] = payload(
            "data_source_inventory_report_v1",
            status="DEGRADED",
            summary={
                "table_status_counts": {"present": 7},
                "context_file_status_counts": {"present": 10, "missing": 2},
                "present_input_payload_file_count": 1,
                "kline_source_counts": {"missing": 20, "tencent": 1000},
                "weakness_count": 2,
                "error_weakness_count": 0,
                "warning_weakness_count": 2,
            },
            weaknesses=[
                {"code": "kline_data_source_missing", "severity": "WARN"},
                {"code": "context_reports_missing", "severity": "WARN"},
            ],
            recommendations=["backfill_or_explain_kline_data_source_provenance"],
        )

        result = report.build_report(inputs, now=NOW)
        inventory = [row for row in result["components"] if row["name"] == "data_source_inventory"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(inventory["reliability_status"], "DEGRADED")
        self.assertIn("data_source_inventory_weaknesses", inventory["reasons"])
        self.assertEqual(inventory["coverage"]["weakness_codes"], ["context_reports_missing", "kline_data_source_missing"])
        self.assertIn(
            "review_data_source_inventory_weaknesses_before_hermes_review",
            result["recommendations"],
        )

    def test_data_source_inventory_error_fails_source_reliability(self):
        inputs = ok_payloads()
        inputs["data_source_inventory"] = payload(
            "data_source_inventory_report_v1",
            status="FAIL",
            summary={
                "table_status_counts": {"present": 5, "missing": 2},
                "context_file_status_counts": {"present": 12},
                "present_input_payload_file_count": 4,
                "kline_source_counts": {},
                "weakness_count": 1,
                "error_weakness_count": 1,
                "warning_weakness_count": 0,
            },
            weaknesses=[{"code": "critical_database_tables_missing", "severity": "ERROR"}],
        )

        result = report.build_report(inputs, now=NOW)
        inventory = [row for row in result["components"] if row["name"] == "data_source_inventory"][0]

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(inventory["reliability_status"], "FAIL")
        self.assertIn("data_source_inventory_errors", inventory["reasons"])
        self.assertIn(
            "fix_data_source_inventory_errors_before_claiming_data_visibility",
            result["recommendations"],
        )

    def test_kline_source_granularity_proposal_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["kline_source_granularity"] = payload(
            "kline_source_granularity_report_v1",
            status="ACTION_REQUIRED",
            summary={
                "source_granularity_column_exists": False,
                "proposal_action_count": 2,
                "estimated_backfill_row_count": 193240,
                "unmapped_missing_granularity_group_count": 0,
            },
            proposal={
                "schema": "kline_source_granularity_proposal_v1",
                "proposal_hash": "granularity1234",
                "action_count": 2,
                "apply_command": "/usr/bin/python3 /root/kline_source_granularity_report.py --apply --confirm-proposal-hash granularity1234 --output /tmp/kline_source_granularity_report.json --text",
            },
        )

        result = report.build_report(inputs, now=NOW)
        component = [row for row in result["components"] if row["name"] == "kline_source_granularity"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(component["reliability_status"], "DEGRADED")
        self.assertIn("kline_source_granularity_column_missing", component["reasons"])
        self.assertIn("kline_source_granularity_backfill_proposal_pending", component["reasons"])
        self.assertEqual(component["coverage"]["proposal_hash"], "granularity1234")
        self.assertIn(
            "review_hash_confirmed_source_granularity_backfill_before_full_intraday_claims",
            result["recommendations"],
        )

    def test_public_fallback_external_context_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["external_market_context"] = payload(
            "external_market_context_report_v1",
            status="OK",
            summary={
                "fresh_item_count": 10,
                "fallback_rss_item_count": 10,
                "trusted_provider_item_count": 0,
                "producer_fetch_failed_count": 1,
                "capital_flow_item_count": 0,
                "by_provider": {"rss": 10},
            },
            warnings=["producer_warning:fetch_failed:infohub_macro_global:timeout"],
        )

        result = report.build_report(inputs, now=NOW)
        external = [row for row in result["components"] if row["name"] == "external_market_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(external["reliability_status"], "DEGRADED")
        self.assertIn("external_context_only_public_fallback_sources", external["reasons"])
        self.assertIn("external_context_provider_fetch_failed", external["reasons"])
        self.assertIn("external_context_capital_flow_missing", external["reasons"])
        self.assertIn(
            "wire_structured_wudao_infohub_or_broker_context_before_claiming_full_event_awareness",
            result["recommendations"],
        )

    def test_intraday_context_stale_or_missing_symbols_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_context"] = payload(
            "intraday_context_report_v1",
            status="OK",
            summary={
                "market_count": 2,
                "symbol_count": 4,
                "ok_symbol_count": 2,
                "stale_symbol_count": 1,
                "missing_symbol_count": 1,
            },
            recommendations=["HK:refresh_intraday_context_before_trade_judgment"],
        )

        result = report.build_report(inputs, now=NOW)
        intraday = [row for row in result["components"] if row["name"] == "intraday_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(intraday["reliability_status"], "DEGRADED")
        self.assertIn("intraday_context_stale_symbols", intraday["reasons"])
        self.assertIn("intraday_context_missing_symbols", intraday["reasons"])
        self.assertEqual(intraday["coverage"]["stale_symbol_count"], 1)
        self.assertIn("refresh_intraday_context_before_trade_judgment", result["recommendations"])
        self.assertIn("wire_minute_kline_refresh_for_watchlist_symbols", result["recommendations"])

    def test_intraday_quality_degradation_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_context"] = payload(
            "intraday_context_report_v1",
            status="OK",
            summary={
                "market_count": 1,
                "symbol_count": 2,
                "ok_symbol_count": 2,
                "stale_symbol_count": 0,
                "missing_symbol_count": 0,
                "quality_degraded_symbol_count": 1,
                "large_gap_symbol_count": 1,
                "invalid_ohlc_symbol_count": 1,
                "bad_timestamp_symbol_count": 0,
                "duplicate_timestamp_symbol_count": 1,
            },
        )

        result = report.build_report(inputs, now=NOW)
        intraday = [row for row in result["components"] if row["name"] == "intraday_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(intraday["reliability_status"], "DEGRADED")
        self.assertEqual(intraday["coverage"]["quality_degraded_symbol_count"], 1)
        self.assertIn("intraday_context_quality_degraded_symbols", intraday["reasons"])
        self.assertIn("intraday_context_large_minute_gaps", intraday["reasons"])
        self.assertIn("intraday_context_invalid_minute_rows", intraday["reasons"])
        self.assertIn("review_intraday_quality_before_using_minute_path_evidence", result["recommendations"])
        self.assertIn("refresh_or_repair_minute_kline_gap_coverage", result["recommendations"])
        self.assertIn("fix_invalid_intraday_kline_rows_before_trusting_path_evidence", result["recommendations"])

    def test_intraday_context_low_fidelity_sources_degrade_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_context"] = payload(
            "intraday_context_report_v1",
            status="OK",
            summary={
                "market_count": 1,
                "symbol_count": 2,
                "ok_symbol_count": 2,
                "stale_symbol_count": 0,
                "missing_symbol_count": 0,
                "quality_degraded_symbol_count": 1,
                "missing_source_granularity_symbol_count": 1,
                "low_fidelity_source_symbol_count": 1,
                "snapshot_like_symbol_count": 1,
                "full_ohlc_symbol_count": 1,
            },
        )

        result = report.build_report(inputs, now=NOW)
        intraday = [row for row in result["components"] if row["name"] == "intraday_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(intraday["coverage"]["low_fidelity_source_symbol_count"], 1)
        self.assertEqual(intraday["coverage"]["snapshot_like_symbol_count"], 1)
        self.assertIn("intraday_context_source_granularity_missing", intraday["reasons"])
        self.assertIn("intraday_context_low_fidelity_minute_source", intraday["reasons"])
        self.assertIn("intraday_context_snapshot_like_minute_rows", intraday["reasons"])
        self.assertIn(
            "avoid_using_snapshot_like_minute_rows_as_full_ohlcv_intraday_evidence",
            result["recommendations"],
        )

    def test_intraday_timeframe_quality_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_timeframe_quality"] = payload(
            "intraday_timeframe_quality_report_v1",
            status="DEGRADED",
            summary={
                "market_count": 1,
                "symbol_count": 3,
                "degraded_symbol_count": 2,
                "missing_symbol_count": 1,
                "limited_timeframe_symbol_count": 2,
                "missing_timeframe_symbol_count": 1,
                "conflict_symbol_count": 1,
                "low_fidelity_symbol_count": 1,
                "snapshot_like_symbol_count": 1,
                "missing_source_granularity_symbol_count": 1,
                "closed_symbol_count": 1,
                "stale_symbol_count": 0,
                "timeframes": {
                    "30m": {"ok_symbol_count": 1, "limited_symbol_count": 2, "missing_symbol_count": 0},
                    "60m": {"ok_symbol_count": 1, "limited_symbol_count": 1, "missing_symbol_count": 1},
                },
            },
            source={
                "read_only": True,
                "input_file": "/tmp/intraday_context_report.json",
                "queries_database": False,
                "submits_orders": False,
                "writes_database": False,
                "changes_strategy": False,
                "changes_crontab": False,
            },
        )

        result = report.build_report(inputs, now=NOW)
        quality = [row for row in result["components"] if row["name"] == "intraday_timeframe_quality"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(quality["reliability_status"], "DEGRADED")
        self.assertEqual(quality["coverage"]["limited_timeframe_symbol_count"], 2)
        self.assertIn("intraday_timeframe_coverage_limited", quality["reasons"])
        self.assertIn("intraday_timeframe_coverage_missing", quality["reasons"])
        self.assertIn("intraday_timeframe_conflicts", quality["reasons"])
        self.assertIn("intraday_timeframe_low_fidelity_minute_source", quality["reasons"])
        self.assertIn("intraday_timeframe_snapshot_like_minute_rows", quality["reasons"])
        self.assertIn("intraday_timeframe_source_granularity_missing", quality["reasons"])
        self.assertIn("intraday_timeframe_market_closed", quality["reasons"])
        self.assertIn("do_not_raise_confidence_from_limited_30m_60m_coverage", result["recommendations"])
        self.assertIn("require_hermes_to_discuss_intraday_timeframe_conflicts", result["recommendations"])
        self.assertIn("avoid_using_snapshot_minute_timeframes_as_full_ohlcv_evidence", result["recommendations"])

    def test_intraday_kline_batch_actionable_plan_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_kline_batch"] = payload(
            "intraday_kline_batch_report_v1",
            status="ACTIONABLE",
            source={
                "dry_run_default": True,
                "provider": "tencent_minute_query",
                "provider_contract": "unofficial_public_web_endpoint_unversioned_best_effort",
                "submits_orders": False,
                "changes_strategy": False,
                "changes_alert_queue": False,
                "changes_crontab": False,
                "repairs_daily_klines": False,
            },
            summary={
                "requested_symbol_count": 3,
                "action_count": 2,
                "planned_row_count": 300,
                "unresolved_count": 1,
                "sparse_us_action_count": 1,
                "invalid_source_row_count": 1,
            },
        )

        result = report.build_report(inputs, now=NOW)
        intraday = [row for row in result["components"] if row["name"] == "intraday_kline_batch"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(intraday["reliability_status"], "DEGRADED")
        self.assertEqual(intraday["coverage"]["planned_row_count"], 300)
        self.assertIn("intraday_kline_batch_unofficial_public_provider", intraday["reasons"])
        self.assertIn("intraday_kline_batch_apply_pending", intraday["reasons"])
        self.assertIn("intraday_kline_batch_unresolved_symbols", intraday["reasons"])
        self.assertIn("intraday_kline_batch_sparse_us_rows", intraday["reasons"])
        self.assertIn("intraday_kline_batch_invalid_source_rows", intraday["reasons"])
        self.assertIn(
            "operator_review_hash_confirmed_intraday_minute_apply_before_claiming_collection",
            result["recommendations"],
        )

    def test_intraday_kline_batch_unsafe_contract_fails_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_kline_batch"] = payload(
            "intraday_kline_batch_report_v1",
            status="OK",
            source={
                "submits_orders": True,
                "changes_strategy": False,
                "changes_alert_queue": False,
                "changes_crontab": False,
                "repairs_daily_klines": False,
            },
            summary={"action_count": 0},
        )

        result = report.build_report(inputs, now=NOW)
        intraday = [row for row in result["components"] if row["name"] == "intraday_kline_batch"][0]

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(intraday["reliability_status"], "FAIL")
        self.assertIn("intraday_kline_batch_safety_contract_unsafe", intraday["reasons"])

    def test_outcome_low_fidelity_intraday_path_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["rt_signal_outcome"] = payload(
            "rt_signal_outcome_report_v1",
            status="OK",
            primary_horizon=1,
            primary_horizon_metric={
                "horizon_days": 1,
                "resolved_count": 8,
                "effective_unresolved_first_hit_rate_pct": 12.5,
                "effective_first_hit_counts": {
                    "intraday_target": 3,
                    "intraday_stop": 3,
                    "ambiguous_intraday_low_fidelity": 1,
                    "none": 1,
                },
            },
            intraday_sequence_summary={
                "schema": "intraday_sequence_summary_v1",
                "ambiguous_daily_count": 4,
                "resolved_count": 2,
                "missing_count": 0,
                "ambiguous_count": 0,
                "unresolved_count": 0,
                "low_fidelity_count": 1,
                "status_counts": {"RESOLVED": 2, "LOW_FIDELITY": 1},
                "first_hit_counts": {"intraday_target": 1, "intraday_stop": 1, "none": 1},
            },
        )

        result = report.build_report(inputs, now=NOW)
        outcome = [row for row in result["components"] if row["name"] == "rt_signal_outcome"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(outcome["reliability_status"], "DEGRADED")
        self.assertEqual(outcome["coverage"]["low_fidelity_count"], 1)
        self.assertIn("outcome_intraday_path_low_fidelity", outcome["reasons"])
        self.assertIn(
            "collect_full_ohlcv_minute_path_evidence_before_claiming_intraday_path_resolution",
            result["recommendations"],
        )

    def test_outcome_missing_or_unresolved_intraday_path_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["rt_signal_outcome"] = payload(
            "rt_signal_outcome_report_v1",
            status="OK",
            primary_horizon=1,
            primary_horizon_metric={
                "horizon_days": 1,
                "resolved_count": 8,
                "effective_unresolved_first_hit_rate_pct": 37.5,
                "effective_first_hit_counts": {
                    "ambiguous_intraday_missing": 1,
                    "ambiguous_intraday_unresolved": 1,
                    "ambiguous_intraday_same_minute": 1,
                    "intraday_target": 3,
                    "intraday_stop": 2,
                },
            },
            intraday_sequence_summary={
                "schema": "intraday_sequence_summary_v1",
                "ambiguous_daily_count": 5,
                "resolved_count": 2,
                "missing_count": 1,
                "ambiguous_count": 1,
                "unresolved_count": 1,
                "low_fidelity_count": 0,
                "status_counts": {"RESOLVED": 2, "MISSING": 1, "AMBIGUOUS": 1, "UNRESOLVED": 1},
                "first_hit_counts": {"intraday_target": 1, "intraday_stop": 1, "none": 3},
            },
        )

        result = report.build_report(inputs, now=NOW)
        outcome = [row for row in result["components"] if row["name"] == "rt_signal_outcome"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertIn("outcome_intraday_path_missing_minute_rows", outcome["reasons"])
        self.assertIn("outcome_intraday_path_same_minute_ambiguous", outcome["reasons"])
        self.assertIn("outcome_intraday_path_unresolved", outcome["reasons"])
        self.assertIn("outcome_intraday_path_high_unresolved_rate", outcome["reasons"])
        self.assertIn(
            "increase_full_ohlcv_minute_coverage_before_using_first_hit_rates",
            result["recommendations"],
        )

    def test_intraday_context_market_closed_degrades_without_claiming_live_coverage(self):
        inputs = ok_payloads()
        inputs["intraday_context"] = payload(
            "intraday_context_report_v1",
            status="CLOSED",
            source={
                "read_only": True,
                "submits_orders": False,
                "market_session_overrides_file": "/root/intraday_market_sessions.json",
            },
            summary={
                "market_count": 1,
                "symbol_count": 2,
                "ok_symbol_count": 0,
                "closed_symbol_count": 2,
                "stale_symbol_count": 0,
                "missing_symbol_count": 0,
            },
        )

        result = report.build_report(inputs, now=NOW)
        intraday = [row for row in result["components"] if row["name"] == "intraday_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(intraday["reliability_status"], "DEGRADED")
        self.assertEqual(intraday["coverage"]["closed_symbol_count"], 2)
        self.assertEqual(
            intraday["coverage"]["market_session_overrides_file"],
            "/root/intraday_market_sessions.json",
        )
        self.assertIn("intraday_context_market_closed", intraday["reasons"])
        self.assertIn(
            "treat_intraday_context_as_last_session_only_until_market_reopens",
            result["recommendations"],
        )

    def test_intraday_session_override_warning_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_context"] = payload(
            "intraday_context_report_v1",
            status="OK",
            source={
                "read_only": True,
                "submits_orders": False,
                "market_session_overrides_file": "/root/intraday_market_sessions.json",
            },
            summary={
                "market_count": 1,
                "symbol_count": 2,
                "ok_symbol_count": 2,
                "closed_symbol_count": 0,
                "stale_symbol_count": 0,
                "missing_symbol_count": 0,
            },
            warnings=[
                "intraday_market_session_overrides_file_missing:/root/intraday_market_sessions.json"
            ],
        )

        result = report.build_report(inputs, now=NOW)
        intraday = [row for row in result["components"] if row["name"] == "intraday_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(intraday["reliability_status"], "DEGRADED")
        self.assertIn("intraday_market_session_overrides_unavailable", intraday["reasons"])
        self.assertIn(
            "configure_or_fix_intraday_market_session_overrides_for_holidays_and_half_days",
            result["recommendations"],
        )

    def test_intraday_market_session_override_report_warn_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_market_session_overrides"] = payload(
            "intraday_market_session_overrides_report_v1",
            status="WARN",
            summary={
                "market_count": 2,
                "ok_market_count": 1,
                "warning_market_count": 1,
                "failed_market_count": 0,
                "warning_count": 1,
                "error_count": 0,
            },
            warnings=["HK:future_override_coverage_lt_30d"],
        )

        result = report.build_report(inputs, now=NOW)
        calendar = [row for row in result["components"] if row["name"] == "intraday_market_session_overrides"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(calendar["reliability_status"], "DEGRADED")
        self.assertIn("intraday_market_session_overrides_incomplete", calendar["reasons"])
        self.assertIn(
            "review_intraday_market_session_override_coverage_for_holidays_and_half_days",
            result["recommendations"],
        )

    def test_intraday_market_session_override_report_fail_fails_source_reliability(self):
        inputs = ok_payloads()
        inputs["intraday_market_session_overrides"] = payload(
            "intraday_market_session_overrides_report_v1",
            status="FAIL",
            summary={
                "market_count": 2,
                "ok_market_count": 1,
                "warning_market_count": 0,
                "failed_market_count": 1,
                "warning_count": 0,
                "error_count": 1,
            },
            errors=["HK:half_days:2026-12-24:invalid_session_windows"],
        )

        result = report.build_report(inputs, now=NOW)
        calendar = [row for row in result["components"] if row["name"] == "intraday_market_session_overrides"][0]

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(calendar["reliability_status"], "FAIL")
        self.assertIn("intraday_market_session_overrides_invalid", calendar["reasons"])
        self.assertIn(
            "fix_intraday_market_session_override_schema_before_trusting_calendar",
            result["recommendations"],
        )

    def test_structured_external_context_with_capital_flow_does_not_degrade_source_reliability(self):
        inputs = ok_payloads()
        inputs["external_market_context"] = payload(
            "external_market_context_report_v1",
            status="OK",
            summary={
                "fresh_item_count": 6,
                "fallback_rss_item_count": 2,
                "trusted_provider_item_count": 4,
                "producer_fetch_failed_count": 0,
                "capital_flow_item_count": 2,
                "by_provider": {
                    "google_news_us_market": 2,
                    "wudao_mcp_flash_news": 2,
                    "capital_flow_snapshot": 2,
                },
            },
        )

        result = report.build_report(inputs, now=NOW)
        external = [row for row in result["components"] if row["name"] == "external_market_context"][0]

        self.assertEqual(external["reliability_status"], "OK")
        self.assertNotIn("external_context_only_public_fallback_sources", external["reasons"])
        self.assertNotIn("external_context_capital_flow_missing", external["reasons"])

    def test_positive_high_impact_public_fallback_external_context_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["external_market_context"] = payload(
            "external_market_context_report_v1",
            status="OK",
            summary={
                "fresh_item_count": 6,
                "fallback_rss_item_count": 1,
                "trusted_provider_item_count": 5,
                "producer_fetch_failed_count": 0,
                "capital_flow_item_count": 2,
                "fallback_positive_high_impact_count": 1,
                "unknown_positive_high_impact_count": 0,
                "by_provider": {
                    "google_news_us_market": 1,
                    "wudao_mcp_flash_news": 3,
                    "capital_flow_snapshot": 2,
                },
            },
        )

        result = report.build_report(inputs, now=NOW)
        external = [row for row in result["components"] if row["name"] == "external_market_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(external["reliability_status"], "DEGRADED")
        self.assertIn("external_context_positive_high_impact_public_fallback", external["reasons"])
        self.assertNotIn("external_context_only_public_fallback_sources", external["reasons"])
        self.assertIn(
            "positive_high_impact_public_fallback_requires_source_limit_acknowledgement",
            result["recommendations"],
        )

    def test_cron_missing_jobs_degrades_but_dangerous_cron_fails(self):
        inputs = ok_payloads()
        inputs["cron_audit"] = payload(
            "cron_audit_report_v1",
            status="WARN",
            summary={"missing_required_job_count": 3, "dangerous_enabled_count": 0},
        )
        degraded = report.build_report(inputs, now=NOW)
        cron = [row for row in degraded["components"] if row["name"] == "cron_audit"][0]
        self.assertEqual(degraded["status"], "DEGRADED")
        self.assertIn("required_read_only_cron_jobs_missing", cron["reasons"])

        inputs["cron_audit"] = payload(
            "cron_audit_report_v1",
            status="FAIL",
            summary={"missing_required_job_count": 0, "dangerous_enabled_count": 1},
        )
        failed = report.build_report(inputs, now=NOW)
        cron = [row for row in failed["components"] if row["name"] == "cron_audit"][0]
        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(cron["reliability_status"], "FAIL")
        self.assertIn("dangerous_execution_cron_enabled", cron["reasons"])

    def test_trusted_source_preflight_warning_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["trusted_source_preflight"] = payload(
            "trusted_source_preflight_report_v1",
            status="WARN",
            summary={"failed_component_count": 0, "warning_or_missing_component_count": 2},
            recommendations=["wire_wudao_mcp_broker_or_official_macro_provider_before_claiming_trusted_event_awareness"],
        )

        result = report.build_report(inputs, now=NOW)
        preflight = [row for row in result["components"] if row["name"] == "trusted_source_preflight"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(preflight["reliability_status"], "DEGRADED")
        self.assertIn("trusted_source_preflight_not_clean", preflight["reasons"])
        self.assertIn(
            "wire_trusted_wudao_broker_or_official_payloads_before_claiming_full_context_coverage",
            result["recommendations"],
        )

    def test_trusted_source_preflight_failure_fails_source_reliability(self):
        inputs = ok_payloads()
        inputs["trusted_source_preflight"] = payload(
            "trusted_source_preflight_report_v1",
            status="FAIL",
            summary={"failed_component_count": 1, "warning_or_missing_component_count": 1},
        )

        result = report.build_report(inputs, now=NOW)
        preflight = [row for row in result["components"] if row["name"] == "trusted_source_preflight"][0]

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(preflight["reliability_status"], "FAIL")
        self.assertIn("trusted_source_preflight_failed", preflight["reasons"])
        self.assertIn("fix_trusted_source_payload_schema_before_ingest", result["recommendations"])

    def test_trusted_source_discovery_missing_capabilities_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["trusted_source_discovery"] = payload(
            "trusted_source_discovery_report_v1",
            status="WARN",
            summary={"missing_capability_count": 3},
            capabilities=[
                {"capability": "trusted_event_context", "status": "MISSING"},
                {"capability": "capital_flow_context", "status": "MISSING"},
                {"capability": "market_sentiment_context", "status": "CONFIGURED_UNVERIFIED"},
                {"capability": "full_fundamentals_context", "status": "MISSING"},
            ],
            recommendations=[
                "configure_wudao_broker_or_official_event_source",
                "run_dry_run_export_and_trusted_source_preflight_for_configured_sources",
            ],
        )

        result = report.build_report(inputs, now=NOW)
        discovery = [row for row in result["components"] if row["name"] == "trusted_source_discovery"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(discovery["reliability_status"], "DEGRADED")
        self.assertIn("trusted_source_capabilities_missing", discovery["reasons"])
        self.assertIn("trusted_source_capabilities_configured_but_unverified", discovery["reasons"])
        self.assertEqual(
            discovery["missing_capabilities"],
            ["capital_flow_context", "full_fundamentals_context", "trusted_event_context"],
        )
        self.assertIn(
            "configure_wudao_broker_or_official_event_source",
            result["recommendations"],
        )

    def test_stale_report_is_stale(self):
        inputs = ok_payloads()
        inputs["market_sentiment"]["generated_at"] = (NOW - timedelta(hours=3)).isoformat(timespec="seconds")

        result = report.build_report(inputs, now=NOW, max_age_minutes=90)
        sentiment = [row for row in result["components"] if row["name"] == "market_sentiment"][0]

        self.assertEqual(result["status"], "STALE")
        self.assertEqual(sentiment["reliability_status"], "STALE")
        self.assertIn("report_stale", sentiment["reasons"])

    def test_missing_report_is_missing(self):
        inputs = ok_payloads()
        inputs["external_market_context"] = {}

        result = report.build_report(inputs, now=NOW)
        external = [row for row in result["components"] if row["name"] == "external_market_context"][0]

        self.assertEqual(result["status"], "MISSING")
        self.assertEqual(external["reliability_status"], "MISSING")
        self.assertIn("report_missing_or_unreadable", external["reasons"])

    def test_existing_report_without_status_is_unknown_but_not_missing(self):
        inputs = ok_payloads()
        inputs["market_context"] = {
            "schema": "market_context_report_v1",
            "generated_at": FRESH,
            "source": {"read_only": True, "submits_orders": False},
            "summary": {"market_count": 2},
        }

        result = report.build_report(inputs, now=NOW)
        market = [row for row in result["components"] if row["name"] == "market_context"][0]

        self.assertEqual(result["status"], "OK")
        self.assertEqual(market["report_status"], "UNKNOWN")
        self.assertEqual(market["reliability_status"], "OK")
        self.assertNotIn("report_missing_or_unreadable", market["reasons"])

    def test_market_context_missing_native_index_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["market_context"] = payload(
            "market_context_report_v1",
            markets={
                "HK": {
                    "regime": "mixed",
                    "native_index_context": {
                        "schema": "market_context_native_index_v1",
                        "status": "MISSING",
                        "alignment": "incomplete",
                    },
                },
                "US": {
                    "regime": "risk_on",
                    "native_index_context": {
                        "schema": "market_context_native_index_v1",
                        "status": "OK",
                        "alignment": "confirms_breadth",
                        "primary_index": {"symbol": "^GSPC", "provider_grade": "broker_or_official"},
                    },
                },
            },
        )

        result = report.build_report(inputs, now=NOW)
        market = [row for row in result["components"] if row["name"] == "market_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(market["reliability_status"], "DEGRADED")
        self.assertIn("market_context_native_index_missing_or_incomplete", market["reasons"])
        self.assertIn(
            "populate_native_hk_us_index_ohlcv_before_claiming_real_index_market_regime",
            result["recommendations"],
        )

    def test_market_context_native_index_conflict_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["market_context"] = payload(
            "market_context_report_v1",
            markets={
                "HK": {
                    "regime": "risk_off",
                    "native_index_context": {
                        "schema": "market_context_native_index_v1",
                        "status": "OK",
                        "alignment": "conflicts_with_breadth",
                        "primary_index": {"symbol": "^HSI", "provider_grade": "broker_or_official"},
                    },
                },
            },
        )

        result = report.build_report(inputs, now=NOW)
        market = [row for row in result["components"] if row["name"] == "market_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertIn("market_context_native_index_conflicts_with_breadth", market["reasons"])
        self.assertIn(
            "require_hermes_to_discuss_native_index_vs_stock_pool_breadth_conflict",
            result["recommendations"],
        )

    def test_market_context_public_fallback_native_index_degrades_source_reliability(self):
        inputs = ok_payloads()
        inputs["market_context"] = payload(
            "market_context_report_v1",
            markets={
                "US": {
                    "regime": "risk_on",
                    "native_index_context": {
                        "schema": "market_context_native_index_v1",
                        "status": "OK",
                        "alignment": "confirms_breadth",
                        "primary_index": {
                            "symbol": "^GSPC",
                            "source": "yahoo_chart_snapshot",
                            "provider_grade": "public_fallback",
                        },
                    },
                },
            },
        )

        result = report.build_report(inputs, now=NOW)
        market = [row for row in result["components"] if row["name"] == "market_context"][0]

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(market["reliability_status"], "DEGRADED")
        self.assertIn("market_context_native_index_public_fallback_only", market["reasons"])
        self.assertIn(
            "replace_public_index_snapshot_with_broker_vendor_or_official_index_feed",
            result["recommendations"],
        )


if __name__ == "__main__":
    unittest.main()
