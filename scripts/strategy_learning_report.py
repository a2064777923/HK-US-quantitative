#!/usr/bin/env python3
"""Read-only cross-stage learning report for v5 alerts, Hermes, intake, and outcomes."""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime

try:
    import rt_order_intake as intake
except ImportError:
    from scripts import rt_order_intake as intake


ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
JUDGMENT_FILE = os.environ.get("RT_ORDER_JUDGMENT_FILE", "/tmp/hermes_trade_judgments.jsonl")
JUDGMENT_AUDIT_FILE = os.environ.get("HERMES_JUDGMENT_AUDIT_FILE", "/tmp/hermes_judgment_audit_report.json")
INTAKE_STATE_FILE = os.environ.get("RT_ORDER_STATE_FILE", "/tmp/rt_order_intake_state.json")
OUTCOME_REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
WATCHLIST_DIFF_REPORT_FILE = os.environ.get("WATCHLIST_DIFF_REPORT_FILE", "/tmp/watchlist_diff_report.json")
REPORT_FILE = os.environ.get("STRATEGY_LEARNING_REPORT_FILE", "/tmp/strategy_learning_report.json")
DEFAULT_QUEUE_SCAN_LIMIT = int(os.environ.get("STRATEGY_LEARNING_QUEUE_SCAN_LIMIT", "2000"))
DEFAULT_HORIZON = os.environ.get("STRATEGY_LEARNING_HORIZON", "1d")
MIN_LEARNING_SAMPLE = int(os.environ.get("STRATEGY_LEARNING_MIN_SAMPLE", "5"))
DEFAULT_SAMPLE_SCOPE_MODE = os.environ.get("STRATEGY_LEARNING_SAMPLE_SCOPE", "current")
INTRADAY_SUPPORT_ALIGNMENTS = ("supports_signal", "supports_with_limits")
INTRADAY_CHALLENGE_ALIGNMENTS = ("challenges_signal",)
INTRADAY_ALIGNMENT_ALIASES = {
    "conflicting_intraday_context": "conflicting_timeframes",
    "insufficient_intraday_context": "neutral_or_insufficient",
    "missing_minute_rows_before_signal": "unavailable_or_stale",
    "missing_signal_timestamp_or_symbol": "unavailable_or_stale",
    "missing_intraday_signal_context": "unavailable_or_stale",
}
REQUIRED_CONTEXT_REVIEW_FLAGS = (
    "technical_signal_reviewed",
    "portfolio_risk_reviewed",
    "strategy_evidence_reviewed",
    "data_health_reviewed",
    "execution_readiness_reviewed",
    "market_context_reviewed",
    "intraday_context_reviewed",
    "external_market_context_reviewed",
    "event_catalysts_reviewed",
    "event_catalyst_signals_reviewed",
    "market_sentiment_reviewed",
    "fundamentals_context_reviewed",
    "source_reliability_reviewed",
    "simulation_performance_reviewed",
    "cron_wiring_reviewed",
)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


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


