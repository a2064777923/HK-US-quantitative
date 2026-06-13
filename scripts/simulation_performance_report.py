#!/usr/bin/env python3
"""Read-only simulation performance attribution for Hermes and readiness."""
import argparse
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime

try:
    import portfolio_report
except ImportError:
    from scripts import portfolio_report


PORTFOLIO_REPORT_FILE = os.environ.get("PORTFOLIO_REPORT_FILE", "/tmp/portfolio_report.json")
REPORT_FILE = os.environ.get("SIMULATION_PERFORMANCE_REPORT_FILE", "/tmp/simulation_performance_report.json")
RT_ORDER_STATE_FILE = os.environ.get("RT_ORDER_STATE_FILE", "/tmp/rt_order_intake_state.json")
MIN_CLOSED_WIN_RATE_PCT = float(os.environ.get("SIM_PERF_MIN_CLOSED_WIN_RATE_PCT", "50"))
MIN_CLOSED_PNL_HKD = float(os.environ.get("SIM_PERF_MIN_CLOSED_PNL_HKD", "0"))
MIN_RETURN_PCT = float(os.environ.get("SIM_PERF_MIN_RETURN_PCT", "0"))


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


def clean_text(value):
    text = str(value if value is not None else "").strip()
    return "" if text.lower() in ("", "none", "null") else text


def unique_text(values):
    seen = set()
    result = []
    for value in values or []:
        text = clean_text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def values_from_field(value):
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value] if clean_text(value) else []


def simulation_portfolio(payload):
    for row in payload.get("portfolio_reports") or []:
        if isinstance(row, dict) and str(row.get("role") or "").lower() == "simulation":
            return row
    return {}


def simulation_risk(payload):
    risk = payload.get("portfolio_risk") if isinstance(payload.get("portfolio_risk"), dict) else {}
    for row in risk.get("reports") or []:
        if isinstance(row, dict) and str(row.get("role") or "").lower() == "simulation":
            return row
    return {}


def symbol_trade_attribution(closed_trades):
    stats = defaultdict(lambda: {"symbol": "", "closed_trade_count": 0, "wins": 0, "losses": 0, "pnl_hkd_est": 0.0})
    for trade in closed_trades or []:
        symbol = str(trade.get("symbol") or "").upper()
        if not symbol:
            continue
        pnl = as_float(trade.get("pnl_hkd_est"), 0.0) or 0.0
        row = stats[symbol]
        row["symbol"] = symbol
        row["closed_trade_count"] += 1
        row["pnl_hkd_est"] += pnl
        if pnl > 0:
            row["wins"] += 1
        else:
            row["losses"] += 1
    rows = []
    for row in stats.values():
        count = row["closed_trade_count"]
        row["pnl_hkd_est"] = round(row["pnl_hkd_est"], 2)
        row["win_rate_pct"] = round(row["wins"] / count * 100, 2) if count else 0.0
        rows.append(row)
    return sorted(rows, key=lambda item: item["pnl_hkd_est"])


def open_position_risk_rows(sim_report):
    rows = []
    for pos in sim_report.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        rows.append(
            {
                "symbol": str(pos.get("symbol") or "").upper(),
                "name": pos.get("name"),
                "market": pos.get("market"),
                "quantity": pos.get("quantity"),
                "market_value_hkd": pos.get("market_value_hkd"),
                "unrealized_pnl_hkd": pos.get("unrealized_pnl_hkd"),
                "unrealized_pnl_pct": pos.get("unrealized_pnl_pct"),
                "priority": pos.get("priority"),
                "recommendation": pos.get("recommendation"),
                "recommendation_reasons": pos.get("recommendation_reasons") or [],
                "signal_side": (pos.get("signal") or {}).get("side") if isinstance(pos.get("signal"), dict) else None,
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            0 if item.get("priority") == "high" else 1,
            as_float(item.get("unrealized_pnl_pct"), 0.0) or 0.0,
        ),
    )


