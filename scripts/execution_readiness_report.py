#!/usr/bin/env python3
"""Read-only execution readiness dashboard for the v5/Hermes stack."""
import argparse
import json
import os
from datetime import datetime


SYSTEM_HEALTH_FILE = os.environ.get("SYSTEM_HEALTH_REPORT_FILE", "/tmp/quantmind_system_health.json")
DATA_HEALTH_FILE = os.environ.get("DATA_HEALTH_REPORT_FILE", "/tmp/data_health_report.json")
OUTCOME_REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
STRATEGY_LEARNING_FILE = os.environ.get("STRATEGY_LEARNING_REPORT_FILE", "/tmp/strategy_learning_report.json")
PORTFOLIO_REPORT_FILE = os.environ.get("PORTFOLIO_REPORT_FILE", "/tmp/portfolio_report.json")
WATCHLIST_DIFF_FILE = os.environ.get("WATCHLIST_DIFF_REPORT_FILE", "/tmp/watchlist_diff_report.json")
MARKET_CONTEXT_FILE = os.environ.get("MARKET_CONTEXT_REPORT_FILE", "/tmp/market_context_report.json")
ALERT_QUALITY_FILE = os.environ.get("ALERT_QUALITY_REPORT_FILE", "/tmp/rt_alert_quality_report.json")
JUDGMENT_AUDIT_FILE = os.environ.get("HERMES_JUDGMENT_AUDIT_FILE", "/tmp/hermes_judgment_audit_report.json")
SIMULATION_PERFORMANCE_FILE = os.environ.get(
    "SIMULATION_PERFORMANCE_REPORT_FILE",
    "/tmp/simulation_performance_report.json",
)
POSITION_JUDGMENT_AUDIT_FILE = os.environ.get(
    "HERMES_POSITION_JUDGMENT_AUDIT_FILE",
    "/tmp/hermes_position_judgment_audit_report.json",
)
CRON_AUDIT_FILE = os.environ.get("CRON_AUDIT_REPORT_FILE", "/tmp/cron_audit_report.json")
ORDER_INTAKE_EVENT_STORE_FILE = os.environ.get(
    "RT_ORDER_INTAKE_EVENT_STORE_REPORT_FILE",
    "/tmp/rt_order_intake_event_store_report.json",
)
REPORT_FILE = os.environ.get("EXECUTION_READINESS_REPORT_FILE", "/tmp/execution_readiness_report.json")
MIN_RESOLVED_OUTCOMES = int(os.environ.get("EXECUTION_READINESS_MIN_RESOLVED_OUTCOMES", "5"))
MIN_DIRECTIONAL_INTAKE_COVERAGE_PCT = float(
    os.environ.get("EXECUTION_READINESS_MIN_DIRECTIONAL_INTAKE_COVERAGE_PCT", "80")
)
MIN_WIN_RATE_PCT = float(os.environ.get("EXECUTION_READINESS_MIN_WIN_RATE_PCT", "50"))
MAX_STOP_HIT_RATE_PCT = float(os.environ.get("EXECUTION_READINESS_MAX_STOP_HIT_RATE_PCT", "50"))
MIN_STOP_TARGET_IMBALANCE_BLOCK_PCT = float(
    os.environ.get("EXECUTION_READINESS_MIN_STOP_TARGET_IMBALANCE_BLOCK_PCT", "5")
)
MIN_FAVORABLE_TO_ADVERSE_RATIO = float(
    os.environ.get("EXECUTION_READINESS_MIN_FAVORABLE_TO_ADVERSE_RATIO", "1")
)
MIN_HERMES_EFFECT_SAMPLE = int(os.environ.get("EXECUTION_READINESS_MIN_HERMES_EFFECT_SAMPLE", "5"))
MIN_SIM_CLOSED_TRADES = int(os.environ.get("EXECUTION_READINESS_MIN_SIM_CLOSED_TRADES", "3"))
MIN_SIM_RETURN_PCT = float(os.environ.get("EXECUTION_READINESS_MIN_SIM_RETURN_PCT", "0"))
MIN_SIM_UNREALIZED_PNL_PCT = float(os.environ.get("EXECUTION_READINESS_MIN_SIM_UNREALIZED_PNL_PCT", "-5"))
MAX_REPORT_AGE_MINUTES = float(os.environ.get("EXECUTION_READINESS_MAX_REPORT_AGE_MINUTES", "90"))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


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


def report_timestamp(payload, keys=("generated_at", "checked_at", "updated_at")):
    payload = payload if isinstance(payload, dict) else {}
    for key in keys:
        parsed = parse_timestamp(payload.get(key))
        if parsed:
            return key, parsed, payload.get(key)
    return None, None, None


def freshness_summary(reports, now=None, max_age_minutes=MAX_REPORT_AGE_MINUTES):
    now = now or datetime.now()
    rows = []
    stale_or_missing = []
    for name, payload, keys in reports:
        key, ts, raw = report_timestamp(payload, keys=keys)
        if ts is None:
            row = {
                "report": name,
                "status": "missing_timestamp",
                "timestamp_key": None,
                "timestamp": None,
                "age_minutes": None,
                "max_age_minutes": max_age_minutes,
            }
            stale_or_missing.append(row)
            rows.append(row)
            continue
        age = round((now - ts).total_seconds() / 60, 2)
        status = "fresh" if age <= max_age_minutes and age >= -5 else "stale"
        row = {
            "report": name,
            "status": status,
            "timestamp_key": key,
            "timestamp": raw,
            "age_minutes": age,
            "max_age_minutes": max_age_minutes,
        }
        if status != "fresh":
            stale_or_missing.append(row)
        rows.append(row)
    return rows, stale_or_missing


def add_gate(gates, gate, status, detail, data=None):
    gates.append({"gate": gate, "status": status, "detail": detail, "data": data or {}})


def report_status_gate_status(payload, expected_schema, ok_status="OK"):
    if not isinstance(payload, dict) or payload.get("schema") != expected_schema:
        return "BLOCK"
    status = str(payload.get("status") or "MISSING").upper()
    if status == ok_status:
        return "PASS"
    if status == "WARN":
        return "WARN"
    return "BLOCK"


def worst_status(gates):
    statuses = [gate.get("status") for gate in gates]
    if "BLOCK" in statuses:
        return "BLOCKED"
    if "WARN" in statuses:
        return "WARN"
    return "READY"


def latest_outcome_counts(outcome_report, strategy_learning):
    learning_overall = strategy_learning.get("overall") if isinstance(strategy_learning.get("overall"), dict) else {}
    resolved = learning_overall.get("resolved_count")
    if resolved is None:
        resolved = outcome_report.get("resolved_signal_count")
    primary_metric = (
        outcome_report.get("primary_horizon_metric")
        if isinstance(outcome_report.get("primary_horizon_metric"), dict)
        else {}
    )
    avg_signed_return = learning_overall.get("avg_signed_return_pct")
    if avg_signed_return is None:
        avg_signed_return = primary_metric.get("avg_signed_close_return_pct")
    win_rate = learning_overall.get("win_rate_pct")
    if win_rate is None:
        win_rate = primary_metric.get("win_rate_pct")
    target_hit_rate = primary_metric.get("target_hit_rate_pct")
    stop_hit_rate = primary_metric.get("stop_hit_rate_pct")
    favorable_to_adverse_ratio = primary_metric.get("favorable_to_adverse_ratio")
    pending = outcome_report.get("pending_signal_count")
    evaluated = outcome_report.get("evaluated_signal_count")
    return {
        "resolved_count": int(resolved or 0),
        "pending_signal_count": int(pending or 0),
        "evaluated_signal_count": int(evaluated or 0),
        "avg_signed_return_pct": avg_signed_return,
        "win_rate_pct": win_rate,
        "target_hit_rate_pct": target_hit_rate,
        "stop_hit_rate_pct": stop_hit_rate,
        "favorable_to_adverse_ratio": favorable_to_adverse_ratio,
    }


