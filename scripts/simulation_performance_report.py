#!/usr/bin/env python3
"""Read-only simulation performance attribution for Hermes and readiness."""
import argparse
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
MIN_CLOSED_WIN_RATE_PCT = float(os.environ.get("SIM_PERF_MIN_CLOSED_WIN_RATE_PCT", "50"))
MIN_CLOSED_PNL_HKD = float(os.environ.get("SIM_PERF_MIN_CLOSED_PNL_HKD", "0"))
MIN_RETURN_PCT = float(os.environ.get("SIM_PERF_MIN_RETURN_PCT", "0"))


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


def build_recommendations(status, sim_report, risk_report, trade_review, symbol_rows, open_risk_rows):
    recs = []
    if status == "FAIL":
        recs.append("keep_alert_sim_disabled_until_simulation_performance_recovers")
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


def build_report(portfolio_payload=None):
    payload = portfolio_payload if isinstance(portfolio_payload, dict) else {}
    sim_report = simulation_portfolio(payload)
    risk_report = simulation_risk(payload)
    trade_review = payload.get("simulation_trade_review") if isinstance(payload.get("simulation_trade_review"), dict) else {}
    closed_trades = trade_review.get("recent_closed") if isinstance(trade_review.get("recent_closed"), list) else []
    symbol_rows = symbol_trade_attribution(closed_trades)
    open_risk_rows = open_position_risk_rows(sim_report)

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
        "simulation_portfolio_risk_critical",
    )):
        status = "FAIL"
    elif reasons:
        status = "WARN"

    return {
        "schema": "simulation_performance_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "portfolio_report_file": PORTFOLIO_REPORT_FILE,
            "min_closed_win_rate_pct": MIN_CLOSED_WIN_RATE_PCT,
            "min_closed_pnl_hkd": MIN_CLOSED_PNL_HKD,
            "min_return_pct": MIN_RETURN_PCT,
        },
        "summary": {
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
            "review_notes": review_notes,
        },
        "reason_codes": reasons,
        "closed_trade_attribution_by_symbol": symbol_rows,
        "worst_closed_symbols": symbol_rows[:5],
        "open_position_risk": open_risk_rows[:20],
        "recommendations": build_recommendations(status, sim_report, risk_report, trade_review, symbol_rows, open_risk_rows),
        "hermes_use": [
            "Use this report to critique whether recent simulation behavior supports new exposure.",
            "FAIL means Hermes should reject or hold new trade approvals unless an operator explicitly keeps the system in research-only mode.",
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
