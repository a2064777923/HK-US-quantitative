#!/usr/bin/env python3
"""Read-only fundamentals and valuation context for Hermes review packets."""
import argparse
import json
import os
from collections import Counter
from datetime import datetime


INPUT_FILE = os.environ.get("FUNDAMENTALS_CONTEXT_INPUT_FILE", "/tmp/fundamentals_context_inputs.json")
INPUT_JSONL_FILE = os.environ.get(
    "FUNDAMENTALS_CONTEXT_INPUT_JSONL_FILE",
    "/tmp/fundamentals_context_inputs.jsonl",
)
REPORT_FILE = os.environ.get("FUNDAMENTALS_CONTEXT_REPORT_FILE", "/tmp/fundamentals_context_report.json")
MAX_ITEM_AGE_DAYS = float(os.environ.get("FUNDAMENTALS_CONTEXT_MAX_ITEM_AGE_DAYS", "120"))
PE_OVERVALUED_THRESHOLD = float(os.environ.get("FUNDAMENTALS_CONTEXT_PE_OVERVALUED", "60"))
PB_OVERVALUED_THRESHOLD = float(os.environ.get("FUNDAMENTALS_CONTEXT_PB_OVERVALUED", "8"))
PS_OVERVALUED_THRESHOLD = float(os.environ.get("FUNDAMENTALS_CONTEXT_PS_OVERVALUED", "15"))
ROE_WEAK_THRESHOLD = float(os.environ.get("FUNDAMENTALS_CONTEXT_ROE_WEAK_PCT", "5"))
DEBT_TO_EQUITY_HIGH_THRESHOLD = float(os.environ.get("FUNDAMENTALS_CONTEXT_DEBT_TO_EQUITY_HIGH", "2.5"))
METRIC_FIELDS = (
    "market_cap",
    "pe_ttm",
    "pb",
    "ps",
    "roe_pct",
    "revenue_growth_pct",
    "earnings_growth_pct",
    "dividend_yield_pct",
    "debt_to_equity",
)
CORE_COMPLETENESS_FIELDS = (
    "pe_ttm",
    "pb",
    "ps",
    "roe_pct",
    "revenue_growth_pct",
    "earnings_growth_pct",
    "debt_to_equity",
)
PARTIAL_SOURCES = {"tencent_quote_snapshot"}
MIN_COMPLETE_CORE_METRICS = int(os.environ.get("FUNDAMENTALS_CONTEXT_MIN_COMPLETE_CORE_METRICS", "5"))


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


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_symbol(value):
    return str(value or "").strip().upper()


def normalize_market(value, symbol=""):
    market = str(value or "").strip().upper()
    if market in ("HK", "US"):
        return market
    symbol = normalize_symbol(symbol)
    if symbol[:1].isdigit() and len(symbol) == 5:
        return "HK"
    return "US" if symbol else ""


def load_items(path=INPUT_FILE, jsonl_path=INPUT_JSONL_FILE):
    items = []
    warnings = []
    loaded_json_input = False
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            loaded_json_input = True
            if isinstance(loaded, dict):
                producer_warnings = loaded.get("warnings")
                if isinstance(producer_warnings, list):
                    for warning in producer_warnings:
                        warning_text = str(warning).strip()
                        if warning_text:
                            warnings.append(f"producer_warning:{warning_text}")
                raw = loaded.get("items") or loaded.get("fundamentals") or []
                if isinstance(raw, list):
                    items.extend(item for item in raw if isinstance(item, dict))
                else:
                    warnings.append("json_input_items_not_list")
            elif isinstance(loaded, list):
                items.extend(item for item in loaded if isinstance(item, dict))
            else:
                warnings.append("json_input_invalid_type")
        except Exception as exc:
            warnings.append(f"json_input_read_failed:{exc}")
    elif path:
        warnings.append(f"json_input_missing:{path}")

    if jsonl_path and os.path.exists(jsonl_path):
        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for idx, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        warnings.append(f"jsonl_bad_line:{idx}")
                        continue
                    if isinstance(item, dict):
                        items.append(item)
        except Exception as exc:
            warnings.append(f"jsonl_input_read_failed:{exc}")
    elif jsonl_path and not loaded_json_input and not path:
        warnings.append(f"jsonl_input_missing:{jsonl_path}")
    return items, warnings


def completeness_profile(item):
    missing = [field for field in METRIC_FIELDS if item.get(field) is None]
    available = [field for field in METRIC_FIELDS if item.get(field) is not None]
    core_available = [field for field in CORE_COMPLETENESS_FIELDS if item.get(field) is not None]
    source = str(item.get("source") or "").strip()
    if not available:
        level = "empty"
    elif source in PARTIAL_SOURCES or len(core_available) < MIN_COMPLETE_CORE_METRICS:
        level = "partial"
    else:
        level = "full"
    return {
        "level": level,
        "available_metric_count": len(available),
        "missing_metric_count": len(missing),
        "core_available_metric_count": len(core_available),
        "metric_count": len(METRIC_FIELDS),
        "coverage_ratio": round(len(available) / float(len(METRIC_FIELDS)), 4),
        "available_metrics": available,
        "missing_metrics": missing,
    }