def diagnostic_candidate_outcome_count(outcome_report):
    if not isinstance(outcome_report, dict):
        return 0
    counts = outcome_report.get("counts") if isinstance(outcome_report.get("counts"), dict) else {}
    for value in (
        outcome_report.get("downgraded_directional_alert_count"),
        counts.get("downgraded_directional_alert_count"),
    ):
        try:
            count = int(float(value or 0))
            if count > 0:
                return count
        except (TypeError, ValueError):
            continue
    return 0


def outcome_horizon_metric(section, horizon):
    if not isinstance(section, dict):
        return {}
    horizons = section.get("horizons") if isinstance(section.get("horizons"), dict) else {}
    metric = horizons.get(horizon)
    return metric if isinstance(metric, dict) else {}


def executable_only_outcome_metric(outcome_report, horizon):
    if not isinstance(outcome_report, dict):
        return {}
    containers = [
        outcome_report.get("execution_candidate_overall"),
        outcome_report.get("overall_execution_candidate"),
    ]
    execution_candidate = outcome_report.get("execution_candidate")
    if isinstance(execution_candidate, dict):
        containers.append(execution_candidate.get("overall"))
    overall = outcome_report.get("overall")
    if isinstance(overall, dict) and (
        overall.get("execution_candidate") is True
        or str(overall.get("scope") or "").strip().lower() in ("execution_candidate", "execution_candidate_only")
    ):
        containers.append(overall)
    for container in containers:
        metric = outcome_horizon_metric(container, horizon)
        if metric:
            return metric
    return {}


def simulation_risk_reports(portfolio_report):
    risk = portfolio_report.get("portfolio_risk") if isinstance(portfolio_report.get("portfolio_risk"), dict) else {}
    reports = risk.get("reports") if isinstance(risk.get("reports"), list) else []
    return [
        report
        for report in reports
        if isinstance(report, dict) and str(report.get("role") or "").lower() == "simulation"
    ]


def simulation_portfolio_report(portfolio_report):
    reports = portfolio_report.get("portfolio_reports") if isinstance(portfolio_report.get("portfolio_reports"), list) else []
    for report in reports:
        if isinstance(report, dict) and str(report.get("role") or "").lower() == "simulation":
            return report
    return {}


def judgment_effect_metrics(strategy_learning):
    execution_scope = (
        strategy_learning.get("execution_candidate_scope")
        if isinstance(strategy_learning.get("execution_candidate_scope"), dict)
        else {}
    )
    execution_audit_effect = (
        strategy_learning.get("execution_candidate_audit_pass_judgment_effect")
        if isinstance(strategy_learning.get("execution_candidate_audit_pass_judgment_effect"), dict)
        else {}
    )
    audit_effect = (
        strategy_learning.get("audit_pass_judgment_effect")
        if isinstance(strategy_learning.get("audit_pass_judgment_effect"), dict)
        else {}
    )
    raw_effect = (
        strategy_learning.get("judgment_effect")
        if isinstance(strategy_learning.get("judgment_effect"), dict)
        else {}
    )
    downgraded_directional_count = int(execution_scope.get("downgraded_directional_count") or 0)
    use_execution_candidate_effect = downgraded_directional_count > 0 and bool(execution_audit_effect)
    execution_candidate_effect_required = downgraded_directional_count > 0
    execution_candidate_effect_missing = execution_candidate_effect_required and not execution_audit_effect
    effect = execution_audit_effect if use_execution_candidate_effect else audit_effect or raw_effect
    approved = effect.get("approved_or_reduced") if isinstance(effect.get("approved_or_reduced"), dict) else {}
    rejected = effect.get("rejected_or_held") if isinstance(effect.get("rejected_or_held"), dict) else {}
    return {
        "sample_filter": effect.get("sample_filter") or ("judgment_audit_status_PASS" if audit_effect else "raw_judgment_decision"),
        "downgraded_directional_count": downgraded_directional_count,
        "uses_execution_candidate_audit_pass_judgment_effect": use_execution_candidate_effect,
        "execution_candidate_audit_pass_judgment_effect_required": execution_candidate_effect_required,
        "execution_candidate_audit_pass_judgment_effect_missing": execution_candidate_effect_missing,
        "uses_audit_pass_judgment_effect": bool(audit_effect),
        "execution_candidate_audit_pass_judgment_effect_present": bool(execution_audit_effect),
        "raw_judgment_effect_present": bool(raw_effect),
        "approved_resolved_count": int(approved.get("resolved_count") or 0),
        "approved_avg_signed_return_pct": approved.get("avg_signed_return_pct"),
        "approved_win_rate_pct": approved.get("win_rate_pct"),
        "rejected_resolved_count": int(rejected.get("resolved_count") or 0),
        "rejected_avg_signed_return_pct": rejected.get("avg_signed_return_pct"),
        "rejected_win_rate_pct": rejected.get("win_rate_pct"),
    }


def simulation_trade_review_metrics(portfolio_report):
    review = (
        portfolio_report.get("simulation_trade_review")
        if isinstance(portfolio_report.get("simulation_trade_review"), dict)
        else {}
    )
    notes = review.get("review_notes") if isinstance(review.get("review_notes"), list) else []
    return {
        "lookback_days": review.get("lookback_days"),
        "trade_count": int(review.get("trade_count") or 0),
        "closed_trade_count": int(review.get("closed_trade_count") or 0),
        "closed_win_rate_pct": review.get("closed_win_rate_pct"),
        "closed_pnl_hkd_est": review.get("closed_pnl_hkd_est"),
        "largest_loss": review.get("largest_loss"),
        "largest_win": review.get("largest_win"),
        "review_notes": notes,
    }


def compact_simulation_remediation(simulation_performance):
    remediation = (
        simulation_performance.get("remediation_plan")
        if isinstance(simulation_performance.get("remediation_plan"), dict)
        else {}
    )
    contract = (
        remediation.get("operator_contract")
        if isinstance(remediation.get("operator_contract"), dict)
        else {}
    )
    actions = remediation.get("actions") if isinstance(remediation.get("actions"), list) else []
    return {
        "status": remediation.get("status"),
        "proposal_hash": remediation.get("proposal_hash"),
        "manual_review_required": remediation.get("manual_review_required"),
        "auto_applied": remediation.get("auto_applied"),
        "action_ids": [row.get("action_id") for row in actions[:12] if isinstance(row, dict) and row.get("action_id")],
        "operator_contract": {
            "read_only": contract.get("read_only"),
            "submits_orders": contract.get("submits_orders"),
            "changes_execution_mode": contract.get("changes_execution_mode"),
            "changes_strategy_config": contract.get("changes_strategy_config"),
            "changes_watchlists": contract.get("changes_watchlists"),
            "requires_operator_review_before_promotion": contract.get("requires_operator_review_before_promotion"),
        },
    }


