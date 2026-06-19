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
POSITION_JUDGMENT_WRITE_PACKET_FILE = os.environ.get(
    "HERMES_POSITION_JUDGMENT_WRITE_PACKET_FILE",
    "/tmp/hermes_position_judgment_write_packet.json",
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
STRATEGY_REVIEW_FILE = os.environ.get("STRATEGY_REVIEW_REPORT_FILE", "/tmp/strategy_review_report.json")
V5_LOCAL_REPLAY_FILE = os.environ.get("V5_LOCAL_REPLAY_REPORT_FILE", "/tmp/v5_local_replay_report.json")
V5_REPLAY_STRATEGY_REVIEW_FILE = os.environ.get(
    "V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE",
    "/tmp/v5_replay_strategy_review_report.json",
)
TRIGGER_EVIDENCE_CONVERGENCE_FILE = os.environ.get(
    "TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE",
    "/tmp/trigger_evidence_convergence_report.json",
)
MAX_REPLAY_CONTEXT_AGE_HOURS = float(os.environ.get("OPERATOR_ACTION_MAX_REPLAY_CONTEXT_AGE_HOURS", "24"))


PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def parse_timestamp(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


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


def review_thread_key_for_item(item):
    item = safe_dict(item)
    explicit = str(item.get("review_thread_key") or "").strip()
    if explicit:
        return explicit
    role = str(item.get("role") or "").strip()
    portfolio_id = item.get("portfolio_id")
    symbol = str(item.get("symbol") or "").strip().upper()
    if not role or portfolio_id in (None, "") or not symbol:
        return ""
    return f"{role}:{portfolio_id}:{symbol}"


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
        packet_items = safe_list(packet_review.get("items"))
        template_summary = packet_review.get("position_judgment_template_summary") or {}
        task_index = safe_dict(packet.get("position_judgment_tasks"))
        task_rows = safe_list(task_index.get("tasks"))
        task_index_schema = str(task_index.get("schema") or "").strip()
        task_index_usable = task_index_schema == "hermes_position_judgment_task_index_v1" and bool(task_rows)
        worklist = safe_dict(packet.get("position_judgment_worklist"))
        worklist_rows = safe_list(worklist.get("items"))
        preferred_work_items = [
            safe_dict(row)
            for row in worklist_rows[: min(high_unjudged, 20)]
            if isinstance(row, dict)
        ]
        items_by_id = {str(item.get("review_id") or "").strip(): item for item in packet_items if isinstance(item, dict)}
        items_by_thread = {
            review_thread_key_for_item(item): item
            for item in packet_items
            if isinstance(item, dict) and review_thread_key_for_item(item)
        }
        if task_index_usable:
            write_plan = [safe_dict(row) for row in task_rows[: min(high_unjudged, 20)] if isinstance(row, dict)]
        else:
            write_plan = []
            for example in examples[: min(high_unjudged, 20)]:
                if not isinstance(example, dict):
                    continue
                review_id = str(example.get("review_id") or "").strip()
                item = items_by_id.get(review_id)
                if item is None:
                    item = items_by_thread.get(review_thread_key_for_item(example))
                advisory_plan = safe_dict(item.get("advisory_plan")) if isinstance(item, dict) else {}
                dynamic = safe_dict(advisory_plan.get("dynamic_management_context"))
                intraday_contract = safe_dict(advisory_plan.get("intraday_review_contract"))
                decision_points = [
                    {
                        "decision": point.get("decision"),
                        "quantity_fraction": point.get("quantity_fraction"),
                        "quantity_hint": point.get("quantity_hint"),
                        "price_reference": point.get("price_reference"),
                        "manual_only": point.get("manual_only"),
                        "condition": point.get("condition"),
                    }
                    for point in safe_list(advisory_plan.get("operator_decision_points"))[:5]
                    if isinstance(point, dict)
                ]
                template = safe_dict(item.get("position_judgment_template")) if isinstance(item, dict) else {}
                digest = safe_dict(item.get("context_digest")) if isinstance(item, dict) else {}
                write_plan.append(
                    {
                        "review_id": review_id,
                        "review_thread_key": review_thread_key_for_item(item or example),
                        "portfolio_id": example.get("portfolio_id"),
                        "role": example.get("role"),
                        "symbol": example.get("symbol"),
                        "urgency": example.get("urgency"),
                        "recommended_action": example.get("recommended_action"),
                        "packet_item_found": item is not None,
                        "allowed_decisions": template.get("allowed_decisions"),
                        "required_attention_codes": safe_list(template.get("required_position_attention_codes"))
                        or safe_list(digest.get("position_attention")),
                        "dynamic_management": {
                            "target_status": dynamic.get("target_status"),
                            "price_snapshot_age_hours": dynamic.get("price_snapshot_age_hours"),
                            "price_snapshot_fresh": dynamic.get("price_snapshot_fresh"),
                            "distance_to_signal_take_profit_pct": dynamic.get("distance_to_signal_take_profit_pct"),
                            "distance_above_signal_stop_loss_pct": dynamic.get("distance_above_signal_stop_loss_pct"),
                            "review_focus": safe_list(dynamic.get("review_focus"))[:4],
                        },
                        "intraday_review_contract": {
                            "decision_use": intraday_contract.get("decision_use"),
                            "required_timeframes": safe_list(intraday_contract.get("required_timeframes"))[:6],
                            "required_checks": safe_list(intraday_contract.get("required_checks"))[:6],
                            "hard_limits": safe_list(intraday_contract.get("hard_limits"))[:4],
                        } if intraday_contract else {},
                        "operator_decision_points": decision_points,
                    }
                )
        review_workflow_command = (
            f"/usr/bin/python3 /root/hermes_review_packet.py --ephemeral-state --output {PACKET_FILE} && "
            f"/usr/bin/python3 /root/hermes_position_judgment_write_packet.py "
            f"--output {POSITION_JUDGMENT_WRITE_PACKET_FILE} --text && "
            f"/usr/bin/python3 /root/hermes_position_judgment_audit_report.py "
            f"--output {POSITION_AUDIT_FILE} --text"
        )
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
                    "packet_file": PACKET_FILE,
                    "write_packet_file": POSITION_JUDGMENT_WRITE_PACKET_FILE,
                    "judgment_file": POSITION_JUDGMENT_FILE,
                    "manual_append_target": POSITION_JUDGMENT_FILE,
                    "template_source": f"{PACKET_FILE} position_review.items[].position_judgment_template",
                    "template_summary": template_summary,
                    "task_index": {
                        "schema": task_index.get("schema"),
                        "task_count": task_index.get("task_count"),
                        "included_task_count": task_index.get("included_task_count"),
                        "high_urgency_task_count": task_index.get("high_urgency_task_count"),
                        "submits_orders": task_index.get("submits_orders"),
                        "used_for_write_plan": task_index_usable,
                    } if task_index else {},
                    "worklist": {
                        "schema": worklist.get("schema"),
                        "item_count": worklist.get("item_count"),
                        "included_item_count": worklist.get("included_item_count"),
                        "high_urgency_item_count": worklist.get("high_urgency_item_count"),
                        "submits_orders": worklist.get("submits_orders"),
                        "preferred_input": bool(worklist_rows),
                        "first_review_id": safe_dict(worklist_rows[0]).get("review_id") if worklist_rows else None,
                    } if worklist else {},
                    "unjudged_examples": examples,
                    "position_judgment_work_items": preferred_work_items,
                    "position_judgment_write_plan": write_plan,
                    "safety": {
                        "template_only": bool(template_summary.get("template_only")),
                        "ready_to_append_without_hermes_review": False,
                        "order_submission": False,
                    },
                },
                next_step=(
                    f"Refresh {PACKET_FILE} and {POSITION_JUDGMENT_WRITE_PACKET_FILE}, use "
                    "hermes_position_judgment_write_packet.items as the compact preferred Hermes input, and use "
                    "position_judgment_worklist.items or position_judgment_write_plan only as index/fallback. Review "
                    "the full position_review item when needed, replace every placeholder, and append completed Hermes-reviewed "
                    f"JSONL objects to {POSITION_JUDGMENT_FILE}. Do not copy template placeholders or set "
                    "order_submission=true; then rerun hermes_position_judgment_audit_report.py."
                ),
                command=review_workflow_command,
                operator_effect={
                    "writes_judgments": True,
                    "advisory_only": True,
                    "submits_orders": False,
                    "changes_portfolio": False,
                    "changes_strategy": False,
                    "changes_crontab": False,
                },
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
            operator_effect={
                "simulation_recovery_review": True,
                "writes_postmortem_notes": False,
                "enables_alert_sim": False,
                "changes_execution_mode": False,
                "changes_strategy_config": False,
                "submits_orders": False,
                "changes_strategy": False,
                "changes_portfolio": False,
            },
            blockers=["simulation_performance_fail"],
        )
    ]


