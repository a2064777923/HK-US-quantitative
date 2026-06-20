#!/usr/bin/env python3
"""Read-only proposal generator for realtime v5 strategy config changes."""
import argparse
import json
import os
import sys
from datetime import datetime

try:
    import rt_signal_engine_v5 as rt
except ImportError:
    from scripts import rt_signal_engine_v5 as rt


STRATEGY_REVIEW_REPORT_FILE = os.environ.get("STRATEGY_REVIEW_REPORT_FILE", "/tmp/strategy_review_report.json")
CURRENT_CONFIG_FILE = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_FILE", "/root/rt_signal_strategy_config.json")
PROPOSAL_FILE = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_PROPOSAL_FILE", "/tmp/rt_signal_strategy_config_proposal.json")
SIMULATION_PERFORMANCE_REPORT_FILE = os.environ.get(
    "SIMULATION_PERFORMANCE_REPORT_FILE",
    "/tmp/simulation_performance_report.json",
)
EXECUTION_READINESS_REPORT_FILE = os.environ.get(
    "EXECUTION_READINESS_REPORT_FILE",
    "/tmp/execution_readiness_report.json",
)
STRATEGY_LEARNING_REPORT_FILE = os.environ.get(
    "STRATEGY_LEARNING_REPORT_FILE",
    "/tmp/strategy_learning_report.json",
)
TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE = os.environ.get(
    "TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE",
    "/tmp/trigger_evidence_convergence_report.json",
)
LOCAL_BACKTEST_RELIABILITY_REPORT_FILE = os.environ.get(
    "LOCAL_BACKTEST_RELIABILITY_REPORT_FILE",
    "/tmp/local_backtest_reliability_report.json",
)
MAX_SIMULATION_PERFORMANCE_AGE_MINUTES = float(
    os.environ.get(
        "STRATEGY_CONFIG_PROPOSAL_MAX_SIMULATION_PERFORMANCE_AGE_MINUTES",
        os.environ.get("EXECUTION_READINESS_MAX_REPORT_AGE_MINUTES", "90"),
    )
)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json_file(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def parse_timestamp(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def as_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def simulation_performance_freshness(payload, now=None, max_age_minutes=MAX_SIMULATION_PERFORMANCE_AGE_MINUTES):
    now = now or datetime.now()
    if not isinstance(payload, dict) or not payload:
        return {
            "status": "missing",
            "age_minutes": None,
            "max_age_minutes": max_age_minutes,
            "checked_at": now.isoformat(timespec="seconds"),
            "reason": "simulation_performance_report_missing",
        }
    timestamp_raw = payload.get("generated_at")
    timestamp = parse_timestamp(timestamp_raw)
    if timestamp is None:
        return {
            "status": "missing_timestamp",
            "age_minutes": None,
            "max_age_minutes": max_age_minutes,
            "checked_at": now.isoformat(timespec="seconds"),
            "reason": "simulation_performance_generated_at_missing_or_invalid",
            "timestamp_raw": timestamp_raw,
        }
    age = round((now - timestamp).total_seconds() / 60.0, 2)
    if age < -5:
        status = "future_timestamp"
        reason = "simulation_performance_generated_at_in_future"
    elif age > max_age_minutes:
        status = "stale"
        reason = "simulation_performance_report_stale"
    else:
        status = "fresh"
        reason = None
    return {
        "status": status,
        "age_minutes": age,
        "max_age_minutes": max_age_minutes,
        "checked_at": now.isoformat(timespec="seconds"),
        "reason": reason,
        "timestamp_raw": timestamp_raw,
    }


def signal_threshold(config, signal_type):
    thresholds = config.get("confirmation_thresholds") or {}
    if signal_type == "SELL":
        return rt.as_float(
            (thresholds.get("SELL") or {}).get("max_full_score"),
            rt.SELL_CONFIRMATION_MAX_SCORE,
        )
    return rt.as_float(
        (thresholds.get("BUY") or {}).get("min_full_score"),
        rt.BUY_CONFIRMATION_MIN_SCORE,
    )


def proposed_tightened_threshold(config, row):
    signal_type = str(row.get("signal_type") or "").upper()
    key = row.get("key")
    existing = ((config.get("trigger_overrides") or {}).get(key) or {})
    if signal_type == "SELL":
        base = rt.as_float(existing.get("max_full_score"), signal_threshold(config, "SELL"))
        return {"max_full_score": round(max(base - 0.10, -0.85), 4)}
    base = rt.as_float(existing.get("min_full_score"), signal_threshold(config, "BUY"))
    return {"min_full_score": round(min(base + 0.10, 0.85), 4)}


def apply_policy_to_config(config, row):
    proposed = json.loads(json.dumps(config))
    overrides = proposed.setdefault("trigger_overrides", {})
    key = row.get("key")
    if not key:
        return proposed, None
    policy = row.get("policy")
    current_override = dict(overrides.get(key) or {})
    change = {
        "key": key,
        "policy": policy,
        "from": dict(current_override),
        "reasons": row.get("reasons") or [],
    }
    if policy == "disable_execution_review":
        current_override["enabled"] = False
        current_override["review_mode"] = "disabled_pending_rework"
    elif policy == "tighten_thresholds":
        current_override["enabled"] = current_override.get("enabled", True)
        current_override.update(proposed_tightened_threshold(proposed, row))
        current_override["review_mode"] = "tightened_pending_retest"
    elif policy == "shadow_only":
        current_override["enabled"] = current_override.get("enabled", True)
        current_override["review_mode"] = "shadow_only_pending_sample"
    else:
        return proposed, None
    current_override["strategy_review_reasons"] = row.get("reasons") or []
    overrides[key] = current_override
    change["to"] = dict(current_override)
    return proposed, change


def proposal_hash(proposed_config):
    normalized, _warnings = rt.normalize_strategy_config(proposed_config)
    return normalized["config_id"]


def compact_simulation_performance_context(
    simulation_performance,
    now=None,
    max_age_minutes=MAX_SIMULATION_PERFORMANCE_AGE_MINUTES,
):
    payload = simulation_performance if isinstance(simulation_performance, dict) else {}
    freshness = simulation_performance_freshness(payload, now=now, max_age_minutes=max_age_minutes)
    report_status = payload.get("status") or "MISSING"
    if not payload:
        effective_status = "MISSING"
    elif freshness["status"] != "fresh":
        effective_status = "STALE"
    else:
        effective_status = report_status
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    remediation = payload.get("remediation_plan") if isinstance(payload.get("remediation_plan"), dict) else {}
    actions = remediation.get("actions") if isinstance(remediation.get("actions"), list) else []
    action_ids = [
        str(action.get("action_id"))
        for action in actions
        if isinstance(action, dict) and action.get("action_id")
    ]
    operator_contract = (
        remediation.get("operator_contract")
        if isinstance(remediation.get("operator_contract"), dict)
        else {}
    )
    return {
        "schema": "rt_signal_strategy_config_proposal_simulation_context_v1",
        "present": bool(payload),
        "source_report_file": SIMULATION_PERFORMANCE_REPORT_FILE,
        "source_report_schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "status": effective_status,
        "report_status": report_status,
        "freshness": freshness,
        "summary": {
            "portfolio_id": summary.get("portfolio_id"),
            "total_value_hkd": summary.get("total_value_hkd"),
            "return_pct_vs_initial": summary.get("return_pct_vs_initial"),
            "risk_level": summary.get("risk_level"),
            "closed_trade_count": summary.get("closed_trade_count"),
            "closed_win_rate_pct": summary.get("closed_win_rate_pct"),
            "closed_pnl_hkd_est": summary.get("closed_pnl_hkd_est"),
        },
        "reason_codes": payload.get("reason_codes") if isinstance(payload.get("reason_codes"), list) else [],
        "recommendations": (
            payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
        ),
        "remediation_plan": {
            "schema": remediation.get("schema"),
            "status": remediation.get("status"),
            "proposal_hash": remediation.get("proposal_hash"),
            "manual_review_required": remediation.get("manual_review_required"),
            "auto_applied": remediation.get("auto_applied"),
            "action_ids": action_ids,
            "operator_contract": {
                "read_only": operator_contract.get("read_only"),
                "submits_orders": operator_contract.get("submits_orders"),
                "changes_execution_mode": operator_contract.get("changes_execution_mode"),
                "changes_strategy_config": operator_contract.get("changes_strategy_config"),
                "requires_operator_review_before_promotion": operator_contract.get(
                    "requires_operator_review_before_promotion"
                ),
            },
        },
    }


def simulation_performance_promotion_guards(context):
    status = str(context.get("status") or "MISSING").upper()
    remediation = context.get("remediation_plan") if isinstance(context.get("remediation_plan"), dict) else {}
    base = {
        "simulation_status": status,
        "simulation_report_file": context.get("source_report_file"),
        "remediation_hash": remediation.get("proposal_hash"),
        "remediation_status": remediation.get("status"),
        "remediation_action_ids": remediation.get("action_ids") or [],
        "reason_codes": context.get("reason_codes") or [],
    }
    blockers = []
    warnings = []
    if status == "FAIL":
        row = dict(base)
        row["code"] = "simulation_performance_fail_blocks_strategy_promotion"
        row["detail"] = (
            "simulation portfolio performance is failing; strategy threshold changes must not be "
            "promoted until later simulation, outcome, readiness, and Hermes judgment-effect evidence recover"
        )
        blockers.append(row)
    elif status == "WARN":
        row = dict(base)
        row["code"] = "simulation_performance_warn_requires_operator_review"
        row["detail"] = "simulation performance has warning-level risk; review remediation context before promotion"
        warnings.append(row)
    elif status == "MISSING":
        row = dict(base)
        row["code"] = "simulation_performance_missing_blocks_strategy_promotion"
        row["detail"] = "simulation performance context is unavailable; strategy promotion review is incomplete"
        row["freshness"] = context.get("freshness") or {}
        blockers.append(row)
    elif status == "STALE":
        row = dict(base)
        row["code"] = "simulation_performance_stale_blocks_strategy_promotion"
        row["detail"] = "simulation performance context is stale or has an invalid timestamp; refresh it before promotion"
        row["freshness"] = context.get("freshness") or {}
        blockers.append(row)
    elif status not in ("OK", "PASS"):
        row = dict(base)
        row["code"] = "simulation_performance_unknown_status_blocks_strategy_promotion"
        row["detail"] = "simulation performance status is not recognized as clean"
        blockers.append(row)
    return blockers, warnings


def compact_execution_readiness_context(execution_readiness):
    payload = execution_readiness if isinstance(execution_readiness, dict) else {}
    blocking_gates = payload.get("blocking_gates") if isinstance(payload.get("blocking_gates"), list) else []
    warning_gates = payload.get("warning_gates") if isinstance(payload.get("warning_gates"), list) else []
    return {
        "schema": "rt_signal_strategy_config_proposal_readiness_context_v1",
        "present": bool(payload),
        "source_report_file": EXECUTION_READINESS_REPORT_FILE,
        "source_report_schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "status": payload.get("status") or "MISSING",
        "ready_for_execute": payload.get("ready_for_execute"),
        "blocking_gate_count": len(blocking_gates),
        "warning_gate_count": len(warning_gates),
        "blocking_gate_names": [row.get("gate") for row in blocking_gates[:20] if isinstance(row, dict)],
        "warning_gate_names": [row.get("gate") for row in warning_gates[:20] if isinstance(row, dict)],
    }


def execution_readiness_promotion_guards(context):
    status = str(context.get("status") or "MISSING").upper()
    clean = status == "READY" and context.get("ready_for_execute") is True
    if clean:
        return []
    return [
        {
            "code": "execution_readiness_not_ready_blocks_strategy_promotion",
            "detail": "execution readiness is not READY; strategy config promotion would change alert behavior before the full gate stack is clean",
            "readiness_status": status,
            "ready_for_execute": context.get("ready_for_execute"),
            "blocking_gate_names": context.get("blocking_gate_names") or [],
            "warning_gate_names": context.get("warning_gate_names") or [],
        }
    ]


def compact_strategy_learning_context(strategy_learning):
    payload = strategy_learning if isinstance(strategy_learning, dict) else {}
    audit_effect = (
        payload.get("audit_pass_judgment_effect")
        if isinstance(payload.get("audit_pass_judgment_effect"), dict)
        else {}
    )
    raw_effect = payload.get("judgment_effect") if isinstance(payload.get("judgment_effect"), dict) else {}
    coverage = payload.get("judgment_audit_coverage") if isinstance(payload.get("judgment_audit_coverage"), dict) else {}
    approved = audit_effect.get("approved_or_reduced") if isinstance(audit_effect.get("approved_or_reduced"), dict) else {}
    rejected = audit_effect.get("rejected_or_held") if isinstance(audit_effect.get("rejected_or_held"), dict) else {}
    execution_scope = (
        payload.get("execution_candidate_scope")
        if isinstance(payload.get("execution_candidate_scope"), dict)
        else {}
    )
    execution_candidate_summary = (
        execution_scope.get("execution_candidate")
        if isinstance(execution_scope.get("execution_candidate"), dict)
        else {}
    )
    execution_audit_effect = (
        payload.get("execution_candidate_audit_pass_judgment_effect")
        if isinstance(payload.get("execution_candidate_audit_pass_judgment_effect"), dict)
        else {}
    )
    execution_approved = (
        execution_audit_effect.get("approved_or_reduced")
        if isinstance(execution_audit_effect.get("approved_or_reduced"), dict)
        else {}
    )
    execution_rejected = (
        execution_audit_effect.get("rejected_or_held")
        if isinstance(execution_audit_effect.get("rejected_or_held"), dict)
        else {}
    )
    return {
        "schema": "rt_signal_strategy_config_proposal_learning_context_v1",
        "present": bool(payload),
        "source_report_file": STRATEGY_LEARNING_REPORT_FILE,
        "source_report_schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "has_audit_pass_judgment_effect": bool(audit_effect),
        "audit_pass_judgment_effect": {
            "sample_filter": audit_effect.get("sample_filter"),
            "approved_or_reduced": {
                "resolved_count": approved.get("resolved_count"),
                "avg_signed_return_pct": approved.get("avg_signed_return_pct"),
                "win_rate_pct": approved.get("win_rate_pct"),
            },
            "rejected_or_held": {
                "resolved_count": rejected.get("resolved_count"),
                "avg_signed_return_pct": rejected.get("avg_signed_return_pct"),
                "win_rate_pct": rejected.get("win_rate_pct"),
            },
        },
        "raw_judgment_effect_present": bool(raw_effect),
        "judgment_audit_coverage": {
            "audit_report_available": coverage.get("audit_report_available"),
            "audit_report_status": coverage.get("audit_report_status"),
            "audit_report_truncated": coverage.get("audit_report_truncated"),
            "joined_judgment_count": coverage.get("joined_judgment_count"),
            "audit_pass_count": coverage.get("audit_pass_count"),
            "audit_fail_count": coverage.get("audit_fail_count"),
            "audit_missing_count": coverage.get("audit_missing_count"),
            "approved_or_reduced_audit_fail_or_missing_count": coverage.get(
                "approved_or_reduced_audit_fail_or_missing_count"
            ),
            "rejected_or_held_audit_fail_or_missing_count": coverage.get(
                "rejected_or_held_audit_fail_or_missing_count"
            ),
        },
        "execution_candidate_scope": {
            "present": bool(execution_scope),
            "joined_signal_count": execution_scope.get("joined_signal_count"),
            "execution_candidate_count": execution_scope.get("execution_candidate_count"),
            "non_execution_candidate_count": execution_scope.get("non_execution_candidate_count"),
            "downgraded_directional_count": execution_scope.get("downgraded_directional_count"),
            "unknown_execution_candidate_count": execution_scope.get("unknown_execution_candidate_count"),
            "execution_candidate_resolved_count": execution_candidate_summary.get("resolved_count"),
            "promotion_evidence_requirement": execution_scope.get("promotion_evidence_requirement"),
        },
        "has_execution_candidate_audit_pass_judgment_effect": bool(execution_audit_effect),
        "execution_candidate_audit_pass_judgment_effect": {
            "sample_filter": execution_audit_effect.get("sample_filter"),
            "approved_or_reduced": {
                "resolved_count": execution_approved.get("resolved_count"),
                "avg_signed_return_pct": execution_approved.get("avg_signed_return_pct"),
                "win_rate_pct": execution_approved.get("win_rate_pct"),
            },
            "rejected_or_held": {
                "resolved_count": execution_rejected.get("resolved_count"),
                "avg_signed_return_pct": execution_rejected.get("avg_signed_return_pct"),
                "win_rate_pct": execution_rejected.get("win_rate_pct"),
            },
        },
    }


def strategy_learning_promotion_guards(context, min_sample=5):
    blockers = []
    coverage = context.get("judgment_audit_coverage") if isinstance(context.get("judgment_audit_coverage"), dict) else {}
    effect = (
        context.get("audit_pass_judgment_effect")
        if isinstance(context.get("audit_pass_judgment_effect"), dict)
        else {}
    )
    approved = effect.get("approved_or_reduced") if isinstance(effect.get("approved_or_reduced"), dict) else {}
    rejected = effect.get("rejected_or_held") if isinstance(effect.get("rejected_or_held"), dict) else {}
    approved_count = as_int(approved.get("resolved_count"))
    rejected_count = as_int(rejected.get("resolved_count"))
    approved_avg = as_float(approved.get("avg_signed_return_pct"))
    rejected_avg = as_float(rejected.get("avg_signed_return_pct"))
    execution_scope = (
        context.get("execution_candidate_scope")
        if isinstance(context.get("execution_candidate_scope"), dict)
        else {}
    )
    execution_effect = (
        context.get("execution_candidate_audit_pass_judgment_effect")
        if isinstance(context.get("execution_candidate_audit_pass_judgment_effect"), dict)
        else {}
    )
    execution_approved = (
        execution_effect.get("approved_or_reduced")
        if isinstance(execution_effect.get("approved_or_reduced"), dict)
        else {}
    )
    execution_rejected = (
        execution_effect.get("rejected_or_held")
        if isinstance(execution_effect.get("rejected_or_held"), dict)
        else {}
    )
    downgraded_directional_count = as_int(execution_scope.get("downgraded_directional_count"))
    execution_approved_count = as_int(execution_approved.get("resolved_count"))
    execution_rejected_count = as_int(execution_rejected.get("resolved_count"))
    execution_approved_avg = as_float(execution_approved.get("avg_signed_return_pct"))
    execution_rejected_avg = as_float(execution_rejected.get("avg_signed_return_pct"))

    if not context.get("present"):
        blockers.append(
            {
                "code": "strategy_learning_missing_blocks_strategy_promotion",
                "detail": "strategy learning context is unavailable; threshold changes would not be backed by recent self-review evidence",
            }
        )
        return blockers
    if not context.get("has_audit_pass_judgment_effect"):
        blockers.append(
            {
                "code": "strategy_learning_audit_pass_effect_missing_blocks_strategy_promotion",
                "detail": "strategy learning does not include audit-pass Hermes judgment-effect evidence",
                "raw_judgment_effect_present": context.get("raw_judgment_effect_present"),
            }
        )
    if coverage.get("audit_report_available") is not True:
        blockers.append(
            {
                "code": "strategy_learning_judgment_audit_missing_blocks_strategy_promotion",
                "detail": "strategy learning cannot prove Hermes judgment rows were joined to a judgment audit report",
            }
        )
    if coverage.get("audit_report_truncated"):
        blockers.append(
            {
                "code": "strategy_learning_judgment_audit_truncated_blocks_strategy_promotion",
                "detail": "strategy learning judgment audit coverage is truncated",
            }
        )
    fail_or_missing = as_int(coverage.get("audit_fail_count")) + as_int(coverage.get("audit_missing_count"))
    if fail_or_missing:
        blockers.append(
            {
                "code": "strategy_learning_judgment_audit_gaps_block_strategy_promotion",
                "detail": "strategy learning includes Hermes judgments that failed or missed audit coverage",
                "audit_fail_count": coverage.get("audit_fail_count"),
                "audit_missing_count": coverage.get("audit_missing_count"),
            }
        )
    if approved_count < min_sample or rejected_count < min_sample:
        blockers.append(
            {
                "code": "strategy_learning_audit_pass_sample_too_small_blocks_strategy_promotion",
                "detail": "audit-pass Hermes judgment-effect sample is too small for strategy promotion review",
                "min_sample": min_sample,
                "approved_resolved_count": approved_count,
                "rejected_resolved_count": rejected_count,
            }
        )
    elif approved_avg is None or rejected_avg is None or approved_avg <= 0 or approved_avg <= rejected_avg:
        blockers.append(
            {
                "code": "strategy_learning_audit_pass_effect_not_supportive_blocks_strategy_promotion",
                "detail": "audit-pass Hermes approvals must have positive average return and outperform rejected/held judgments before strategy promotion",
                "approved_avg_signed_return_pct": approved_avg,
                "rejected_avg_signed_return_pct": rejected_avg,
                "approval_vs_rejection_delta_pct": round(approved_avg - rejected_avg, 6)
                if approved_avg is not None and rejected_avg is not None
                else None,
            }
        )
    if downgraded_directional_count:
        if not context.get("has_execution_candidate_audit_pass_judgment_effect"):
            blockers.append(
                {
                    "code": "strategy_learning_execution_candidate_evidence_missing_blocks_strategy_promotion",
                    "detail": "strategy learning includes downgraded diagnostic directional rows, but no executable-only audit-pass learning cohort",
                    "downgraded_directional_count": downgraded_directional_count,
                    "execution_candidate_count": execution_scope.get("execution_candidate_count"),
                }
            )
        elif execution_approved_count < min_sample or execution_rejected_count < min_sample:
            blockers.append(
                {
                    "code": "strategy_learning_execution_candidate_audit_pass_sample_too_small_blocks_strategy_promotion",
                    "detail": "executable-only audit-pass Hermes judgment-effect sample is too small for strategy promotion review",
                    "min_sample": min_sample,
                    "downgraded_directional_count": downgraded_directional_count,
                    "approved_resolved_count": execution_approved_count,
                    "rejected_resolved_count": execution_rejected_count,
                }
            )
        elif (
            execution_approved_avg is None
            or execution_rejected_avg is None
            or execution_approved_avg <= 0
            or execution_approved_avg <= execution_rejected_avg
        ):
            blockers.append(
                {
                    "code": "strategy_learning_execution_candidate_audit_pass_effect_not_supportive_blocks_strategy_promotion",
                    "detail": "executable-only audit-pass Hermes approvals must have positive average return and outperform rejected/held judgments before strategy promotion",
                    "downgraded_directional_count": downgraded_directional_count,
                    "approved_avg_signed_return_pct": execution_approved_avg,
                    "rejected_avg_signed_return_pct": execution_rejected_avg,
                    "approval_vs_rejection_delta_pct": round(execution_approved_avg - execution_rejected_avg, 6)
                    if execution_approved_avg is not None and execution_rejected_avg is not None
                    else None,
                }
            )
    return blockers


def compact_trigger_evidence_convergence_context(trigger_evidence_convergence):
    payload = trigger_evidence_convergence if isinstance(trigger_evidence_convergence, dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    rows = payload.get("trigger_evidence") if isinstance(payload.get("trigger_evidence"), list) else []
    top_risks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("status") not in ("CONVERGED_RISK", "REPLAY_CHALLENGES_FORWARD", "INSUFFICIENT_FORWARD_SAMPLE"):
            continue
        top_risks.append(
            {
                "key": row.get("key"),
                "status": row.get("status"),
                "confidence": row.get("confidence"),
                "reasons": row.get("reasons") if isinstance(row.get("reasons"), list) else [],
                "forward_policy": (row.get("forward") or {}).get("policy")
                if isinstance(row.get("forward"), dict)
                else None,
                "replay_policy": (row.get("replay") or {}).get("policy") if isinstance(row.get("replay"), dict) else None,
                "replay_gate_blocker_reason_counts": (row.get("replay") or {}).get(
                    "gate_blocker_reason_counts"
                )
                or {},
                "replay_top_gate_blockers": (row.get("replay") or {}).get("top_gate_blockers") or [],
            }
        )
    return {
        "schema": "rt_signal_strategy_config_proposal_convergence_context_v1",
        "present": bool(payload),
        "source_report_file": TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE,
        "source_report_schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "status": summary.get("status") or ("MISSING" if not payload else "UNKNOWN"),
        "promotion_ready": summary.get("promotion_ready"),
        "promotion_eligible": summary.get("promotion_eligible"),
        "trigger_count": summary.get("trigger_count"),
        "converged_risk_count": as_int(summary.get("converged_risk_count")),
        "replay_challenges_forward_count": as_int(summary.get("replay_challenges_forward_count")),
        "insufficient_forward_sample_count": as_int(summary.get("insufficient_forward_sample_count")),
        "status_counts": summary.get("status_counts") if isinstance(summary.get("status_counts"), dict) else {},
        "top_risk_triggers": top_risks[:8],
        "recommendations": payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else [],
        "operator_contract": payload.get("operator_contract") if isinstance(payload.get("operator_contract"), dict) else {},
    }


def trigger_evidence_convergence_promotion_guards(context):
    blockers = []
    if not context.get("present") or context.get("source_report_schema") != "trigger_evidence_convergence_report_v1":
        blockers.append(
            {
                "code": "trigger_evidence_convergence_missing_blocks_strategy_promotion",
                "detail": "trigger convergence context is unavailable; strategy promotion cannot compare forward outcome policy with replay-derived trigger noise",
                "source_report_file": context.get("source_report_file"),
                "source_report_schema": context.get("source_report_schema"),
            }
        )
        return blockers
    risk_count = as_int(context.get("converged_risk_count"))
    replay_challenges = as_int(context.get("replay_challenges_forward_count"))
    insufficient = as_int(context.get("insufficient_forward_sample_count"))
    if risk_count or replay_challenges:
        blockers.append(
            {
                "code": "trigger_evidence_convergence_risk_blocks_strategy_promotion",
                "detail": "forward outcome policy and replay-derived trigger noise disagree or converge on risk; review trigger thresholds before promotion",
                "converged_risk_count": risk_count,
                "replay_challenges_forward_count": replay_challenges,
                "top_risk_triggers": context.get("top_risk_triggers") or [],
                "recommendations": context.get("recommendations") or [],
            }
        )
    if insufficient:
        blockers.append(
            {
                "code": "trigger_evidence_forward_sample_insufficient_blocks_strategy_promotion",
                "detail": "one or more trigger policies lack resolved forward outcomes; do not promote threshold/config changes from incomplete samples",
                "insufficient_forward_sample_count": insufficient,
                "top_risk_triggers": context.get("top_risk_triggers") or [],
                "recommendations": context.get("recommendations") or [],
            }
        )
    return blockers


def compact_local_backtest_reliability_context(local_backtest_reliability):
    payload = local_backtest_reliability if isinstance(local_backtest_reliability, dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    dataset = payload.get("dataset") if isinstance(payload.get("dataset"), dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    contract = payload.get("hermes_contract") if isinstance(payload.get("hermes_contract"), dict) else {}
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    backtests = payload.get("backtests") if isinstance(payload.get("backtests"), list) else []
    compact_backtests = []
    for row in backtests[:4]:
        if not isinstance(row, dict):
            continue
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        compact_backtests.append(
            {
                "name": row.get("name"),
                "status": row.get("status"),
                "total_return_pct": metrics.get("total_return_pct"),
                "annual_return_pct": metrics.get("annual_return_pct"),
                "sharpe": metrics.get("sharpe"),
                "max_drawdown_pct": metrics.get("max_drawdown_pct"),
                "trades": metrics.get("trades"),
                "win_rate_pct": metrics.get("win_rate_pct"),
            }
        )
    return {
        "schema": "rt_signal_strategy_config_proposal_local_backtest_context_v1",
        "present": bool(payload),
        "source_report_file": LOCAL_BACKTEST_RELIABILITY_REPORT_FILE,
        "source_report_schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "status": summary.get("overall_status") or ("MISSING" if not payload else "UNKNOWN"),
        "promotion_ready": summary.get("promotion_ready"),
        "hermes_use": summary.get("hermes_use"),
        "dataset_status": summary.get("dataset_status") or dataset.get("status"),
        "backtest_status_counts": (
            summary.get("backtest_status_counts")
            if isinstance(summary.get("backtest_status_counts"), dict)
            else {}
        ),
        "best_backtest_by_sharpe": summary.get("best_backtest_by_sharpe"),
        "dataset": {
            "total_symbol_count": dataset.get("total_symbol_count"),
            "total_row_count": dataset.get("total_row_count"),
            "date_range": dataset.get("date_range") if isinstance(dataset.get("date_range"), dict) else {},
        },
        "backtests": compact_backtests,
        "recommendation_codes": [
            item.get("code") if isinstance(item, dict) else str(item)
            for item in recommendations[:12]
        ],
        "source_contract": {
            "read_only_inputs": source.get("read_only_inputs"),
            "local_only": source.get("local_only"),
            "changes_v5": source.get("changes_v5"),
            "changes_order_intake": source.get("changes_order_intake"),
            "changes_simulation": source.get("changes_simulation"),
            "uses_credentials": source.get("uses_credentials"),
        },
        "hermes_contract": {
            "contract": contract.get("contract"),
            "forbidden_use": contract.get("forbidden_use") if isinstance(contract.get("forbidden_use"), list) else [],
        },
    }


def local_backtest_reliability_promotion_guards(context):
    status = str(context.get("status") or "MISSING").upper()
    base = {
        "local_backtest_status": status,
        "local_backtest_report_file": context.get("source_report_file"),
        "local_backtest_schema": context.get("source_report_schema"),
        "dataset_status": context.get("dataset_status"),
        "backtest_status_counts": context.get("backtest_status_counts") or {},
        "recommendation_codes": context.get("recommendation_codes") or [],
    }
    blockers = []
    warnings = []
    if not context.get("present") or context.get("source_report_schema") != "local_backtest_reliability_report_v1":
        row = dict(base)
        row["code"] = "local_backtest_reliability_missing_requires_operator_review"
        row["detail"] = "local backtest reliability context is unavailable; use simulation, forward outcomes, and Hermes learning as authority before promotion"
        warnings.append(row)
        return blockers, warnings
    if status == "INSUFFICIENT_EVIDENCE":
        row = dict(base)
        row["code"] = "local_backtest_insufficient_evidence_blocks_strategy_promotion"
        row["detail"] = "local dataset or backtest reliability has hard failures; do not promote strategy config changes until the research evidence is repaired"
        blockers.append(row)
    elif context.get("promotion_ready") is not True:
        row = dict(base)
        row["code"] = "local_backtest_research_only_requires_operator_review"
        row["detail"] = "local backtest reliability is research evidence only; it can support or challenge hypotheses but cannot authorize strategy promotion by itself"
        warnings.append(row)
    elif status not in ("OK", "PASS", "PROMOTION_READY"):
        row = dict(base)
        row["code"] = "local_backtest_reliability_unknown_status_requires_operator_review"
        row["detail"] = "local backtest reliability status is not recognized as clean promotion evidence"
        warnings.append(row)
    return blockers, warnings


def build_report(
    strategy_review=None,
    current_config=None,
    simulation_performance=None,
    execution_readiness=None,
    strategy_learning=None,
    trigger_evidence_convergence=None,
    local_backtest_reliability=None,
    now=None,
    max_simulation_performance_age_minutes=MAX_SIMULATION_PERFORMANCE_AGE_MINUTES,
):
    strategy_review = strategy_review if strategy_review is not None else load_json_file(STRATEGY_REVIEW_REPORT_FILE)
    if current_config is None:
        current_config = load_json_file(CURRENT_CONFIG_FILE, rt.default_strategy_config())
    if simulation_performance is None:
        simulation_performance = load_json_file(SIMULATION_PERFORMANCE_REPORT_FILE)
    if execution_readiness is None:
        execution_readiness = load_json_file(EXECUTION_READINESS_REPORT_FILE)
    if strategy_learning is None:
        strategy_learning = load_json_file(STRATEGY_LEARNING_REPORT_FILE)
    if trigger_evidence_convergence is None:
        trigger_evidence_convergence = load_json_file(TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE)
    if local_backtest_reliability is None:
        local_backtest_reliability = load_json_file(LOCAL_BACKTEST_RELIABILITY_REPORT_FILE)
    current_config, config_warnings = rt.normalize_strategy_config(current_config)
    proposed_config = json.loads(json.dumps(current_config))
    changes = []
    for row in strategy_review.get("trigger_policies") or []:
        proposed_config, change = apply_policy_to_config(proposed_config, row)
        if change:
            changes.append(change)
    proposed_config["schema"] = "rt_signal_strategy_config_v1"
    proposed_config["version"] = f"proposal-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    proposed_config["description"] = "Candidate strategy config generated from strategy_review_report.py. Manual review required."
    proposed_config, proposed_warnings = rt.normalize_strategy_config(proposed_config)
    simulation_context = compact_simulation_performance_context(
        simulation_performance,
        now=now,
        max_age_minutes=max_simulation_performance_age_minutes,
    )
    promotion_blockers, promotion_risk_warnings = simulation_performance_promotion_guards(simulation_context)
    readiness_context = compact_execution_readiness_context(execution_readiness)
    learning_context = compact_strategy_learning_context(strategy_learning)
    convergence_context = compact_trigger_evidence_convergence_context(trigger_evidence_convergence)
    local_backtest_context = compact_local_backtest_reliability_context(local_backtest_reliability)
    promotion_blockers.extend(execution_readiness_promotion_guards(readiness_context))
    promotion_blockers.extend(strategy_learning_promotion_guards(learning_context))
    promotion_blockers.extend(trigger_evidence_convergence_promotion_guards(convergence_context))
    local_backtest_blockers, local_backtest_warnings = local_backtest_reliability_promotion_guards(
        local_backtest_context
    )
    promotion_blockers.extend(local_backtest_blockers)
    promotion_risk_warnings.extend(local_backtest_warnings)
    return {
        "schema": "rt_signal_strategy_config_proposal_v1",
        "generated_at": now_iso(),
        "source": {
            "read_only": True,
            "manual_review_required": True,
            "auto_applied": False,
            "strategy_review_report_file": STRATEGY_REVIEW_REPORT_FILE,
            "simulation_performance_report_file": SIMULATION_PERFORMANCE_REPORT_FILE,
            "execution_readiness_report_file": EXECUTION_READINESS_REPORT_FILE,
            "strategy_learning_report_file": STRATEGY_LEARNING_REPORT_FILE,
            "trigger_evidence_convergence_report_file": TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE,
            "local_backtest_reliability_report_file": LOCAL_BACKTEST_RELIABILITY_REPORT_FILE,
            "current_config_file": CURRENT_CONFIG_FILE,
            "current_config_id": current_config.get("config_id"),
            "strategy_review_schema": strategy_review.get("schema"),
            "strategy_review_policy": (strategy_review.get("overall_policy") or {}).get("policy"),
            "simulation_performance_schema": simulation_performance.get("schema")
            if isinstance(simulation_performance, dict)
            else None,
            "simulation_performance_status": simulation_context.get("status"),
            "simulation_performance_report_status": simulation_context.get("report_status"),
            "execution_readiness_status": readiness_context.get("status"),
            "execution_readiness_ready_for_execute": readiness_context.get("ready_for_execute"),
            "strategy_learning_schema": strategy_learning.get("schema")
            if isinstance(strategy_learning, dict)
            else None,
            "strategy_learning_has_audit_pass_judgment_effect": learning_context.get(
                "has_audit_pass_judgment_effect"
            ),
            "trigger_evidence_convergence_schema": trigger_evidence_convergence.get("schema")
            if isinstance(trigger_evidence_convergence, dict)
            else None,
            "trigger_evidence_convergence_status": convergence_context.get("status"),
            "local_backtest_reliability_schema": local_backtest_reliability.get("schema")
            if isinstance(local_backtest_reliability, dict)
            else None,
            "local_backtest_reliability_status": local_backtest_context.get("status"),
            "max_simulation_performance_age_minutes": max_simulation_performance_age_minutes,
        },
        "proposal_hash": proposal_hash(proposed_config),
        "change_count": len(changes),
        "changes": changes,
        "proposed_config": proposed_config,
        "simulation_performance_context": simulation_context,
        "execution_readiness_context": readiness_context,
        "strategy_learning_context": learning_context,
        "trigger_evidence_convergence_context": convergence_context,
        "local_backtest_reliability_context": local_backtest_context,
        "promotion_blockers": promotion_blockers,
        "promotion_risk_warnings": promotion_risk_warnings,
        "promotion": {
            "copy_target": CURRENT_CONFIG_FILE,
            "requires_operator_review": True,
            "restart_required": "rt_signal_engine_v5.service",
            "do_not_auto_apply": True,
            "blocked": bool(promotion_blockers),
            "promotion_blockers": promotion_blockers,
            "promotion_risk_warnings": promotion_risk_warnings,
        },
        "warnings": config_warnings + proposed_warnings,
    }


def build_text_report(payload):
    lines = [
        f"Strategy config proposal {payload['generated_at']}",
        f"changes={payload['change_count']} current={payload['source']['current_config_id']} proposal={payload['proposal_hash']}",
        f"auto_applied={payload['source']['auto_applied']} manual_review_required={payload['source']['manual_review_required']}",
    ]
    sim_context = payload.get("simulation_performance_context") or {}
    if sim_context:
        remediation = sim_context.get("remediation_plan") or {}
        lines.append(
            "simulation_performance={status} remediation={hash} blockers={blockers} risk_warnings={warnings}".format(
                status=sim_context.get("status"),
                hash=remediation.get("proposal_hash"),
                blockers=len(payload.get("promotion_blockers") or []),
                warnings=len(payload.get("promotion_risk_warnings") or []),
            )
        )
    readiness = payload.get("execution_readiness_context") or {}
    if readiness:
        lines.append(
            "execution_readiness={status} ready={ready} blocking_gates={blocking} warning_gates={warning}".format(
                status=readiness.get("status"),
                ready=readiness.get("ready_for_execute"),
                blocking=readiness.get("blocking_gate_count"),
                warning=readiness.get("warning_gate_count"),
            )
        )
    learning = payload.get("strategy_learning_context") or {}
    if learning:
        coverage = learning.get("judgment_audit_coverage") or {}
        effect = learning.get("audit_pass_judgment_effect") or {}
        approved = effect.get("approved_or_reduced") or {}
        rejected = effect.get("rejected_or_held") or {}
        lines.append(
            "strategy_learning=audit_pass_effect:{has_effect} approved_n={approved_n} rejected_n={rejected_n} "
            "audit_fail={audit_fail} audit_missing={audit_missing}".format(
                has_effect=learning.get("has_audit_pass_judgment_effect"),
                approved_n=approved.get("resolved_count"),
                rejected_n=rejected.get("resolved_count"),
                audit_fail=coverage.get("audit_fail_count"),
                audit_missing=coverage.get("audit_missing_count"),
            )
        )
    convergence = payload.get("trigger_evidence_convergence_context") or {}
    if convergence:
        lines.append(
            "trigger_convergence={status} risk={risk} replay_challenges={challenges} insufficient={insufficient}".format(
                status=convergence.get("status"),
                risk=convergence.get("converged_risk_count"),
                challenges=convergence.get("replay_challenges_forward_count"),
                insufficient=convergence.get("insufficient_forward_sample_count"),
            )
        )
    local_backtest = payload.get("local_backtest_reliability_context") or {}
    if local_backtest:
        lines.append(
            "local_backtest={status} promotion_ready={promotion_ready} dataset={dataset} best={best}".format(
                status=local_backtest.get("status"),
                promotion_ready=local_backtest.get("promotion_ready"),
                dataset=local_backtest.get("dataset_status"),
                best=local_backtest.get("best_backtest_by_sharpe"),
            )
        )
    for change in payload.get("changes", [])[:12]:
        lines.append(f"  {change['key']}: {change['policy']} -> {change['to']}")
    if payload.get("promotion_blockers"):
        lines.append(
            "Promotion blockers: "
            + ", ".join(blocker.get("code", "unknown") for blocker in payload["promotion_blockers"])
        )
    if payload.get("promotion_risk_warnings"):
        lines.append(
            "Promotion risk warnings: "
            + ", ".join(warning.get("code", "unknown") for warning in payload["promotion_risk_warnings"])
        )
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-review-file", default=STRATEGY_REVIEW_REPORT_FILE)
    parser.add_argument("--current-config-file", default=CURRENT_CONFIG_FILE)
    parser.add_argument("--simulation-performance-file", default=SIMULATION_PERFORMANCE_REPORT_FILE)
    parser.add_argument("--execution-readiness-file", default=EXECUTION_READINESS_REPORT_FILE)
    parser.add_argument("--strategy-learning-file", default=STRATEGY_LEARNING_REPORT_FILE)
    parser.add_argument("--trigger-evidence-convergence-file", default=TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE)
    parser.add_argument("--local-backtest-reliability-file", default=LOCAL_BACKTEST_RELIABILITY_REPORT_FILE)
    parser.add_argument(
        "--max-simulation-performance-age-minutes",
        type=float,
        default=MAX_SIMULATION_PERFORMANCE_AGE_MINUTES,
    )
    parser.add_argument("--output", default=PROPOSAL_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global STRATEGY_REVIEW_REPORT_FILE, CURRENT_CONFIG_FILE, SIMULATION_PERFORMANCE_REPORT_FILE
    global EXECUTION_READINESS_REPORT_FILE, STRATEGY_LEARNING_REPORT_FILE, TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE
    global LOCAL_BACKTEST_RELIABILITY_REPORT_FILE
    STRATEGY_REVIEW_REPORT_FILE = args.strategy_review_file
    CURRENT_CONFIG_FILE = args.current_config_file
    SIMULATION_PERFORMANCE_REPORT_FILE = args.simulation_performance_file
    EXECUTION_READINESS_REPORT_FILE = args.execution_readiness_file
    STRATEGY_LEARNING_REPORT_FILE = args.strategy_learning_file
    TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE = args.trigger_evidence_convergence_file
    LOCAL_BACKTEST_RELIABILITY_REPORT_FILE = args.local_backtest_reliability_file
    payload = build_report(max_simulation_performance_age_minutes=args.max_simulation_performance_age_minutes)
    if args.output:
        save_json_atomic(args.output, payload)
    text = build_text_report(payload)
    if args.text:
        print(text)
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