def compact_simulation_failure_postmortem(simulation_performance):
    postmortem = (
        simulation_performance.get("failure_postmortem")
        if isinstance(simulation_performance.get("failure_postmortem"), dict)
        else {}
    )
    hypotheses = postmortem.get("hypotheses") if isinstance(postmortem.get("hypotheses"), list) else []
    required_record = (
        postmortem.get("required_learning_record")
        if isinstance(postmortem.get("required_learning_record"), dict)
        else {}
    )
    return {
        "status": postmortem.get("status"),
        "diagnostics": postmortem.get("diagnostics") if isinstance(postmortem.get("diagnostics"), dict) else {},
        "hypothesis_ids": [row.get("id") for row in hypotheses[:12] if isinstance(row, dict) and row.get("id")],
        "required_learning_record_fields": required_record.get("required_fields") or [],
    }


def market_context_summary(market_context):
    markets = market_context.get("markets") if isinstance(market_context.get("markets"), dict) else {}
    rows = []
    risk_off = []
    high_risk = []
    for market, summary in sorted(markets.items()):
        if not isinstance(summary, dict):
            continue
        row = {
            "market": market,
            "regime": summary.get("regime"),
            "risk_level": summary.get("risk_level"),
            "latest_date": summary.get("latest_date"),
            "notes": summary.get("notes") or [],
        }
        if row["regime"] == "risk_off":
            risk_off.append(market)
        if row["risk_level"] == "high":
            high_risk.append(market)
        rows.append(row)
    return {
        "schema": market_context.get("schema"),
        "market_count": len(rows),
        "markets": rows,
        "risk_off_markets": risk_off,
        "high_risk_markets": high_risk,
    }


def cron_audit_gate_payload(cron_audit):
    cron_audit = cron_audit if isinstance(cron_audit, dict) else {}
    summary = cron_audit.get("summary") if isinstance(cron_audit.get("summary"), dict) else {}
    installation_plan = (
        cron_audit.get("installation_plan")
        if isinstance(cron_audit.get("installation_plan"), dict)
        else {}
    )
    limited_pilot = cron_audit.get("limited_pilot_execution_jobs")
    if not isinstance(limited_pilot, list):
        limited_pilot = []
    dangerous = cron_audit.get("dangerous_enabled_jobs")
    if not isinstance(dangerous, list):
        dangerous = []
    missing = cron_audit.get("missing_required_jobs")
    if not isinstance(missing, list):
        missing = []
    return {
        "schema": cron_audit.get("schema"),
        "status": cron_audit.get("status"),
        "generated_at": cron_audit.get("generated_at"),
        "missing_required_job_count": as_int(summary.get("missing_required_job_count"), len(missing)),
        "dangerous_enabled_count": as_int(summary.get("dangerous_enabled_count"), len(dangerous)),
        "limited_pilot_execution_count": as_int(
            summary.get("limited_pilot_execution_count"),
            len(limited_pilot),
        ),
        "limited_pilot_execution_jobs": [
            {
                "mode": row.get("mode"),
                "broker": row.get("broker"),
                "safety_contract": row.get("safety_contract") if isinstance(row.get("safety_contract"), dict) else {},
            }
            for row in limited_pilot[:5]
            if isinstance(row, dict)
        ],
        "installation_plan": {
            "schema": installation_plan.get("schema"),
            "status": installation_plan.get("status"),
            "proposal_hash": installation_plan.get("proposal_hash"),
            "manual_review_required": bool(installation_plan.get("manual_review_required")),
            "install_line_count": as_int(installation_plan.get("install_line_count"), 0),
            "rejected_line_count": as_int(installation_plan.get("rejected_line_count"), 0),
        },
        "recommendations": cron_audit.get("recommendations") or [],
    }


def order_intake_broker_reconciliation_payload(order_intake_event_store):
    order_intake_event_store = order_intake_event_store if isinstance(order_intake_event_store, dict) else {}
    recon = (
        order_intake_event_store.get("broker_reconciliation")
        if isinstance(order_intake_event_store.get("broker_reconciliation"), dict)
        else {}
    )
    lineage = (
        order_intake_event_store.get("lineage_summary")
        if isinstance(order_intake_event_store.get("lineage_summary"), dict)
        else {}
    )
    return {
        "schema": recon.get("schema"),
        "status": recon.get("status") or "MISSING",
        "broker": recon.get("broker"),
        "broker_order_count": as_int(recon.get("broker_order_count"), 0),
        "matched_order_count": as_int(recon.get("matched_order_count"), 0),
        "unmatched_broker_order_count": as_int(recon.get("unmatched_broker_order_count"), 0),
        "state_submitted_count": as_int(recon.get("state_submitted_count"), 0),
        "state_order_id_count": as_int(recon.get("state_order_id_count"), 0),
        "sample_state_order_ids": recon.get("sample_state_order_ids") or [],
        "unmatched_broker_orders": recon.get("unmatched_broker_orders") or [],
        "reason_codes": recon.get("reason_codes") or [],
        "lineage_status": lineage.get("status"),
        "lineage_submitted_count": as_int(lineage.get("submitted_count"), 0),
        "generated_at": order_intake_event_store.get("generated_at"),
        "read_only": recon.get("read_only", True),
    }


BOOTSTRAP_SIMULATION_REASON_CODES = {
    "simulation_closed_trade_sample_missing",
    "simulation_closed_trade_lineage_legacy_or_external",
    "closed_trade_signal_traceability_missing",
}

HARD_SIMULATION_REASON_CODES = {
    "simulation_total_return_not_positive",
    "simulation_closed_pnl_not_positive",
    "simulation_closed_win_rate_too_low",
    "simulation_trade_review_blocking_notes",
    "simulation_portfolio_risk_high",
    "worst_closed_symbols_negative",
}


def compact_pilot_gate(gate, reason):
    return {
        "gate": gate.get("gate"),
        "status": gate.get("status"),
        "reason": reason,
        "detail": gate.get("detail"),
    }


def bootstrap_blocker_reason(gate):
    if gate.get("status") != "BLOCK":
        return None
    name = gate.get("gate")
    detail = str(gate.get("detail") or "")
    data = gate.get("data") if isinstance(gate.get("data"), dict) else {}

    if name == "forward_outcome_evidence":
        if "resolved outcomes" in detail:
            return "needs_forward_outcome_sample"
        if "executable-only cohort is missing" in detail:
            return "needs_executable_only_forward_outcome_cohort"
        return None

    if name == "directional_intake_coverage":
        return "needs_directional_intake_coverage_sample"

    if name == "hermes_judgment_effect":
        if "resolved sample" in detail or "comparison sample" in detail:
            return "needs_hermes_judgment_effect_sample"
        if "executable-only audit-pass" in detail:
            return "needs_executable_only_hermes_effect_cohort"
        return None

    if name == "simulation_trade_review" and "closed trade sample" in detail:
        return "needs_closed_trade_sample"

    if name == "simulation_performance_attribution":
        reason_codes = set(data.get("reason_codes") or [])
        evidence = data.get("v5_hermes_evidence") if isinstance(data.get("v5_hermes_evidence"), dict) else {}
        promotion_blockers = set(evidence.get("promotion_blockers") or [])
        if reason_codes & HARD_SIMULATION_REASON_CODES:
            return None
        if reason_codes and reason_codes <= BOOTSTRAP_SIMULATION_REASON_CODES:
            return "needs_v5_hermes_traceable_simulation_lineage"
        if promotion_blockers & {
            "no_v5_hermes_traceable_closed_trade_sample",
            "closed_trade_lineage_not_full_scope",
        }:
            return "needs_v5_hermes_traceable_simulation_sample"
    return None