def load_json_file(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def load_jsonl_tail(path, limit=DEFAULT_QUEUE_SCAN_LIMIT):
    rows = deque(maxlen=limit if limit and limit > 0 else None)
    warnings = []
    invalid = 0
    total = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                total = line_no
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if isinstance(loaded, dict):
                    rows.append(loaded)
                else:
                    invalid += 1
    except FileNotFoundError:
        warnings.append(f"alert_queue_missing:{path}")
    except Exception as exc:
        warnings.append(f"alert_queue_read_failed:{exc}")
    if invalid:
        warnings.append(f"alert_queue_invalid_lines:{invalid}")
    return list(rows), {"path": path, "total_lines": total, "loaded_rows": len(rows), "invalid_lines": invalid}, warnings


def signal_id(payload):
    return intake.signal_id(payload)


def latest_by_signal_id(items, time_keys=("generated_at", "checked_at", "reviewed_at", "submitted_at")):
    by_id = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        sid = str(signal_id(item)).strip()
        if not sid:
            continue
        by_id[sid] = item
    return by_id


def load_alerts(path=ALERT_QUEUE_FILE, limit=DEFAULT_QUEUE_SCAN_LIMIT):
    rows, stats, warnings = load_jsonl_tail(path, limit)
    return latest_by_signal_id(rows), stats, warnings


def load_judgments(path=JUDGMENT_FILE):
    judgments = intake.load_judgments(path)
    return latest_by_signal_id(judgments, time_keys=("reviewed_at", "created_at")), {
        "path": path,
        "judgment_count": len(judgments),
    }


def load_judgment_audit(path=JUDGMENT_AUDIT_FILE):
    if not path or not os.path.exists(path):
        return {}, {
            "path": path,
            "available": False,
            "schema": None,
            "status": "MISSING",
            "generated_at": None,
            "judgment_count": 0,
            "audited_judgment_count": 0,
            "report_truncated": False,
        }, [f"judgment_audit_missing:{path}"]
    report = load_json_file(path)
    rows = report.get("judgments") if isinstance(report.get("judgments"), list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    judgment_count = int(counts.get("judgment_count") or len(rows))
    return latest_by_signal_id(rows), {
        "path": path,
        "available": True,
        "schema": report.get("schema"),
        "status": report.get("status"),
        "generated_at": report.get("generated_at"),
        "judgment_count": judgment_count,
        "audited_judgment_count": len(rows),
        "report_truncated": judgment_count > len(rows),
        "status_counts": counts.get("status_counts") or {},
        "reason_counts": counts.get("reason_counts") or {},
    }, []


def load_intake_decisions(path=INTAKE_STATE_FILE):
    state = intake.load_state(path)
    decisions = {}
    for ledger in ("dry_runs", "processed"):
        for sid, decision in (state.get(ledger) or {}).items():
            if not isinstance(decision, dict):
                continue
            item = dict(decision)
            item["_ledger"] = ledger
            decisions[str(item.get("signal_id") or sid)] = item
    return decisions, {
        "path": path,
        "dry_run_count": len(state.get("dry_runs") or {}),
        "processed_count": len(state.get("processed") or {}),
    }


def outcome_evaluations(report):
    evaluations = report.get("evaluations")
    if isinstance(evaluations, list):
        return [item for item in evaluations if isinstance(item, dict)], False
    recent = report.get("recent_evaluations")
    if isinstance(recent, list):
        return [item for item in recent if isinstance(item, dict)], True
    return [], False


def load_outcomes(path=OUTCOME_REPORT_FILE):
    report = load_json_file(path)
    rows, recent_only = outcome_evaluations(report)
    return latest_by_signal_id(rows), {
        "path": path,
        "schema": report.get("schema"),
        "status": report.get("status"),
        "generated_at": report.get("generated_at"),
        "sample_scope": report.get("sample_scope"),
        "evaluated_signal_count": report.get("evaluated_signal_count"),
        "resolved_signal_count": report.get("resolved_signal_count"),
        "pending_signal_count": report.get("pending_signal_count"),
        "recent_only": recent_only,
        "outcome_maturity": report.get("outcome_maturity") if isinstance(report.get("outcome_maturity"), dict) else {},
    }


def is_directional_payload(payload):
    return str((payload or {}).get("signal_type") or "").upper() in ("BUY", "SELL")


def infer_current_sample_scope(alerts, sample_scope_mode=DEFAULT_SAMPLE_SCOPE_MODE):
    if sample_scope_mode == "all":
        return {
            "mode": "all_joined_signals",
            "strategy_config_id": None,
            "watchlist_id": None,
            "latest_signal_id": None,
        }
    latest = None
    latest_key = ""
    for alert in alerts.values():
        if not is_directional_payload(alert):
            continue
        strategy_config_id = alert.get("strategy_config_id")
        watchlist_id = alert.get("watchlist_id")
        if not strategy_config_id or not watchlist_id:
            continue
        key = str(alert.get("generated_at") or alert.get("quote_time") or alert.get("time") or "")
        if latest is None or key >= latest_key:
            latest = alert
            latest_key = key
    if not latest:
        return {
            "mode": "all_joined_signals",
            "strategy_config_id": None,
            "watchlist_id": None,
            "latest_signal_id": None,
        }
    return {
        "mode": "latest_strategy_config_and_watchlist",
        "strategy_config_id": str(latest.get("strategy_config_id")),
        "watchlist_id": str(latest.get("watchlist_id")),
        "latest_signal_id": signal_id(latest),
    }


def row_matches_scope(row, scope):
    if (scope or {}).get("mode") != "latest_strategy_config_and_watchlist":
        return True
    return (
        str(row.get("strategy_config_id") or "") == scope.get("strategy_config_id")
        and str(row.get("watchlist_id") or "") == scope.get("watchlist_id")
    )


def apply_sample_scope(rows, scope):
    scoped = [row for row in rows if row_matches_scope(row, scope)]
    scope = dict(scope or {})
    scope.update(
        {
            "joined_signal_count_before_filter": len(rows),
            "joined_signal_count": len(scoped),
            "excluded_joined_signal_count": len(rows) - len(scoped),
        }
    )
    return scoped, scope


def trigger_key(alert, outcome=None):
    source = alert or outcome or {}
    return f"{str(source.get('signal_type') or 'UNKNOWN').upper()}:{source.get('trigger') or 'UNKNOWN'}"


def signed_return(outcome, horizon=DEFAULT_HORIZON):
    row = ((outcome or {}).get("outcomes") or {}).get(horizon) or {}
    value = row.get("signed_close_return_pct")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def outcome_status(outcome, horizon=DEFAULT_HORIZON):
    if not outcome:
        return "missing"
    row = ((outcome.get("outcomes") or {}).get(horizon) or {})
    return row.get("status") or outcome.get("status") or "missing"


def intraday_signal_alignment(outcome):
    context = outcome.get("intraday_signal_context") if isinstance((outcome or {}).get("intraday_signal_context"), dict) else {}
    return normalize_intraday_signal_alignment(context.get("alignment"))


def normalize_intraday_signal_alignment(value):
    text = str(value or "unavailable_or_stale").strip() or "unavailable_or_stale"
    return INTRADAY_ALIGNMENT_ALIASES.get(text, text)


def judgment_decision(judgment):
    return str((judgment or {}).get("decision") or "missing").strip().lower() or "missing"


def context_review_summary(judgment):
    if not judgment:
        return {
            "present": False,
            "complete": False,
            "missing_flags": list(REQUIRED_CONTEXT_REVIEW_FLAGS),
            "reviewed_count": 0,
            "required_count": len(REQUIRED_CONTEXT_REVIEW_FLAGS),
        }
    review = judgment.get("context_review")
    if not isinstance(review, dict):
        return {
            "present": False,
            "complete": False,
            "missing_flags": list(REQUIRED_CONTEXT_REVIEW_FLAGS),
            "reviewed_count": 0,
            "required_count": len(REQUIRED_CONTEXT_REVIEW_FLAGS),
        }
    missing = [flag for flag in REQUIRED_CONTEXT_REVIEW_FLAGS if review.get(flag) is not True]
    return {
        "present": True,
        "complete": not missing,
        "missing_flags": missing,
        "reviewed_count": len(REQUIRED_CONTEXT_REVIEW_FLAGS) - len(missing),
        "required_count": len(REQUIRED_CONTEXT_REVIEW_FLAGS),
    }


def context_review_cohort(row):
    decision = row.get("judgment_decision")
    if decision in ("approve", "reduce"):
        return "approved_or_reduced_context_complete" if row.get("context_review_complete") else "approved_or_reduced_context_incomplete"
    if decision in ("reject", "hold"):
        return "rejected_or_held"
    return "missing_judgment"


def intake_status(decision):
    return str((decision or {}).get("status") or "missing")


def effective_intake_reasons(decision):
    """Return top-level and dry-run gate blockers through one intake-decision interface."""
    if not decision:
        return []
    reasons = []
    seen = set()

    def add(reason):
        text = str(reason or "").strip()
        if text and text not in seen:
            seen.add(text)
            reasons.append(text)

    for reason in (decision or {}).get("reasons") or []:
        add(reason)

    gate_specs = (
        ("execution_readiness", "execution_readiness_would_block_execute"),
        ("strategy_evidence", "strategy_evidence_would_block_execute"),
        ("symbol_conflict", "symbol_conflict_would_block_execute"),
        ("market_context", "market_context_would_block_execute"),
    )
    for field, blocker in gate_specs:
        gate = decision.get(field) if isinstance(decision.get(field), dict) else {}
        if gate.get("would_block_execute") or gate.get("status") == "REJECTED":
            add(blocker)
            for reason in gate.get("reasons") or []:
                add(f"{field}:{reason}")
    return reasons


def intake_reason_bucket(decision):
    if not decision:
        return "missing_intake_decision"
    reasons = effective_intake_reasons(decision)
    if reasons:
        return str(reasons[0])
    if intake_status(decision) == "dry_run":
        return "accepted_dry_run"
    if intake_status(decision) == "submitted":
        return "submitted"
    return "no_reason"


def actionability_category(decision):
    if not decision:
        return "missing_intake_decision"
    status = intake_status(decision)
    reasons = set(effective_intake_reasons(decision))
    if status in ("dry_run", "submitted"):
        if "execution_readiness_would_block_execute" in reasons:
            return "blocked_execution_readiness"
        if "strategy_evidence_would_block_execute" in reasons:
            return "blocked_strategy_evidence"
        if "symbol_conflict_would_block_execute" in reasons:
            return "blocked_symbol_conflict"
        if "market_context_would_block_execute" in reasons:
            return "blocked_market_context"
        return "trade_candidate"
    if reasons & {"execution_readiness_gate_failed", "execution_readiness_would_block_execute"}:
        return "blocked_execution_readiness"
    if reasons & {"health_gate_failed"}:
        return "blocked_system_health"
    if reasons & {"hermes_judgment_gate_failed"}:
        return "blocked_hermes_judgment"
    if reasons & {"market_context_gate_failed", "market_context_would_block_execute"}:
        return "blocked_market_context"
    if "alert_too_old" in reasons:
        return "observation_only_stale_alert"
    if "sell_without_position" in reasons:
        return "observation_only_no_position"
    if reasons & {"strategy_evidence_gate_failed", "strategy_evidence_would_block_execute"}:
        return "blocked_strategy_evidence"
    if reasons & {"symbol_conflict_opposite_direction_in_queue", "symbol_conflict_would_block_execute"}:
        return "blocked_symbol_conflict"
    if reasons & {"quantity_zero_after_risk_and_lot_rounding", "sell_quantity_zero", "reduced_quantity_zero"}:
        return "blocked_sizing_or_lot"
    if reasons & {"already_holding_symbol", "max_positions_reached", "insufficient_cash_after_rounding"}:
        return "blocked_portfolio_constraint"
    if reasons:
        return "blocked_validation_or_context"
    if status == "rejected":
        return "blocked_without_reason"
    return "unknown_actionability"


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sizing_diagnostics(alert, decision):
    reasons = set((decision or {}).get("reasons") or [])
    if "quantity_zero_after_risk_and_lot_rounding" not in reasons:
        return None
    side = str((alert or {}).get("signal_type") or "").upper()
    if side != "BUY":
        return None
    symbol = str((alert or {}).get("symbol") or "").strip().upper()
    entry = as_float((alert or {}).get("entry_price"))
    stop = as_float((alert or {}).get("stop_loss"))
    if not symbol or entry is None or stop is None or entry <= 0:
        return {
            "status": "insufficient_alert_price_fields",
            "symbol": symbol,
            "reason": "quantity_zero_after_risk_and_lot_rounding",
        }

    context = (decision or {}).get("context") or {}
    equity = as_float(context.get("equity_hkd")) or intake.DEFAULT_EQUITY_HKD
    cash = as_float(context.get("cash_hkd")) or equity
    fx = intake.fx_to_hkd(symbol)
    lot = intake.lot_size(symbol)
    risk_per_share_hkd = abs(entry - stop) * fx
    max_loss_hkd = equity * intake.MAX_RISK_PCT
    max_alloc_hkd = min(equity * intake.POSITION_SIZE_PCT, cash)
    quantity_by_risk = max_loss_hkd / risk_per_share_hkd if risk_per_share_hkd > 0 else 0
    quantity_by_alloc = max_alloc_hkd / (entry * fx)
    raw_quantity = min(quantity_by_risk, quantity_by_alloc)
    rounded_quantity = intake.round_down_lot(raw_quantity, symbol)
    min_lot_notional_hkd = lot * entry * fx
    min_lot_risk_hkd = lot * risk_per_share_hkd
    binding_limits = []
    if max_loss_hkd < min_lot_risk_hkd:
        binding_limits.append("risk_budget_below_one_lot")
    if max_alloc_hkd < min_lot_notional_hkd:
        binding_limits.append("allocation_budget_below_one_lot")
    if cash < min_lot_notional_hkd:
        binding_limits.append("cash_below_one_lot")
    if raw_quantity < lot:
        binding_limits.append("raw_quantity_below_lot_size")
    return {
        "status": "diagnosed",
        "symbol": symbol,
        "market": intake.alert_market(alert or {}),
        "entry_price": entry,
        "stop_loss": stop,
        "fx_to_hkd": fx,
        "lot_size": lot,
        "cash_hkd": round(cash, 2),
        "equity_hkd": round(equity, 2),
        "position_size_pct": intake.POSITION_SIZE_PCT,
        "max_risk_pct": intake.MAX_RISK_PCT,
        "risk_per_share_hkd": round(risk_per_share_hkd, 4),
        "max_loss_hkd": round(max_loss_hkd, 2),
        "max_alloc_hkd": round(max_alloc_hkd, 2),
        "quantity_by_risk": round(quantity_by_risk, 4),
        "quantity_by_alloc": round(quantity_by_alloc, 4),
        "raw_quantity_before_lot": round(raw_quantity, 4),
        "rounded_quantity": rounded_quantity,
        "min_lot_notional_hkd": round(min_lot_notional_hkd, 2),
        "min_lot_risk_hkd": round(min_lot_risk_hkd, 2),
        "min_equity_for_one_lot_by_alloc_hkd": round(min_lot_notional_hkd / intake.POSITION_SIZE_PCT, 2)
        if intake.POSITION_SIZE_PCT > 0
        else None,
        "min_equity_for_one_lot_by_risk_hkd": round(min_lot_risk_hkd / intake.MAX_RISK_PCT, 2)
        if intake.MAX_RISK_PCT > 0
        else None,
        "binding_limits": binding_limits or ["unknown_sizing_limit"],
    }


def audit_status_for_row(judgment, audit_row):
    if not judgment:
        return "not_applicable"
    if not audit_row:
        return "MISSING"
    return str(audit_row.get("status") or "MISSING").strip().upper() or "MISSING"


def build_join_rows(alerts, judgments, intake_decisions, outcomes, judgment_audit=None, horizon=DEFAULT_HORIZON):
    judgment_audit = judgment_audit or {}
    ids = sorted(set(alerts) | set(judgments) | set(intake_decisions) | set(outcomes))
    rows = []
    for sid in ids:
        alert = alerts.get(sid) or {}
        judgment = judgments.get(sid) or {}
        audit_row = judgment_audit.get(sid) or {}
        decision = intake_decisions.get(sid) or {}
        outcome = outcomes.get(sid) or {}
        ret = signed_return(outcome, horizon=horizon)
        context_review = context_review_summary(judgment)
        audit_status = audit_status_for_row(judgment, audit_row)
        audit_reasons = audit_row.get("reasons") if isinstance(audit_row.get("reasons"), list) else []
        row = {
            "signal_id": sid,
            "symbol": alert.get("symbol") or outcome.get("symbol"),
            "market": alert.get("market") or outcome.get("market"),
            "strategy_config_id": alert.get("strategy_config_id") or outcome.get("strategy_config_id"),
            "watchlist_id": alert.get("watchlist_id") or outcome.get("watchlist_id"),
            "trigger_key": trigger_key(alert, outcome),
            "signal_type": str((alert or outcome).get("signal_type") or "").upper(),
            "confirmed": alert.get("confirmed", outcome.get("confirmed")),
            "full_score": alert.get("full_score", outcome.get("full_score")),
            "judgment_decision": judgment_decision(judgment),
            "judgment_confidence": judgment.get("confidence"),
            "judgment_audit_status": audit_status,
            "judgment_audit_pass": audit_status == "PASS",
            "judgment_audit_reasons": audit_reasons,
            "judgment_audit_packet_source": audit_row.get("packet_source"),
            "context_review_present": context_review["present"],
            "context_review_complete": context_review["complete"],
            "context_review_missing_flags": context_review["missing_flags"],
            "context_review_reviewed_count": context_review["reviewed_count"],
            "context_review_required_count": context_review["required_count"],
            "context_review_cohort": None,
            "intake_status": intake_status(decision),
            "intake_ledger": decision.get("_ledger"),
            "intake_reason_bucket": intake_reason_bucket(decision),
            "actionability_category": actionability_category(decision),
            "outcome_status": outcome_status(outcome, horizon=horizon),
            "intraday_signal_alignment": intraday_signal_alignment(outcome),
            "signed_return_pct": ret,
            "win": ret is not None and ret > 0,
            "generated_at": alert.get("generated_at") or outcome.get("generated_at"),
        }
        row["context_review_cohort"] = context_review_cohort(row)
        diagnostics = sizing_diagnostics(alert, decision)
        if diagnostics:
            row["sizing_diagnostics"] = diagnostics
        rows.append(row)
    return rows


def metric_summary(rows):
    resolved = [row for row in rows if row.get("signed_return_pct") is not None]
    avg = None
    win_rate = None
    if resolved:
        avg = round(sum(row["signed_return_pct"] for row in resolved) / len(resolved), 4)
        win_rate = round(len([row for row in resolved if row["signed_return_pct"] > 0]) / len(resolved) * 100, 2)
    return {
        "count": len(rows),
        "resolved_count": len(resolved),
        "pending_or_missing_count": len(rows) - len(resolved),
        "avg_signed_return_pct": avg,
        "win_rate_pct": win_rate,
    }


def grouped_summary(rows, key_fn):
    groups = defaultdict(list)
    for row in rows:
        groups[key_fn(row)].append(row)
    out = []
    for key, items in sorted(groups.items(), key=lambda pair: str(pair[0])):
        row = {"key": str(key)}
        row.update(metric_summary(items))
        out.append(row)
    return out


def intraday_alignment_rows(rows, alignments):
    wanted = set(alignments or [])
    return [
        row
        for row in rows
        if normalize_intraday_signal_alignment(row.get("intraday_signal_alignment")) in wanted
    ]


def intraday_alignment_effect(rows, minimum_sample=MIN_LEARNING_SAMPLE):
    groups = grouped_summary(
        rows,
        lambda row: normalize_intraday_signal_alignment(row.get("intraday_signal_alignment")),
    )
    support = metric_summary(intraday_alignment_rows(rows, INTRADAY_SUPPORT_ALIGNMENTS))
    challenge = metric_summary(intraday_alignment_rows(rows, INTRADAY_CHALLENGE_ALIGNMENTS))
    support_avg = support.get("avg_signed_return_pct")
    challenge_avg = challenge.get("avg_signed_return_pct")
    delta = round(support_avg - challenge_avg, 6) if support_avg is not None and challenge_avg is not None else None
    reasons = []
    if not rows:
        reasons.append("intraday_alignment_learning_rows_missing")
    if support.get("resolved_count", 0) < minimum_sample:
        reasons.append("support_alignment_sample_below_minimum")
    if challenge.get("resolved_count", 0) < minimum_sample:
        reasons.append("challenge_alignment_sample_below_minimum")
    if support_avg is None:
        reasons.append("support_alignment_avg_return_missing")
    elif support_avg <= 0:
        reasons.append("support_alignment_avg_return_not_positive")
    if challenge_avg is None:
        reasons.append("challenge_alignment_avg_return_missing")
    elif challenge_avg > 0:
        reasons.append("challenge_alignment_avg_return_positive")
    if delta is None:
        reasons.append("support_vs_challenge_delta_missing")
    elif delta <= 0:
        reasons.append("support_alignment_not_outperforming_challenge")

    insufficient_reasons = {
        "intraday_alignment_learning_rows_missing",
        "support_alignment_sample_below_minimum",
        "challenge_alignment_sample_below_minimum",
        "support_alignment_avg_return_missing",
        "challenge_alignment_avg_return_missing",
        "support_vs_challenge_delta_missing",
    }
    negative_reasons = {
        "support_alignment_avg_return_not_positive",
        "challenge_alignment_avg_return_positive",
        "support_alignment_not_outperforming_challenge",
    }
    if "intraday_alignment_learning_rows_missing" in reasons:
        status = "MISSING"
    elif any(reason in reasons for reason in insufficient_reasons):
        status = "INSUFFICIENT"
    elif any(reason in reasons for reason in negative_reasons):
        status = "NEGATIVE"
    else:
        status = "SUPPORTIVE"

    if status == "SUPPORTIVE":
        hermes_note = "intraday_alignment_supportive_as_soft_confirmation_not_execution_permission"
        policy = "use_support_as_soft_confirmation_and_challenge_as_confidence_cap"
    elif status == "NEGATIVE":
        hermes_note = "intraday_alignment_not_validated_review_labeling_thresholds_or_market_regime"
        policy = "do_not_use_intraday_alignment_as_filter_until_forward_evidence_improves"
    elif status == "MISSING":
        hermes_note = "intraday_alignment_learning_missing_keep_intraday_context_diagnostic_only"
        policy = "collect_signal_time_minute_context_before_using_alignment"
    else:
        hermes_note = "intraday_alignment_samples_below_threshold_keep_collecting_before_using_as_hard_rule"
        policy = "keep_alignment_read_only_until_support_and_challenge_samples_mature"

    return {
        "schema": "intraday_alignment_effect_v1",
        "read_only": True,
        "submits_orders": False,
        "status": status,
        "minimum_sample": minimum_sample,
        "support_alignments": list(INTRADAY_SUPPORT_ALIGNMENTS),
        "challenge_alignments": list(INTRADAY_CHALLENGE_ALIGNMENTS),
        "supports_signal_like": support,
        "challenges_signal": challenge,
        "support_vs_challenge_delta_pct": delta,
        "groups": groups,
        "reasons": reasons,
        "hermes_note": hermes_note,
        "policy": policy,
    }


def compare_judgment_effect(rows):
    approved = [row for row in rows if row["judgment_decision"] in ("approve", "reduce")]
    rejected = [row for row in rows if row["judgment_decision"] in ("reject", "hold")]
    missing = [row for row in rows if row["judgment_decision"] == "missing"]
    return {
        "approved_or_reduced": metric_summary(approved),
        "rejected_or_held": metric_summary(rejected),
        "missing_judgment": metric_summary(missing),
    }


def compare_audit_pass_judgment_effect(rows):
    approved_pass = [
        row
        for row in rows
        if row.get("judgment_decision") in ("approve", "reduce")
        and row.get("judgment_audit_status") == "PASS"
    ]
    rejected_pass = [
        row
        for row in rows
        if row.get("judgment_decision") in ("reject", "hold")
        and row.get("judgment_audit_status") == "PASS"
    ]
    approved_excluded = [
        row
        for row in rows
        if row.get("judgment_decision") in ("approve", "reduce")
        and row.get("judgment_audit_status") != "PASS"
    ]
    rejected_excluded = [
        row
        for row in rows
        if row.get("judgment_decision") in ("reject", "hold")
        and row.get("judgment_audit_status") != "PASS"
    ]
    return {
        "sample_filter": "judgment_audit_status_PASS",
        "approved_or_reduced": metric_summary(approved_pass),
        "rejected_or_held": metric_summary(rejected_pass),
        "missing_judgment": metric_summary([row for row in rows if row.get("judgment_decision") == "missing"]),
        "excluded_approved_or_reduced": metric_summary(approved_excluded),
        "excluded_rejected_or_held": metric_summary(rejected_excluded),
    }


def compare_context_review_effect(rows):
    return {
        "approved_or_reduced_context_complete": metric_summary(
            [row for row in rows if row.get("context_review_cohort") == "approved_or_reduced_context_complete"]
        ),
        "approved_or_reduced_context_incomplete": metric_summary(
            [row for row in rows if row.get("context_review_cohort") == "approved_or_reduced_context_incomplete"]
        ),
        "rejected_or_held": metric_summary(
            [row for row in rows if row.get("context_review_cohort") == "rejected_or_held"]
        ),
        "missing_judgment": metric_summary(
            [row for row in rows if row.get("context_review_cohort") == "missing_judgment"]
        ),
    }


def build_context_review_quality(rows):
    approval_rows = [row for row in rows if row.get("judgment_decision") in ("approve", "reduce")]
    missing_counter = Counter()
    for row in approval_rows:
        for flag in row.get("context_review_missing_flags") or []:
            missing_counter[flag] += 1
    complete_count = len([row for row in approval_rows if row.get("context_review_complete")])
    return {
        "required_flags": list(REQUIRED_CONTEXT_REVIEW_FLAGS),
        "approved_or_reduced_count": len(approval_rows),
        "complete_context_review_count": complete_count,
        "incomplete_context_review_count": len(approval_rows) - complete_count,
        "complete_context_review_pct": round(complete_count / len(approval_rows) * 100, 2)
        if approval_rows
        else None,
        "missing_flag_counts": [
            {"key": key, "count": count}
            for key, count in missing_counter.most_common()
        ],
    }


def build_judgment_audit_coverage(rows, audit_stats):
    judgment_rows = [row for row in rows if row.get("judgment_decision") != "missing"]
    approval_rows = [row for row in judgment_rows if row.get("judgment_decision") in ("approve", "reduce")]
    rejected_rows = [row for row in judgment_rows if row.get("judgment_decision") in ("reject", "hold")]
    missing_rows = [row for row in judgment_rows if row.get("judgment_audit_status") == "MISSING"]
    failed_rows = [row for row in judgment_rows if row.get("judgment_audit_status") == "FAIL"]
    pass_rows = [row for row in judgment_rows if row.get("judgment_audit_status") == "PASS"]
    reason_counter = Counter()
    for row in failed_rows:
        for reason in row.get("judgment_audit_reasons") or []:
            reason_counter[str(reason)] += 1
    return {
        "audit_report_available": bool((audit_stats or {}).get("available")),
        "audit_report_status": (audit_stats or {}).get("status"),
        "audit_report_generated_at": (audit_stats or {}).get("generated_at"),
        "audit_report_truncated": bool((audit_stats or {}).get("report_truncated")),
        "joined_judgment_count": len(judgment_rows),
        "audit_pass_count": len(pass_rows),
        "audit_fail_count": len(failed_rows),
        "audit_missing_count": len(missing_rows),
        "approved_or_reduced_count": len(approval_rows),
        "approved_or_reduced_audit_pass_count": len(
            [row for row in approval_rows if row.get("judgment_audit_status") == "PASS"]
        ),
        "approved_or_reduced_audit_fail_or_missing_count": len(
            [row for row in approval_rows if row.get("judgment_audit_status") != "PASS"]
        ),
        "rejected_or_held_count": len(rejected_rows),
        "rejected_or_held_audit_pass_count": len(
            [row for row in rejected_rows if row.get("judgment_audit_status") == "PASS"]
        ),
        "rejected_or_held_audit_fail_or_missing_count": len(
            [row for row in rejected_rows if row.get("judgment_audit_status") != "PASS"]
        ),
        "failed_reason_counts": [{"key": key, "count": count} for key, count in reason_counter.most_common()],
    }


def build_sizing_blocker_diagnostics(rows):
    diagnostics = [row for row in rows if row.get("sizing_diagnostics")]
    limit_counts = Counter()
    symbol_counts = Counter()
    for row in diagnostics:
        diag = row["sizing_diagnostics"]
        symbol_counts[diag.get("symbol") or row.get("symbol") or "missing"] += 1
        for limit in diag.get("binding_limits") or []:
            limit_counts[limit] += 1
    examples = []
    for row in diagnostics[:20]:
        diag = row["sizing_diagnostics"]
        examples.append(
            {
                "signal_id": row["signal_id"],
                "symbol": row.get("symbol"),
                "trigger_key": row.get("trigger_key"),
                "generated_at": row.get("generated_at"),
                "binding_limits": diag.get("binding_limits"),
                "lot_size": diag.get("lot_size"),
                "raw_quantity_before_lot": diag.get("raw_quantity_before_lot"),
                "min_lot_notional_hkd": diag.get("min_lot_notional_hkd"),
                "min_lot_risk_hkd": diag.get("min_lot_risk_hkd"),
                "max_alloc_hkd": diag.get("max_alloc_hkd"),
                "max_loss_hkd": diag.get("max_loss_hkd"),
            }
        )
    return {
        "count": len(diagnostics),
        "by_binding_limit": [{"key": key, "count": count} for key, count in limit_counts.most_common()],
        "by_symbol": [{"key": key, "count": count} for key, count in symbol_counts.most_common()],
        "examples": examples,
    }


def proposal_remove_symbols(watchlist_diff_payload):
    proposal = (watchlist_diff_payload or {}).get("proposal") or {}
    out = {}
    for market, payload in (proposal.get("markets") or {}).items():
        out[str(market).upper()] = {
            str(symbol).upper()
            for symbol in payload.get("remove_symbols") or []
            if str(symbol or "").strip()
        }
    return out


def build_sizing_blocker_remediation(rows, watchlist_diff_payload):
    diagnostics = [row for row in rows if row.get("sizing_diagnostics")]
    remove_by_market = proposal_remove_symbols(watchlist_diff_payload)
    proposal_hash = ((watchlist_diff_payload or {}).get("proposal") or {}).get("proposal_hash")
    covered = []
    uncovered = []
    for row in diagnostics:
        symbol = str(row.get("symbol") or "").upper()
        market = str(row.get("market") or "").upper()
        if symbol in remove_by_market.get(market, set()):
            covered.append(row)
        else:
            uncovered.append(row)
    return {
        "schema": "sizing_blocker_remediation_v1",
        "watchlist_proposal_hash": proposal_hash,
        "sizing_blocker_count": len(diagnostics),
        "covered_by_watchlist_removal_count": len(covered),
        "uncovered_count": len(uncovered),
        "coverage_pct": round(len(covered) / len(diagnostics) * 100, 2) if diagnostics else 0.0,
        "covered_symbols": sorted({row.get("symbol") for row in covered if row.get("symbol")}),
        "uncovered_symbols": sorted({row.get("symbol") for row in uncovered if row.get("symbol")}),
        "recommendation": (
            "review_watchlist_proposal_for_sizing_blockers"
            if covered
            else "no_watchlist_proposal_coverage_for_sizing_blockers"
        ),
    }


def build_intake_coverage(rows):
    def coverage_for(items):
        total = len(items)
        missing = [row for row in items if row.get("intake_reason_bucket") == "missing_intake_decision"]
        with_decision = total - len(missing)
        return {
            "joined_signal_count": total,
            "with_intake_decision_count": with_decision,
            "missing_intake_decision_count": len(missing),
            "coverage_pct": round(with_decision / total * 100, 2) if total else 0.0,
            "missing_pct": round(len(missing) / total * 100, 2) if total else 0.0,
        }

    missing = [row for row in rows if row.get("intake_reason_bucket") == "missing_intake_decision"]
    missing_by_trigger = Counter(row.get("trigger_key") or "missing" for row in missing)
    missing_by_symbol = Counter(row.get("symbol") or "missing" for row in missing)
    directional = [row for row in rows if row.get("signal_type") in ("BUY", "SELL")]
    watch = [row for row in rows if row.get("signal_type") == "WATCH"]
    other = [row for row in rows if row.get("signal_type") not in ("BUY", "SELL", "WATCH")]
    examples = [
        {
            "signal_id": row.get("signal_id"),
            "symbol": row.get("symbol"),
            "trigger_key": row.get("trigger_key"),
            "generated_at": row.get("generated_at"),
            "strategy_config_id": row.get("strategy_config_id"),
            "watchlist_id": row.get("watchlist_id"),
        }
        for row in missing[:30]
    ]
    out = coverage_for(rows)
    out.update(
        {
            "directional": coverage_for(directional),
            "watch": coverage_for(watch),
            "other": coverage_for(other),
        }
    )
    out.update({
        "missing_by_trigger": [
            {"key": key, "count": count}
            for key, count in missing_by_trigger.most_common(12)
        ],
        "missing_by_symbol": [
            {"key": key, "count": count}
            for key, count in missing_by_symbol.most_common(12)
        ],
        "missing_examples": examples,
    })
    return out


def build_recommendations(payload):
    recs = []
    overall = payload["overall"]
    if overall["resolved_count"] < MIN_LEARNING_SAMPLE:
        recs.append(f"learning_sample_below_{MIN_LEARNING_SAMPLE}_keep_collecting_outcomes")
    effect = payload["judgment_effect"]
    audit_effect = payload.get("audit_pass_judgment_effect") if isinstance(payload.get("audit_pass_judgment_effect"), dict) else None
    effect_for_prompt_review = audit_effect or effect
    approved_avg = effect["approved_or_reduced"].get("avg_signed_return_pct")
    rejected_avg = effect["rejected_or_held"].get("avg_signed_return_pct")
    if approved_avg is not None and rejected_avg is not None and approved_avg <= rejected_avg:
        recs.append("hermes_approval_not_outperforming_rejections_review_prompt_and_gates")
    audit_coverage = payload.get("judgment_audit_coverage") or {}
    if audit_coverage.get("approved_or_reduced_audit_fail_or_missing_count", 0) > 0:
        recs.append("audit_failed_or_missing_approvals_excluded_from_hermes_effect_learning")
    if audit_coverage.get("rejected_or_held_audit_fail_or_missing_count", 0) > 0:
        recs.append("audit_failed_or_missing_rejections_excluded_from_hermes_effect_learning")
    context_quality = payload.get("context_review_quality") or {}
    if context_quality.get("incomplete_context_review_count", 0) > 0:
        recs.append("context_review_incomplete_for_approved_or_reduced_judgments")
    context_effect = payload.get("context_review_effect") or {}
    complete = context_effect.get("approved_or_reduced_context_complete") or {}
    incomplete = context_effect.get("approved_or_reduced_context_incomplete") or {}
    rejected_context = context_effect.get("rejected_or_held") or {}
    complete_avg = complete.get("avg_signed_return_pct")
    incomplete_avg = incomplete.get("avg_signed_return_pct")
    rejected_context_avg = rejected_context.get("avg_signed_return_pct")
    if (
        complete.get("resolved_count", 0) >= MIN_LEARNING_SAMPLE
        and rejected_context.get("resolved_count", 0) >= MIN_LEARNING_SAMPLE
        and complete_avg is not None
        and rejected_context_avg is not None
        and complete_avg <= rejected_context_avg
    ):
        recs.append("context_reviewed_approvals_not_outperforming_rejections_review_prompt")
    if (
        complete.get("resolved_count", 0) >= MIN_LEARNING_SAMPLE
        and incomplete.get("resolved_count", 0) >= MIN_LEARNING_SAMPLE
        and complete_avg is not None
        and incomplete_avg is not None
        and complete_avg <= incomplete_avg
    ):
        recs.append("context_review_not_improving_approval_outcomes_review_context_usage")
    approved_effect = effect_for_prompt_review.get("approved_or_reduced") or {}
    rejected_effect = effect_for_prompt_review.get("rejected_or_held") or {}
    approved_effect_avg = approved_effect.get("avg_signed_return_pct")
    rejected_effect_avg = rejected_effect.get("avg_signed_return_pct")
    if (
        audit_effect
        and approved_effect.get("resolved_count", 0) >= MIN_LEARNING_SAMPLE
        and rejected_effect.get("resolved_count", 0) >= MIN_LEARNING_SAMPLE
        and approved_effect_avg is not None
        and rejected_effect_avg is not None
        and approved_effect_avg <= rejected_effect_avg
    ):
        recs.append("audit_pass_hermes_approval_not_outperforming_rejections_review_prompt_and_gates")
    weak_triggers = [
        row["key"]
        for row in payload["by_trigger"]
        if row["resolved_count"] >= MIN_LEARNING_SAMPLE and row.get("avg_signed_return_pct") is not None and row["avg_signed_return_pct"] <= 0
    ]
    for key in weak_triggers[:8]:
        recs.append(f"trigger_forward_return_non_positive:{key}")
    intraday_alignment = {
        normalize_intraday_signal_alignment(row.get("key")): row
        for row in payload.get("by_intraday_signal_alignment") or []
    }
    intraday_effect = payload.get("intraday_alignment_effect") or {}
    if intraday_effect.get("status") == "NEGATIVE":
        recs.append("intraday_alignment_effect_negative_keep_diagnostic_only")
    challenged = intraday_alignment.get("challenges_signal") or {}
    if challenged.get("resolved_count", 0) >= MIN_LEARNING_SAMPLE:
        challenged_avg = challenged.get("avg_signed_return_pct")
        if challenged_avg is not None and challenged_avg <= 0:
            recs.append("intraday_challenge_alignment_underperforms_consider_hermes_hold_rule")
        elif challenged_avg is not None and challenged_avg > 0:
            recs.append("intraday_challenge_alignment_profitable_review_context_labeling")
    sizing_remediation = payload.get("sizing_blocker_remediation") or {}
    sizing_blockers_fully_covered = (
        sizing_remediation.get("sizing_blocker_count", 0) >= MIN_LEARNING_SAMPLE
        and sizing_remediation.get("uncovered_count", 0) == 0
        and sizing_remediation.get("covered_by_watchlist_removal_count", 0)
        == sizing_remediation.get("sizing_blocker_count", 0)
    )
    blockers = [
        row
        for row in payload["by_intake_reason"]
        if row["key"]
        not in (
            "accepted_dry_run",
            "submitted",
            "missing_intake_decision",
            "no_reason",
            "sell_without_position",
            "alert_too_old",
            "quantity_zero_after_risk_and_lot_rounding" if sizing_blockers_fully_covered else "",
        )
    ]
    if blockers and blockers[0]["count"] >= MIN_LEARNING_SAMPLE:
        recs.append(f"dominant_intake_blocker:{blockers[0]['key']}")
    actionable_blockers = [
        row
        for row in payload.get("by_actionability") or []
        if row["key"]
        not in (
            "trade_candidate",
            "observation_only_no_position",
            "observation_only_stale_alert",
            "missing_intake_decision",
            "unknown_actionability",
            "blocked_sizing_or_lot" if sizing_blockers_fully_covered else "",
        )
    ]
    if actionable_blockers and actionable_blockers[0]["count"] >= MIN_LEARNING_SAMPLE:
        recs.append(f"dominant_actionability_blocker:{actionable_blockers[0]['key']}")
    sizing_limits = (payload.get("sizing_blocker_diagnostics") or {}).get("by_binding_limit") or []
    if not sizing_blockers_fully_covered and sizing_limits and sizing_limits[0]["count"] >= MIN_LEARNING_SAMPLE:
        recs.append(f"review_sizing_rule:{sizing_limits[0]['key']}")
    if sizing_remediation.get("covered_by_watchlist_removal_count", 0) >= MIN_LEARNING_SAMPLE:
        recs.append(
            "review_watchlist_proposal_for_sizing_blockers:"
            + str(sizing_remediation.get("watchlist_proposal_hash") or "missing_hash")
        )
    intake_coverage = payload.get("intake_coverage") or {}
    directional_coverage = intake_coverage.get("directional") or {}
    if (
        directional_coverage.get("joined_signal_count", 0) >= MIN_LEARNING_SAMPLE
        and directional_coverage.get("coverage_pct", 100) < 80
    ):
        recs.append("directional_intake_coverage_below_80pct_learning_incomplete")
    elif intake_coverage.get("joined_signal_count", 0) >= MIN_LEARNING_SAMPLE and intake_coverage.get("coverage_pct", 100) < 50:
        recs.append("overall_intake_coverage_below_50pct_due_to_observations")
    if not recs:
        recs.append("learning_report_ready_continue_collecting_and_compare_cohorts")
    return recs


def build_report(
    alert_queue_file=ALERT_QUEUE_FILE,
    judgment_file=JUDGMENT_FILE,
    judgment_audit_file=JUDGMENT_AUDIT_FILE,
    intake_state_file=INTAKE_STATE_FILE,
    outcome_report_file=OUTCOME_REPORT_FILE,
    watchlist_diff_report_file=WATCHLIST_DIFF_REPORT_FILE,
    horizon=DEFAULT_HORIZON,
    queue_scan_limit=DEFAULT_QUEUE_SCAN_LIMIT,
    sample_scope_mode=DEFAULT_SAMPLE_SCOPE_MODE,
):
    alerts, alert_stats, alert_warnings = load_alerts(alert_queue_file, queue_scan_limit)
    judgments, judgment_stats = load_judgments(judgment_file)
    judgment_audit, judgment_audit_stats, judgment_audit_warnings = load_judgment_audit(judgment_audit_file)
    intake_decisions, intake_stats = load_intake_decisions(intake_state_file)
    outcomes, outcome_stats = load_outcomes(outcome_report_file)
    watchlist_diff_payload = load_json_file(watchlist_diff_report_file)
    all_rows = build_join_rows(alerts, judgments, intake_decisions, outcomes, judgment_audit=judgment_audit, horizon=horizon)
    rows, sample_scope = apply_sample_scope(
        all_rows,
        infer_current_sample_scope(alerts, sample_scope_mode=sample_scope_mode),
    )
    intraday_groups = grouped_summary(
        rows,
        lambda row: normalize_intraday_signal_alignment(row.get("intraday_signal_alignment")),
    )
    payload = {
        "schema": "strategy_learning_report_v1",
        "generated_at": now_iso(),
        "source": {
            "read_only": True,
            "auto_applies_strategy_changes": False,
            "submits_orders": False,
            "alert_queue": alert_stats,
            "judgments": judgment_stats,
            "judgment_audit": judgment_audit_stats,
            "intake_state": intake_stats,
            "outcomes": outcome_stats,
            "watchlist_diff": {
                "path": watchlist_diff_report_file,
                "schema": watchlist_diff_payload.get("schema"),
                "proposal_hash": ((watchlist_diff_payload.get("proposal") or {}).get("proposal_hash")),
            },
            "horizon": horizon,
            "sample_scope_mode": sample_scope_mode,
        },
        "sample_scope": sample_scope,
        "all_join_counts": {
            "joined_signal_count": len(all_rows),
            "signals_with_alert_and_outcome": len([row for row in all_rows if row["signal_id"] in alerts and row["signal_id"] in outcomes]),
            "signals_with_judgment_and_outcome": len([row for row in all_rows if row["signal_id"] in judgments and row["signal_id"] in outcomes]),
            "signals_with_intake_and_outcome": len([row for row in all_rows if row["signal_id"] in intake_decisions and row["signal_id"] in outcomes]),
        },
        "join_counts": {
            "alert_count": len(alerts),
            "judgment_count": len(judgments),
            "intake_decision_count": len(intake_decisions),
            "outcome_count": len(outcomes),
            "joined_signal_count": len(rows),
            "signals_with_alert_and_outcome": len([row for row in rows if row["signal_id"] in alerts and row["signal_id"] in outcomes]),
            "signals_with_judgment_and_outcome": len([row for row in rows if row["signal_id"] in judgments and row["signal_id"] in outcomes]),
            "signals_with_intake_and_outcome": len([row for row in rows if row["signal_id"] in intake_decisions and row["signal_id"] in outcomes]),
        },
        "overall": metric_summary(rows),
        "judgment_effect": compare_judgment_effect(rows),
        "audit_pass_judgment_effect": compare_audit_pass_judgment_effect(rows),
        "judgment_audit_coverage": build_judgment_audit_coverage(rows, judgment_audit_stats),
        "context_review_effect": compare_context_review_effect(rows),
        "context_review_quality": build_context_review_quality(rows),
        "by_trigger": sorted(grouped_summary(rows, lambda row: row["trigger_key"]), key=lambda row: (-row["resolved_count"], row["key"])),
        "by_judgment_decision": grouped_summary(rows, lambda row: row["judgment_decision"]),
        "by_context_review_cohort": grouped_summary(rows, lambda row: row["context_review_cohort"]),
        "by_intraday_signal_alignment": intraday_groups,
        "intraday_alignment_effect": intraday_alignment_effect(rows),
        "by_intake_status": grouped_summary(rows, lambda row: row["intake_status"]),
        "by_intake_reason": sorted(grouped_summary(rows, lambda row: row["intake_reason_bucket"]), key=lambda row: (-row["count"], row["key"])),
        "by_actionability": sorted(grouped_summary(rows, lambda row: row["actionability_category"]), key=lambda row: (-row["count"], row["key"])),
        "intake_coverage": build_intake_coverage(rows),
        "sizing_blocker_diagnostics": build_sizing_blocker_diagnostics(rows),
        "sizing_blocker_remediation": build_sizing_blocker_remediation(rows, watchlist_diff_payload),
        "by_strategy_config": grouped_summary(rows, lambda row: row.get("strategy_config_id") or "missing"),
        "recent_joined_rows": rows[-100:],
        "warnings": alert_warnings + judgment_audit_warnings + (["outcome_report_recent_only"] if outcome_stats.get("recent_only") else []),
    }
    payload["recommendations"] = build_recommendations(payload)
    return payload


def build_text_report(payload):
    overall = payload["overall"]
    lines = [
        f"Strategy learning report {payload['generated_at']}",
        (
            f"joined={payload['join_counts']['joined_signal_count']} resolved={overall['resolved_count']} "
            f"avg={overall['avg_signed_return_pct']} win={overall['win_rate_pct']}"
        ),
        (
            f"sample_scope={payload.get('sample_scope', {}).get('mode')} "
            f"excluded={payload.get('sample_scope', {}).get('excluded_joined_signal_count', 0)}"
        ),
        (
            "judgment_effect="
            + json.dumps(payload["judgment_effect"], ensure_ascii=False, sort_keys=True)
        ),
        (
        "context_review_effect="
            + json.dumps(payload.get("context_review_effect") or {}, ensure_ascii=False, sort_keys=True)
        ),
        (
            "intraday_alignment_effect="
            + json.dumps(payload.get("intraday_alignment_effect") or {}, ensure_ascii=False, sort_keys=True)
        ),
        (
            "audit_pass_judgment_effect="
            + json.dumps(payload.get("audit_pass_judgment_effect") or {}, ensure_ascii=False, sort_keys=True)
        ),
    ]
    for row in payload["by_trigger"][:10]:
        lines.append(
            f"  {row['key']}: count={row['count']} resolved={row['resolved_count']} "
            f"avg={row['avg_signed_return_pct']} win={row['win_rate_pct']}"
        )
    if payload.get("by_actionability"):
        lines.append("Actionability:")
        for row in payload["by_actionability"][:10]:
            lines.append(
                f"  {row['key']}: count={row['count']} resolved={row['resolved_count']} "
                f"avg={row['avg_signed_return_pct']} win={row['win_rate_pct']}"
            )
    if payload.get("intake_coverage"):
        coverage = payload["intake_coverage"]
        lines.append(
            f"Intake coverage: {coverage['with_intake_decision_count']}/{coverage['joined_signal_count']} "
            f"({coverage['coverage_pct']}%)"
        )
    lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alert-queue-file", default=ALERT_QUEUE_FILE)
    parser.add_argument("--judgment-file", default=JUDGMENT_FILE)
    parser.add_argument("--judgment-audit-file", default=JUDGMENT_AUDIT_FILE)
    parser.add_argument("--intake-state-file", default=INTAKE_STATE_FILE)
    parser.add_argument("--outcome-report-file", default=OUTCOME_REPORT_FILE)
    parser.add_argument("--watchlist-diff-report-file", default=WATCHLIST_DIFF_REPORT_FILE)
    parser.add_argument("--horizon", default=DEFAULT_HORIZON)
    parser.add_argument("--queue-scan-limit", type=int, default=DEFAULT_QUEUE_SCAN_LIMIT)
    parser.add_argument(
        "--sample-scope",
        choices=("current", "all"),
        default=DEFAULT_SAMPLE_SCOPE_MODE,
        help="current scopes learning to latest strategy_config_id + watchlist_id; all keeps historical mixed rows",
    )
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        alert_queue_file=args.alert_queue_file,
        judgment_file=args.judgment_file,
        judgment_audit_file=args.judgment_audit_file,
        intake_state_file=args.intake_state_file,
        outcome_report_file=args.outcome_report_file,
        watchlist_diff_report_file=args.watchlist_diff_report_file,
        horizon=args.horizon,
        queue_scan_limit=args.queue_scan_limit,
        sample_scope_mode=args.sample_scope,
    )
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
