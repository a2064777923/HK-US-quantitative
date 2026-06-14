#!/usr/bin/env python3
"""Read-only 5m/15m/30m/60m intraday timeframe quality matrix.

This report summarizes intraday_context_report.py output so Hermes can see
whether finer-grained evidence is complete enough to use as confirmation or is
only a limited/snapshot advisory layer.
"""
import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime


REPORT_FILE = os.environ.get("INTRADAY_TIMEFRAME_QUALITY_REPORT_FILE", "/tmp/intraday_timeframe_quality_report.json")
INTRADAY_CONTEXT_FILE = os.environ.get("INTRADAY_CONTEXT_REPORT_FILE", "/tmp/intraday_context_report.json")
TIMEFRAMES = ("5m", "15m", "30m", "60m")
TIMEFRAME_ROLES = {
    "5m": "entry_timing_noise_check",
    "15m": "near_term_confirmation",
    "30m": "session_structure_confirmation",
    "60m": "session_structure_context",
}
SYMBOL_DECISION_EFFECTS = {
    "soft_confirmation_eligible": ["soft_confirm_signal", "cap_confidence", "challenge_signal"],
    "cap_or_challenge_only": ["cap_confidence", "challenge_signal"],
    "diagnostic_only": [],
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
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
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


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


def rate(part, whole):
    return round(part / whole * 100.0, 2) if whole else 0.0


def timeframe_window(symbol_row, timeframe):
    windows = symbol_row.get("rolling_windows") if isinstance(symbol_row.get("rolling_windows"), dict) else {}
    window = windows.get(timeframe)
    if isinstance(window, dict):
        return window
    latest = symbol_row.get(f"latest_{timeframe}") if isinstance(symbol_row.get(f"latest_{timeframe}"), dict) else {}
    return latest


def expected_minutes_for_timeframe(window, timeframe):
    expected = as_int((window or {}).get("expected_minute_count"))
    if expected <= 0:
        expected = as_int(timeframe.rstrip("m"), 0)
    return expected


def timeframe_status(window, timeframe):
    status = str((window or {}).get("coverage_status") or "").upper()
    rows = as_int((window or {}).get("row_count"))
    expected = expected_minutes_for_timeframe(window, timeframe)
    if rows <= 0:
        return "MISSING"
    if expected > 0 and rows < expected:
        return "LIMITED"
    if status in ("LIMITED", "MISSING"):
        return status
    return "OK"


def coverage_pct(window, timeframe):
    expected = expected_minutes_for_timeframe(window, timeframe)
    rows = as_int((window or {}).get("row_count"))
    return rate(rows, expected) if expected else None


def symbol_decision_use(status, reasons):
    if status == "OK" and not reasons:
        return "soft_confirmation_eligible"
    if status == "MISSING":
        return "diagnostic_only"
    return "cap_or_challenge_only"


def summarize_symbol(symbol_row):
    quality = symbol_row.get("quality") if isinstance(symbol_row.get("quality"), dict) else {}
    mtf = (
        symbol_row.get("multi_timeframe_confirmation")
        if isinstance(symbol_row.get("multi_timeframe_confirmation"), dict)
        else {}
    )
    windows = {}
    limited = []
    missing = []
    for timeframe in TIMEFRAMES:
        window = timeframe_window(symbol_row, timeframe)
        status = timeframe_status(window, timeframe)
        if status == "LIMITED":
            limited.append(timeframe)
        elif status == "MISSING":
            missing.append(timeframe)
        windows[timeframe] = {
            "status": status,
            "row_count": as_int(window.get("row_count")) if isinstance(window, dict) else 0,
            "expected_minute_count": as_int(window.get("expected_minute_count")) if isinstance(window, dict) else 0,
            "coverage_pct": coverage_pct(window, timeframe),
            "change_pct": window.get("change_pct") if isinstance(window, dict) else None,
            "momentum": window.get("momentum") if isinstance(window, dict) else None,
        }
    reasons = []
    if limited:
        reasons.append("timeframe_coverage_limited")
    if missing:
        reasons.append("timeframe_coverage_missing")
    if mtf.get("alignment") == "conflicting_timeframes" or mtf.get("contradictions"):
        reasons.append("multi_timeframe_conflict")
    if quality.get("missing_source_granularity_count"):
        reasons.append("source_granularity_missing")
    if quality.get("low_fidelity_point_count"):
        reasons.append("low_fidelity_minute_source")
    if quality.get("snapshot_like_row_count"):
        reasons.append("snapshot_like_minute_rows")
    if quality.get("status") not in (None, "", "OK"):
        reasons.append("intraday_quality_degraded")
    if symbol_row.get("status") in ("STALE", "MISSING", "CLOSED"):
        reasons.append(f"symbol_status_{str(symbol_row.get('status')).lower()}")
    status = "OK" if not reasons else "DEGRADED"
    if missing and len(missing) == len(TIMEFRAMES):
        status = "MISSING"
    decision_use = symbol_decision_use(status, reasons)
    return {
        "symbol": symbol_row.get("symbol"),
        "market": symbol_row.get("market"),
        "status": status,
        "decision_use": decision_use,
        "allowed_effects": SYMBOL_DECISION_EFFECTS[decision_use],
        "source_status": symbol_row.get("status"),
        "alignment": mtf.get("alignment"),
        "dominant_direction": mtf.get("dominant_direction"),
        "contradictions": mtf.get("contradictions") or [],
        "limited_timeframes": limited,
        "missing_timeframes": missing,
        "timeframes": windows,
        "quality": {
            "status": quality.get("status"),
            "missing_source_granularity_count": as_int(quality.get("missing_source_granularity_count")),
            "low_fidelity_point_count": as_int(quality.get("low_fidelity_point_count")),
            "snapshot_like_row_count": as_int(quality.get("snapshot_like_row_count")),
            "full_ohlc_row_count": as_int(quality.get("full_ohlc_row_count")),
            "valid_point_count": as_int(quality.get("valid_point_count")),
        },
        "reasons": sorted(set(reasons)),
    }


def summarize_timeframes(symbols):
    result = {}
    for timeframe in TIMEFRAMES:
        statuses = Counter()
        coverage_values = []
        for row in symbols:
            window = row.get("timeframes", {}).get(timeframe, {})
            statuses[window.get("status") or "MISSING"] += 1
            value = as_float(window.get("coverage_pct"))
            if value is not None:
                coverage_values.append(value)
        result[timeframe] = {
            "symbol_count": len(symbols),
            "status_counts": dict(statuses),
            "ok_symbol_count": statuses.get("OK", 0),
            "limited_symbol_count": statuses.get("LIMITED", 0),
            "missing_symbol_count": statuses.get("MISSING", 0),
            "avg_coverage_pct": round(sum(coverage_values) / len(coverage_values), 2) if coverage_values else None,
        }
    return result


def summarize_market(market, payload):
    symbols = [
        summarize_symbol(row)
        for row in (payload.get("symbols") if isinstance(payload.get("symbols"), list) else [])
        if isinstance(row, dict)
    ]
    statuses = Counter(row.get("status") for row in symbols)
    reasons = Counter(reason for row in symbols for reason in row.get("reasons") or [])
    return {
        "market": market,
        "source_status": payload.get("status"),
        "market_session": payload.get("market_session") if isinstance(payload.get("market_session"), dict) else {},
        "symbol_count": len(symbols),
        "status_counts": dict(statuses),
        "reason_counts": dict(reasons),
        "timeframes": summarize_timeframes(symbols),
        "symbols": symbols,
    }


def aggregate_summary(markets):
    market_rows = list((markets or {}).values())
    symbols = [symbol for market in market_rows for symbol in market.get("symbols") or []]
    reasons = Counter(reason for symbol in symbols for reason in symbol.get("reasons") or [])
    decision_uses = Counter(symbol.get("decision_use") or "diagnostic_only" for symbol in symbols)
    timeframe_totals = {}
    for timeframe in TIMEFRAMES:
        statuses = Counter()
        coverage_values = []
        for symbol in symbols:
            window = symbol.get("timeframes", {}).get(timeframe, {})
            statuses[window.get("status") or "MISSING"] += 1
            value = as_float(window.get("coverage_pct"))
            if value is not None:
                coverage_values.append(value)
        timeframe_totals[timeframe] = {
            "status_counts": dict(statuses),
            "ok_symbol_count": statuses.get("OK", 0),
            "limited_symbol_count": statuses.get("LIMITED", 0),
            "missing_symbol_count": statuses.get("MISSING", 0),
            "avg_coverage_pct": round(sum(coverage_values) / len(coverage_values), 2) if coverage_values else None,
        }
    return {
        "market_count": len(market_rows),
        "symbol_count": len(symbols),
        "degraded_symbol_count": len([row for row in symbols if row.get("status") == "DEGRADED"]),
        "missing_symbol_count": len([row for row in symbols if row.get("status") == "MISSING"]),
        "conflict_symbol_count": reasons.get("multi_timeframe_conflict", 0),
        "limited_timeframe_symbol_count": reasons.get("timeframe_coverage_limited", 0),
        "missing_timeframe_symbol_count": reasons.get("timeframe_coverage_missing", 0),
        "low_fidelity_symbol_count": reasons.get("low_fidelity_minute_source", 0),
        "snapshot_like_symbol_count": reasons.get("snapshot_like_minute_rows", 0),
        "missing_source_granularity_symbol_count": reasons.get("source_granularity_missing", 0),
        "closed_symbol_count": reasons.get("symbol_status_closed", 0),
        "stale_symbol_count": reasons.get("symbol_status_stale", 0),
        "decision_use_counts": dict(decision_uses),
        "soft_confirmation_eligible_symbol_count": decision_uses.get("soft_confirmation_eligible", 0),
        "cap_or_challenge_only_symbol_count": decision_uses.get("cap_or_challenge_only", 0),
        "diagnostic_only_symbol_count": decision_uses.get("diagnostic_only", 0),
        "reason_counts": dict(reasons),
        "timeframes": timeframe_totals,
    }


def classify_status(intraday_context, summary):
    if not intraday_context:
        return "MISSING"
    if intraday_context.get("schema") != "intraday_context_report_v1":
        return "FAIL"
    if str(intraday_context.get("status") or "").upper() == "FAIL":
        return "FAIL"
    if summary.get("symbol_count", 0) <= 0:
        return "MISSING"
    if (
        summary.get("degraded_symbol_count")
        or summary.get("missing_symbol_count")
        or summary.get("limited_timeframe_symbol_count")
        or summary.get("missing_timeframe_symbol_count")
        or summary.get("low_fidelity_symbol_count")
        or summary.get("snapshot_like_symbol_count")
        or summary.get("missing_source_granularity_symbol_count")
        or summary.get("closed_symbol_count")
        or summary.get("stale_symbol_count")
    ):
        return "DEGRADED"
    return "OK"


def build_decision_policy(status, summary):
    reason_codes = []
    if status == "MISSING":
        reason_codes.append("intraday_context_missing")
    if status == "FAIL":
        reason_codes.append("intraday_context_failed")
    if summary.get("limited_timeframe_symbol_count"):
        reason_codes.append("timeframe_coverage_limited")
    if summary.get("missing_timeframe_symbol_count") or summary.get("missing_symbol_count"):
        reason_codes.append("timeframe_coverage_missing")
    if summary.get("conflict_symbol_count"):
        reason_codes.append("multi_timeframe_conflict")
    if summary.get("low_fidelity_symbol_count"):
        reason_codes.append("low_fidelity_minute_source")
    if summary.get("snapshot_like_symbol_count"):
        reason_codes.append("snapshot_like_minute_rows")
    if summary.get("missing_source_granularity_symbol_count"):
        reason_codes.append("source_granularity_missing")
    if summary.get("closed_symbol_count"):
        reason_codes.append("market_closed")
    if summary.get("stale_symbol_count"):
        reason_codes.append("stale_intraday_context")
    if summary.get("degraded_symbol_count"):
        reason_codes.append("intraday_quality_degraded")

    if status in ("MISSING", "FAIL"):
        confidence_use = "diagnostic_only"
        allowed_effects = []
    elif reason_codes:
        confidence_use = "cap_or_challenge_only"
        allowed_effects = ["cap_confidence", "challenge_signal"]
    else:
        confidence_use = "soft_confirmation_eligible"
        allowed_effects = ["soft_confirm_signal", "cap_confidence", "challenge_signal"]

    return {
        "schema": "intraday_timeframe_decision_policy_v1",
        "confidence_use": confidence_use,
        "may_raise_confidence": False,
        "requires_forward_evidence_before_confidence_raise": True,
        "can_override_daily_gates": False,
        "execution_permission": False,
        "timeframe_roles": TIMEFRAME_ROLES,
        "allowed_effects": allowed_effects,
        "reason_codes": sorted(set(reason_codes)),
    }


def build_recommendations(status, summary):
    recs = []
    if status == "MISSING":
        recs.append("refresh_intraday_context_before_timeframe_quality_review")
    if status == "FAIL":
        recs.append("fix_intraday_context_report_before_timeframe_quality_review")
    if summary.get("limited_timeframe_symbol_count"):
        recs.append("do_not_raise_confidence_from_limited_30m_60m_coverage")
    if summary.get("missing_timeframe_symbol_count"):
        recs.append("collect_more_minute_rows_before_using_all_timeframes")
    if summary.get("conflict_symbol_count"):
        recs.append("require_hermes_to_discuss_intraday_timeframe_conflicts")
    if summary.get("low_fidelity_symbol_count") or summary.get("snapshot_like_symbol_count"):
        recs.append("treat_snapshot_minute_timeframes_as_advisory_until_full_ohlcv")
    if summary.get("missing_source_granularity_symbol_count"):
        recs.append("apply_or_review_source_granularity_provenance_before_full_intraday_claims")
    if summary.get("closed_symbol_count"):
        recs.append("treat_timeframe_quality_as_last_session_only_until_market_reopens")
    if not recs:
        recs.append("intraday_timeframe_quality_clean")
    return sorted(set(recs))


def build_report(intraday_context=None):
    intraday_context = intraday_context if intraday_context is not None else load_json_file(INTRADAY_CONTEXT_FILE)
    market_payloads = intraday_context.get("markets") if isinstance(intraday_context.get("markets"), dict) else {}
    markets = {
        market: summarize_market(market, payload)
        for market, payload in sorted(market_payloads.items())
        if isinstance(payload, dict)
    }
    summary = aggregate_summary(markets)
    status = classify_status(intraday_context, summary)
    decision_policy = build_decision_policy(status, summary)
    return {
        "schema": "intraday_timeframe_quality_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "input_file": INTRADAY_CONTEXT_FILE,
            "queries_database": False,
            "submits_orders": False,
            "writes_database": False,
            "changes_strategy": False,
            "changes_crontab": False,
            "timeframes": list(TIMEFRAMES),
        },
        "upstream": {
            "schema": intraday_context.get("schema"),
            "status": intraday_context.get("status"),
            "generated_at": intraday_context.get("generated_at"),
            "granularity_policy": intraday_context.get("granularity_policy")
            if isinstance(intraday_context.get("granularity_policy"), dict)
            else {},
        },
        "summary": summary,
        "markets": markets,
        "decision_policy": decision_policy,
        "recommendations": build_recommendations(status, summary),
        "hermes_use": [
            "Use this report to decide whether 5m/15m/30m/60m evidence is complete enough for confirmation.",
            "Limited or snapshot-like timeframe evidence can challenge or cap confidence, but must not raise confidence or override daily/readiness gates.",
            "This report is read-only and does not fetch minute rows, write DB rows, submit orders, or change strategy.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Intraday timeframe quality {payload.get('generated_at')} status={payload.get('status')}",
        (
            f"symbols={summary.get('symbol_count')} degraded={summary.get('degraded_symbol_count')} "
            f"limited={summary.get('limited_timeframe_symbol_count')} "
            f"conflicts={summary.get('conflict_symbol_count')} low_fidelity={summary.get('low_fidelity_symbol_count')}"
        ),
        (
            "decision_use="
            f"soft={summary.get('soft_confirmation_eligible_symbol_count', 0)} "
            f"cap_or_challenge={summary.get('cap_or_challenge_only_symbol_count', 0)} "
            f"diagnostic={summary.get('diagnostic_only_symbol_count', 0)}"
        ),
    ]
    for timeframe, row in sorted((summary.get("timeframes") or {}).items()):
        lines.append(
            f"  {timeframe}: ok={row.get('ok_symbol_count')} limited={row.get('limited_symbol_count')} "
            f"missing={row.get('missing_symbol_count')} avg_coverage={row.get('avg_coverage_pct')}"
        )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    policy = payload.get("decision_policy") if isinstance(payload.get("decision_policy"), dict) else {}
    if policy:
        lines.append(
            "Decision policy: "
            f"confidence_use={policy.get('confidence_use')} "
            f"may_raise_confidence={policy.get('may_raise_confidence')} "
            f"can_override_daily_gates={policy.get('can_override_daily_gates')}"
        )
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--intraday-context-file", default=INTRADAY_CONTEXT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global INTRADAY_CONTEXT_FILE
    INTRADAY_CONTEXT_FILE = args.intraday_context_file
    payload = build_report()
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text or not args.output:
        print(build_text_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