def hard_pilot_blocker_reason(gate):
    status = gate.get("status")
    name = gate.get("gate")
    data = gate.get("data") if isinstance(gate.get("data"), dict) else {}
    if status == "BLOCK" and bootstrap_blocker_reason(gate):
        return None
    if status == "BLOCK":
        return f"{name}_blocks_evidence_pilot"
    if name == "market_context" and status == "WARN":
        if data.get("risk_off_markets") or data.get("high_risk_markets"):
            return "market_risk_off_or_high_risk_blocks_evidence_pilot"
    if name == "simulation_portfolio_risk" and status == "WARN":
        if as_int(data.get("high_count"), 0) > 0:
            return "simulation_portfolio_high_risk_blocks_evidence_pilot"
    return None


def build_pilot_readiness(gates, full_ready):
    bootstrap_blockers = []
    hard_blockers = []
    warnings = []
    for gate in gates:
        bootstrap_reason = bootstrap_blocker_reason(gate)
        if bootstrap_reason:
            bootstrap_blockers.append(compact_pilot_gate(gate, bootstrap_reason))
            continue
        hard_reason = hard_pilot_blocker_reason(gate)
        if hard_reason:
            hard_blockers.append(compact_pilot_gate(gate, hard_reason))
        elif gate.get("status") == "WARN":
            warnings.append(compact_pilot_gate(gate, "operator_review_required"))

    if full_ready:
        status = "FULL_EXECUTION_READY"
        reviewable = True
        detail = "full execution readiness is already READY; evidence pilot is not required"
    elif hard_blockers:
        status = "HARD_BLOCKED"
        reviewable = False
        detail = "hard safety blockers must be resolved before any paper/sim evidence pilot is reviewed"
    elif bootstrap_blockers:
        status = "EVIDENCE_PILOT_REVIEWABLE"
        reviewable = True
        detail = "only bootstrap evidence blockers remain; a separately approved capped paper/sim pilot can be reviewed"
    else:
        status = "OPERATOR_REVIEW_REQUIRED"
        reviewable = False
        detail = "no hard blockers were found, but readiness is not READY and there is no pure bootstrap-evidence path"

    return {
        "schema": "execution_readiness_pilot_gate_v1",
        "status": status,
        "paper_pilot_reviewable": reviewable,
        "detail": detail,
        "does_not_change_ready_for_execute": True,
        "submits_orders": False,
        "changes_execution_mode": False,
        "requires_manual_enable": True,
        "full_execution_ready": full_ready,
        "hard_blockers": hard_blockers,
        "bootstrap_blockers": bootstrap_blockers,
        "warning_gates": warnings,
        "required_controls": [
            "pilot must remain paper/simulation only",
            "pilot must use explicit RT_ORDER_EXECUTE_PILOT_ENABLED=1 and capped notional/risk/daily order limits",
            "pilot must still require packet eligibility, Hermes judgment, readiness visibility, market context, and broker reconciliation",
            "pilot evidence must be traceable through rt_order_intake state before promotion",
            "this report field is advisory and rt_order_intake.py does not consume it as an execution bypass",
        ],
    }