def valuation_flags(item, stale=False):
    flags = []
    pe = item.get("pe_ttm")
    pb = item.get("pb")
    ps = item.get("ps")
    roe = item.get("roe_pct")
    earnings_growth = item.get("earnings_growth_pct")
    debt_to_equity = item.get("debt_to_equity")
    if stale:
        flags.append("stale_fundamentals")
    if pe is None and pb is None and ps is None and roe is None:
        flags.append("missing_core_fundamentals")
    completeness = item.get("fundamental_completeness") or {}
    if completeness.get("level") in ("partial", "empty"):
        flags.append("partial_fundamentals")
    if pe is not None and pe < 0:
        flags.append("negative_earnings")
    if pe is not None and pe >= PE_OVERVALUED_THRESHOLD:
        flags.append("high_pe")
    if pb is not None and pb >= PB_OVERVALUED_THRESHOLD:
        flags.append("high_pb")
    if ps is not None and ps >= PS_OVERVALUED_THRESHOLD:
        flags.append("high_ps")
    if any(flag in flags for flag in ("high_pe", "high_pb", "high_ps")):
        flags.append("overvalued")
    if roe is not None and roe < ROE_WEAK_THRESHOLD:
        flags.append("weak_profitability")
    if earnings_growth is not None and earnings_growth < 0:
        flags.append("earnings_decline")
    if debt_to_equity is not None and debt_to_equity >= DEBT_TO_EQUITY_HIGH_THRESHOLD:
        flags.append("high_leverage")
    return flags


def normalize_item(item, now=None, max_age_days=MAX_ITEM_AGE_DAYS):
    now = now or datetime.now()
    symbol = normalize_symbol(item.get("symbol") or item.get("ticker"))
    as_of = item.get("as_of") or item.get("reported_at") or item.get("updated_at")
    parsed_time = parse_timestamp(as_of)
    age_days = None
    stale = True
    if parsed_time:
        age_days = round((now - parsed_time).total_seconds() / 86400, 2)
        stale = age_days > max_age_days or age_days < -1
    normalized = {
        "symbol": symbol,
        "market": normalize_market(item.get("market"), symbol),
        "name": str(item.get("name") or "")[:160],
        "source": str(item.get("source") or "unknown"),
        "provider_symbol": str(item.get("provider_symbol") or "")[:80],
        "as_of": as_of,
        "age_days": age_days,
        "stale": stale,
        "currency": str(item.get("currency") or "").upper(),
        "market_cap": as_float(item.get("market_cap")),
        "pe_ttm": as_float(item.get("pe_ttm") or item.get("pe")),
        "pb": as_float(item.get("pb")),
        "ps": as_float(item.get("ps")),
        "roe_pct": as_float(item.get("roe_pct") or item.get("roe")),
        "revenue_growth_pct": as_float(item.get("revenue_growth_pct")),
        "earnings_growth_pct": as_float(item.get("earnings_growth_pct")),
        "dividend_yield_pct": as_float(item.get("dividend_yield_pct")),
        "debt_to_equity": as_float(item.get("debt_to_equity")),
        "summary": str(item.get("summary") or "")[:500],
    }
    normalized["fundamental_completeness"] = completeness_profile(normalized)
    normalized["valuation_flags"] = valuation_flags(normalized, stale=stale)
    return normalized


def dedupe_items(items):
    latest = {}
    for item in items:
        key = (item.get("market"), item.get("symbol"))
        if not item.get("symbol"):
            continue
        current = latest.get(key)
        if not current:
            latest[key] = item
            continue
        current_age = current.get("age_days")
        item_age = item.get("age_days")
        if current_age is None:
            latest[key] = item
        elif item_age is not None and item_age < current_age:
            latest[key] = item
    return list(latest.values())


def warning_counts(warnings):
    producer_fetch_failed = 0
    fallback_provider_used = 0
    tencent_fetch_missing = 0
    for warning in warnings or []:
        text = str(warning)
        bare = text[len("producer_warning:") :] if text.startswith("producer_warning:") else text
        if bare.startswith("fetch_failed:"):
            producer_fetch_failed += 1
        if bare.startswith("fallback_provider_used:"):
            fallback_provider_used += 1
        if bare.startswith("tencent_fetch_missing:"):
            tencent_fetch_missing += 1
    return {
        "producer_fetch_failed_count": producer_fetch_failed,
        "fallback_provider_used_count": fallback_provider_used,
        "tencent_fetch_missing_count": tencent_fetch_missing,
    }


