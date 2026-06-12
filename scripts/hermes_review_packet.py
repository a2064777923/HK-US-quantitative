#!/usr/bin/env python3
"""Build a single review packet for Hermes trade judgment.

The packet is review-only. It may run rt_order_intake in dry-run mode to
produce sizing/rejection context, but it never submits simulation orders.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime

try:
    import portfolio_report
    import rt_order_intake as intake
    import system_health_check
except ImportError:
    from scripts import portfolio_report
    from scripts import rt_order_intake as intake
    from scripts import system_health_check


PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
PACKET_ARCHIVE_DIR = os.environ.get("HERMES_REVIEW_PACKET_ARCHIVE_DIR", "/tmp/hermes_review_packet_archive")
JUDGMENT_SCHEMA = os.environ.get("HERMES_JUDGMENT_SCHEMA", "hermes_trade_judgment_v1")
POSITION_JUDGMENT_SCHEMA = os.environ.get("HERMES_POSITION_JUDGMENT_SCHEMA", "hermes_position_judgment_v1")
POSITION_JUDGMENT_FILE = os.environ.get("HERMES_POSITION_JUDGMENT_FILE", "/tmp/hermes_position_judgments.jsonl")
OUTCOME_REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
ALERT_QUALITY_REPORT_FILE = os.environ.get("ALERT_QUALITY_REPORT_FILE", "/tmp/rt_alert_quality_report.json")
ALERT_EVENT_STORE_REPORT_FILE = os.environ.get("RT_ALERT_EVENT_STORE_REPORT_FILE", "/tmp/rt_alert_event_store_report.json")
JUDGMENT_EVENT_STORE_REPORT_FILE = os.environ.get(
    "HERMES_JUDGMENT_EVENT_STORE_REPORT_FILE",
    "/tmp/hermes_judgment_event_store_report.json",
)
INTAKE_EVENT_STORE_REPORT_FILE = os.environ.get(
    "RT_ORDER_INTAKE_EVENT_STORE_REPORT_FILE",
    "/tmp/rt_order_intake_event_store_report.json",
)
OUTCOME_EVENT_STORE_REPORT_FILE = os.environ.get(
    "RT_SIGNAL_OUTCOME_EVENT_STORE_REPORT_FILE",
    "/tmp/rt_signal_outcome_event_store_report.json",
)
STRATEGY_REVIEW_REPORT_FILE = os.environ.get("STRATEGY_REVIEW_REPORT_FILE", "/tmp/strategy_review_report.json")
STRATEGY_LEARNING_REPORT_FILE = os.environ.get("STRATEGY_LEARNING_REPORT_FILE", "/tmp/strategy_learning_report.json")
EXECUTION_READINESS_REPORT_FILE = os.environ.get("EXECUTION_READINESS_REPORT_FILE", "/tmp/execution_readiness_report.json")
SIMULATION_PERFORMANCE_REPORT_FILE = os.environ.get(
    "SIMULATION_PERFORMANCE_REPORT_FILE",
    "/tmp/simulation_performance_report.json",
)
MARKET_CONTEXT_FILE = os.environ.get("MARKET_CONTEXT_REPORT_FILE", "/tmp/market_context_report.json")
DATA_HEALTH_REPORT_FILE = os.environ.get("DATA_HEALTH_REPORT_FILE", "/tmp/data_health_report.json")
UNIVERSE_REPORT_FILE = os.environ.get("UNIVERSE_RANK_REPORT_FILE", "/tmp/universe_rank_report.json")
WATCHLIST_DIFF_REPORT_FILE = os.environ.get("WATCHLIST_DIFF_REPORT_FILE", "/tmp/watchlist_diff_report.json")
UNIVERSE_HYGIENE_REPORT_FILE = os.environ.get("UNIVERSE_HYGIENE_REPORT_FILE", "/tmp/universe_hygiene_report.json")
JUDGMENT_AUDIT_FILE = os.environ.get("HERMES_JUDGMENT_AUDIT_FILE", "/tmp/hermes_judgment_audit_report.json")
POSITION_JUDGMENT_AUDIT_FILE = os.environ.get(
    "HERMES_POSITION_JUDGMENT_AUDIT_FILE",
    "/tmp/hermes_position_judgment_audit_report.json",
)
DEFAULT_REVIEW_LIMIT = int(os.environ.get("HERMES_REVIEW_LIMIT", "20"))
DEFAULT_QUEUE_SCAN_LIMIT = int(os.environ.get("HERMES_QUEUE_SCAN_LIMIT", "500"))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def safe_file_stem(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))[:120]


def archive_packet(packet, archive_dir=PACKET_ARCHIVE_DIR):
    if not archive_dir:
        return ""
    packet_id = safe_file_stem(packet.get("packet_id"))
    if not packet_id:
        return ""
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, f"{packet_id}.json")
    save_json_atomic(path, packet)
    return path


def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except Exception as exc:
        return {"status": "missing", "path": path, "error": str(exc)}
    return {"status": "invalid", "path": path}


def packet_id_for(alerts, health_payload):
    signal_ids = [intake.signal_id(alert) for alert in alerts]
    seed = {
        "signal_ids": signal_ids,
        "health_checked_at": health_payload.get("checked_at"),
        "health_status": health_payload.get("status"),
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def is_directional(alert):
    return str(alert.get("signal_type", "")).upper() in ("BUY", "SELL")


def infer_current_sample_scope(alerts, sample_scope_mode="current"):
    if sample_scope_mode == "all":
        return {
            "mode": "all_scanned_alerts",
            "strategy_config_id": None,
            "watchlist_id": None,
            "latest_signal_id": None,
        }
    for alert in reversed(alerts):
        if not is_directional(alert):
            continue
        strategy_config_id = alert.get("strategy_config_id")
        watchlist_id = alert.get("watchlist_id")
        if strategy_config_id and watchlist_id:
            return {
                "mode": "latest_strategy_config_and_watchlist",
                "strategy_config_id": str(strategy_config_id),
                "watchlist_id": str(watchlist_id),
                "latest_signal_id": intake.signal_id(alert),
            }
    return {
        "mode": "all_scanned_alerts",
        "strategy_config_id": None,
        "watchlist_id": None,
        "latest_signal_id": None,
    }


def alert_matches_scope(alert, scope):
    if (scope or {}).get("mode") != "latest_strategy_config_and_watchlist":
        return True
    return (
        str(alert.get("strategy_config_id") or "") == scope.get("strategy_config_id")
        and str(alert.get("watchlist_id") or "") == scope.get("watchlist_id")
    )


def apply_sample_scope(alerts, sample_scope_mode="current"):
    scope = infer_current_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    scoped = [alert for alert in alerts if alert_matches_scope(alert, scope)]
    all_directional = [alert for alert in alerts if is_directional(alert)]
    scoped_directional = [alert for alert in scoped if is_directional(alert)]
    scope.update(
        {
            "raw_alert_count_before_filter": len(alerts),
            "raw_alert_count": len(scoped),
            "excluded_alert_count": len(alerts) - len(scoped),
            "directional_alert_count_before_filter": len(all_directional),
            "directional_alert_count": len(scoped_directional),
            "excluded_directional_alert_count": len(all_directional) - len(scoped_directional),
        }
    )
    return scoped, scope


def alert_selection_stats(source_alerts, review_alerts, sample_scope=None):
    by_type = {}
    directional = 0
    confirmed_directional = 0
    unconfirmed_directional = 0
    for alert in source_alerts:
        side = str(alert.get("signal_type", "UNKNOWN")).upper() or "UNKNOWN"
        by_type[side] = by_type.get(side, 0) + 1
        if side in ("BUY", "SELL"):
            directional += 1
            if alert.get("confirmed") is True:
                confirmed_directional += 1
            else:
                unconfirmed_directional += 1
    return {
        "source_alert_count": len(source_alerts),
        "review_alert_count": len(review_alerts),
        "directional_count": directional,
        "confirmed_directional_count": confirmed_directional,
        "unconfirmed_directional_count": unconfirmed_directional,
        "directional_not_selected_count": max(directional - len(review_alerts), 0),
        "by_signal_type": by_type,
        "review_signal_ids": [intake.signal_id(alert) for alert in review_alerts],
        "sample_scope": sample_scope or infer_current_sample_scope(source_alerts),
    }


def select_review_alerts(
    alerts,
    limit=DEFAULT_REVIEW_LIMIT,
    include_watch=False,
    include_unconfirmed=False,
    sample_scope_mode="current",
):
    scoped_alerts, _scope = apply_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    if include_watch:
        candidates = scoped_alerts
    else:
        candidates = [
            alert
            for alert in scoped_alerts
            if is_directional(alert) and (include_unconfirmed or alert.get("confirmed") is True)
        ]
    return candidates[-limit:] if limit and limit > 0 else candidates


def load_source_alerts(alert_json=None, alert_file=None, queue_file=None, scan_limit=DEFAULT_QUEUE_SCAN_LIMIT):
    args = type(
        "Args",
        (),
        {
            "alert_json": alert_json,
            "alert_file": alert_file,
            "queue_file": queue_file,
            "limit": scan_limit,
        },
    )()
    return intake.load_alerts_from_args(args)


def load_alerts(
    alert_json=None,
    alert_file=None,
    queue_file=None,
    limit=DEFAULT_REVIEW_LIMIT,
    scan_limit=DEFAULT_QUEUE_SCAN_LIMIT,
    include_watch=False,
    include_unconfirmed=False,
    sample_scope_mode="current",
):
    source_alerts = load_source_alerts(alert_json, alert_file, queue_file, scan_limit)
    return select_review_alerts(
        source_alerts,
        limit=limit,
        include_watch=include_watch,
        include_unconfirmed=include_unconfirmed,
        sample_scope_mode=sample_scope_mode,
    )


def run_intake_dry_runs(alerts, state_file, judgment_file):
    state = intake.load_state(state_file)
    results = []
    for alert in alerts:
        results.append(intake.process_alert(alert, "dry-run", state, state_file, judgment_file))
    return results


def alert_summary(alert):
    return {
        "signal_id": intake.signal_id(alert),
        "source": alert.get("source"),
        "symbol": alert.get("symbol"),
        "market": alert.get("market"),
        "signal_type": alert.get("signal_type"),
        "trigger": alert.get("trigger"),
        "confirmed": alert.get("confirmed"),
        "full_score": alert.get("full_score"),
        "rr_ratio": alert.get("rr_ratio"),
        "entry_price": alert.get("entry_price"),
        "stop_loss": alert.get("stop_loss"),
        "take_profit": alert.get("take_profit"),
        "generated_at": alert.get("generated_at"),
        "quote_time": alert.get("quote_time") or alert.get("time"),
    }


def review_item(alert, result, health_status):
    reasons = []
    if health_status == "FAIL":
        reasons.append("system_health_fail")
    if result.get("status") != "dry_run":
        reasons.append(f"intake_status_{result.get('status', 'unknown')}")
    if not result.get("plan"):
        reasons.append("no_order_plan")
    reasons.extend(result.get("reasons") or [])
    strategy_gate = result.get("strategy_evidence") or {}
    if strategy_gate.get("would_block_execute") or strategy_gate.get("status") == "REJECTED":
        reasons.append("strategy_evidence_would_block_execute")
        for reason in strategy_gate.get("reasons") or []:
            reasons.append(f"strategy_evidence:{reason}")
    conflict_gate = result.get("symbol_conflict") or {}
    if conflict_gate.get("would_block_execute") or conflict_gate.get("status") == "REJECTED":
        reasons.append("symbol_conflict_would_block_execute")
        for reason in conflict_gate.get("reasons") or []:
            reasons.append(f"symbol_conflict:{reason}")

    eligible = not reasons
    return {
        "signal_id": result.get("signal_id") or intake.signal_id(alert),
        "eligible_for_approval": eligible,
        "recommended_judgment": "approve_or_reduce_allowed_after_llm_review" if eligible else "reject_or_hold",
        "blocking_reasons": reasons,
        "alert": alert_summary(alert),
        "intake": result,
    }


def is_non_actionable_observation(alert, result):
    side = str((alert or {}).get("signal_type", "")).upper()
    reasons = set((result or {}).get("reasons") or [])
    if (result or {}).get("status") != "rejected":
        return False
    if "alert_too_old" in reasons:
        return True
    return side == "SELL" and "sell_without_position" in reasons


def non_actionable_reason(alert, result):
    side = str((alert or {}).get("signal_type", "")).upper()
    reasons = set((result or {}).get("reasons") or [])
    if "alert_too_old" in reasons:
        return "alert_too_old"
    if side == "SELL" and "sell_without_position" in reasons:
        return "sell_without_position"
    return "non_actionable_rejected_alert"


def non_actionable_observation(alert, result):
    reason = non_actionable_reason(alert, result)
    return {
        "signal_id": result.get("signal_id") or intake.signal_id(alert),
        "reason": reason,
        "recommended_use": "observation_only_no_trade_judgment_required",
        "alert": alert_summary(alert),
        "intake": {
            "status": result.get("status"),
            "reasons": result.get("reasons") or [],
            "signal_id": result.get("signal_id") or intake.signal_id(alert),
            "plan": result.get("plan"),
        },
    }


def portfolio_risk_blocking_reasons(portfolio_payload, alert=None):
    risk_payload = (portfolio_payload or {}).get("portfolio_risk") or {}
    side = str((alert or {}).get("signal_type", "")).upper()
    reasons = []
    for risk in risk_payload.get("reports") or []:
        if risk.get("role") != "simulation":
            continue
        flags = set(risk.get("risk_flags") or [])
        if risk.get("risk_level") == "critical":
            reasons.append("simulation_portfolio_risk_critical")
            for flag in flags:
                reasons.append(f"portfolio_risk:{flag}")
        if risk.get("trade_position_reconciliation_status") == "FAIL":
            reasons.append("portfolio_risk:trade_position_reconciliation_failed")
        if side == "BUY" and "exit_pressure_above_30pct" in flags:
            reasons.append("portfolio_risk:exit_pressure_requires_review_before_new_buy")
    deduped = []
    seen = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return deduped


def apply_portfolio_risk_to_items(items, portfolio_payload):
    for item in items:
        reasons = portfolio_risk_blocking_reasons(portfolio_payload, item.get("alert") or {})
        if reasons:
            merge_blocking_reasons([item], reasons)
    return items


def data_health_blocking_reasons(data_health_payload):
    if not isinstance(data_health_payload, dict):
        return []
    if data_health_payload.get("schema") != "data_health_report_v1":
        return []
    if data_health_payload.get("status") != "FAIL":
        return []
    reasons = ["data_health_fail"]
    for market, summary in sorted((data_health_payload.get("markets") or {}).items()):
        if summary.get("status") == "FAIL":
            for reason in summary.get("failures") or []:
                reasons.append(f"data_health:{market}:{reason}")
    return reasons


def apply_data_health_to_items(items, data_health_payload):
    return merge_blocking_reasons(items, data_health_blocking_reasons(data_health_payload))


def merge_blocking_reasons(items, reasons):
    if not reasons:
        return items
    for item in items:
        merged = list(item.get("blocking_reasons") or [])
        for reason in reasons:
            if reason not in merged:
                merged.append(reason)
        item["blocking_reasons"] = merged
        item["eligible_for_approval"] = False
        item["recommended_judgment"] = "reject_or_hold"
    return items


def judgment_contract(judgment_file):
    return {
        "judgment_file": judgment_file,
        "schema": JUDGMENT_SCHEMA,
        "append_jsonl_object": {
            "schema": JUDGMENT_SCHEMA,
            "packet_id": "<copy from packet_id>",
            "signal_id": "<copy from review_items[].signal_id>",
            "decision": "approve|reject|reduce|hold",
            "confidence": "0.0-1.0",
            "reviewed_at": "ISO-8601 datetime",
            "reviewer": "hermes",
            "supporting_factors": ["facts supporting the decision"],
            "opposing_factors": ["facts against the decision"],
            "risk_notes": ["position sizing, stale data, event risk, market condition"],
            "max_quantity": "required only when decision=reduce",
        },
        "hard_rules": [
            "Do not approve when eligible_for_approval is false.",
            "Do not approve if system health status is FAIL.",
            "Do not approve unconfirmed alerts or alerts rejected by intake.",
            "Do not approve for execute when strategy_evidence is missing, unresolved, or below the configured outcome thresholds.",
            "Do not approve for execute when symbol_conflict indicates an opposite same-symbol alert in the current queue scope.",
            "Do not approve when portfolio_risk reports critical simulation data integrity failures.",
            "Do not approve when data_health status is FAIL or when the relevant market data is stale, incomplete, or internally inconsistent.",
            "Do not approve new BUY exposure while portfolio_risk shows unresolved exit_pressure_above_30pct.",
            "Use market_context to reduce or reject new BUY approvals in risk_off regimes unless there is a specific documented exception with market_regime_exception=true.",
            "Use position_review to evaluate existing holdings before adding exposure.",
            "Use reduce instead of approve when the plan is directionally valid but sizing is too aggressive.",
            "Use reject or hold when context is incomplete, stale, contradictory, or outside the strategy mandate.",
        ],
    }


def position_judgment_contract(judgment_file):
    return {
        "judgment_file": judgment_file,
        "schema": POSITION_JUDGMENT_SCHEMA,
        "append_jsonl_object": {
            "schema": POSITION_JUDGMENT_SCHEMA,
            "packet_id": "<copy from packet_id>",
            "review_id": "<copy from position_review.items[].review_id>",
            "portfolio_id": "<copy from position_review.items[].portfolio_id>",
            "role": "<copy from position_review.items[].role>",
            "symbol": "<copy from position_review.items[].symbol>",
            "decision": "hold|watch|reduce|exit|trail_stop",
            "confidence": "0.0-1.0",
            "reviewed_at": "ISO-8601 datetime",
            "reviewer": "hermes",
            "advisory_only": True,
            "submits_orders": False,
            "max_exit_quantity": "optional advisory cap only when decision=reduce|exit",
            "supporting_factors": ["facts supporting the decision"],
            "opposing_factors": ["facts against the decision"],
            "risk_notes": ["position risk, stale data, concentration, market condition"],
            "follow_up": ["optional manual follow-up items"],
        },
        "hard_rules": [
            "Position judgments are advisory review artifacts only.",
            "Position judgments do not approve trades and must not be consumed by rt_order_intake.py.",
            "Always set advisory_only=true and submits_orders=false.",
            "Copy packet_id and review_id exactly so audits can resolve the packet and position_review item reviewed.",
            "For user role, keep machine-readable decisions to hold or watch; put manual reduce/exit advice only in risk_notes.",
            "For simulation role, reduce/exit/trail_stop remains advisory and still requires a separate gated execution path.",
            "Do not call the simulation API from this judgment path.",
        ],
    }


def strategy_learning_brief(strategy_learning_payload, watchlist_diff_payload=None):
    payload = strategy_learning_payload if isinstance(strategy_learning_payload, dict) else {}
    watchlist_diff_payload = watchlist_diff_payload if isinstance(watchlist_diff_payload, dict) else {}
    intake_coverage = payload.get("intake_coverage") if isinstance(payload.get("intake_coverage"), dict) else {}
    directional_coverage = intake_coverage.get("directional") if isinstance(intake_coverage.get("directional"), dict) else {}
    watch_coverage = intake_coverage.get("watch") if isinstance(intake_coverage.get("watch"), dict) else {}
    sizing_remediation = (
        payload.get("sizing_blocker_remediation")
        if isinstance(payload.get("sizing_blocker_remediation"), dict)
        else {}
    )
    proposal = watchlist_diff_payload.get("proposal") if isinstance(watchlist_diff_payload.get("proposal"), dict) else {}
    sample_scope = payload.get("sample_scope") if isinstance(payload.get("sample_scope"), dict) else {}
    overall = payload.get("overall") if isinstance(payload.get("overall"), dict) else {}
    judgment_effect = payload.get("judgment_effect") if isinstance(payload.get("judgment_effect"), dict) else {}
    approved_effect = (
        judgment_effect.get("approved_or_reduced")
        if isinstance(judgment_effect.get("approved_or_reduced"), dict)
        else {}
    )
    rejected_effect = (
        judgment_effect.get("rejected_or_held")
        if isinstance(judgment_effect.get("rejected_or_held"), dict)
        else {}
    )
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    covered = sizing_remediation.get("covered_by_watchlist_removal_count") or 0
    blocker_count = sizing_remediation.get("sizing_blocker_count") or 0
    uncovered = sizing_remediation.get("uncovered_count") or 0
    if blocker_count and covered == blocker_count and not uncovered:
        remediation_status = "fully_covered_by_manual_watchlist_proposal"
    elif blocker_count and covered:
        remediation_status = "partially_covered_by_manual_watchlist_proposal"
    elif blocker_count:
        remediation_status = "not_covered_by_current_watchlist_proposal"
    else:
        remediation_status = "no_sizing_blockers_in_learning_scope"
    return {
        "schema": "hermes_strategy_learning_brief_v1",
        "read_only": True,
        "submits_orders": False,
        "sample_scope": {
            "mode": sample_scope.get("mode"),
            "strategy_config_id": sample_scope.get("strategy_config_id"),
            "watchlist_id": sample_scope.get("watchlist_id"),
            "joined_signal_count": sample_scope.get("joined_signal_count"),
            "excluded_joined_signal_count": sample_scope.get("excluded_joined_signal_count"),
        },
        "outcome_evidence": {
            "resolved_count": overall.get("resolved_count"),
            "avg_signed_return_pct": overall.get("avg_signed_return_pct"),
            "win_rate_pct": overall.get("win_rate_pct"),
            "minimum_sample_met": (overall.get("resolved_count") or 0) >= 5,
        },
        "intake_coverage": {
            "overall_pct": intake_coverage.get("coverage_pct"),
            "directional_pct": directional_coverage.get("coverage_pct"),
            "directional_joined_signal_count": directional_coverage.get("joined_signal_count"),
            "watch_pct": watch_coverage.get("coverage_pct"),
            "watch_joined_signal_count": watch_coverage.get("joined_signal_count"),
        },
        "judgment_effect": {
            "approved_or_reduced": {
                "resolved_count": approved_effect.get("resolved_count"),
                "avg_signed_return_pct": approved_effect.get("avg_signed_return_pct"),
                "win_rate_pct": approved_effect.get("win_rate_pct"),
            },
            "rejected_or_held": {
                "resolved_count": rejected_effect.get("resolved_count"),
                "avg_signed_return_pct": rejected_effect.get("avg_signed_return_pct"),
                "win_rate_pct": rejected_effect.get("win_rate_pct"),
            },
        },
        "sizing_blocker_remediation": {
            "status": remediation_status,
            "sizing_blocker_count": blocker_count,
            "covered_by_watchlist_removal_count": covered,
            "uncovered_count": uncovered,
            "covered_symbols": sizing_remediation.get("covered_symbols") or [],
            "uncovered_symbols": sizing_remediation.get("uncovered_symbols") or [],
            "watchlist_proposal_hash": sizing_remediation.get("watchlist_proposal_hash"),
            "proposed_watchlist_id": proposal.get("proposed_watchlist_id"),
            "current_watchlist_id": proposal.get("current_watchlist_id"),
            "manual_review_required": proposal.get("manual_review_required", True),
            "auto_applied": proposal.get("auto_applied", False),
            "does_not_restart_services": proposal.get("does_not_restart_services", True),
            "does_not_submit_orders": proposal.get("does_not_submit_orders", True),
        },
        "recommendations": recommendations[:12],
        "hermes_use": [
            "Use this brief to decide whether a signal is ready for judgment review or only useful for diagnostics.",
            "Treat low directional intake coverage as incomplete learning evidence.",
            "Treat sizing blocker remediation as manual watchlist proposal context only; do not apply or restart services from this packet.",
        ],
    }


def simulation_trade_review_brief(portfolio_payload):
    payload = portfolio_payload if isinstance(portfolio_payload, dict) else {}
    reports = payload.get("portfolio_reports") if isinstance(payload.get("portfolio_reports"), list) else []
    sim_report = {}
    for report in reports:
        if isinstance(report, dict) and str(report.get("role") or "").lower() == "simulation":
            sim_report = report
            break
    risk = payload.get("portfolio_risk") if isinstance(payload.get("portfolio_risk"), dict) else {}
    risk_reports = risk.get("reports") if isinstance(risk.get("reports"), list) else []
    sim_risk = {}
    for report in risk_reports:
        if isinstance(report, dict) and str(report.get("role") or "").lower() == "simulation":
            sim_risk = report
            break
    unrealized = sim_risk.get("unrealized_pnl") if isinstance(sim_risk.get("unrealized_pnl"), dict) else {}
    review = (
        payload.get("simulation_trade_review")
        if isinstance(payload.get("simulation_trade_review"), dict)
        else {}
    )
    return {
        "schema": "hermes_simulation_trade_review_brief_v1",
        "read_only": True,
        "submits_orders": False,
        "portfolio_id": sim_report.get("portfolio_id") or review.get("portfolio_id"),
        "total_value_hkd": sim_report.get("total_value_hkd"),
        "return_pct_vs_initial": sim_report.get("return_pct_vs_initial"),
        "unrealized_pnl_pct_of_cost": unrealized.get("unrealized_pnl_pct_of_cost"),
        "lookback_days": review.get("lookback_days"),
        "trade_count": review.get("trade_count"),
        "closed_trade_count": review.get("closed_trade_count"),
        "closed_win_rate_pct": review.get("closed_win_rate_pct"),
        "closed_pnl_hkd_est": review.get("closed_pnl_hkd_est"),
        "largest_loss": review.get("largest_loss"),
        "largest_win": review.get("largest_win"),
        "review_notes": review.get("review_notes") or [],
        "hermes_use": [
            "Use this brief as realized simulation-trade context before approving new exposure.",
            "Do not treat positive paper signal outcomes as sufficient when realized simulation trade review is weak or missing.",
            "This brief is read-only and does not submit orders.",
        ],
    }


def build_packet(
    alerts,
    health_payload=None,
    portfolio_payload=None,
    intake_results=None,
    judgment_file=None,
    source_alerts=None,
    alert_sample_scope=None,
    strategy_evidence_payload=None,
    alert_quality_payload=None,
    alert_event_store_payload=None,
    judgment_event_store_payload=None,
    intake_event_store_payload=None,
    outcome_event_store_payload=None,
    strategy_review_payload=None,
    strategy_learning_payload=None,
    execution_readiness_payload=None,
    simulation_performance_payload=None,
    market_context_payload=None,
    data_health_payload=None,
    universe_payload=None,
    watchlist_diff_payload=None,
    universe_hygiene_payload=None,
    judgment_audit_payload=None,
    position_judgment_file=None,
    position_judgment_audit_payload=None,
):
    judgment_file = judgment_file or intake.JUDGMENT_FILE
    position_judgment_file = position_judgment_file or POSITION_JUDGMENT_FILE
    health_payload = health_payload if health_payload is not None else system_health_check.build_payload()
    portfolio_payload = (
        portfolio_payload
        if portfolio_payload is not None
        else portfolio_report.build_payload(
            sim_portfolio_id=portfolio_report.SIM_PORTFOLIO_ID,
            user_portfolio_ids=portfolio_report.USER_PORTFOLIO_IDS,
        )
    )
    if intake_results is None:
        intake_results = []
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, "rt_order_intake_state.json")
            intake_results = run_intake_dry_runs(alerts, state_file, judgment_file)

    health_status = health_payload.get("status", "UNKNOWN")
    alert_result_pairs = list(zip(alerts, intake_results))
    actionable_pairs = [
        (alert, result)
        for alert, result in alert_result_pairs
        if not is_non_actionable_observation(alert, result)
    ]
    observation_pairs = [
        (alert, result)
        for alert, result in alert_result_pairs
        if is_non_actionable_observation(alert, result)
    ]
    items = [review_item(alert, result, health_status) for alert, result in actionable_pairs]
    observations = [non_actionable_observation(alert, result) for alert, result in observation_pairs]
    source_alerts = source_alerts if source_alerts is not None else alerts
    if strategy_evidence_payload is None:
        strategy_evidence_payload = load_json_file(OUTCOME_REPORT_FILE)
    if alert_quality_payload is None:
        alert_quality_payload = load_json_file(ALERT_QUALITY_REPORT_FILE)
    if alert_event_store_payload is None:
        alert_event_store_payload = load_json_file(ALERT_EVENT_STORE_REPORT_FILE)
    if judgment_event_store_payload is None:
        judgment_event_store_payload = load_json_file(JUDGMENT_EVENT_STORE_REPORT_FILE)
    if intake_event_store_payload is None:
        intake_event_store_payload = load_json_file(INTAKE_EVENT_STORE_REPORT_FILE)
    if outcome_event_store_payload is None:
        outcome_event_store_payload = load_json_file(OUTCOME_EVENT_STORE_REPORT_FILE)
    if strategy_review_payload is None:
        strategy_review_payload = load_json_file(STRATEGY_REVIEW_REPORT_FILE)
    if strategy_learning_payload is None:
        strategy_learning_payload = load_json_file(STRATEGY_LEARNING_REPORT_FILE)
    if execution_readiness_payload is None:
        execution_readiness_payload = load_json_file(EXECUTION_READINESS_REPORT_FILE)
    if simulation_performance_payload is None:
        simulation_performance_payload = load_json_file(SIMULATION_PERFORMANCE_REPORT_FILE)
    if market_context_payload is None:
        market_context_payload = load_json_file(MARKET_CONTEXT_FILE)
    if data_health_payload is None:
        data_health_payload = load_json_file(DATA_HEALTH_REPORT_FILE)
    if universe_payload is None:
        universe_payload = load_json_file(UNIVERSE_REPORT_FILE)
    if watchlist_diff_payload is None:
        watchlist_diff_payload = load_json_file(WATCHLIST_DIFF_REPORT_FILE)
    if universe_hygiene_payload is None:
        universe_hygiene_payload = load_json_file(UNIVERSE_HYGIENE_REPORT_FILE)
    if judgment_audit_payload is None:
        judgment_audit_payload = load_json_file(JUDGMENT_AUDIT_FILE)
    if position_judgment_audit_payload is None:
        position_judgment_audit_payload = load_json_file(POSITION_JUDGMENT_AUDIT_FILE)
    items = apply_portfolio_risk_to_items(items, portfolio_payload)
    items = apply_data_health_to_items(items, data_health_payload)
    return {
        "schema": "hermes_signal_review_packet_v1",
        "packet_id": packet_id_for(alerts, health_payload),
        "generated_at": now_iso(),
        "execution_safety": {
            "review_only": True,
            "submits_orders": False,
            "intake_mode": "dry-run",
            "execute_path": "rt_order_intake.py --mode execute, gated by matching Hermes judgment",
        },
        "health": health_payload,
        "portfolio_context": portfolio_payload,
        "portfolio_risk": portfolio_payload.get("portfolio_risk", {}),
        "position_review": portfolio_payload.get("position_review", {}),
        "market_context": market_context_payload,
        "data_health": data_health_payload,
        "universe_context": universe_payload,
        "watchlist_diff": watchlist_diff_payload,
        "universe_hygiene": universe_hygiene_payload,
        "strategy_evidence": strategy_evidence_payload,
        "alert_quality_summary": alert_quality_payload,
        "alert_event_store": alert_event_store_payload,
        "judgment_event_store": judgment_event_store_payload,
        "order_intake_event_store": intake_event_store_payload,
        "signal_outcome_event_store": outcome_event_store_payload,
        "strategy_review": strategy_review_payload,
        "strategy_learning": strategy_learning_payload,
        "strategy_learning_brief": strategy_learning_brief(strategy_learning_payload, watchlist_diff_payload),
        "simulation_trade_review_brief": simulation_trade_review_brief(portfolio_payload),
        "execution_readiness": execution_readiness_payload,
        "simulation_performance": simulation_performance_payload,
        "judgment_audit": judgment_audit_payload,
        "position_judgment_audit": position_judgment_audit_payload,
        "alert_selection": alert_selection_stats(source_alerts, alerts, sample_scope=alert_sample_scope),
        "review_items": items,
        "non_actionable_observations": observations,
        "non_actionable_observation_count": len(observations),
        "judgment_contract": judgment_contract(judgment_file),
        "position_judgment_contract": position_judgment_contract(position_judgment_file),
        "operator_notes": [
            "This packet is the input for Hermes judgment, not an execution command.",
            "Hermes should copy packet_id into every judgment so audits can resolve the exact packet version reviewed.",
            "Hermes should write judgments only for review_items that it explicitly reviewed.",
            "non_actionable_observations are visible for learning and diagnostics only; Hermes should not write trade judgments for them.",
            "Hermes may write position judgments for position_review.items, but those judgments are advisory and never submit orders.",
            "Universe context is for watchlist quality review only; do not auto-apply candidate watchlists without operator review.",
            "Watchlist diff is read-only proposal context; do not replace the live watchlist without manual review and service restart planning.",
            "Universe hygiene is for active-stock data quality review only; do not deactivate symbols without operator review.",
            "Alert quality summary is read-only session diagnostics; it does not approve execution.",
            "Alert event store status is read-only durability/audit context; missing or dry-run status should not block packet generation but means alert history still depends on JSONL retention.",
            "Judgment event store status is read-only durability/audit context; Hermes should still write judgments to the JSONL contract and let the store persist them.",
            "Order intake event store status is read-only durability/audit context; it must not be interpreted as execution permission.",
            "Signal outcome event store status is read-only durability/audit context; strategy evidence gates still read rt_signal_outcome_report.json.",
            "Strategy review is read-only trigger policy context; execute mode still requires rt_order_intake.py gates.",
            "Strategy learning is read-only cohort evidence for improving prompts, triggers, and review discipline; it does not approve execution.",
            "Strategy learning brief is a top-level summary for Hermes attention only; the full strategy_learning object remains authoritative.",
            "Simulation trade review brief is realized simulation portfolio context; it does not approve execution by itself.",
            "Execution readiness is read-only dashboard context; READY is necessary but not sufficient for execute mode.",
            "Simulation performance is read-only attribution context; FAIL means recent simulation behavior does not support new exposure.",
            "Data health is read-only integrity context; FAIL means Hermes should reject or hold until K-line, signal, or feature-run evidence is repaired.",
            "Critical simulation portfolio_risk means the portfolio state is not trustworthy enough for approval.",
            "High-urgency position_review items should be handled before new BUY exposure is approved.",
            "Simulation execution remains disabled until the bridge is switched to alert-sim and execute gates pass.",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alert-json", help="one alert JSON object or list")
    parser.add_argument("--alert-file", help="JSON file containing one alert object or list")
    parser.add_argument("--queue-file", help="JSONL alert queue; defaults to RT_ALERT_QUEUE_FILE")
    parser.add_argument("--limit", type=int, default=DEFAULT_REVIEW_LIMIT, help="max review items after filtering")
    parser.add_argument(
        "--queue-scan-limit",
        type=int,
        default=DEFAULT_QUEUE_SCAN_LIMIT,
        help="max raw JSONL tail lines to scan before selecting review items",
    )
    parser.add_argument("--include-watch", action="store_true", help="include WATCH alerts in review_items")
    parser.add_argument(
        "--include-unconfirmed",
        action="store_true",
        help="include unconfirmed BUY/SELL alerts in review_items",
    )
    parser.add_argument("--sample-scope", choices=("current", "all"), default="current")
    parser.add_argument("--state-file", default=intake.STATE_FILE)
    parser.add_argument("--judgment-file", default=intake.JUDGMENT_FILE)
    parser.add_argument("--outcome-report-file", default=OUTCOME_REPORT_FILE)
    parser.add_argument("--alert-quality-file", default=ALERT_QUALITY_REPORT_FILE)
    parser.add_argument("--alert-event-store-file", default=ALERT_EVENT_STORE_REPORT_FILE)
    parser.add_argument("--judgment-event-store-file", default=JUDGMENT_EVENT_STORE_REPORT_FILE)
    parser.add_argument("--intake-event-store-file", default=INTAKE_EVENT_STORE_REPORT_FILE)
    parser.add_argument("--outcome-event-store-file", default=OUTCOME_EVENT_STORE_REPORT_FILE)
    parser.add_argument("--strategy-review-file", default=STRATEGY_REVIEW_REPORT_FILE)
    parser.add_argument("--strategy-learning-file", default=STRATEGY_LEARNING_REPORT_FILE)
    parser.add_argument("--simulation-performance-file", default=SIMULATION_PERFORMANCE_REPORT_FILE)
    parser.add_argument("--market-context-file", default=MARKET_CONTEXT_FILE)
    parser.add_argument("--data-health-file", default=DATA_HEALTH_REPORT_FILE)
    parser.add_argument("--universe-report-file", default=UNIVERSE_REPORT_FILE)
    parser.add_argument("--watchlist-diff-file", default=WATCHLIST_DIFF_REPORT_FILE)
    parser.add_argument("--universe-hygiene-file", default=UNIVERSE_HYGIENE_REPORT_FILE)
    parser.add_argument("--execution-readiness-file", default=EXECUTION_READINESS_REPORT_FILE)
    parser.add_argument("--judgment-audit-file", default=JUDGMENT_AUDIT_FILE)
    parser.add_argument("--position-judgment-file", default=POSITION_JUDGMENT_FILE)
    parser.add_argument("--position-judgment-audit-file", default=POSITION_JUDGMENT_AUDIT_FILE)
    parser.add_argument("--output", default=PACKET_FILE)
    parser.add_argument("--archive-dir", default=PACKET_ARCHIVE_DIR)
    parser.add_argument("--no-archive", action="store_true", help="do not write packet snapshot archive")
    parser.add_argument("--stdout", action="store_true", help="print packet JSON to stdout")
    parser.add_argument(
        "--ephemeral-state",
        action="store_true",
        help="do not persist dry-run decisions to the intake dry_runs ledger",
    )
    parser.add_argument("--review-days", type=int, default=30)
    parser.add_argument("--sim-portfolio-id", type=int, default=portfolio_report.SIM_PORTFOLIO_ID)
    parser.add_argument("--user-portfolio-id", action="append", type=int, default=[])
    return parser.parse_args()


def main():
    args = parse_args()
    source_alerts = load_source_alerts(args.alert_json, args.alert_file, args.queue_file, args.queue_scan_limit)
    _scoped_source_alerts, alert_sample_scope = apply_sample_scope(source_alerts, sample_scope_mode=args.sample_scope)
    alerts = select_review_alerts(
        source_alerts,
        limit=args.limit,
        include_watch=args.include_watch,
        include_unconfirmed=args.include_unconfirmed,
        sample_scope_mode=args.sample_scope,
    )
    health_payload = system_health_check.build_payload()
    portfolio_payload = portfolio_report.build_payload(
        sim_portfolio_id=args.sim_portfolio_id,
        user_portfolio_ids=args.user_portfolio_id or portfolio_report.USER_PORTFOLIO_IDS,
        review_days=args.review_days,
    )

    if args.ephemeral_state:
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, "rt_order_intake_state.json")
            intake_results = run_intake_dry_runs(alerts, state_file, args.judgment_file)
    else:
        intake_results = run_intake_dry_runs(alerts, args.state_file, args.judgment_file)

    packet = build_packet(
        alerts,
        health_payload=health_payload,
        portfolio_payload=portfolio_payload,
        intake_results=intake_results,
        judgment_file=args.judgment_file,
        source_alerts=source_alerts,
        alert_sample_scope=alert_sample_scope,
        strategy_evidence_payload=load_json_file(args.outcome_report_file),
        alert_quality_payload=load_json_file(args.alert_quality_file),
        alert_event_store_payload=load_json_file(args.alert_event_store_file),
        judgment_event_store_payload=load_json_file(args.judgment_event_store_file),
        intake_event_store_payload=load_json_file(args.intake_event_store_file),
        outcome_event_store_payload=load_json_file(args.outcome_event_store_file),
        strategy_review_payload=load_json_file(args.strategy_review_file),
        strategy_learning_payload=load_json_file(args.strategy_learning_file),
        execution_readiness_payload=load_json_file(args.execution_readiness_file),
        simulation_performance_payload=load_json_file(args.simulation_performance_file),
        market_context_payload=load_json_file(args.market_context_file),
        data_health_payload=load_json_file(args.data_health_file),
        universe_payload=load_json_file(args.universe_report_file),
        watchlist_diff_payload=load_json_file(args.watchlist_diff_file),
        universe_hygiene_payload=load_json_file(args.universe_hygiene_file),
        judgment_audit_payload=load_json_file(args.judgment_audit_file),
        position_judgment_file=args.position_judgment_file,
        position_judgment_audit_payload=load_json_file(args.position_judgment_audit_file),
    )
    if args.output:
        save_json_atomic(args.output, packet)
    if not args.no_archive:
        archive_packet(packet, args.archive_dir)
    if args.stdout or not args.output:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