def build_report(
    system_health=None,
    data_health=None,
    outcome_report=None,
    strategy_learning=None,
    portfolio_report=None,
    watchlist_diff=None,
    market_context=None,
    alert_quality=None,
    judgment_audit=None,
    simulation_performance=None,
    position_judgment_audit=None,
    cron_audit=None,
    order_intake_event_store=None,
    min_resolved_outcomes=MIN_RESOLVED_OUTCOMES,
    min_directional_intake_coverage_pct=MIN_DIRECTIONAL_INTAKE_COVERAGE_PCT,
    min_win_rate_pct=MIN_WIN_RATE_PCT,
    max_stop_hit_rate_pct=MAX_STOP_HIT_RATE_PCT,
    min_stop_target_imbalance_block_pct=MIN_STOP_TARGET_IMBALANCE_BLOCK_PCT,
    min_favorable_to_adverse_ratio=MIN_FAVORABLE_TO_ADVERSE_RATIO,
    min_hermes_effect_sample=MIN_HERMES_EFFECT_SAMPLE,
    min_sim_closed_trades=MIN_SIM_CLOSED_TRADES,
    min_sim_return_pct=MIN_SIM_RETURN_PCT,
    min_sim_unrealized_pnl_pct=MIN_SIM_UNREALIZED_PNL_PCT,
    max_report_age_minutes=MAX_REPORT_AGE_MINUTES,
    now=None,
):
    system_health = system_health if isinstance(system_health, dict) else {}
    data_health = data_health if isinstance(data_health, dict) else {}
    outcome_report = outcome_report if isinstance(outcome_report, dict) else {}
    strategy_learning = strategy_learning if isinstance(strategy_learning, dict) else {}
    portfolio_report = portfolio_report if isinstance(portfolio_report, dict) else {}
    watchlist_diff = watchlist_diff if isinstance(watchlist_diff, dict) else {}
    market_context = market_context if isinstance(market_context, dict) else {}
    alert_quality = alert_quality if isinstance(alert_quality, dict) else {}
    judgment_audit = judgment_audit if isinstance(judgment_audit, dict) else {}
    simulation_performance = simulation_performance if isinstance(simulation_performance, dict) else {}
    position_judgment_audit = position_judgment_audit if isinstance(position_judgment_audit, dict) else {}
    cron_audit = cron_audit if isinstance(cron_audit, dict) else {}
    order_intake_event_store = order_intake_event_store if isinstance(order_intake_event_store, dict) else {}
    gates = []
    now = now or datetime.now()

    add_gate(
        gates,
        "execution_mode",
        "INFO",
        "Readiness report is diagnostic only; it does not enable execute mode or submit orders.",
        {"submits_orders": False, "changes_execution_mode": False},
    )

    freshness_rows, stale_or_missing = freshness_summary(
        [
            ("system_health", system_health, ("generated_at", "checked_at")),
            ("data_health", data_health, ("generated_at", "checked_at")),
            ("outcome_report", outcome_report, ("generated_at",)),
            ("strategy_learning", strategy_learning, ("generated_at",)),
            ("portfolio_report", portfolio_report, ("generated_at",)),
            ("watchlist_diff", watchlist_diff, ("generated_at",)),
            ("market_context", market_context, ("generated_at",)),
            ("alert_quality", alert_quality, ("generated_at",)),
            ("judgment_audit", judgment_audit, ("generated_at",)),
            ("simulation_performance", simulation_performance, ("generated_at",)),
            ("position_judgment_audit", position_judgment_audit, ("generated_at",)),
            ("cron_audit", cron_audit, ("generated_at",)),
            ("order_intake_event_store", order_intake_event_store, ("generated_at",)),
        ],
        now=now,
        max_age_minutes=max_report_age_minutes,
    )
    add_gate(
        gates,
        "report_freshness",
        "PASS" if not stale_or_missing else "BLOCK",
        "all input reports are fresh"
        if not stale_or_missing
        else "stale or missing report timestamps: "
        + ",".join(row["report"] for row in stale_or_missing),
        {
            "max_report_age_minutes": max_report_age_minutes,
            "reports": freshness_rows,
        },
    )

    health_status = system_health.get("status") or "MISSING"
    add_gate(
        gates,
        "system_health",
        report_status_gate_status(system_health, "quantmind_system_health_v1"),
        f"system_health status is {health_status}",
        {"status": health_status, "generated_at": system_health.get("generated_at") or system_health.get("checked_at")},
    )

    data_status = data_health.get("status") or "MISSING"
    add_gate(
        gates,
        "data_health",
        report_status_gate_status(data_health, "data_health_report_v1"),
        f"data_health status is {data_status}",
        {"status": data_status, "generated_at": data_health.get("generated_at")},
    )

    cron_payload = cron_audit_gate_payload(cron_audit)
    cron_status = cron_payload.get("status") or "MISSING"
    if cron_payload.get("schema") != "cron_audit_report_v1":
        cron_gate_status = "WARN"
        cron_detail = "cron audit schema is missing or invalid"
    elif cron_status == "OK":
        cron_gate_status = "PASS"
        cron_detail = "cron audit status is OK"
    elif cron_status == "FAIL":
        cron_gate_status = "BLOCK"
        cron_detail = "cron audit status is FAIL"
    else:
        cron_gate_status = "WARN"
        cron_detail = f"cron audit status is {cron_status}"
    add_gate(
        gates,
        "cron_audit",
        cron_gate_status,
        cron_detail,
        cron_payload,
    )

    broker_recon = order_intake_broker_reconciliation_payload(order_intake_event_store)
    broker_recon_status = broker_recon.get("status") or "MISSING"
    if broker_recon.get("schema") != "rt_order_intake_broker_reconciliation_v1":
        broker_recon_gate_status = "WARN"
        broker_recon_detail = "order intake broker reconciliation schema is missing or invalid"
    elif broker_recon_status == "FAIL":
        broker_recon_gate_status = "BLOCK"
        broker_recon_detail = (
            "broker has "
            f"{broker_recon['unmatched_broker_order_count']} order(s) missing from intake state"
        )
    elif broker_recon_status == "DEGRADED":
        broker_recon_gate_status = "BLOCK"
        broker_recon_detail = "broker order reconciliation query failed"
    elif broker_recon_status == "MISSING_CREDENTIALS":
        broker_recon_gate_status = "BLOCK"
        broker_recon_detail = "broker reconciliation is configured but credentials are unavailable"
    elif broker_recon_status == "OK":
        broker_recon_gate_status = "PASS"
        broker_recon_detail = "broker orders match intake state lineage"
    else:
        broker_recon_gate_status = "INFO"
        broker_recon_detail = f"broker reconciliation status is {broker_recon_status}"
    add_gate(
        gates,
        "order_intake_broker_reconciliation",
        broker_recon_gate_status,
        broker_recon_detail,
        broker_recon,
    )

    counts = latest_outcome_counts(outcome_report, strategy_learning)
    diagnostic_candidate_outcomes = diagnostic_candidate_outcome_count(outcome_report)
    if diagnostic_candidate_outcomes > 0:
        executable_metric = executable_only_outcome_metric(
            outcome_report,
            str(outcome_report.get("primary_horizon") or "1d"),
        )
        if executable_metric:
            if executable_metric.get("resolved_count") is not None:
                counts["resolved_count"] = int(executable_metric.get("resolved_count") or counts["resolved_count"])
            if executable_metric.get("avg_signed_close_return_pct") is not None:
                counts["avg_signed_return_pct"] = executable_metric.get("avg_signed_close_return_pct")
            if executable_metric.get("win_rate_pct") is not None:
                counts["win_rate_pct"] = executable_metric.get("win_rate_pct")
            if executable_metric.get("target_hit_rate_pct") is not None:
                counts["target_hit_rate_pct"] = executable_metric.get("target_hit_rate_pct")
            if executable_metric.get("stop_hit_rate_pct") is not None:
                counts["stop_hit_rate_pct"] = executable_metric.get("stop_hit_rate_pct")
            if executable_metric.get("favorable_to_adverse_ratio") is not None:
                counts["favorable_to_adverse_ratio"] = executable_metric.get("favorable_to_adverse_ratio")
            counts["strategy_evidence_metric_scope"] = "execution_candidate"
            counts["diagnostic_candidate_outcome_count"] = diagnostic_candidate_outcomes
        else:
            counts["strategy_evidence_metric_scope"] = "execution_candidate_missing"
            counts["diagnostic_candidate_outcome_count"] = diagnostic_candidate_outcomes
    avg_signed_return = as_float(counts.get("avg_signed_return_pct"))
    win_rate = as_float(counts.get("win_rate_pct"))
    target_hit_rate = as_float(counts.get("target_hit_rate_pct"))
    stop_hit_rate = as_float(counts.get("stop_hit_rate_pct"))
    favorable_to_adverse_ratio = as_float(counts.get("favorable_to_adverse_ratio"))
    stop_target_imbalance = (
        round(stop_hit_rate - target_hit_rate, 6)
        if stop_hit_rate is not None and target_hit_rate is not None
        else None
    )
    if counts.get("strategy_evidence_metric_scope") == "execution_candidate_missing":
        outcome_status = "BLOCK"
        outcome_detail = "diagnostic candidate outcomes exist but executable-only cohort is missing"
    elif counts["resolved_count"] < min_resolved_outcomes:
        outcome_status = "BLOCK"
        outcome_detail = f"resolved outcomes {counts['resolved_count']} / required {min_resolved_outcomes}"
    elif avg_signed_return is None:
        outcome_status = "BLOCK"
        outcome_detail = "resolved outcome sample is large enough but average signed return is missing"
    elif avg_signed_return <= 0:
        outcome_status = "BLOCK"
        outcome_detail = f"average signed return {avg_signed_return}% is not positive"
    elif win_rate is None:
        outcome_status = "BLOCK"
        outcome_detail = "resolved outcome sample is large enough but win rate is missing"
    elif win_rate <= min_win_rate_pct:
        outcome_status = "BLOCK"
        outcome_detail = f"win rate {win_rate}% is not above required {min_win_rate_pct}%"
    elif target_hit_rate is None or stop_hit_rate is None:
        outcome_status = "BLOCK"
        outcome_detail = "resolved outcome sample is large enough but target/stop hit rates are missing"
    elif (
        stop_target_imbalance is not None
        and stop_target_imbalance >= min_stop_target_imbalance_block_pct
    ):
        outcome_status = "BLOCK"
        outcome_detail = (
            f"stop hit rate {stop_hit_rate}% exceeds target hit rate {target_hit_rate}% "
            f"by {stop_target_imbalance}%"
        )
    elif stop_hit_rate > max_stop_hit_rate_pct:
        outcome_status = "BLOCK"
        outcome_detail = f"stop hit rate {stop_hit_rate}% exceeds maximum {max_stop_hit_rate_pct}%"
    elif favorable_to_adverse_ratio is None:
        outcome_status = "BLOCK"
        outcome_detail = "resolved outcome sample is large enough but favorable/adverse ratio is missing"
    elif favorable_to_adverse_ratio <= min_favorable_to_adverse_ratio:
        outcome_status = "BLOCK"
        outcome_detail = (
            f"favorable/adverse ratio {favorable_to_adverse_ratio} is not above "
            f"required {min_favorable_to_adverse_ratio}"
        )
    else:
        outcome_status = "PASS"
        outcome_detail = (
            f"resolved outcomes {counts['resolved_count']} / required {min_resolved_outcomes}; "
            f"average signed return {avg_signed_return}%; win rate {win_rate}%; "
            f"stop hit rate {stop_hit_rate}%; favorable/adverse ratio {favorable_to_adverse_ratio}"
        )
    counts["min_win_rate_pct"] = min_win_rate_pct
    counts["max_stop_hit_rate_pct"] = max_stop_hit_rate_pct
    counts["min_stop_target_imbalance_block_pct"] = min_stop_target_imbalance_block_pct
    counts["stop_target_imbalance_pct"] = stop_target_imbalance
    counts["min_favorable_to_adverse_ratio"] = min_favorable_to_adverse_ratio
    add_gate(
        gates,
        "forward_outcome_evidence",
        outcome_status,
        outcome_detail,
        counts,
    )

    intake_coverage = (
        strategy_learning.get("intake_coverage")
        if isinstance(strategy_learning.get("intake_coverage"), dict)
        else {}
    )
    directional = intake_coverage.get("directional") if isinstance(intake_coverage.get("directional"), dict) else {}
    directional_count = int(directional.get("joined_signal_count") or 0)
    directional_pct = as_float(directional.get("coverage_pct"), 0.0)
    if directional_count == 0:
        directional_status = "WARN"
        directional_detail = "no directional learning rows in current sample scope"
    elif directional_pct >= min_directional_intake_coverage_pct:
        directional_status = "PASS"
        directional_detail = f"directional intake coverage is {directional_pct}%"
    else:
        directional_status = "BLOCK"
        directional_detail = (
            f"directional intake coverage {directional_pct}% below "
            f"{min_directional_intake_coverage_pct}%"
        )
    add_gate(
        gates,
        "directional_intake_coverage",
        directional_status,
        directional_detail,
        {
            "coverage_pct": directional_pct,
            "joined_signal_count": directional_count,
            "minimum_pct": min_directional_intake_coverage_pct,
            "overall_coverage_pct": intake_coverage.get("coverage_pct"),
            "watch_coverage_pct": (intake_coverage.get("watch") or {}).get("coverage_pct")
            if isinstance(intake_coverage.get("watch"), dict)
            else None,
        },
    )

    market_summary = market_context_summary(market_context)
    if market_summary["schema"] != "market_context_report_v1":
        market_status = "BLOCK"
        market_detail = "market context schema is missing or invalid"
    elif market_summary["market_count"] == 0:
        market_status = "BLOCK"
        market_detail = "market context has no market summaries"
    elif market_summary["risk_off_markets"] or market_summary["high_risk_markets"]:
        market_status = "WARN"
        detail_parts = []
        if market_summary["risk_off_markets"]:
            detail_parts.append("risk_off=" + ",".join(market_summary["risk_off_markets"]))
        if market_summary["high_risk_markets"]:
            detail_parts.append("high_risk=" + ",".join(market_summary["high_risk_markets"]))
        market_detail = "market context requires stricter buy review: " + " ".join(detail_parts)
    else:
        market_status = "PASS"
        market_detail = "market context has no risk_off or high-risk markets"
    add_gate(
        gates,
        "market_context",
        market_status,
        market_detail,
        market_summary,
    )

    hermes_effect = judgment_effect_metrics(strategy_learning)
    approved_avg = as_float(hermes_effect.get("approved_avg_signed_return_pct"))
    approved_win = as_float(hermes_effect.get("approved_win_rate_pct"))
    rejected_avg = as_float(hermes_effect.get("rejected_avg_signed_return_pct"))
    if hermes_effect.get("execution_candidate_audit_pass_judgment_effect_missing"):
        hermes_status = "BLOCK"
        hermes_detail = "diagnostic directional rows exist but executable-only audit-pass Hermes judgment-effect cohort is missing"
    elif hermes_effect["approved_resolved_count"] < min_hermes_effect_sample:
        hermes_status = "BLOCK"
        hermes_detail = (
            f"Hermes approved/reduced resolved sample {hermes_effect['approved_resolved_count']} "
            f"/ required {min_hermes_effect_sample}"
        )
    elif hermes_effect["rejected_resolved_count"] < min_hermes_effect_sample:
        hermes_status = "BLOCK"
        hermes_detail = (
            f"Hermes rejected/held comparison sample {hermes_effect['rejected_resolved_count']} "
            f"/ required {min_hermes_effect_sample}"
        )
    elif approved_avg is None:
        hermes_status = "BLOCK"
        hermes_detail = "Hermes approved/reduced average signed return is missing"
    elif approved_avg <= 0:
        hermes_status = "BLOCK"
        hermes_detail = f"Hermes approved/reduced average signed return {approved_avg}% is not positive"
    elif approved_win is None:
        hermes_status = "BLOCK"
        hermes_detail = "Hermes approved/reduced win rate is missing"
    elif approved_win <= min_win_rate_pct:
        hermes_status = "BLOCK"
        hermes_detail = f"Hermes approved/reduced win rate {approved_win}% is not above required {min_win_rate_pct}%"
    elif rejected_avg is None:
        hermes_status = "BLOCK"
        hermes_detail = "Hermes rejected/held average signed return is missing"
    elif approved_avg <= rejected_avg:
        hermes_status = "BLOCK"
        hermes_detail = (
            f"Hermes approved/reduced average {approved_avg}% does not outperform "
            f"rejected/held average {rejected_avg}%"
        )
    else:
        hermes_status = "PASS"
        hermes_detail = (
            f"Hermes approved/reduced average {approved_avg}% outperforms "
            f"rejected/held average {rejected_avg}%"
        )
    hermes_effect["min_hermes_effect_sample"] = min_hermes_effect_sample
    hermes_effect["min_win_rate_pct"] = min_win_rate_pct
    add_gate(
        gates,
        "hermes_judgment_effect",
        hermes_status,
        hermes_detail,
        hermes_effect,
    )

    sim_reports = simulation_risk_reports(portfolio_report)
    critical_reports = [
        report
        for report in sim_reports
        if str(report.get("risk_level") or "").lower() == "critical"
        or str(report.get("trade_position_reconciliation_status") or "").upper() == "FAIL"
    ]
    high_reports = [report for report in sim_reports if str(report.get("risk_level") or "").lower() == "high"]
    if not sim_reports:
        portfolio_status = "BLOCK"
        portfolio_detail = "simulation portfolio risk report is missing"
    elif critical_reports:
        portfolio_status = "BLOCK"
        portfolio_detail = "simulation portfolio has critical risk or reconciliation failure"
    elif high_reports:
        portfolio_status = "WARN"
        portfolio_detail = "simulation portfolio has high risk flags"
    else:
        portfolio_status = "PASS"
        portfolio_detail = "simulation portfolio risk has no critical flags"
    add_gate(
        gates,
        "simulation_portfolio_risk",
        portfolio_status,
        portfolio_detail,
        {
            "simulation_report_count": len(sim_reports),
            "critical_count": len(critical_reports),
            "high_count": len(high_reports),
        },
    )

    sim_report = simulation_portfolio_report(portfolio_report)
    sim_risk_report = sim_reports[0] if sim_reports else {}
    unrealized = (
        sim_risk_report.get("unrealized_pnl")
        if isinstance(sim_risk_report.get("unrealized_pnl"), dict)
        else {}
    )
    sim_return = as_float(sim_report.get("return_pct_vs_initial"))
    sim_total_value = as_float(sim_report.get("total_value_hkd"))
    sim_unrealized_pct = as_float(unrealized.get("unrealized_pnl_pct_of_cost"))
    if not sim_report:
        sim_perf_status = "BLOCK"
        sim_perf_detail = "simulation portfolio report is missing"
    elif sim_return is None:
        sim_perf_status = "BLOCK"
        sim_perf_detail = "simulation portfolio return versus initial capital is missing"
    elif sim_return <= min_sim_return_pct:
        sim_perf_status = "BLOCK"
        sim_perf_detail = f"simulation return {sim_return}% is not above required {min_sim_return_pct}%"
    elif sim_unrealized_pct is not None and sim_unrealized_pct < min_sim_unrealized_pnl_pct:
        sim_perf_status = "BLOCK"
        sim_perf_detail = (
            f"simulation unrealized PnL {sim_unrealized_pct}% is below "
            f"minimum {min_sim_unrealized_pnl_pct}%"
        )
    else:
        sim_perf_status = "PASS"
        sim_perf_detail = f"simulation return {sim_return}% with total value {sim_total_value} HKD"
    add_gate(
        gates,
        "simulation_portfolio_performance",
        sim_perf_status,
        sim_perf_detail,
        {
            "portfolio_id": sim_report.get("portfolio_id"),
            "total_value_hkd": sim_total_value,
            "return_pct_vs_initial": sim_return,
            "unrealized_pnl_pct_of_cost": sim_unrealized_pct,
            "min_sim_return_pct": min_sim_return_pct,
            "min_sim_unrealized_pnl_pct": min_sim_unrealized_pnl_pct,
        },
    )

    sim_trade_review = simulation_trade_review_metrics(portfolio_report)
    sim_closed_pnl = as_float(sim_trade_review.get("closed_pnl_hkd_est"))
    sim_win_rate = as_float(sim_trade_review.get("closed_win_rate_pct"))
    blocking_notes = [
        note
        for note in sim_trade_review.get("review_notes") or []
        if note in ("recent_closed_trades_negative", "loss_rate_above_60pct")
    ]
    if sim_trade_review["closed_trade_count"] < min_sim_closed_trades:
        sim_trade_status = "BLOCK"
        sim_trade_detail = (
            f"simulation closed trade sample {sim_trade_review['closed_trade_count']} "
            f"/ required {min_sim_closed_trades}"
        )
    elif sim_closed_pnl is None:
        sim_trade_status = "BLOCK"
        sim_trade_detail = "simulation closed trade PnL is missing"
    elif sim_closed_pnl <= 0:
        sim_trade_status = "BLOCK"
        sim_trade_detail = f"simulation closed trade PnL {sim_closed_pnl} HKD is not positive"
    elif sim_win_rate is None:
        sim_trade_status = "BLOCK"
        sim_trade_detail = "simulation closed trade win rate is missing"
    elif sim_win_rate <= min_win_rate_pct:
        sim_trade_status = "BLOCK"
        sim_trade_detail = f"simulation closed trade win rate {sim_win_rate}% is not above required {min_win_rate_pct}%"
    elif blocking_notes:
        sim_trade_status = "BLOCK"
        sim_trade_detail = "simulation trade review has blocking notes: " + ",".join(blocking_notes)
    else:
        sim_trade_status = "PASS"
        sim_trade_detail = (
            f"simulation closed PnL {sim_closed_pnl} HKD; "
            f"win rate {sim_win_rate}% across {sim_trade_review['closed_trade_count']} closed trades"
        )
    sim_trade_review["min_sim_closed_trades"] = min_sim_closed_trades
    sim_trade_review["min_win_rate_pct"] = min_win_rate_pct
    add_gate(
        gates,
        "simulation_trade_review",
        sim_trade_status,
        sim_trade_detail,
        sim_trade_review,
    )

    sim_perf_status = simulation_performance.get("status") or "MISSING"
    if sim_perf_status in ("OK", "PASS"):
        sim_perf_gate_status = "PASS"
    elif sim_perf_status == "FAIL":
        sim_perf_gate_status = "BLOCK"
    else:
        sim_perf_gate_status = "WARN"
    add_gate(
        gates,
        "simulation_performance_attribution",
        sim_perf_gate_status,
        f"simulation performance report status is {sim_perf_status}",
        {
            "status": sim_perf_status,
            "summary": simulation_performance.get("summary") or {},
            "v5_hermes_evidence": simulation_performance.get("v5_hermes_evidence") or {},
            "reason_codes": simulation_performance.get("reason_codes") or [],
            "recommendations": simulation_performance.get("recommendations") or [],
            "remediation_plan": compact_simulation_remediation(simulation_performance),
            "failure_postmortem": compact_simulation_failure_postmortem(simulation_performance),
        },
    )

    sizing_remediation = (
        strategy_learning.get("sizing_blocker_remediation")
        if isinstance(strategy_learning.get("sizing_blocker_remediation"), dict)
        else {}
    )
    proposal = watchlist_diff.get("proposal") if isinstance(watchlist_diff.get("proposal"), dict) else {}
    sizing_count = int(sizing_remediation.get("sizing_blocker_count") or 0)
    covered = int(sizing_remediation.get("covered_by_watchlist_removal_count") or 0)
    uncovered = int(sizing_remediation.get("uncovered_count") or 0)
    if sizing_count and covered == sizing_count and uncovered == 0:
        watchlist_status = "WARN"
        watchlist_detail = "sizing blockers are covered by a manual watchlist proposal that still needs review"
    elif sizing_count:
        watchlist_status = "WARN"
        watchlist_detail = "sizing blockers remain partially or fully uncovered by the current watchlist proposal"
    else:
        watchlist_status = "PASS"
        watchlist_detail = "no sizing blocker remediation required in current learning scope"
    add_gate(
        gates,
        "watchlist_proposal",
        watchlist_status,
        watchlist_detail,
        {
            "sizing_blocker_count": sizing_count,
            "covered_by_watchlist_removal_count": covered,
            "uncovered_count": uncovered,
            "proposal_hash": sizing_remediation.get("watchlist_proposal_hash") or proposal.get("proposal_hash"),
            "current_watchlist_id": proposal.get("current_watchlist_id"),
            "proposed_watchlist_id": proposal.get("proposed_watchlist_id"),
            "manual_review_required": proposal.get("manual_review_required"),
            "auto_applied": proposal.get("auto_applied"),
        },
    )

    audit_status = judgment_audit.get("status") or "MISSING"
    if audit_status in ("OK", "PASS"):
        audit_gate_status = "PASS"
    elif audit_status == "FAIL":
        audit_gate_status = "BLOCK"
    else:
        audit_gate_status = "WARN"
    add_gate(
        gates,
        "hermes_judgment_audit",
        audit_gate_status,
        f"Hermes judgment audit status is {audit_status}",
        {
            "status": audit_status,
            "counts": judgment_audit.get("counts") or {},
            "recommendations": judgment_audit.get("recommendations") or [],
        },
    )

    position_audit_status = position_judgment_audit.get("status") or "MISSING"
    position_audit_coverage = (
        position_judgment_audit.get("coverage")
        if isinstance(position_judgment_audit.get("coverage"), dict)
        else {}
    )
    unjudged_high_urgency_count = as_int(
        position_audit_coverage.get("unjudged_high_urgency_review_count"),
        0,
    )
    if position_audit_status in ("OK", "PASS") and not unjudged_high_urgency_count:
        position_audit_gate_status = "PASS"
    elif position_audit_status == "FAIL" or unjudged_high_urgency_count:
        position_audit_gate_status = "BLOCK"
    else:
        position_audit_gate_status = "WARN"
    position_audit_detail = f"Hermes position judgment audit status is {position_audit_status}"
    if unjudged_high_urgency_count:
        position_audit_detail += (
            f"; unjudged high-urgency position reviews={unjudged_high_urgency_count}"
        )
    add_gate(
        gates,
        "hermes_position_judgment_audit",
        position_audit_gate_status,
        position_audit_detail,
        {
            "status": position_audit_status,
            "counts": position_judgment_audit.get("counts") or {},
            "coverage": position_audit_coverage,
            "recommendations": position_judgment_audit.get("recommendations") or [],
        },
    )

    alert_status = alert_quality.get("status") or "MISSING"
    add_gate(
        gates,
        "alert_quality",
        "PASS" if alert_status == "OK" else "WARN",
        f"alert quality status is {alert_status}",
        {
            "status": alert_status,
            "directional_alert_count": alert_quality.get("directional_alert_count"),
            "watch_alert_count": alert_quality.get("watch_alert_count"),
        },
    )

    status = worst_status(gates)
    ready_for_execute = status == "READY"
    pilot_readiness = build_pilot_readiness(gates, ready_for_execute)
    return {
        "schema": "execution_readiness_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "ready_for_execute": ready_for_execute,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_execution_mode": False,
            "auto_applies_watchlist": False,
            "min_resolved_outcomes": min_resolved_outcomes,
            "min_directional_intake_coverage_pct": min_directional_intake_coverage_pct,
            "min_win_rate_pct": min_win_rate_pct,
            "max_stop_hit_rate_pct": max_stop_hit_rate_pct,
            "min_stop_target_imbalance_block_pct": min_stop_target_imbalance_block_pct,
            "min_favorable_to_adverse_ratio": min_favorable_to_adverse_ratio,
            "min_hermes_effect_sample": min_hermes_effect_sample,
            "min_sim_closed_trades": min_sim_closed_trades,
            "min_sim_return_pct": min_sim_return_pct,
            "min_sim_unrealized_pnl_pct": min_sim_unrealized_pnl_pct,
            "max_report_age_minutes": max_report_age_minutes,
        },
        "gates": gates,
        "blocking_gates": [gate for gate in gates if gate["status"] == "BLOCK"],
        "warning_gates": [gate for gate in gates if gate["status"] == "WARN"],
        "pilot_readiness": pilot_readiness,
        "operator_summary": [
            "Do not enable execute mode unless status is READY and the operator has separately approved the mode change.",
            "READY here is necessary context only; rt_order_intake.py execute gates and Hermes matching judgments remain authoritative.",
            "pilot_readiness is report-only: it classifies evidence-collection blockers but does not relax ready_for_execute.",
            "WARN watchlist proposal gates require manual review and service restart planning before new watchlist-scoped evidence is trusted.",
        ],
    }


