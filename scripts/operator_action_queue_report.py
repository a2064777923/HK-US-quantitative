#!/usr/bin/env python3
"""Read-only operator/Hermes action queue across readiness reports."""
import argparse
import json
import os
from collections import Counter
from datetime import datetime


REPORT_FILE = os.environ.get("OPERATOR_ACTION_QUEUE_REPORT_FILE", "/tmp/operator_action_queue_report.json")
READINESS_FILE = os.environ.get("EXECUTION_READINESS_REPORT_FILE", "/tmp/execution_readiness_report.json")
CRON_AUDIT_FILE = os.environ.get("CRON_AUDIT_REPORT_FILE", "/tmp/cron_audit_report.json")
CRON_PROMOTION_FILE = os.environ.get(
    "CRON_INSTALL_PROMOTION_REPORT_FILE",
    "/tmp/cron_install_promotion_report.json",
)
PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
POSITION_AUDIT_FILE = os.environ.get(
    "HERMES_POSITION_JUDGMENT_AUDIT_FILE",
    "/tmp/hermes_position_judgment_audit_report.json",
)
SOURCE_RELIABILITY_FILE = os.environ.get("SOURCE_RELIABILITY_REPORT_FILE", "/tmp/source_reliability_report.json")
TRUSTED_SOURCE_DISCOVERY_FILE = os.environ.get(
    "TRUSTED_SOURCE_DISCOVERY_REPORT_FILE",
    "/tmp/trusted_source_discovery_report.json",
)
TRUSTED_SOURCE_PREFLIGHT_FILE = os.environ.get(
    "TRUSTED_SOURCE_PREFLIGHT_REPORT_FILE",
    "/tmp/trusted_source_preflight_report.json",
)
SIMULATION_PERFORMANCE_FILE = os.environ.get(
    "SIMULATION_PERFORMANCE_REPORT_FILE",
    "/tmp/simulation_performance_report.json",
)
SIMULATION_POSTMORTEM_AUDIT_FILE = os.environ.get(
    "SIMULATION_POSTMORTEM_AUDIT_REPORT_FILE",
    "/tmp/simulation_postmortem_audit_report.json",
)
SIMULATION_POSTMORTEM_NOTE_DRAFT_FILE = os.environ.get(
    "SIMULATION_POSTMORTEM_NOTE_DRAFT_REPORT_FILE",
    "/tmp/simulation_postmortem_note_draft_report.json",
)
OUTCOME_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
POSITION_JUDGMENT_FILE = os.environ.get("HERMES_POSITION_JUDGMENT_FILE", "/tmp/hermes_position_judgments.jsonl")
SIMULATION_POSTMORTEM_NOTE_FILE = os.environ.get(
    "SIMULATION_POSTMORTEM_NOTE_FILE",
    "/tmp/simulation_postmortem_notes.jsonl",
)


PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_json_file(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def save_json_atomic(path, payload):
    tmp = f"{path}.{os.getpid()}.{datetime.now().strftime('%Y%m%d%H%M%S%f')}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def safe_list(value):
    return value if isinstance(value, list) else []


def safe_dict(value):
    return value if isinstance(value, dict) else {}


def action(
    action_id,
    priority,
    category,
    title,
    detail,
    evidence=None,
    next_step=None,
    command=None,
    operator_effect=None,
    blockers=None,
):
    effect = {
        "report_only": True,
        "submits_orders": False,
        "writes_judgments": False,
        "changes_portfolio": False,
        "changes_strategy": False,
        "changes_crontab": False,
        "requires_manual_operator": True,
    }
    if operator_effect:
        effect.update(operator_effect)
    return {
        "id": action_id,
        "priority": priority,
        "category": category,
        "title": title,
        "detail": detail,
        "evidence": evidence or {},
        "recommended_next_step": next_step,
        "operator_command": command,
        "operator_effect": effect,
        "blockers": safe_list(blockers),
    }


def dedupe_actions(actions):
    by_id = {}
    for item in actions:
        if item.get("id") not in by_id:
            by_id[item.get("id")] = item
            continue
        existing = by_id[item.get("id")]
        if PRIORITY_RANK.get(item.get("priority"), 99) < PRIORITY_RANK.get(existing.get("priority"), 99):
            by_id[item.get("id")] = item
    return sorted(
        by_id.values(),
        key=lambda item: (PRIORITY_RANK.get(item.get("priority"), 99), item.get("category") or "", item.get("id") or ""),
    )


def cron_promotion_context(cron_audit, cron_promotion):
    plan = safe_dict(cron_audit.get("installation_plan"))
    audit_hash = plan.get("proposal_hash")
    promotion_hash = cron_promotion.get("proposal_hash")
    promotion_status = cron_promotion.get("status")
    new_lines = safe_list(cron_promotion.get("new_install_lines"))
    blockers = []
    if not audit_hash:
        blockers.append("cron_audit_installation_plan_hash_missing")
    if not promotion_hash:
        blockers.append("cron_promotion_report_missing_or_hash_missing")
    elif audit_hash and promotion_hash != audit_hash:
        blockers.append("cron_promotion_hash_mismatch")
    if promotion_status != "dry_run":
        blockers.append("cron_promotion_report_not_current_dry_run")
    if not new_lines:
        blockers.append("cron_promotion_new_install_lines_missing")
    usable = not blockers
    return {
        "audit_proposal_hash": audit_hash,
        "promotion_hash": promotion_hash,
        "promotion_status": promotion_status,
        "new_install_lines": new_lines,
        "promotion_usable": usable,
        "promotion_blockers": blockers,
    }


def cron_promotion_command(promotion_hash, promotion_status, install_lines, promotion_usable=False):
    if promotion_usable and promotion_hash and promotion_status == "dry_run" and install_lines:
        return (
            "/usr/bin/python3 /root/cron_install_promote.py "
            "--cron-audit-file /tmp/cron_audit_report.json "
            f"--apply --confirm-proposal-hash {promotion_hash} "
            "--output /tmp/cron_install_promotion_report.json --text"
        )
    return (
        "/usr/bin/python3 /root/cron_install_promote.py "
        "--cron-audit-file /tmp/cron_audit_report.json "
        "--output /tmp/cron_install_promotion_report.json --text"
    )


def missing_cron_job_map(cron_audit):
    rows = safe_list(cron_audit.get("missing_required_jobs"))
    return {row.get("name"): row for row in rows if isinstance(row, dict) and row.get("name")}


def matching_install_lines(job, new_lines):
    recommended = str(job.get("recommended_cron") or "").strip() if isinstance(job, dict) else ""
    if recommended:
        matches = [line for line in new_lines if str(line).strip() == recommended]
        return matches or [recommended]
    return list(new_lines)


def hash_gated_cron_effect(promotion_hash, promotion_status, install_lines, promotion_usable=False):
    return {
        "changes_crontab": bool(promotion_usable and promotion_hash and promotion_status == "dry_run" and install_lines),
        "backs_up_crontab_before_apply": True,
        "uses_execute_mode": False,
        "enables_alert_sim": False,
        "enables_legacy_sim": False,
        "sends_feishu": False,
    }


def cron_actions(cron_audit, cron_promotion):
    actions = []
    alert_delivery = cron_audit.get("alert_delivery") if isinstance(cron_audit.get("alert_delivery"), dict) else {}
    warnings = set(alert_delivery.get("warnings") or [])
    missing_jobs = missing_cron_job_map(cron_audit)
    missing = set(missing_jobs)
    promotion = cron_promotion_context(cron_audit, cron_promotion)
    promotion_hash = promotion["promotion_hash"]
    promotion_status = promotion["promotion_status"]
    new_lines = promotion["new_install_lines"]
    promotion_usable = promotion["promotion_usable"]
    cron_next_step = (
        "Review the dry-run promotion report, then apply only with the matching proposal hash if the read-only cron is desired."
        if promotion_usable
        else (
            "Regenerate /tmp/cron_install_promotion_report.json in dry-run mode for the current cron audit hash, "
            "then review and apply only with the matching proposal hash if the read-only cron is desired."
        )
    )
    promotion_evidence = {
        "promotion_status": promotion_status,
        "proposal_hash": promotion_hash,
        "cron_audit_proposal_hash": promotion["audit_proposal_hash"],
        "promotion_usable": promotion_usable,
        "promotion_blockers": promotion["promotion_blockers"],
    }
    if "data_source_inventory" in missing:
        install_lines = matching_install_lines(missing_jobs.get("data_source_inventory") or {}, new_lines)
        command = cron_promotion_command(promotion_hash, promotion_status, install_lines, promotion_usable)
        actions.append(
            action(
                "install_data_source_inventory_cron",
                "P1",
                "operator_wiring",
                "Install read-only data-source inventory cron",
                "Hermes data-source visibility is not self-refreshing while data_source_inventory cron is missing.",
                evidence={
                    "cron_audit_status": cron_audit.get("status"),
                    **promotion_evidence,
                    "missing_job": missing_jobs.get("data_source_inventory"),
                    "install_lines": install_lines,
                },
                next_step=cron_next_step,
                command=command,
                operator_effect=hash_gated_cron_effect(promotion_hash, promotion_status, install_lines, promotion_usable),
                blockers=[] if promotion_usable else ["cron_promotion_report_stale_or_mismatched"],
            )
        )
    if "kline_source_granularity" in missing:
        install_lines = matching_install_lines(missing_jobs.get("kline_source_granularity") or {}, new_lines)
        command = cron_promotion_command(promotion_hash, promotion_status, install_lines, promotion_usable)
        actions.append(
            action(
                "install_kline_source_granularity_cron",
                "P1",
                "operator_wiring",
                "Install read-only K-line source-granularity cron",
                "Hermes minute-data provenance proposals are not self-refreshing while kline_source_granularity cron is missing.",
                evidence={
                    "cron_audit_status": cron_audit.get("status"),
                    **promotion_evidence,
                    "missing_job": missing_jobs.get("kline_source_granularity"),
                    "install_lines": install_lines,
                },
                next_step=cron_next_step,
                command=command,
                operator_effect=hash_gated_cron_effect(promotion_hash, promotion_status, install_lines, promotion_usable),
                blockers=[] if promotion_usable else ["cron_promotion_report_stale_or_mismatched"],
            )
        )
    if "intraday_timeframe_quality" in missing:
        install_lines = matching_install_lines(missing_jobs.get("intraday_timeframe_quality") or {}, new_lines)
        command = cron_promotion_command(promotion_hash, promotion_status, install_lines, promotion_usable)
        actions.append(
            action(
                "install_intraday_timeframe_quality_cron",
                "P1",
                "operator_wiring",
                "Install read-only intraday timeframe-quality cron",
                "Hermes 5m/15m/30m/60m confirmation-quality checks are not self-refreshing while intraday_timeframe_quality cron is missing.",
                evidence={
                    "cron_audit_status": cron_audit.get("status"),
                    **promotion_evidence,
                    "missing_job": missing_jobs.get("intraday_timeframe_quality"),
                    "install_lines": install_lines,
                },
                next_step=cron_next_step,
                command=command,
                operator_effect=hash_gated_cron_effect(promotion_hash, promotion_status, install_lines, promotion_usable),
                blockers=[] if promotion_usable else ["cron_promotion_report_stale_or_mismatched"],
            )
        )
    if "operator_action_queue" in missing:
        install_lines = matching_install_lines(missing_jobs.get("operator_action_queue") or {}, new_lines)
        command = cron_promotion_command(promotion_hash, promotion_status, install_lines, promotion_usable)
        actions.append(
            action(
                "install_operator_action_queue_cron",
                "P1",
                "operator_wiring",
                "Install read-only operator action queue cron",
                "Hermes/operator remediation priorities are not self-refreshing while operator_action_queue cron is missing.",
                evidence={
                    "cron_audit_status": cron_audit.get("status"),
                    **promotion_evidence,
                    "missing_job": missing_jobs.get("operator_action_queue"),
                    "install_lines": install_lines,
                },
                next_step=cron_next_step,
                command=command,
                operator_effect=hash_gated_cron_effect(promotion_hash, promotion_status, install_lines, promotion_usable),
                blockers=[] if promotion_usable else ["cron_promotion_report_stale_or_mismatched"],
            )
        )
    postmortem_missing = [
        name
        for name in ("simulation_postmortem_audit", "simulation_postmortem_note_draft")
        if name in missing
    ]
    if postmortem_missing:
        install_lines = []
        for name in postmortem_missing:
            install_lines.extend(matching_install_lines(missing_jobs.get(name) or {}, new_lines))
        command = cron_promotion_command(promotion_hash, promotion_status, install_lines, promotion_usable)
        actions.append(
            action(
                "install_simulation_postmortem_review_crons",
                "P1",
                "operator_wiring",
                "Install read-only simulation postmortem review crons",
                (
                    "Simulation loss postmortem audit and note-draft reports are not self-refreshing while "
                    "their read-only cron jobs are missing."
                ),
                evidence={
                    "cron_audit_status": cron_audit.get("status"),
                    **promotion_evidence,
                    "missing_jobs": [missing_jobs.get(name) for name in postmortem_missing],
                    "install_lines": install_lines,
                },
                next_step=cron_next_step + " This does not write notes or change strategy.",
                command=command,
                operator_effect=hash_gated_cron_effect(promotion_hash, promotion_status, install_lines, promotion_usable),
                blockers=[] if promotion_usable else ["cron_promotion_report_stale_or_mismatched"],
            )
        )
    if "rt_alert_bridge_notify" in missing or "rt_alert_bridge_notify_cron_missing" in warnings:
        install_lines = matching_install_lines(missing_jobs.get("rt_alert_bridge_notify") or {}, new_lines)
        command = cron_promotion_command(promotion_hash, promotion_status, install_lines, promotion_usable)
        actions.append(
            action(
                "install_rt_alert_bridge_notify_cron",
                "P1",
                "operator_wiring",
                "Install local notify-only v5/Hermes bridge cron",
                "Hermes/operator notifications are not automated while rt_alert_bridge notify/local cron is missing.",
                evidence={
                    "cron_audit_status": cron_audit.get("status"),
                    "alert_delivery_status": alert_delivery.get("status"),
                    **promotion_evidence,
                    "missing_job": missing_jobs.get("rt_alert_bridge_notify"),
                    "install_lines": install_lines,
                },
                next_step=cron_next_step,
                command=command,
                operator_effect=hash_gated_cron_effect(promotion_hash, promotion_status, install_lines, promotion_usable),
                blockers=[] if promotion_usable else ["cron_promotion_report_stale_or_mismatched"],
            )
        )
    feishu = alert_delivery.get("feishu_config") if isinstance(alert_delivery.get("feishu_config"), dict) else {}
    if alert_delivery.get("feishu_delivery_enabled") is False and feishu.get("missing_keys"):
        actions.append(
            action(
                "configure_feishu_credentials_before_delivery",
                "P2",
                "operator_wiring",
                "Configure Feishu credentials before enabling RT_ALERT_SEND_FEISHU",
                "Notify cron can run locally without Feishu, but Feishu delivery needs env-backed app credentials.",
                evidence={
                    "missing_keys": feishu.get("missing_keys"),
                    "env_file_path": feishu.get("env_file_path"),
                    "values_redacted": feishu.get("values_redacted"),
                },
                next_step="Create or update /root/.quantmind_env with FEISHU_APP_ID, FEISHU_APP_SECRET, and FEISHU_CHAT_ID; only then enable RT_ALERT_SEND_FEISHU=1.",
                operator_effect={"changes_secret_file": True, "sends_feishu": False},
            )
        )
    return actions


def position_actions(position_audit, packet):
    actions = []
    coverage = position_audit.get("coverage") if isinstance(position_audit.get("coverage"), dict) else {}
    high_unjudged = int(coverage.get("unjudged_high_urgency_review_count") or 0)
    if high_unjudged:
        examples = safe_list(coverage.get("unjudged_high_urgency_examples"))
        packet_review = packet.get("position_review") if isinstance(packet.get("position_review"), dict) else {}
        template_summary = packet_review.get("position_judgment_template_summary") or {}
        actions.append(
            action(
                "write_high_urgency_position_judgments",
                "P0",
                "advisory_review",
                "Write advisory Hermes judgments for high-urgency position reviews",
                "High-risk holdings remain unreviewed until Hermes appends advisory-only position judgments.",
                evidence={
                    "position_judgment_audit_status": position_audit.get("status"),
                    "coverage": coverage,
                    "packet_id": packet.get("packet_id"),
                    "template_summary": template_summary,
                    "unjudged_examples": examples,
                },
                next_step=(
                    "Use position_review.items[].position_judgment_template from the latest packet, replace placeholders, "
                    f"append completed JSONL objects to {POSITION_JUDGMENT_FILE}, then rerun hermes_position_judgment_audit_report.py."
                ),
                operator_effect={"writes_judgments": True, "advisory_only": True, "submits_orders": False},
            )
        )
    return actions


def packet_actions(packet):
    suppression = packet.get("review_item_suppression") if isinstance(packet.get("review_item_suppression"), dict) else {}
    if not suppression:
        return []
    status = suppression.get("status")
    reason_counts = {
        row.get("key"): int(row.get("count") or 0)
        for row in safe_list(suppression.get("reason_counts"))
        if isinstance(row, dict) and row.get("key")
    }
    if status != "ALL_SELECTED_ALERTS_SUPPRESSED" or not reason_counts.get("alert_too_old"):
        return []
    return [
        action(
            "refresh_stale_alert_review_packet",
            "P1",
            "operator_wiring",
            "Refresh v5 alerts before expecting Hermes trade judgments",
            "The latest Hermes packet selected alerts, but all selected alerts were too old and were moved to observation-only rows.",
            evidence={
                "packet_id": packet.get("packet_id"),
                "alert_selection": safe_dict(packet.get("alert_selection")),
                "review_item_suppression": suppression,
                "non_actionable_observation_count": packet.get("non_actionable_observation_count"),
            },
            next_step=(
                "During the relevant HK/US market session, confirm rt_signal_engine_v5 is producing fresh confirmed alerts, "
                "then regenerate hermes_review_packet.py. Do not write trade judgments for alert_too_old observations."
            ),
            operator_effect={
                "refreshes_reports": True,
                "restarts_services": False,
                "writes_judgments": False,
                "submits_orders": False,
            },
        )
    ]


def readiness_actions(readiness):
    actions = []
    blocking = {gate.get("gate"): gate for gate in safe_list(readiness.get("blocking_gates"))}
    if any(key in blocking for key in ("simulation_portfolio_performance", "simulation_trade_review", "simulation_performance_attribution")):
        actions.append(
            action(
                "keep_simulation_execution_disabled_until_recovery",
                "P0",
                "simulation_recovery",
                "Keep alert-sim disabled and review simulation losses",
                "The simulation portfolio evidence contradicts adding automated exposure.",
                evidence={
                    "readiness_status": readiness.get("status"),
                    "blocking_gates": [
                        key
                        for key in (
                            "simulation_portfolio_performance",
                            "simulation_trade_review",
                            "simulation_performance_attribution",
                        )
                        if key in blocking
                    ],
                },
                next_step="Use simulation_performance_report and portfolio position reviews to diagnose losses before enabling any simulation execution bridge.",
            )
        )
    if "forward_outcome_evidence" in blocking:
        actions.append(
            action(
                "collect_forward_outcome_evidence",
                "P1",
                "evidence_collection",
                "Collect resolved v5 forward outcomes before treating strategy as proven",
                "Forward outcomes are below the minimum sample required for execute readiness.",
                evidence={"gate": blocking["forward_outcome_evidence"]},
                next_step="Keep rt_signal_outcome_report fresh and wait for enough post-signal daily K-lines; do not promote readiness based on pending rows.",
            )
        )
    if "hermes_judgment_effect" in blocking:
        actions.append(
            action(
                "collect_audit_pass_hermes_judgment_effect",
                "P1",
                "evidence_collection",
                "Collect audit-pass Hermes approval vs rejection evidence",
                "The LLM layer has not yet proven that approved/reduced decisions outperform rejected/held decisions.",
                evidence={"gate": blocking["hermes_judgment_effect"]},
                next_step="After Hermes writes trade judgments, keep judgment audit and strategy_learning_report fresh until both approved and rejected cohorts have resolved samples.",
            )
        )
    if "report_freshness" in blocking:
        actions.append(
            action(
                "refresh_stale_readiness_inputs",
                "P1",
                "operator_wiring",
                "Refresh stale readiness input reports",
                "Execution readiness is blocked by stale or missing report timestamps.",
                evidence={"gate": blocking["report_freshness"]},
                next_step="Run the read-only readiness refresh command embedded in the report_freshness gate, then regenerate execution_readiness_report.py.",
                operator_effect={"refreshes_reports": True},
            )
        )
    return actions


def source_reliability_actions(source_reliability):
    actions = []
    components = safe_list(source_reliability.get("components"))
    by_name = {row.get("name"): row for row in components if isinstance(row, dict)}
    inventory = by_name.get("data_source_inventory") or {}
    inventory_reasons = set(inventory.get("reasons") or [])
    if inventory_reasons & {"data_source_inventory_errors", "data_source_inventory_weaknesses"}:
        actions.append(
            action(
                "review_data_source_inventory_weaknesses",
                "P1" if "data_source_inventory_errors" in inventory_reasons else "P2",
                "source_provider",
                "Review data-source visibility inventory weaknesses",
                (
                    "The system cannot claim full data-source visibility while DB tables, K-line provenance, "
                    "context files, or provider payloads have inventory weaknesses."
                ),
                evidence={
                    "component_status": inventory.get("reliability_status"),
                    "report_status": inventory.get("report_status"),
                    "reasons": inventory.get("reasons"),
                    "coverage": inventory.get("coverage"),
                    "summary": inventory.get("summary"),
                },
                next_step=(
                    "Open /tmp/data_source_inventory_report.json, repair missing reports or provenance at the source, "
                    "then rerun data_source_inventory_report.py and source_reliability_report.py before Hermes relies on context."
                ),
                blockers=["data_source_visibility_or_provenance_review_required"],
            )
        )
    granularity = by_name.get("kline_source_granularity") or {}
    granularity_reasons = set(granularity.get("reasons") or [])
    if granularity_reasons & {
        "kline_source_granularity_report_failed",
        "kline_source_granularity_column_missing",
        "kline_source_granularity_backfill_proposal_pending",
        "kline_source_granularity_unmapped_sources",
    }:
        coverage = granularity.get("coverage") if isinstance(granularity.get("coverage"), dict) else {}
        proposal_hash = coverage.get("proposal_hash")
        command = (
            "/usr/bin/python3 /root/kline_source_granularity_report.py "
            f"--apply --confirm-proposal-hash {proposal_hash} "
            "--output /tmp/kline_source_granularity_report.json --text"
            if proposal_hash
            and granularity_reasons
            & {"kline_source_granularity_column_missing", "kline_source_granularity_backfill_proposal_pending"}
            else "/usr/bin/python3 /root/kline_source_granularity_report.py --output /tmp/kline_source_granularity_report.json --text"
        )
        actions.append(
            action(
                "review_kline_source_granularity_proposal",
                "P1" if "kline_source_granularity_report_failed" in granularity_reasons else "P2",
                "source_provider",
                "Review K-line source-granularity provenance proposal",
                (
                    "Minute K-line rows cannot support full path evidence until source_granularity is persisted "
                    "and snapshot-like public rows are labelled explicitly."
                ),
                evidence={
                    "component_status": granularity.get("reliability_status"),
                    "report_status": granularity.get("report_status"),
                    "reasons": granularity.get("reasons"),
                    "coverage": coverage,
                },
                next_step=(
                    "Open /tmp/kline_source_granularity_report.json, review the proposal SQL, then apply only "
                    "with the matching proposal hash if the provenance-only schema/backfill is approved. Rerun "
                    "data_source_inventory_report.py, intraday_context_report.py, source_reliability_report.py, "
                    "and hermes_review_packet.py afterwards."
                ),
                command=command,
                operator_effect={
                    "writes_database": bool(proposal_hash),
                    "changes_schema": "kline_source_granularity_column_missing" in granularity_reasons,
                    "does_not_change_ohlcv_prices_or_volumes": True,
                    "submits_orders": False,
                    "changes_strategy": False,
                    "changes_portfolio": False,
                    "changes_crontab": False,
                    "requires_confirm_proposal_hash": bool(proposal_hash),
                },
                blockers=["source_granularity_provenance_review_required"],
            )
        )
    fundamentals = by_name.get("fundamentals_context") or {}
    if fundamentals.get("reliability_status") in ("STALE", "DEGRADED", "FAIL"):
        reasons = set(fundamentals.get("reasons") or [])
        if reasons & {"report_stale", "fundamentals_primary_provider_fetch_failed", "fundamentals_partial_metric_coverage"}:
            actions.append(
                action(
                    "configure_trusted_fundamentals_provider",
                    "P1",
                    "source_provider",
                    "Configure broker/vendor/official fundamentals source",
                    "Fundamentals context is stale or partial; Hermes must not treat Tencent fallback metrics as full PE/PB/ROE/growth/leverage coverage.",
                    evidence={
                        "component_status": fundamentals.get("reliability_status"),
                        "report_status": fundamentals.get("report_status"),
                        "reasons": fundamentals.get("reasons"),
                        "summary": fundamentals.get("summary"),
                        "sample_warnings": safe_list(fundamentals.get("warnings"))[:5],
                    },
                    next_step="Add a trusted fundamentals payload/provider and rerun fundamentals_context_producer.py, fundamentals_context_report.py, trusted_source_preflight.py, and source_reliability_report.py.",
                    blockers=["external_credentials_or_provider_access_required"],
                )
            )
    external = by_name.get("external_market_context") or {}
    if "external_context_only_public_fallback_sources" in set(external.get("reasons") or []):
        actions.append(
            action(
                "wire_trusted_event_macro_source",
                "P1",
                "source_provider",
                "Wire trusted Wudao/InfoHub/broker event and macro source",
                "External context exists, but source reliability says it is public fallback only.",
                evidence={
                    "component_status": external.get("reliability_status"),
                    "report_status": external.get("report_status"),
                    "summary": external.get("summary"),
                },
                next_step="Provide structured Wudao, broker, official, or vendor payloads and rerun trusted_source_preflight before letting Hermes cite the context as trusted evidence.",
                blockers=["trusted_source_payload_required"],
            )
        )
    discovery = by_name.get("trusted_source_discovery") or {}
    missing_caps = safe_list(discovery.get("missing_capabilities"))
    if missing_caps:
        actions.append(
            action(
                "configure_missing_trusted_source_capabilities",
                "P2",
                "source_provider",
                "Configure missing trusted source capabilities",
                "Trusted source discovery still lacks one or more provider capabilities.",
                evidence={"missing_capabilities": missing_caps, "summary": discovery.get("summary")},
                next_step="Configure providers for the missing capabilities, then run trusted_source_discovery_report.py and trusted_source_preflight.py.",
                blockers=["external_provider_configuration_required"],
            )
        )
    intraday = by_name.get("intraday_kline_batch") or {}
    if "intraday_kline_batch_unofficial_public_provider" in set(intraday.get("reasons") or []):
        actions.append(
            action(
                "upgrade_intraday_minute_provider",
                "P2",
                "source_provider",
                "Upgrade minute K-line provider before using intraday path evidence as full OHLCV",
                "Current intraday producer is an unofficial public provider; it is acceptable as advisory context but not institutional-grade path evidence.",
                evidence={"component_status": intraday.get("reliability_status"), "coverage": intraday.get("coverage")},
                next_step="Add broker/vendor/official full-OHLCV minute source and persist source_granularity before using minute bars for path learning.",
                blockers=["broker_vendor_minute_feed_required"],
            )
        )
    timeframe_quality = by_name.get("intraday_timeframe_quality") or {}
    timeframe_reasons = set(timeframe_quality.get("reasons") or [])
    if timeframe_reasons & {
        "intraday_timeframe_coverage_limited",
        "intraday_timeframe_coverage_missing",
        "intraday_timeframe_conflicts",
        "intraday_timeframe_low_fidelity_minute_source",
        "intraday_timeframe_snapshot_like_minute_rows",
        "intraday_timeframe_source_granularity_missing",
        "intraday_timeframe_quality_degraded_symbols",
    }:
        actions.append(
            action(
                "review_intraday_timeframe_quality_limits",
                "P2",
                "source_provider",
                "Review intraday timeframe quality before using finer data as confirmation",
                (
                    "5m/15m/30m/60m evidence is useful for timing and contradiction checks, but the current quality "
                    "matrix says it should cap confidence rather than strengthen signals."
                ),
                evidence={
                    "component_status": timeframe_quality.get("reliability_status"),
                    "report_status": timeframe_quality.get("report_status"),
                    "reasons": timeframe_quality.get("reasons"),
                    "coverage": timeframe_quality.get("coverage"),
                    "recommendations": safe_list(timeframe_quality.get("recommendations")),
                },
                next_step=(
                    "Open /tmp/intraday_timeframe_quality_report.json and inspect limited/missing timeframes, conflicts, "
                    "and snapshot/low-fidelity source counts. Keep daily/readiness gates authoritative until coverage and provenance improve."
                ),
                blockers=["intraday_timeframe_quality_review_required"],
            )
        )
    return actions


def provider_env_brief(discovery):
    briefs = []
    for row in safe_list(discovery.get("providers")):
        if not isinstance(row, dict):
            continue
        env = safe_dict(row.get("env"))
        provider = row.get("provider")
        if not provider:
            continue
        briefs.append(
            {
                "provider": provider,
                "status": row.get("status"),
                "configured": bool(row.get("configured")),
                "reachable": bool(row.get("reachable")),
                "present_env_keys": safe_list(env.get("present_env_keys")),
                "missing_env_keys": safe_list(env.get("missing_env_keys")),
                "secret_values_redacted": env.get("secret_values_redacted") is True,
            }
        )
    return briefs


def capability_brief(discovery):
    rows = []
    for row in safe_list(discovery.get("capabilities")):
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        if status == "READY_TO_VALIDATE_PAYLOAD":
            continue
        rows.append(
            {
                "capability": row.get("capability"),
                "status": status,
                "candidate_providers": safe_list(row.get("candidate_providers")),
                "configured_or_reachable_providers": safe_list(row.get("configured_or_reachable_providers")),
                "ready_providers": safe_list(row.get("ready_providers")),
            }
        )
    return rows


def preflight_component_brief(preflight):
    rows = []
    for component in safe_list(preflight.get("components")):
        if not isinstance(component, dict):
            continue
        if component.get("status") == "OK":
            continue
        rows.append(
            {
                "name": component.get("name"),
                "status": component.get("status"),
                "reasons": safe_list(component.get("reasons")),
                "recommendations": safe_list(component.get("recommendations")),
                "warnings_count": len(safe_list(component.get("warnings"))),
                "item_count": component.get("item_count"),
                "trusted_item_count": component.get("trusted_item_count"),
                "trusted_full_item_count": component.get("trusted_full_item_count"),
                "fallback_item_count": component.get("fallback_item_count"),
            }
        )
    return rows


def trusted_source_onboarding_actions(discovery, preflight):
    discovery = safe_dict(discovery)
    preflight = safe_dict(preflight)
    if not discovery and not preflight:
        return []

    missing_or_unverified = capability_brief(discovery)
    preflight_issues = preflight_component_brief(preflight)
    discovery_status = discovery.get("status")
    preflight_status = preflight.get("status")
    if (
        discovery_status not in ("WARN", "MISSING")
        and preflight_status not in ("WARN", "FAIL", "MISSING")
        and not missing_or_unverified
        and not preflight_issues
    ):
        return []

    priority = "P1" if discovery_status in ("WARN", "MISSING") or preflight_status in ("WARN", "FAIL") else "P2"
    ingest_workflow = safe_dict(preflight.get("ingest_workflow"))
    dry_run_commands = {
        key: value
        for key, value in ingest_workflow.items()
        if isinstance(value, str) and key.endswith("_dry_run")
    }
    return [
        action(
            "onboard_trusted_source_payloads",
            priority,
            "source_provider",
            "Onboard trusted event, sentiment, flow, and fundamentals sources",
            (
                "Current event/macro/sentiment/fundamentals context is fallback or partial. "
                "Hermes needs configured trusted providers and preflight-passing payloads before treating this context as institutional-grade evidence."
            ),
            evidence={
                "discovery_status": discovery_status,
                "preflight_status": preflight_status,
                "missing_or_unverified_capabilities": missing_or_unverified,
                "provider_env_requirements": provider_env_brief(discovery),
                "preflight_issues": preflight_issues,
                "dry_run_commands": dry_run_commands,
                "post_ingest_refresh": safe_list(ingest_workflow.get("post_ingest_refresh")),
                "recommendations": sorted(
                    set(safe_list(discovery.get("recommendations")) + safe_list(preflight.get("recommendations")))
                ),
                "secret_values_redacted": True,
            },
            next_step=(
                "Configure the required provider env keys outside the repo, export trusted JSON payloads, run the dry-run ingest/preflight commands, "
                "then append only payloads that pass trusted_source_preflight.py and refresh source_reliability_report.py."
            ),
            operator_effect={
                "changes_secret_file": True,
                "writes_ingest_files": False,
                "prints_secret_values": False,
                "requires_external_provider": True,
            },
            blockers=["external_provider_configuration_required"],
        )
    ]


def simulation_actions(simulation_performance):
    if simulation_performance.get("status") != "FAIL":
        return []
    summary = safe_dict(simulation_performance.get("summary"))
    postmortem = safe_dict(simulation_performance.get("failure_postmortem"))
    remediation_plan = safe_dict(simulation_performance.get("remediation_plan"))
    return [
        action(
            "review_simulation_performance_failure",
            "P0",
            "simulation_recovery",
            "Review failed simulation performance before new exposure",
            "Simulation performance attribution is FAIL, so the system should not claim stable profitability.",
            evidence={
                "status": simulation_performance.get("status"),
                "reason_codes": simulation_performance.get("reason_codes"),
                "summary": summary,
                "worst_closed_symbols": safe_list(simulation_performance.get("worst_closed_symbols")),
                "open_position_risk": safe_list(simulation_performance.get("open_position_risk"))[:10],
                "failure_postmortem": {
                    "status": postmortem.get("status"),
                    "diagnostics": safe_dict(postmortem.get("diagnostics")),
                    "hypotheses": safe_list(postmortem.get("hypotheses")),
                    "required_learning_record": safe_dict(postmortem.get("required_learning_record")),
                },
                "remediation_proposal_hash": remediation_plan.get("proposal_hash"),
            },
            next_step=(
                "Keep alert-sim disabled; complete symbol-level postmortem notes for worst closed symbols and high-risk holdings, "
                "then wait for later simulation, outcome, readiness, and Hermes judgment-effect evidence before changing exposure."
            ),
        )
    ]


def simulation_postmortem_actions(simulation_postmortem_audit, note_draft_report=None):
    status = simulation_postmortem_audit.get("status")
    if status not in ("WARN", "FAIL"):
        return []
    coverage = safe_dict(simulation_postmortem_audit.get("coverage"))
    note_draft_report = safe_dict(note_draft_report)
    missing_count = int(coverage.get("missing_target_count") or 0)
    failed_count = int(coverage.get("failed_note_count") or 0)
    if not missing_count and not failed_count:
        return []
    priority = "P0" if status == "FAIL" else "P1"
    return [
        action(
            "write_or_repair_simulation_postmortem_notes",
            priority,
            "simulation_recovery",
            "Write or repair simulation postmortem notes before strategy changes",
            "Simulation loss-recovery review is incomplete until required closed-loss and high-risk open-position notes pass audit.",
            evidence={
                "audit_status": status,
                "coverage": coverage,
                "missing_required_targets": safe_list(simulation_postmortem_audit.get("missing_required_targets"))[:10],
                "failed_note_examples": safe_list(simulation_postmortem_audit.get("note_audits"))[:10],
                "recommendations": safe_list(simulation_postmortem_audit.get("recommendations")),
                "note_contract": safe_dict(simulation_postmortem_audit.get("note_contract")),
                "draft_report": {
                    "path": SIMULATION_POSTMORTEM_NOTE_DRAFT_FILE,
                    "schema": note_draft_report.get("schema"),
                    "status": note_draft_report.get("status"),
                    "summary": safe_dict(note_draft_report.get("summary")),
                    "append_instructions": safe_dict(note_draft_report.get("append_instructions")),
                    "sample_drafts": safe_list(note_draft_report.get("drafts"))[:3],
                },
            },
            next_step=(
                "Use simulation_postmortem_note_draft_report.py as a read-only draft helper, replace every placeholder, "
                f"remove draft_only, append completed simulation_trade_postmortem_note_v1 JSONL objects to {SIMULATION_POSTMORTEM_NOTE_FILE}, "
                "then rerun simulation_postmortem_audit_report.py. Do not promote strategy/watchlist/config changes from failing or missing notes."
            ),
            operator_effect={
                "writes_postmortem_notes": True,
                "draft_helper_read_only": True,
                "writes_judgments": False,
                "submits_orders": False,
                "changes_strategy": False,
                "changes_portfolio": False,
            },
        )
    ]


def outcome_actions(outcome):
    counts = outcome.get("counts") if isinstance(outcome.get("counts"), dict) else {}
    evaluated = int(counts.get("evaluated_signal_count") or 0)
    status = outcome.get("status")
    if status not in ("PENDING", "INSUFFICIENT") and evaluated:
        return []
    return [
        action(
            "wait_for_outcome_maturity",
            "P1",
            "evidence_collection",
            "Wait for v5 outcome maturity before promotion",
            "Recent v5 alerts do not yet have enough resolved forward-return evidence.",
            evidence={
                "status": status,
                "counts": counts,
                "intraday_signal_context_summary": outcome.get("intraday_signal_context_summary"),
                "recommendations": outcome.get("recommendations"),
            },
            next_step="Keep the outcome report fresh and avoid using pending same-day signals as profitability proof.",
        )
    ]


def build_report(payloads=None):
    payloads = dict(payloads or {})
    readiness = payloads.get("readiness") if isinstance(payloads.get("readiness"), dict) else load_json_file(READINESS_FILE)
    cron_audit = payloads.get("cron_audit") if isinstance(payloads.get("cron_audit"), dict) else load_json_file(CRON_AUDIT_FILE)
    cron_promotion = (
        payloads.get("cron_promotion")
        if isinstance(payloads.get("cron_promotion"), dict)
        else load_json_file(CRON_PROMOTION_FILE)
    )
    packet = payloads.get("packet") if isinstance(payloads.get("packet"), dict) else load_json_file(PACKET_FILE)
    position_audit = (
        payloads.get("position_audit")
        if isinstance(payloads.get("position_audit"), dict)
        else load_json_file(POSITION_AUDIT_FILE)
    )
    source_reliability = (
        payloads.get("source_reliability")
        if isinstance(payloads.get("source_reliability"), dict)
        else load_json_file(SOURCE_RELIABILITY_FILE)
    )
    trusted_source_discovery = (
        payloads.get("trusted_source_discovery")
        if isinstance(payloads.get("trusted_source_discovery"), dict)
        else load_json_file(TRUSTED_SOURCE_DISCOVERY_FILE)
    )
    trusted_source_preflight = (
        payloads.get("trusted_source_preflight")
        if isinstance(payloads.get("trusted_source_preflight"), dict)
        else load_json_file(TRUSTED_SOURCE_PREFLIGHT_FILE)
    )
    simulation_performance = (
        payloads.get("simulation_performance")
        if isinstance(payloads.get("simulation_performance"), dict)
        else load_json_file(SIMULATION_PERFORMANCE_FILE)
    )
    simulation_postmortem_audit = (
        payloads.get("simulation_postmortem_audit")
        if isinstance(payloads.get("simulation_postmortem_audit"), dict)
        else load_json_file(SIMULATION_POSTMORTEM_AUDIT_FILE)
    )
    simulation_postmortem_note_draft = (
        payloads.get("simulation_postmortem_note_draft")
        if isinstance(payloads.get("simulation_postmortem_note_draft"), dict)
        else load_json_file(SIMULATION_POSTMORTEM_NOTE_DRAFT_FILE)
    )
    outcome = payloads.get("outcome") if isinstance(payloads.get("outcome"), dict) else load_json_file(OUTCOME_FILE)

    actions = []
    actions.extend(cron_actions(cron_audit, cron_promotion))
    actions.extend(packet_actions(packet))
    actions.extend(position_actions(position_audit, packet))
    actions.extend(readiness_actions(readiness))
    actions.extend(source_reliability_actions(source_reliability))
    actions.extend(trusted_source_onboarding_actions(trusted_source_discovery, trusted_source_preflight))
    actions.extend(simulation_actions(simulation_performance))
    actions.extend(simulation_postmortem_actions(simulation_postmortem_audit, simulation_postmortem_note_draft))
    actions.extend(outcome_actions(outcome))
    actions = dedupe_actions(actions)

    counts = Counter(item["priority"] for item in actions)
    categories = Counter(item["category"] for item in actions)
    status = "OK"
    if counts.get("P0"):
        status = "ACTION_REQUIRED"
    elif counts.get("P1"):
        status = "REVIEW"
    elif counts.get("P2") or counts.get("P3"):
        status = "WATCH"

    return {
        "schema": "operator_action_queue_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "writes_judgments": False,
            "changes_crontab": False,
            "changes_portfolio": False,
            "changes_strategy": False,
            "input_files": {
                "readiness": READINESS_FILE,
                "cron_audit": CRON_AUDIT_FILE,
                "cron_promotion": CRON_PROMOTION_FILE,
                "packet": PACKET_FILE,
                "position_audit": POSITION_AUDIT_FILE,
                "source_reliability": SOURCE_RELIABILITY_FILE,
                "trusted_source_discovery": TRUSTED_SOURCE_DISCOVERY_FILE,
                "trusted_source_preflight": TRUSTED_SOURCE_PREFLIGHT_FILE,
                "simulation_performance": SIMULATION_PERFORMANCE_FILE,
                "simulation_postmortem_audit": SIMULATION_POSTMORTEM_AUDIT_FILE,
                "simulation_postmortem_note_draft": SIMULATION_POSTMORTEM_NOTE_DRAFT_FILE,
                "outcome": OUTCOME_FILE,
            },
        },
        "summary": {
            "action_count": len(actions),
            "priority_counts": dict(counts),
            "category_counts": dict(categories),
            "p0_action_count": counts.get("P0", 0),
            "p1_action_count": counts.get("P1", 0),
        },
        "actions": actions,
        "operator_notes": [
            "This queue is a read-only prioritization layer; it does not install cron, write judgments, submit orders, or change portfolios.",
            "Actions with operator_effect.changes_crontab=true still require a human/Hermes operator to run the listed hash-confirmed command.",
            "Actions with operator_effect.writes_judgments=true require Hermes to write completed advisory JSONL objects; templates are not judgments.",
            "P0 means the current system evidence contradicts new exposure or urgent position review is unresolved.",
        ],
    }


def build_text_report(payload):
    lines = [
        f"Operator action queue {payload['generated_at']} status={payload['status']}",
        (
            f"actions={payload['summary']['action_count']} "
            f"priorities={json.dumps(payload['summary']['priority_counts'], ensure_ascii=False, sort_keys=True)}"
        ),
    ]
    for item in payload.get("actions", [])[:20]:
        lines.append(f"{item['priority']} {item['category']} {item['id']}: {item['title']}")
        if item.get("recommended_next_step"):
            lines.append(f"  next={item['recommended_next_step']}")
        if item.get("operator_command"):
            lines.append(f"  command={item['operator_command']}")
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report()
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(build_text_report(payload))
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