def build_recommendations(status, sim_report, risk_report, trade_review, symbol_rows, open_risk_rows, traceability=None):
    recs = []
    if status == "FAIL":
        recs.append("keep_alert_sim_disabled_until_simulation_performance_recovers")
    if traceability and traceability.get("status") not in ("OK", "NO_SAMPLE"):
        recs.append("repair_sim_trade_signal_lineage_before_strategy_tuning")
    if as_float(sim_report.get("return_pct_vs_initial"), 0.0) <= MIN_RETURN_PCT:
        recs.append("block_new_buy_exposure_until_simulation_total_return_positive")
    if as_float(trade_review.get("closed_pnl_hkd_est"), 0.0) <= MIN_CLOSED_PNL_HKD:
        recs.append("review_recent_losing_trades_before_approving_new_signals")
    if as_float(trade_review.get("closed_win_rate_pct"), 0.0) <= MIN_CLOSED_WIN_RATE_PCT:
        recs.append("tighten_or_shadow_triggers_until_closed_trade_win_rate_improves")
    if any(note in ("recent_closed_trades_negative", "loss_rate_above_60pct") for note in trade_review.get("review_notes") or []):
        recs.append("keep_hermes_trade_judgments_reject_or_hold_by_default")
    if str(risk_report.get("risk_level") or "").lower() in ("high", "critical"):
        recs.append("prioritize_high_risk_position_reviews_before_new_buy_review")
    if open_risk_rows and any(row.get("priority") == "high" for row in open_risk_rows):
        recs.append("ask_hermes_for_position_judgments_on_high_priority_holdings")
    if symbol_rows:
        worst = symbol_rows[0]
        if worst.get("pnl_hkd_est", 0) < 0:
            recs.append(f"inspect_worst_closed_symbol:{worst['symbol']}")
    if not recs:
        recs.append("simulation_performance_clean_continue_shadow_collection")
    return recs