def build_report_from_files(args):
    return build_report(
        system_health=load_json_file(args.system_health_file),
        data_health=load_json_file(args.data_health_file),
        outcome_report=load_json_file(args.outcome_report_file),
        strategy_learning=load_json_file(args.strategy_learning_file),
        portfolio_report=load_json_file(args.portfolio_report_file),
        watchlist_diff=load_json_file(args.watchlist_diff_file),
        market_context=load_json_file(args.market_context_file),
        alert_quality=load_json_file(args.alert_quality_file),
        judgment_audit=load_json_file(args.judgment_audit_file),
        simulation_performance=load_json_file(args.simulation_performance_file),
        position_judgment_audit=load_json_file(args.position_judgment_audit_file),
        cron_audit=load_json_file(args.cron_audit_file),
        order_intake_event_store=load_json_file(args.order_intake_event_store_file),
        min_resolved_outcomes=args.min_resolved_outcomes,
        min_directional_intake_coverage_pct=args.min_directional_intake_coverage_pct,
        min_win_rate_pct=args.min_win_rate_pct,
        max_stop_hit_rate_pct=args.max_stop_hit_rate_pct,
        min_stop_target_imbalance_block_pct=args.min_stop_target_imbalance_block_pct,
        min_favorable_to_adverse_ratio=args.min_favorable_to_adverse_ratio,
        min_hermes_effect_sample=args.min_hermes_effect_sample,
        min_sim_closed_trades=args.min_sim_closed_trades,
        min_sim_return_pct=args.min_sim_return_pct,
        min_sim_unrealized_pnl_pct=args.min_sim_unrealized_pnl_pct,
        max_report_age_minutes=args.max_report_age_minutes,
    )