def summarize(items, warnings=None):
    by_market = Counter(item.get("market") or "UNKNOWN" for item in items)
    by_source = Counter(item.get("source") or "unknown" for item in items)
    flag_counts = Counter(flag for item in items for flag in item.get("valuation_flags") or [])
    completeness_counts = Counter(
        (item.get("fundamental_completeness") or {}).get("level") or "unknown"
        for item in items
    )
    fresh = [item for item in items if item.get("stale") is False]
    risky = [
        item
        for item in fresh
        if any(
            flag in (item.get("valuation_flags") or [])
            for flag in (
                "overvalued",
                "negative_earnings",
                "weak_profitability",
                "earnings_decline",
                "high_leverage",
                "partial_fundamentals",
            )
        )
    ]
    counts = warning_counts(warnings or [])
    fallback_items = [item for item in items if item.get("source") in PARTIAL_SOURCES]
    return {
        "item_count": len(items),
        "fresh_item_count": len(fresh),
        "stale_item_count": len([item for item in items if item.get("stale") is True]),
        "risky_item_count": len(risky),
        "by_market": dict(by_market),
        "by_source": dict(by_source),
        "completeness_counts": dict(completeness_counts),
        "full_item_count": completeness_counts.get("full", 0),
        "partial_item_count": completeness_counts.get("partial", 0) + completeness_counts.get("empty", 0),
        "fallback_item_count": len(fallback_items),
        "flag_counts": dict(flag_counts),
        **counts,
    }


def classify_status(summary):
    if summary["item_count"] == 0:
        return "MISSING"
    if summary["fresh_item_count"] == 0:
        return "STALE"
    if summary["risky_item_count"]:
        return "RISK"
    return "OK"


def build_recommendations(status, summary):
    if status == "MISSING":
        return [
            "wire_fundamentals_context_producer",
            "hermes_should_not_claim_fundamental_or_valuation_awareness_without_fundamentals_context",
        ]
    if status == "STALE":
        return ["refresh_fundamentals_context_before_buy_judgment"]
    recs = []
    flags = summary.get("flag_counts") or {}
    if flags.get("overvalued"):
        recs.append("require_hermes_explicit_valuation_risk_note_for_overvalued_buy_candidates")
    if flags.get("negative_earnings") or flags.get("earnings_decline"):
        recs.append("require_hermes_explicit_earnings_risk_note_for_buy_candidates")
    if flags.get("weak_profitability") or flags.get("high_leverage"):
        recs.append("tighten_buy_review_for_weak_profitability_or_high_leverage")
    if summary.get("partial_item_count"):
        recs.append("require_hermes_partial_fundamentals_disclosure_for_buy_candidates")
    if summary.get("fallback_item_count") or summary.get("fallback_provider_used_count"):
        recs.append("treat_fallback_provider_fundamentals_as_partial_context_only")
    if summary.get("producer_fetch_failed_count"):
        recs.append("investigate_fundamentals_provider_fetch_failures_before_trusting_full_coverage")
    if not recs:
        recs.append("fundamentals_context_available_require_hermes_to_discuss_relevant_metrics")
    return recs


def build_report(items=None, now=None, warnings=None, input_file=INPUT_FILE, input_jsonl_file=INPUT_JSONL_FILE):
    now = now or datetime.now()
    warnings = list(warnings or [])
    if items is None:
        raw_items, load_warnings = load_items(path=input_file, jsonl_path=input_jsonl_file)
        warnings.extend(load_warnings)
    else:
        raw_items = items
    normalized = dedupe_items(
        [normalize_item(item, now=now) for item in raw_items if isinstance(item, dict)]
    )
    summary = summarize(normalized, warnings=warnings)
    status = classify_status(summary)
    return {
        "schema": "fundamentals_context_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "input_file": input_file,
            "input_jsonl_file": input_jsonl_file,
            "max_item_age_days": MAX_ITEM_AGE_DAYS,
            "metric_fields": list(METRIC_FIELDS),
            "core_completeness_fields": list(CORE_COMPLETENESS_FIELDS),
            "partial_sources": sorted(PARTIAL_SOURCES),
            "min_complete_core_metrics": MIN_COMPLETE_CORE_METRICS,
            "expected_producers": [
                "vendor_or_broker_fundamentals_snapshot",
                "wudao_or_infohub_fundamentals_context",
                "manual_jsonl_append_for_watchlist_symbols",
            ],
        },
        "summary": summary,
        "items": sorted(normalized, key=lambda item: (item["stale"], item["market"], item["symbol"]))[:100],
        "recommendations": build_recommendations(status, summary),
        "warnings": warnings,
        "hermes_use": [
            "Use this as valuation, profitability, growth, dividend, and leverage context.",
            "Do not treat this report as an execution signal; rt_order_intake gates remain authoritative.",
            "When status is MISSING or STALE, Hermes must state fundamental awareness is incomplete.",
            "When items contain partial_fundamentals or fallback provider sources, Hermes must state metric coverage is incomplete before any approve/reduce.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Fundamentals context report {payload['generated_at']} status={payload['status']}",
        (
            f"items={summary.get('item_count')} fresh={summary.get('fresh_item_count')} "
            f"risky={summary.get('risky_item_count')} partial={summary.get('partial_item_count')} "
            f"fallback={summary.get('fallback_item_count')} flags={summary.get('flag_counts', {})}"
        ),
    ]
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", default=INPUT_FILE)
    parser.add_argument("--input-jsonl-file", default=INPUT_JSONL_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(input_file=args.input_file, input_jsonl_file=args.input_jsonl_file)
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