def stable_hash(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def order_result_ids(result):
    ids = []
    if isinstance(result, dict):
        for key in ("order_id", "id"):
            ids.append(result.get(key))
        for nested_key in ("data", "order"):
            nested = result.get(nested_key)
            if isinstance(nested, dict):
                for key in ("order_id", "id"):
                    ids.append(nested.get(key))
    elif result is not None:
        ids.append(result)
    return unique_text(ids)


def decision_summary(signal_id, decision):
    alert = decision.get("alert") if isinstance(decision.get("alert"), dict) else {}
    plan = decision.get("plan") if isinstance(decision.get("plan"), dict) else {}
    hermes = decision.get("hermes") if isinstance(decision.get("hermes"), dict) else {}
    judgment = hermes.get("judgment") if isinstance(hermes.get("judgment"), dict) else {}
    return {
        "signal_id": signal_id,
        "status": decision.get("status"),
        "mode": decision.get("mode"),
        "symbol": alert.get("symbol") or plan.get("symbol"),
        "side": alert.get("signal_type") or plan.get("side"),
        "trigger": alert.get("trigger"),
        "full_score": alert.get("full_score"),
        "checked_at": decision.get("checked_at"),
        "submitted_at": decision.get("submitted_at"),
        "hermes_status": hermes.get("status"),
        "hermes_decision": hermes.get("decision") or judgment.get("decision"),
        "hermes_confidence": judgment.get("confidence"),
    }


def processed_order_index(order_state):
    processed = order_state.get("processed") if isinstance(order_state.get("processed"), dict) else {}
    dry_runs = order_state.get("dry_runs") if isinstance(order_state.get("dry_runs"), dict) else {}
    index = {}
    processed_with_order_id = 0
    for sid, decision in processed.items():
        if not isinstance(decision, dict):
            continue
        order_ids = order_result_ids(decision.get("order_result"))
        if not order_ids:
            continue
        processed_with_order_id += 1
        summary = decision_summary(clean_text(sid), decision)
        for order_id in order_ids:
            index[order_id] = summary
    return {
        "index": index,
        "processed_decision_count": len(processed),
        "processed_with_order_id_count": processed_with_order_id,
        "dry_run_decision_count": len(dry_runs),
    }


def trade_order_ids(trade):
    entry_ids = []
    exit_ids = []
    entry_ids.extend(values_from_field(trade.get("entry_order_ids")))
    entry_ids.extend(values_from_field(trade.get("entry_order_id")))
    for leg in trade.get("entry_legs") or []:
        if isinstance(leg, dict):
            entry_ids.extend(values_from_field(leg.get("order_id")))
    exit_ids.extend(values_from_field(trade.get("exit_order_id")))
    exit_ids.extend(values_from_field(trade.get("order_id")))
    entry_ids = unique_text(entry_ids)
    exit_ids = unique_text(exit_ids)
    return {"entry": entry_ids, "exit": exit_ids, "all": unique_text(entry_ids + exit_ids)}


def build_closed_trade_signal_traceability(closed_trades, order_state_payload=None, state_file=RT_ORDER_STATE_FILE):
    loaded_from_file = order_state_payload is None
    state_exists = os.path.exists(state_file) if loaded_from_file else True
    order_state = load_json_file(state_file) if loaded_from_file else order_state_payload
    order_state = order_state if isinstance(order_state, dict) else {}
    order_index = processed_order_index(order_state)
    index = order_index["index"]

    rows = []
    entry_traceable = 0
    any_traceable = 0
    full_traceable = 0
    no_order_ids = 0
    reason_counts = defaultdict(int)

    for trade in closed_trades or []:
        ids = trade_order_ids(trade)
        entry_matches = [index[order_id] for order_id in ids["entry"] if order_id in index]
        exit_matches = [index[order_id] for order_id in ids["exit"] if order_id in index]
        reasons = []
        if not ids["entry"]:
            reasons.append("entry_order_id_missing")
        elif not entry_matches:
            reasons.append("entry_order_id_unmatched")
        if not ids["exit"]:
            reasons.append("exit_order_id_missing")
        elif not exit_matches:
            reasons.append("exit_order_id_unmatched")

        if not ids["all"]:
            status = "NO_ORDER_IDS"
            no_order_ids += 1
        elif entry_matches and exit_matches:
            status = "FULL"
            entry_traceable += 1
            any_traceable += 1
            full_traceable += 1
        elif entry_matches:
            status = "ENTRY_ONLY"
            entry_traceable += 1
            any_traceable += 1
        elif exit_matches:
            status = "EXIT_ONLY"
            any_traceable += 1
        else:
            status = "UNMATCHED"

        for reason in reasons:
            reason_counts[reason] += 1
        rows.append(
            {
                "symbol": trade.get("symbol"),
                "closed_at": trade.get("closed_at"),
                "pnl_hkd_est": trade.get("pnl_hkd_est"),
                "trace_status": status,
                "entry_order_ids": ids["entry"],
                "exit_order_ids": ids["exit"],
                "entry_signals": entry_matches,
                "exit_signals": exit_matches,
                "reasons": reasons,
            }
        )

    closed_count = len(closed_trades or [])
    reason_codes = []
    if closed_count <= 0:
        status = "NO_SAMPLE"
    elif not state_exists:
        status = "MISSING"
        reason_codes.append("rt_order_intake_state_missing")
    elif order_index["processed_decision_count"] <= 0:
        status = "MISSING"
        reason_codes.append("rt_order_intake_processed_empty")
    elif entry_traceable < closed_count:
        status = "DEGRADED"
        reason_codes.append("closed_trade_signal_linkage_missing")
    else:
        status = "OK"

    if no_order_ids:
        reason_codes.append("closed_trade_order_id_missing")
    if reason_counts.get("entry_order_id_unmatched"):
        reason_codes.append("closed_trade_entry_order_unmatched")

    return {
        "schema": "simulation_closed_trade_signal_traceability_v1",
        "status": status,
        "read_only": True,
        "submits_orders": False,
        "source_file": state_file,
        "state_file_exists": state_exists,
        "closed_trade_count": closed_count,
        "entry_traceable_count": entry_traceable,
        "any_order_traceable_count": any_traceable,
        "fully_traceable_count": full_traceable,
        "untraceable_closed_trade_count": max(closed_count - entry_traceable, 0),
        "entry_traceable_pct": round(entry_traceable / closed_count * 100, 2) if closed_count else 0.0,
        "processed_decision_count": order_index["processed_decision_count"],
        "processed_with_order_id_count": order_index["processed_with_order_id_count"],
        "dry_run_decision_count": order_index["dry_run_decision_count"],
        "reason_codes": sorted(set(reason_codes)),
        "reason_counts": dict(sorted(reason_counts.items())),
        "sample": rows[:10],
    }


def build_remediation_actions(status, summary, reasons, symbol_rows, open_risk_rows):
    actions = []
    reason_set = set(reasons or [])
    if status == "OK":
        return actions

    def add(action_id, priority, target, rationale, review_output, reason_codes=None):
        actions.append(
            {
                "action_id": action_id,
                "priority": priority,
                "target": target,
                "rationale": rationale,
                "reason_codes": reason_codes or [],
                "review_output": review_output,
                "auto_apply": False,
            }
        )

    if status == "FAIL":
        add(
            "keep_alert_sim_disabled",
            "critical",
            "rt_alert_bridge",
            "realized simulation performance does not support adding automated exposure",
            "operator keeps RT_ALERT_EXECUTION_MODE away from alert-sim/legacy-sim/execute",
            sorted(reason_set),
        )
    if reason_set.intersection(
        {
            "simulation_total_return_not_positive",
            "simulation_closed_pnl_not_positive",
            "simulation_closed_win_rate_too_low",
            "simulation_trade_review_blocking_notes",
        }
    ):
        add(
            "reject_or_hold_new_buy_by_default",
            "critical",
            "hermes_trade_judgments",
            "new BUY approval should not outrun a losing simulation ledger",
            "Hermes writes reject/hold unless a later packet proves recovery and all execute gates pass",
            sorted(
                reason_set.intersection(
                    {
                        "simulation_total_return_not_positive",
                        "simulation_closed_pnl_not_positive",
                        "simulation_closed_win_rate_too_low",
                        "simulation_trade_review_blocking_notes",
                    }
                )
            ),
        )
    if "closed_trade_signal_traceability_missing" in reason_set:
        add(
            "repair_closed_trade_signal_lineage",
            "critical",
            "sim_trades_and_rt_order_intake_state",
            "closed simulation P&L cannot be used for strategy learning unless entries can be traced to reviewed signals",
            "preserve order_id/trade_id/signal_id linkage before changing strategy thresholds or watchlists",
            ["closed_trade_signal_traceability_missing"],
        )
    if str(summary.get("risk_level") or "").lower() in ("high", "critical") or summary.get("high_priority_count"):
        add(
            "require_position_judgments_for_high_priority_holdings",
            "high",
            "hermes_position_judgments",
            "open simulation risk should be handled before new BUY exposure is reviewed",
            "Hermes reviews high-priority holdings for exit/reduce/trailing-stop/hold decisions",
            [reason for reason in reasons if reason.startswith("simulation_portfolio_risk_")],
        )
    worst_losses = [row for row in symbol_rows or [] if (row.get("pnl_hkd_est") or 0) < 0]
    if worst_losses:
        add(
            "review_worst_closed_symbols_before_strategy_changes",
            "high",
            "strategy_review",
            "loss attribution should identify whether failures came from signal logic, sizing, exits, or stale data",
            "operator/Hermes records symbol-level postmortem notes before promoting strategy changes",
            ["worst_closed_symbols_negative"],
        )
    high_risk_open = [row for row in open_risk_rows or [] if row.get("priority") == "high"]
    if high_risk_open:
        add(
            "review_open_position_exit_pressure",
            "high",
            "portfolio_position_review",
            "high-priority open positions can mask whether new signals are genuinely improving the portfolio",
            "Hermes resolves high-priority position review items before new BUY approvals",
            ["high_priority_open_positions"],
        )
    if status in ("FAIL", "WARN"):
        add(
            "keep_strategy_changes_manual_and_shadow_only",
            "medium",
            "strategy_config_proposal",
            "simulation losses are evidence for review, not permission to auto-mutate live strategy config",
            "any threshold/watchlist/config change remains hash-reviewed and manually promoted",
            sorted(reason_set),
        )
    return actions


def build_remediation_plan(status, summary, reasons, recommendations, symbol_rows, open_risk_rows):
    actions = build_remediation_actions(status, summary, reasons, symbol_rows, open_risk_rows)
    status_value = "not_required" if status == "OK" and not actions else "operator_review_required"
    evidence = {
        "portfolio_id": summary.get("portfolio_id"),
        "return_pct_vs_initial": summary.get("return_pct_vs_initial"),
        "closed_trade_count": summary.get("closed_trade_count"),
        "closed_win_rate_pct": summary.get("closed_win_rate_pct"),
        "closed_pnl_hkd_est": summary.get("closed_pnl_hkd_est"),
        "risk_level": summary.get("risk_level"),
        "risk_flags": summary.get("risk_flags") or [],
        "closed_trade_signal_traceability": summary.get("closed_trade_signal_traceability") or {},
        "worst_closed_symbols": (symbol_rows or [])[:5],
        "high_priority_open_positions": [
            row for row in (open_risk_rows or [])[:20] if row.get("priority") == "high"
        ],
    }
    hash_input = {
        "status": status_value,
        "report_status": status,
        "reason_codes": reasons,
        "recommendations": recommendations,
        "actions": actions,
        "evidence": evidence,
    }
    return {
        "schema": "simulation_strategy_remediation_v1",
        "status": status_value,
        "proposal_hash": stable_hash(hash_input),
        "manual_review_required": status_value != "not_required",
        "auto_applied": False,
        "source_report_schema": "simulation_performance_report_v1",
        "evidence": evidence,
        "actions": actions,
        "operator_contract": {
            "read_only": True,
            "submits_orders": False,
            "changes_execution_mode": False,
            "changes_strategy_config": False,
            "changes_watchlists": False,
            "changes_crontab": False,
            "repairs_positions": False,
            "requires_operator_review_before_promotion": bool(actions),
        },
        "promotion_guidance": [
            "Use this plan as Hermes/operator review context only.",
            "Do not promote strategy config, watchlist, or execution-mode changes from this plan without the separate hash-confirmed promotion tools.",
            "Recovery evidence must come from later simulation_performance, forward outcome, readiness, and Hermes judgment-effect reports.",
        ],
    }


def build_failure_postmortem(status, summary, reasons, symbol_rows, open_risk_rows, trade_review):
    reason_set = set(reasons or [])
    loss_rows = [row for row in symbol_rows or [] if (row.get("pnl_hkd_est") or 0) < 0]
    win_rows = [row for row in symbol_rows or [] if (row.get("pnl_hkd_est") or 0) > 0]
    high_risk_open = [row for row in open_risk_rows or [] if row.get("priority") == "high"]
    total_loss_abs = round(sum(abs(row.get("pnl_hkd_est") or 0.0) for row in loss_rows), 2)
    worst_loss = loss_rows[0] if loss_rows else {}
    worst_loss_share_pct = (
        round(abs(worst_loss.get("pnl_hkd_est") or 0.0) / total_loss_abs * 100, 2)
        if total_loss_abs
        else None
    )

    hypotheses = []

    def add(hypothesis_id, severity, title, evidence, required_review):
        hypotheses.append(
            {
                "id": hypothesis_id,
                "severity": severity,
                "title": title,
                "evidence": evidence,
                "required_review": required_review,
                "auto_apply": False,
            }
        )

    if reason_set.intersection({"simulation_closed_win_rate_too_low", "simulation_closed_pnl_not_positive"}):
        add(
            "entry_filter_or_signal_quality_weak",
            "high",
            "Closed simulation trades are not proving positive expectancy",
            {
                "closed_trade_count": summary.get("closed_trade_count"),
                "closed_win_rate_pct": summary.get("closed_win_rate_pct"),
                "closed_pnl_hkd_est": summary.get("closed_pnl_hkd_est"),
            },
            [
                "For each losing closed symbol, identify the opening signal, signal_type, score, market regime, source reliability, and Hermes judgment if any.",
                "Compare losing entries against same-day intraday context and event/sentiment/fundamentals context before changing thresholds.",
                "Do not promote BUY-trigger loosening until later closed-trade win rate and forward outcome evidence recover.",
            ],
        )
    if "closed_trade_signal_traceability_missing" in reason_set:
        traceability = summary.get("closed_trade_signal_traceability") or {}
        add(
            "closed_trade_signal_lineage_missing",
            "critical",
            "Closed simulation trades cannot be tied back to reviewed v5/Hermes decisions",
            {
                "traceability_status": traceability.get("status"),
                "closed_trade_count": traceability.get("closed_trade_count"),
                "entry_traceable_count": traceability.get("entry_traceable_count"),
                "processed_decision_count": traceability.get("processed_decision_count"),
                "dry_run_decision_count": traceability.get("dry_run_decision_count"),
                "reason_codes": traceability.get("reason_codes") or [],
            },
            [
                "Do not use closed-trade P&L as strategy-upgrade evidence until entry order IDs map to intake decisions and signal IDs.",
                "Repair the order/signal lineage first, then rerun simulation performance and postmortem audit.",
            ],
        )
    if loss_rows:
        add(
            "loss_concentration_requires_symbol_postmortem",
            "high",
            "Losses are concentrated enough to require symbol-level postmortem notes",
            {
                "losing_symbol_count": len(loss_rows),
                "winning_symbol_count": len(win_rows),
                "total_loss_hkd_abs": total_loss_abs,
                "worst_symbol": worst_loss.get("symbol"),
                "worst_symbol_pnl_hkd_est": worst_loss.get("pnl_hkd_est"),
                "worst_symbol_loss_share_pct": worst_loss_share_pct,
                "worst_closed_symbols": (symbol_rows or [])[:5],
            },
            [
                "For each worst_closed_symbols row, classify failure as entry timing, stop/exit policy, position sizing, stale data/source issue, or event/fundamental surprise.",
                "If a symbol has stale daily data, unresolved mapping, public-fallback context, or low-fidelity minute data, mark the trade as data-limited before using it for strategy tuning.",
            ],
        )
    if reason_set.intersection({"simulation_portfolio_risk_high", "simulation_portfolio_risk_critical"}) or high_risk_open:
        add(
            "open_risk_positions_block_new_exposure",
            "critical" if reason_set.intersection({"simulation_portfolio_risk_critical"}) else "high",
            "Open simulation holdings need position review before new exposure",
            {
                "risk_level": summary.get("risk_level"),
                "risk_flags": summary.get("risk_flags") or [],
                "high_priority_count": summary.get("high_priority_count"),
                "high_risk_open_positions": high_risk_open[:10],
            },
            [
                "Hermes should complete advisory position judgments for high-priority holdings before approving fresh BUY exposure.",
                "Review whether current risk is caused by ignored stop-losses, stale position prices, excessive concentration, or missing exit automation.",
            ],
        )
    if reason_set.intersection({"simulation_total_return_not_positive", "simulation_trade_review_blocking_notes"}):
        add(
            "portfolio_level_recovery_not_proven",
            "critical",
            "Simulation portfolio recovery is not proven",
            {
                "return_pct_vs_initial": summary.get("return_pct_vs_initial"),
                "review_notes": summary.get("review_notes") or [],
                "reason_codes": reasons,
            },
            [
                "Keep alert-sim and legacy-sim disabled until a later report shows positive total return, positive closed PnL, acceptable win rate, and resolved high-risk holdings.",
                "Use strategy_config_proposal only as hash-reviewed research output; do not auto-promote strategy changes from a losing sample.",
            ],
        )

    status_value = "OK" if not hypotheses and status == "OK" else "ACTION_REQUIRED"
    return {
        "schema": "simulation_failure_postmortem_v1",
        "status": status_value,
        "read_only": True,
        "submits_orders": False,
        "changes_strategy": False,
        "changes_portfolio": False,
        "source_scope": {
            "closed_trade_source": "portfolio_report.simulation_trade_review.recent_closed",
            "open_risk_source": "portfolio_reports[].positions and portfolio_risk.reports[]",
            "closed_trade_count": summary.get("closed_trade_count"),
        },
        "diagnostics": {
            "portfolio_id": summary.get("portfolio_id"),
            "return_pct_vs_initial": summary.get("return_pct_vs_initial"),
            "closed_trade_count": summary.get("closed_trade_count"),
            "closed_win_rate_pct": summary.get("closed_win_rate_pct"),
            "closed_pnl_hkd_est": summary.get("closed_pnl_hkd_est"),
            "risk_level": summary.get("risk_level"),
            "closed_trade_signal_traceability_status": (summary.get("closed_trade_signal_traceability") or {}).get("status"),
            "losing_symbol_count": len(loss_rows),
            "winning_symbol_count": len(win_rows),
            "total_loss_hkd_abs": total_loss_abs,
            "worst_symbol": worst_loss.get("symbol"),
            "worst_symbol_loss_share_pct": worst_loss_share_pct,
            "review_notes": summary.get("review_notes") or [],
        },
        "hypotheses": hypotheses,
        "required_learning_record": {
            "schema": "simulation_trade_postmortem_note_requirements_v1",
            "destination": "operator_or_hermes_external_notes_until_a_dedicated_jsonl_store_exists",
            "required_fields": [
                "symbol",
                "closed_at",
                "entry_signal_id_or_trade_id",
                "entry_order_id",
                "signal_lineage_status",
                "exit_reason",
                "failure_category",
                "market_context_status",
                "intraday_context_status",
                "event_or_news_context_ids",
                "fundamentals_context_status",
                "source_reliability_status",
                "lesson",
                "proposed_change",
                "promotion_gate",
            ],
            "promotion_gate": (
                "Any proposed strategy/watchlist/config change remains manual and hash-confirmed; "
                "this postmortem does not change thresholds or execution mode."
            ),
        },
        "sample_closed_trades": (trade_review.get("recent_closed") or [])[:10]
        if isinstance(trade_review.get("recent_closed"), list)
        else [],
    }


def build_report(portfolio_payload=None, order_state_payload=None):
    payload = portfolio_payload if isinstance(portfolio_payload, dict) else {}
    sim_report = simulation_portfolio(payload)
    risk_report = simulation_risk(payload)
    trade_review = payload.get("simulation_trade_review") if isinstance(payload.get("simulation_trade_review"), dict) else {}
    closed_trades = trade_review.get("recent_closed") if isinstance(trade_review.get("recent_closed"), list) else []
    symbol_rows = symbol_trade_attribution(closed_trades)
    open_risk_rows = open_position_risk_rows(sim_report)
    traceability = build_closed_trade_signal_traceability(closed_trades, order_state_payload=order_state_payload)

    reasons = []
    sim_return = as_float(sim_report.get("return_pct_vs_initial"))
    closed_pnl = as_float(trade_review.get("closed_pnl_hkd_est"))
    closed_win_rate = as_float(trade_review.get("closed_win_rate_pct"))
    closed_count = int(trade_review.get("closed_trade_count") or 0)
    review_notes = trade_review.get("review_notes") if isinstance(trade_review.get("review_notes"), list) else []
    risk_level = str(risk_report.get("risk_level") or "").lower()

    if not sim_report:
        reasons.append("simulation_portfolio_report_missing")
    elif sim_return is None or sim_return <= MIN_RETURN_PCT:
        reasons.append("simulation_total_return_not_positive")
    if not trade_review:
        reasons.append("simulation_trade_review_missing")
    elif closed_count <= 0:
        reasons.append("simulation_closed_trade_sample_missing")
    else:
        if closed_pnl is None or closed_pnl <= MIN_CLOSED_PNL_HKD:
            reasons.append("simulation_closed_pnl_not_positive")
        if closed_win_rate is None or closed_win_rate <= MIN_CLOSED_WIN_RATE_PCT:
            reasons.append("simulation_closed_win_rate_too_low")
        if traceability.get("status") not in ("OK", "NO_SAMPLE"):
            reasons.append("closed_trade_signal_traceability_missing")
    if any(note in ("recent_closed_trades_negative", "loss_rate_above_60pct") for note in review_notes):
        reasons.append("simulation_trade_review_blocking_notes")
    if risk_level == "critical":
        reasons.append("simulation_portfolio_risk_critical")
    elif risk_level == "high":
        reasons.append("simulation_portfolio_risk_high")

    status = "OK"
    if any(reason in reasons for reason in (
        "simulation_portfolio_report_missing",
        "simulation_total_return_not_positive",
        "simulation_trade_review_missing",
        "simulation_closed_trade_sample_missing",
        "simulation_closed_pnl_not_positive",
        "simulation_closed_win_rate_too_low",
        "simulation_trade_review_blocking_notes",
        "closed_trade_signal_traceability_missing",
        "simulation_portfolio_risk_critical",
    )):
        status = "FAIL"
    elif reasons:
        status = "WARN"

    summary = {
        "portfolio_id": sim_report.get("portfolio_id") or trade_review.get("portfolio_id"),
        "total_value_hkd": sim_report.get("total_value_hkd"),
        "return_pct_vs_initial": sim_return,
        "position_count": sim_report.get("position_count"),
        "high_priority_count": sim_report.get("high_priority_count"),
        "risk_level": risk_report.get("risk_level"),
        "risk_flags": risk_report.get("risk_flags") or [],
        "closed_trade_count": closed_count,
        "closed_win_rate_pct": closed_win_rate,
        "closed_pnl_hkd_est": closed_pnl,
        "closed_trade_entry_traceable_pct": traceability.get("entry_traceable_pct"),
        "closed_trade_signal_traceability": {
            "status": traceability.get("status"),
            "entry_traceable_count": traceability.get("entry_traceable_count"),
            "untraceable_closed_trade_count": traceability.get("untraceable_closed_trade_count"),
            "reason_codes": traceability.get("reason_codes") or [],
        },
        "review_notes": review_notes,
    }
    recommendations = build_recommendations(
        status,
        sim_report,
        risk_report,
        trade_review,
        symbol_rows,
        open_risk_rows,
        traceability,
    )
    remediation_plan = build_remediation_plan(
        status,
        summary,
        reasons,
        recommendations,
        symbol_rows,
        open_risk_rows,
    )
    failure_postmortem = build_failure_postmortem(
        status,
        summary,
        reasons,
        symbol_rows,
        open_risk_rows,
        trade_review,
    )

    return {
        "schema": "simulation_performance_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "portfolio_report_file": PORTFOLIO_REPORT_FILE,
            "rt_order_state_file": RT_ORDER_STATE_FILE,
            "min_closed_win_rate_pct": MIN_CLOSED_WIN_RATE_PCT,
            "min_closed_pnl_hkd": MIN_CLOSED_PNL_HKD,
            "min_return_pct": MIN_RETURN_PCT,
        },
        "summary": summary,
        "reason_codes": reasons,
        "closed_trade_signal_traceability": traceability,
        "closed_trade_attribution_by_symbol": symbol_rows,
        "worst_closed_symbols": symbol_rows[:5],
        "open_position_risk": open_risk_rows[:20],
        "recommendations": recommendations,
        "remediation_plan": remediation_plan,
        "failure_postmortem": failure_postmortem,
        "hermes_use": [
            "Use this report to critique whether recent simulation behavior supports new exposure.",
            "FAIL means Hermes should reject or hold new trade approvals unless an operator explicitly keeps the system in research-only mode.",
            "Use remediation_plan.proposal_hash as the review identifier when discussing simulation-loss recovery actions.",
            "Use failure_postmortem.hypotheses to record symbol-level lessons before changing strategy thresholds.",
            "This report is read-only and does not submit orders or change strategy settings.",
        ],
    }