def build_text_report(payload):
    lines = [
        f"Execution readiness report {payload['generated_at']}",
        f"status={payload['status']} ready_for_execute={payload['ready_for_execute']}",
    ]
    for gate in payload.get("gates") or []:
        lines.append(f"  {gate['status']} {gate['gate']}: {gate['detail']}")
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--system-health-file", default=SYSTEM_HEALTH_FILE)
    parser.add_argument("--data-health-file", default=DATA_HEALTH_FILE)
    parser.add_argument("--outcome-report-file", default=OUTCOME_REPORT_FILE)
    parser.add_argument("--strategy-learning-file", default=STRATEGY_LEARNING_FILE)
    parser.add_argument("--portfolio-report-file", default=PORTFOLIO_REPORT_FILE)
    parser.add_argument("--watchlist-diff-file", default=WATCHLIST_DIFF_FILE)
    parser.add_argument("--market-context-file", default=MARKET_CONTEXT_FILE)
    parser.add_argument("--alert-quality-file", default=ALERT_QUALITY_FILE)
    parser.add_argument("--judgment-audit-file", default=JUDGMENT_AUDIT_FILE)
    parser.add_argument("--simulation-performance-file", default=SIMULATION_PERFORMANCE_FILE)
    parser.add_argument("--position-judgment-audit-file", default=POSITION_JUDGMENT_AUDIT_FILE)
    parser.add_argument("--cron-audit-file", default=CRON_AUDIT_FILE)
    parser.add_argument("--order-intake-event-store-file", default=ORDER_INTAKE_EVENT_STORE_FILE)
    parser.add_argument("--min-resolved-outcomes", type=int, default=MIN_RESOLVED_OUTCOMES)
    parser.add_argument(
        "--min-directional-intake-coverage-pct",
        type=float,
        default=MIN_DIRECTIONAL_INTAKE_COVERAGE_PCT,
    )
    parser.add_argument("--min-win-rate-pct", type=float, default=MIN_WIN_RATE_PCT)
    parser.add_argument("--max-stop-hit-rate-pct", type=float, default=MAX_STOP_HIT_RATE_PCT)
    parser.add_argument(
        "--min-stop-target-imbalance-block-pct",
        type=float,
        default=MIN_STOP_TARGET_IMBALANCE_BLOCK_PCT,
    )
    parser.add_argument(
        "--min-favorable-to-adverse-ratio",
        type=float,
        default=MIN_FAVORABLE_TO_ADVERSE_RATIO,
    )
    parser.add_argument("--min-hermes-effect-sample", type=int, default=MIN_HERMES_EFFECT_SAMPLE)
    parser.add_argument("--min-sim-closed-trades", type=int, default=MIN_SIM_CLOSED_TRADES)
    parser.add_argument("--min-sim-return-pct", type=float, default=MIN_SIM_RETURN_PCT)
    parser.add_argument("--min-sim-unrealized-pnl-pct", type=float, default=MIN_SIM_UNREALIZED_PNL_PCT)
    parser.add_argument("--max-report-age-minutes", type=float, default=MAX_REPORT_AGE_MINUTES)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report_from_files(args)
    save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(build_text_report(payload))


if __name__ == "__main__":
    main()