def postmortem_draft_target_key(draft):
    draft = safe_dict(draft)
    context = safe_dict(draft.get("draft_context"))
    target_id = str(context.get("target_id") or "").strip()
    if target_id:
        return target_id
    symbol = str(draft.get("symbol") or "").strip().upper()
    target_type = str(draft.get("target_type") or "").strip()
    return f"{target_type}:{symbol}" if target_type and symbol else ""


def postmortem_target_key(target):
    target = safe_dict(target)
    target_id = str(target.get("target_id") or "").strip()
    if target_id:
        return target_id
    symbol = str(target.get("symbol") or "").strip().upper()
    target_type = str(target.get("target_type") or "").strip()
    return f"{target_type}:{symbol}" if target_type and symbol else ""


def postmortem_note_write_plan(simulation_postmortem_audit, note_draft_report, limit=20):
    drafts = safe_list(safe_dict(note_draft_report).get("drafts"))
    drafts_by_target = {
        postmortem_draft_target_key(draft): draft
        for draft in drafts
        if postmortem_draft_target_key(draft)
    }
    rows = []
    targets = safe_list(simulation_postmortem_audit.get("missing_required_targets"))
    for target in targets[:limit]:
        if not isinstance(target, dict):
            continue
        key = postmortem_target_key(target)
        draft = safe_dict(drafts_by_target.get(key))
        context = safe_dict(draft.get("draft_context"))
        rows.append(
            {
                "target_id": key,
                "symbol": target.get("symbol") or draft.get("symbol"),
                "target_type": target.get("target_type") or draft.get("target_type"),
                "draft_found": bool(draft),
                "draft_only": draft.get("draft_only"),
                "failure_category_placeholder": draft.get("failure_category"),
                "lesson_placeholder": draft.get("lesson"),
                "proposed_change_default": draft.get("proposed_change"),
                "promotion_gate": draft.get("promotion_gate"),
                "context_statuses": safe_dict(context.get("context_statuses")),
                "target_evidence": safe_dict(context.get("target_evidence")),
                "matched_context_ids": safe_list(context.get("matched_context_ids"))[:5],
            }
        )
    return rows


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
    write_plan = postmortem_note_write_plan(simulation_postmortem_audit, note_draft_report)
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
                "postmortem_note_write_plan": write_plan,
            },
            next_step=(
                "Use postmortem_note_write_plan to cover each required target, then use simulation_postmortem_note_draft_report.py "
                "as a read-only draft helper, replace every placeholder, "
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


def report_age_hours(payload, now=None):
    generated_at = parse_timestamp(safe_dict(payload).get("generated_at"))
    if not generated_at:
        return None
    now = now or datetime.now()
    return round((now - generated_at).total_seconds() / 3600.0, 2)


def replay_convergence_actions(strategy_review, v5_local_replay, v5_replay_strategy_review, convergence, now=None):
    strategy_review = safe_dict(strategy_review)
    v5_local_replay = safe_dict(v5_local_replay)
    v5_replay_strategy_review = safe_dict(v5_replay_strategy_review)
    convergence = safe_dict(convergence)

    has_forward_context = strategy_review.get("schema") == "strategy_review_report_v1"
    has_local_replay = v5_local_replay.get("schema") == "v5_local_replay_report_v1"
    has_replay_strategy = v5_replay_strategy_review.get("schema") == "v5_replay_strategy_review_report_v1"
    has_convergence = convergence.get("schema") == "trigger_evidence_convergence_report_v1"
    if not any((has_forward_context, has_local_replay, has_replay_strategy, has_convergence)):
        return []

    missing = []
    if not has_forward_context:
        missing.append("strategy_review_report")
    if not has_local_replay:
        missing.append("v5_local_replay_report")
    if not has_replay_strategy:
        missing.append("v5_replay_strategy_review_report")
    if not has_convergence:
        missing.append("trigger_evidence_convergence_report")

    actions = []
    replay_age = report_age_hours(v5_replay_strategy_review, now=now) if has_replay_strategy else None
    convergence_age = report_age_hours(convergence, now=now) if has_convergence else None
    stale = []
    if replay_age is not None and replay_age > MAX_REPLAY_CONTEXT_AGE_HOURS:
        stale.append("v5_replay_strategy_review_report")
    if convergence_age is not None and convergence_age > MAX_REPLAY_CONTEXT_AGE_HOURS:
        stale.append("trigger_evidence_convergence_report")

    if missing or stale:
        commands = []
        refresh_local_replay = not has_local_replay
        refresh_replay_strategy = has_local_replay and (
            not has_replay_strategy or "v5_replay_strategy_review_report" in stale
        )
        if refresh_local_replay:
            commands.append(
                "/usr/bin/python3 /root/v5_local_replay_report.py "
                "--source db --db-lookback-days 365 "
                "--strategy-config-file /root/rt_signal_strategy_config.json "
                "--output /tmp/v5_local_replay_report.json --text"
            )
            refresh_replay_strategy = not has_replay_strategy or "v5_replay_strategy_review_report" in stale
        refresh_convergence = has_forward_context and (
            has_replay_strategy or refresh_replay_strategy
        ) and (
            not has_convergence
            or "trigger_evidence_convergence_report" in stale
            or "v5_replay_strategy_review_report" in stale
        )
        if refresh_replay_strategy:
            commands.append(
                "/usr/bin/python3 /root/v5_replay_strategy_review_report.py "
                "--v5-local-replay-file /tmp/v5_local_replay_report.json "
                "--output /tmp/v5_replay_strategy_review_report.json --text"
            )
        if refresh_convergence:
            commands.append(
                "/usr/bin/python3 /root/trigger_evidence_convergence_report.py "
                "--strategy-review-file /tmp/strategy_review_report.json "
                "--v5-replay-strategy-review-file /tmp/v5_replay_strategy_review_report.json "
                "--output /tmp/trigger_evidence_convergence_report.json --text"
            )
        actions.append(
            action(
                "refresh_v5_replay_convergence_context",
                "P2",
                "evidence_collection",
                "Refresh local replay and trigger-convergence context",
                (
                    "Hermes can compare forward outcome evidence with replay-derived trigger noise only when the replay "
                    "strategy review and convergence reports are present and fresh."
                ),
                evidence={
                    "missing_reports": missing,
                    "stale_reports": stale,
                    "max_age_hours": MAX_REPLAY_CONTEXT_AGE_HOURS,
                    "ages_hours": {
                        "v5_replay_strategy_review": replay_age,
                        "trigger_evidence_convergence": convergence_age,
                    },
                    "schemas": {
                        "strategy_review": strategy_review.get("schema"),
                        "v5_local_replay": v5_local_replay.get("schema"),
                        "v5_replay_strategy_review": v5_replay_strategy_review.get("schema"),
                        "trigger_evidence_convergence": convergence.get("schema"),
                    },
                    "local_only_note": (
                        "Raw replay CSV/minute data should stay local; server DB replay uses existing completed daily "
                        "klines as a read-only snapshot and writes only compact JSON."
                    ),
                },
                next_step=(
                    "Regenerate the missing or stale read-only reports. If v5_local_replay_report is missing on the server, "
                    "prefer running /root/v5_local_replay_report.py --source db --db-lookback-days 365 "
                    "--strategy-config-file /root/rt_signal_strategy_config.json against the existing server DB daily K-line "
                    "snapshot, or copy only a compact local JSON report; do not sync raw CSV/minute data to production by default."
                ),
                command=" && ".join(commands) if commands else None,
                operator_effect={
                    "refreshes_reports": True,
                    "uses_existing_db_snapshot": refresh_local_replay,
                    "uses_local_replay_summary_only": not refresh_local_replay,
                    "copies_raw_data": False,
                    "submits_orders": False,
                    "changes_strategy": False,
                    "changes_portfolio": False,
                    "changes_crontab": False,
                },
                blockers=[],
            )
        )

    summary = safe_dict(convergence.get("summary"))
    risk_count = int(summary.get("converged_risk_count") or 0)
    replay_challenges = int(summary.get("replay_challenges_forward_count") or 0)
    insufficient = int(summary.get("insufficient_forward_sample_count") or 0)
    forward_scope_empty = int(summary.get("forward_scope_empty_count") or 0)
    if has_convergence and forward_scope_empty and not (risk_count or replay_challenges or insufficient):
        top_rows = []
        for row in safe_list(convergence.get("trigger_evidence")):
            if not isinstance(row, dict) or row.get("status") != "FORWARD_SCOPE_EMPTY":
                continue
            top_rows.append(
                {
                    "key": row.get("key"),
                    "status": row.get("status"),
                    "confidence": row.get("confidence"),
                    "reasons": safe_list(row.get("reasons")),
                    "forward_policy": safe_dict(row.get("forward")).get("policy"),
                    "replay_policy": safe_dict(row.get("replay")).get("policy"),
                }
            )
        actions.append(
            action(
                "collect_current_strategy_forward_evidence",
                "P2",
                "evidence_collection",
                "Wait for current strategy forward evidence before promotion",
                (
                    "The current strategy/watchlist scope has no matching forward alerts yet, so replay evidence cannot be "
                    "converged with live outcomes without mixing older strategy versions."
                ),
                evidence={
                    "summary": summary,
                    "top_trigger_evidence": top_rows[:8],
                    "recommendations": safe_list(convergence.get("recommendations")),
                    "operator_contract": safe_dict(convergence.get("operator_contract")),
                },
                next_step=(
                    "Keep the current strategy/watchlist scope separate from older alert history. Wait for current-scope "
                    "alerts to mature into forward outcomes, then refresh rt_signal_outcome_report.py, "
                    "rt_alert_quality_report.py, strategy_review_report.py, and trigger_evidence_convergence_report.py "
                    "before considering any strategy promotion."
                ),
                operator_effect={
                    "refreshes_reports": False,
                    "submits_orders": False,
                    "changes_strategy": False,
                    "changes_portfolio": False,
                    "changes_crontab": False,
                },
            )
        )
    if has_convergence and (risk_count or replay_challenges or insufficient):
        top_rows = []
        for row in safe_list(convergence.get("trigger_evidence")):
            if not isinstance(row, dict):
                continue
            if row.get("status") in ("CONVERGED_RISK", "REPLAY_CHALLENGES_FORWARD", "INSUFFICIENT_FORWARD_SAMPLE"):
                top_rows.append(
                    {
                        "key": row.get("key"),
                        "status": row.get("status"),
                        "confidence": row.get("confidence"),
                        "reasons": safe_list(row.get("reasons")),
                        "forward_policy": safe_dict(row.get("forward")).get("policy"),
                        "replay_policy": safe_dict(row.get("replay")).get("policy"),
                    }
                )
        actions.append(
            action(
                "review_trigger_evidence_convergence_before_promotion",
                "P1" if risk_count or replay_challenges else "P2",
                "evidence_collection",
                "Review replay/forward trigger convergence before promotion",
                (
                    "Forward outcome policy and local replay noise are not fully supportive. Hermes should treat this as "
                    "challenge context and cap confidence until forward samples mature or trigger thresholds are reviewed."
                ),
                evidence={
                    "summary": summary,
                    "top_trigger_evidence": top_rows[:8],
                    "recommendations": safe_list(convergence.get("recommendations")),
                    "operator_contract": safe_dict(convergence.get("operator_contract")),
                },
                next_step=(
                    "Do not promote strategy_config from replay or convergence alone. Prioritize triggers flagged as "
                    "CONVERGED_RISK or REPLAY_CHALLENGES_FORWARD, then wait for resolved forward outcomes before any threshold/config promotion."
                ),
                operator_effect={
                    "refreshes_reports": False,
                    "submits_orders": False,
                    "changes_strategy": False,
                    "changes_portfolio": False,
                    "changes_crontab": False,
                },
            )
        )
    return actions


def outcome_actions(outcome):
    counts = outcome.get("counts") if isinstance(outcome.get("counts"), dict) else {}
    evaluated = int(counts.get("evaluated_signal_count") or 0)
    status = outcome.get("status")
    if status not in ("PENDING", "INSUFFICIENT") and evaluated:
        return []
    overall = outcome.get("overall") if isinstance(outcome.get("overall"), dict) else {}
    maturity = outcome.get("outcome_maturity") if isinstance(outcome.get("outcome_maturity"), dict) else {}
    by_trigger = safe_list(outcome.get("by_trigger"))
    top_pending_triggers = []
    for row in by_trigger:
        if not isinstance(row, dict):
            continue
        horizon = safe_dict(safe_dict(row.get("horizons")).get("1d"))
        pending_count = int(horizon.get("pending_count") or 0)
        if pending_count <= 0:
            continue
        top_pending_triggers.append(
            {
                "key": row.get("key"),
                "count": row.get("count"),
                "confirmed_count": row.get("confirmed_count"),
                "pending_1d_count": pending_count,
                "resolved_1d_count": horizon.get("resolved_count"),
                "avg_full_score": row.get("avg_full_score"),
            }
        )
    top_pending_triggers = sorted(
        top_pending_triggers,
        key=lambda row: (-(int(row.get("pending_1d_count") or 0)), row.get("key") or ""),
    )
    pending_reasons = overall.get("pending_reasons") if isinstance(overall.get("pending_reasons"), dict) else {}
    missing_diagnostics = safe_list(maturity.get("missing_symbol_kline_diagnostics"))
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
                "overall": {
                    "resolved_signal_count": overall.get("resolved_signal_count"),
                    "pending_or_invalid_count": overall.get("pending_or_invalid_count"),
                    "pending_reasons": pending_reasons,
                },
                "outcome_maturity": {
                    "primary_horizon": maturity.get("primary_horizon"),
                    "needed_future_days": maturity.get("needed_future_days"),
                    "latest_signal_date": maturity.get("latest_signal_date"),
                    "latest_kline_date": maturity.get("latest_kline_date"),
                    "pending_or_invalid_count": maturity.get("pending_or_invalid_count"),
                    "missing_symbol_kline_count": maturity.get("missing_symbol_kline_count"),
                    "no_future_daily_kline_count": maturity.get("no_future_daily_kline_count"),
                    "daily_gap_source_category_affected_signal_counts": maturity.get(
                        "daily_gap_source_category_affected_signal_counts"
                    )
                    or {},
                },
                "top_pending_triggers": top_pending_triggers[:8],
                "missing_symbol_kline_diagnostics": missing_diagnostics[:8],
                "intraday_signal_context_summary": outcome.get("intraday_signal_context_summary"),
                "recommendations": outcome.get("recommendations"),
            },
            next_step=(
                "Keep rt_signal_outcome_report fresh. If pending_reasons is mostly no_future_daily_klines, wait for "
                "completed future daily bars; if missing_symbol_klines or daily-gap source categories are present, "
                "run the daily-gap repair/source-diagnostic workflow before using the sample as strategy evidence."
            ),
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
    strategy_review = (
        payloads.get("strategy_review")
        if isinstance(payloads.get("strategy_review"), dict)
        else load_json_file(STRATEGY_REVIEW_FILE)
    )
    v5_local_replay = (
        payloads.get("v5_local_replay")
        if isinstance(payloads.get("v5_local_replay"), dict)
        else load_json_file(V5_LOCAL_REPLAY_FILE)
    )
    v5_replay_strategy_review = (
        payloads.get("v5_replay_strategy_review")
        if isinstance(payloads.get("v5_replay_strategy_review"), dict)
        else load_json_file(V5_REPLAY_STRATEGY_REVIEW_FILE)
    )
    trigger_evidence_convergence = (
        payloads.get("trigger_evidence_convergence")
        if isinstance(payloads.get("trigger_evidence_convergence"), dict)
        else load_json_file(TRIGGER_EVIDENCE_CONVERGENCE_FILE)
    )

    actions = []
    actions.extend(cron_actions(cron_audit, cron_promotion))
    actions.extend(packet_actions(packet))
    actions.extend(position_actions(position_audit, packet))
    actions.extend(readiness_actions(readiness))
    actions.extend(source_reliability_actions(source_reliability))
    actions.extend(trusted_source_onboarding_actions(trusted_source_discovery, trusted_source_preflight))
    actions.extend(simulation_actions(simulation_performance))
    actions.extend(simulation_postmortem_actions(simulation_postmortem_audit, simulation_postmortem_note_draft))
    actions.extend(
        replay_convergence_actions(
            strategy_review,
            v5_local_replay,
            v5_replay_strategy_review,
            trigger_evidence_convergence,
        )
    )
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
                "strategy_review": STRATEGY_REVIEW_FILE,
                "v5_local_replay": V5_LOCAL_REPLAY_FILE,
                "v5_replay_strategy_review": V5_REPLAY_STRATEGY_REVIEW_FILE,
                "trigger_evidence_convergence": TRIGGER_EVIDENCE_CONVERGENCE_FILE,
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