def build_report_from_files(args):
    payload = load_json_file(args.portfolio_report_file)
    if not payload:
        payload = portfolio_report.build_payload(
            sim_portfolio_id=args.sim_portfolio_id,
            user_portfolio_ids=args.user_portfolio_id or portfolio_report.USER_PORTFOLIO_IDS,
            review_days=args.review_days,
        )
    return build_report(payload)


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Simulation performance report {payload['generated_at']} status={payload['status']}",
        (
            f"portfolio={summary.get('portfolio_id')} return={summary.get('return_pct_vs_initial')}% "
            f"closed_pnl={summary.get('closed_pnl_hkd_est')} win_rate={summary.get('closed_win_rate_pct')}% "
            f"risk={summary.get('risk_level')}"
        ),
    ]
    if payload.get("reason_codes"):
        lines.append("Reasons: " + ", ".join(payload["reason_codes"]))
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    traceability = payload.get("closed_trade_signal_traceability") or {}
    if traceability and traceability.get("status") not in ("OK", "NO_SAMPLE"):
        lines.append(
            "Traceability: status={status} entry_traceable={entry}/{total} reasons={reasons}".format(
                status=traceability.get("status"),
                entry=traceability.get("entry_traceable_count"),
                total=traceability.get("closed_trade_count"),
                reasons=",".join(traceability.get("reason_codes") or []),
            )
        )
    remediation = payload.get("remediation_plan") or {}
    if remediation:
        lines.append(
            "Remediation: status={status} hash={hash} actions={actions}".format(
                status=remediation.get("status"),
                hash=remediation.get("proposal_hash"),
                actions=len(remediation.get("actions") or []),
            )
        )
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--portfolio-report-file", default=PORTFOLIO_REPORT_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--review-days", type=int, default=30)
    parser.add_argument("--sim-portfolio-id", type=int, default=portfolio_report.SIM_PORTFOLIO_ID)
    parser.add_argument("--user-portfolio-id", action="append", type=int, default=[])
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report_from_files(args)
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
