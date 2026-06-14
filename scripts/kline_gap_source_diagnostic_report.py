#!/usr/bin/env python3
"""Classify unresolved daily K-line gap symbols for Hermes/operator review."""
import argparse
import json
import os
from collections import Counter
from datetime import datetime


KLINE_DAILY_GAP_REPAIR_FILE = os.environ.get("KLINE_DAILY_GAP_REPAIR_FILE", "/tmp/kline_daily_gap_repair.json")
UNIVERSE_HYGIENE_REPORT_FILE = os.environ.get("UNIVERSE_HYGIENE_REPORT_FILE", "/tmp/universe_hygiene_report.json")
RT_SIGNAL_WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
PORTFOLIO_REPORT_FILE = os.environ.get("PORTFOLIO_REPORT_FILE", "/tmp/portfolio_report.json")
REPORT_FILE = os.environ.get("KLINE_GAP_SOURCE_DIAGNOSTIC_FILE", "/tmp/kline_gap_source_diagnostic_report.json")


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


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_date(value):
    text = str(value or "")[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        return None


def date_lag_days(later, earlier):
    later_date = parse_date(later)
    earlier_date = parse_date(earlier)
    if not later_date or not earlier_date:
        return None
    return (datetime.strptime(later_date, "%Y-%m-%d") - datetime.strptime(earlier_date, "%Y-%m-%d")).days


def hygiene_lookup(payload):
    lookup = {}
    for market, summary in ((payload or {}).get("markets") or {}).items():
        for key in ("active_symbols", "all_problem_symbols", "high_priority_candidates", "refetch_candidates"):
            for item in summary.get(key) or []:
                symbol = item.get("symbol")
                if symbol:
                    lookup[(market, symbol)] = item
                    lookup[(None, symbol)] = item
    return lookup


def all_attempts_empty(attempts):
    return bool(attempts) and all((item.get("status") == "empty" or int(item.get("row_count") or 0) == 0) for item in attempts)


def any_attempt_fetch_failed(attempts):
    return any(item.get("status") == "fetch_failed" for item in attempts or [])


def normalize_symbol(value):
    return str(value or "").strip().upper()


def normalize_symbol_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = [value]
    result = []
    seen = set()
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def market_code(value):
    text = str(value or "").strip().upper()
    if text in ("HK", "HKEX", "HKG"):
        return "HK"
    if text in ("US", "NASDAQ", "NYSE", "AMEX"):
        return "US"
    return text or None


def symbol_market(symbol):
    text = normalize_symbol(symbol)
    if text.isdigit() and len(text) == 5:
        return "HK"
    return "US" if text else None


def symbols_from_watchlist_payload(payload, market):
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get(market),
        payload.get(str(market).lower()),
        payload.get(f"{market}_WATCHLIST"),
        payload.get(f"{str(market).lower()}_watchlist"),
    ]
    for parent_key in ("markets", "watchlists"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            item = parent.get(market) or parent.get(str(market).lower())
            if isinstance(item, dict):
                candidates.append(item.get("symbols"))
            else:
                candidates.append(item)
    for candidate in candidates:
        symbols = normalize_symbol_list(candidate)
        if symbols:
            return symbols
    return []


def watchlist_lookup(payload):
    lookup = {}
    for market in ("HK", "US"):
        for symbol in symbols_from_watchlist_payload(payload or {}, market):
            lookup.setdefault(symbol, {"in_watchlist": False, "markets": []})
            lookup[symbol]["in_watchlist"] = True
            if market not in lookup[symbol]["markets"]:
                lookup[symbol]["markets"].append(market)
    return lookup


def as_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def portfolio_exposure_lookup(payload):
    lookup = {}
    for report in (payload or {}).get("portfolio_reports") or []:
        role = report.get("role")
        portfolio_id = report.get("portfolio_id")
        for position in report.get("positions") or []:
            symbol = normalize_symbol(position.get("symbol"))
            quantity = as_float(position.get("quantity"), 0.0)
            if not symbol or quantity <= 0:
                continue
            entry = lookup.setdefault(symbol, {"positions": [], "trade_ledger_positions": []})
            entry["positions"].append(
                {
                    "portfolio_id": portfolio_id,
                    "role": role,
                    "quantity": quantity,
                    "market_value_hkd": position.get("market_value_hkd"),
                    "unrealized_pnl_hkd": position.get("unrealized_pnl_hkd"),
                    "source": "positions_table",
                }
            )
        trade_review = report.get("simulation_trade_review") or {}
        open_from_trades = trade_review.get("open_positions_from_trades") or {}
        if isinstance(open_from_trades, dict):
            for symbol, item in open_from_trades.items():
                symbol = normalize_symbol(symbol)
                quantity = as_float((item or {}).get("quantity"), 0.0) if isinstance(item, dict) else 0.0
                if not symbol or quantity <= 0:
                    continue
                entry = lookup.setdefault(symbol, {"positions": [], "trade_ledger_positions": []})
                entry["trade_ledger_positions"].append(
                    {
                        "portfolio_id": portfolio_id,
                        "role": role,
                        "quantity": quantity,
                        "avg_cost": (item or {}).get("avg_cost") if isinstance(item, dict) else None,
                        "source": "simulation_trade_ledger",
                    }
                )
    top_level_trade_review = (payload or {}).get("simulation_trade_review") or {}
    open_from_trades = top_level_trade_review.get("open_positions_from_trades") or {}
    if isinstance(open_from_trades, dict):
        for symbol, item in open_from_trades.items():
            symbol = normalize_symbol(symbol)
            quantity = as_float((item or {}).get("quantity"), 0.0) if isinstance(item, dict) else 0.0
            if not symbol or quantity <= 0:
                continue
            entry = lookup.setdefault(symbol, {"positions": [], "trade_ledger_positions": []})
            entry["trade_ledger_positions"].append(
                {
                    "portfolio_id": "simulation",
                    "role": "simulation",
                    "quantity": quantity,
                    "avg_cost": (item or {}).get("avg_cost") if isinstance(item, dict) else None,
                    "source": "simulation_trade_ledger",
                }
            )
    return lookup


def exposure_for_symbol(symbol, market=None, watchlist=None, portfolio=None):
    symbol = normalize_symbol(symbol)
    watch = (watchlist or {}).get(symbol) or {}
    exposure = (portfolio or {}).get(symbol) or {}
    positions = exposure.get("positions") or []
    trade_positions = exposure.get("trade_ledger_positions") or []
    in_watchlist = bool(watch.get("in_watchlist"))
    has_position = bool(positions or trade_positions)
    blockers = []
    if in_watchlist:
        blockers.append("current_v5_watchlist_member")
    if positions:
        blockers.append("open_position_in_positions_table")
    if trade_positions:
        blockers.append("open_position_in_simulation_trade_ledger")
    return {
        "schema": "unresolved_daily_gap_exposure_v1",
        "symbol": symbol,
        "market": market or symbol_market(symbol),
        "in_current_v5_watchlist": in_watchlist,
        "watchlist_markets": watch.get("markets") or [],
        "has_open_position": has_position,
        "positions": positions[:6],
        "trade_ledger_positions": trade_positions[:6],
        "deactivation_blockers": blockers,
        "safe_to_deactivate_without_manual_review": False,
        "notes": (
            [
                "manual_review_required_before_deactivation",
                "watchlist_membership_or_open_position_blocks_safe_auto_deactivation",
            ]
            if blockers
            else [
                "no_current_watchlist_or_open_position_exposure_found",
                "still_requires_symbol_mapping_or_refetch_review_before_deactivation",
            ]
        ),
    }


def classify_unresolved(item, hygiene=None, exposure=None):
    attempts = item.get("source_attempts") or []
    reason = item.get("reason")
    source_reaches_target = bool(item.get("source_reaches_target_end"))
    source_after_daily = bool(item.get("source_after_latest_daily"))
    latest_source = item.get("latest_source_date")
    latest_daily = item.get("latest_daily_date")
    target_end = item.get("target_end_date")
    latest_valid_gap = item.get("latest_valid_gap_row_date")
    invalid_rows = item.get("invalid_source_rows") or []
    hygiene = hygiene or {}
    hygiene_action = hygiene.get("recommended_action")
    hygiene_issues = hygiene.get("issues") or []

    if invalid_rows:
        category = "source_rows_invalid"
        action = "review_provider_rows_before_manual_repair"
        confidence = "high"
    elif hygiene_action in ("candidate_deactivate_or_symbol_mapping", "candidate_remove_from_stock_universe"):
        category = "active_universe_or_symbol_mapping_issue"
        action = "review_active_universe_and_symbol_mapping_before_trusting_symbol"
        confidence = "high"
    elif all_attempts_empty(attempts):
        category = "provider_symbol_mapping_unavailable"
        action = "try_alternate_provider_or_symbol_code_then_review_active_universe"
        confidence = "medium"
    elif any_attempt_fetch_failed(attempts):
        category = "provider_fetch_failed"
        action = "retry_provider_fetch_before_universe_change"
        confidence = "medium"
    elif reason == "source_does_not_reach_target_end" and source_after_daily:
        category = "provider_lag_or_partial_gap"
        action = "wait_or_refetch_daily_provider; do_not_patch_from_minute_bars"
        confidence = "medium"
    elif reason == "source_gap_rows_missing" and not source_after_daily:
        category = "provider_stopped_or_mapping_stale"
        action = "review_source_coverage_symbol_mapping_or_deactivate_candidate"
        confidence = "medium"
    elif not latest_source:
        category = "provider_no_daily_rows"
        action = "review_source_coverage_or_symbol_mapping"
        confidence = "medium"
    else:
        category = "unclassified_daily_gap_source_issue"
        action = "manual_data_source_review_required"
        confidence = "low"

    return {
        "symbol": item.get("symbol"),
        "market": item.get("market"),
        "category": category,
        "recommended_action": action,
        "confidence": confidence,
        "reason": reason,
        "latest_daily_date": latest_daily,
        "target_end_date": target_end,
        "latest_source_date": latest_source,
        "latest_valid_gap_row_date": latest_valid_gap,
        "source_reaches_target_end": source_reaches_target,
        "source_after_latest_daily": source_after_daily,
        "source_lag_days_vs_target": date_lag_days(target_end, latest_source),
        "daily_lag_days_vs_target": date_lag_days(target_end, latest_daily),
        "source_attempts": attempts[:6],
        "invalid_source_rows": invalid_rows[:6],
        "hygiene": {
            "recommended_action": hygiene_action,
            "issues": hygiene_issues,
            "severity": hygiene.get("severity"),
            "lag_days_vs_market_latest": hygiene.get("lag_days_vs_market_latest"),
        }
        if hygiene
        else {},
        "exposure": exposure or {},
    }


def report_status(classifications, warnings):
    if warnings:
        return "WARN"
    if not classifications:
        return "OK"
    high = sum(1 for item in classifications if item.get("confidence") == "high")
    return "ACTION_REQUIRED" if high else "REVIEW"


def build_recommendations(classifications):
    if not classifications:
        return ["no_unresolved_daily_gap_source_issues"]
    recs = ["classify_unresolved_daily_gap_symbols_before_trusting_outcome_evidence"]
    counts = Counter(item.get("category") for item in classifications)
    watchlist_exposed = [
        item for item in classifications if (item.get("exposure") or {}).get("in_current_v5_watchlist")
    ]
    position_exposed = [
        item for item in classifications if (item.get("exposure") or {}).get("has_open_position")
    ]
    if counts.get("active_universe_or_symbol_mapping_issue"):
        recs.append("review_active_universe_or_symbol_mapping_for_unresolved_gap_symbols")
    if watchlist_exposed:
        recs.append(f"review_watchlist_membership_for_unresolved_gap_symbols:{len(watchlist_exposed)}")
    if position_exposed:
        recs.append(f"block_deactivation_until_position_review_for_unresolved_gap_symbols:{len(position_exposed)}")
    if counts.get("provider_symbol_mapping_unavailable") or counts.get("provider_stopped_or_mapping_stale"):
        recs.append("try_alternate_provider_or_symbol_code_for_unresolved_gap_symbols")
    if counts.get("provider_lag_or_partial_gap"):
        recs.append("do_not_patch_provider_lag_symbols_from_minute_bars")
    if counts.get("source_rows_invalid"):
        recs.append("block_manual_repair_until_provider_rows_validate")
    return recs


def build_report(kline_gap_repair=None, universe_hygiene=None, watchlist=None, portfolio_report=None):
    warnings = []
    gap_payload = kline_gap_repair if kline_gap_repair is not None else load_json(KLINE_DAILY_GAP_REPAIR_FILE)
    hygiene_payload = universe_hygiene if universe_hygiene is not None else load_json(UNIVERSE_HYGIENE_REPORT_FILE)
    watchlist_payload = watchlist if watchlist is not None else load_json(RT_SIGNAL_WATCHLIST_FILE)
    portfolio_payload = portfolio_report if portfolio_report is not None else load_json(PORTFOLIO_REPORT_FILE)
    if not gap_payload:
        warnings.append("kline_daily_gap_repair_report_missing")
        gap_payload = {}
    if not hygiene_payload:
        warnings.append("universe_hygiene_report_missing")
        hygiene_payload = {}
    lookup = hygiene_lookup(hygiene_payload)
    watch_lookup = watchlist_lookup(watchlist_payload)
    portfolio_lookup = portfolio_exposure_lookup(portfolio_payload)
    unresolved = gap_payload.get("unresolved") or []
    classifications = []
    for item in unresolved:
        symbol = item.get("symbol")
        market = item.get("market")
        hygiene = lookup.get((market, symbol)) or lookup.get((None, symbol)) or {}
        exposure = exposure_for_symbol(symbol, market=market, watchlist=watch_lookup, portfolio=portfolio_lookup)
        classifications.append(classify_unresolved(item, hygiene=hygiene, exposure=exposure))
    category_counts = Counter(item.get("category") for item in classifications)
    confidence_counts = Counter(item.get("confidence") for item in classifications)
    watchlist_exposed = [
        item for item in classifications if (item.get("exposure") or {}).get("in_current_v5_watchlist")
    ]
    position_exposed = [
        item for item in classifications if (item.get("exposure") or {}).get("has_open_position")
    ]
    generated_at = now_iso()
    status = report_status(classifications, warnings)
    return {
        "schema": "kline_gap_source_diagnostic_report_v1",
        "generated_at": generated_at,
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_crontab": False,
            "applies_kline_repairs": False,
            "changes_watchlists": False,
            "changes_stock_universe": False,
            "auto_excludes_from_evidence": False,
            "kline_daily_gap_repair_status": gap_payload.get("status"),
            "kline_daily_gap_repair_plan_hash": gap_payload.get("plan_hash"),
            "universe_hygiene_status": hygiene_payload.get("status"),
            "watchlist_file": RT_SIGNAL_WATCHLIST_FILE,
            "portfolio_report_file": PORTFOLIO_REPORT_FILE,
        },
        "summary": {
            "unresolved_count": len(unresolved),
            "classified_count": len(classifications),
            "category_counts": dict(category_counts),
            "confidence_counts": dict(confidence_counts),
            "current_v5_watchlist_exposed_count": len(watchlist_exposed),
            "open_position_exposed_count": len(position_exposed),
            "sample_current_v5_watchlist_exposed_symbols": [item.get("symbol") for item in watchlist_exposed[:20]],
            "sample_open_position_exposed_symbols": [item.get("symbol") for item in position_exposed[:20]],
        },
        "classifications": classifications,
        "recommendations": build_recommendations(classifications),
        "warnings": warnings,
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"K-line gap source diagnostic report {payload.get('generated_at')} status={payload.get('status')}",
        "unresolved={unresolved_count} classified={classified_count} categories={category_counts}".format(
            unresolved_count=summary.get("unresolved_count"),
            classified_count=summary.get("classified_count"),
            category_counts=summary.get("category_counts") or {},
        ),
    ]
    for item in (payload.get("classifications") or [])[:20]:
        exposure = item.get("exposure") or {}
        exposure_bits = []
        if exposure.get("in_current_v5_watchlist"):
            exposure_bits.append("watchlist")
        if exposure.get("has_open_position"):
            exposure_bits.append("open_position")
        lines.append(
            "  {symbol}: {category} action={action} exposure={exposure} source_latest={source} target={target}".format(
                symbol=item.get("symbol"),
                category=item.get("category"),
                action=item.get("recommended_action"),
                exposure=",".join(exposure_bits) if exposure_bits else "none",
                source=item.get("latest_source_date"),
                target=item.get("target_end_date"),
            )
        )
    lines.append("Recommendations: " + ", ".join(payload.get("recommendations") or []))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kline-daily-gap-repair-file", default=KLINE_DAILY_GAP_REPAIR_FILE)
    parser.add_argument("--universe-hygiene-file", default=UNIVERSE_HYGIENE_REPORT_FILE)
    parser.add_argument("--watchlist-file", default=RT_SIGNAL_WATCHLIST_FILE)
    parser.add_argument("--portfolio-report-file", default=PORTFOLIO_REPORT_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        kline_gap_repair=load_json(args.kline_daily_gap_repair_file),
        universe_hygiene=load_json(args.universe_hygiene_file),
        watchlist=load_json(args.watchlist_file),
        portfolio_report=load_json(args.portfolio_report_file),
    )
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(build_text_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
