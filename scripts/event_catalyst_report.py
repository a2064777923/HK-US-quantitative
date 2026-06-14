#!/usr/bin/env python3
"""Read-only event catalyst overlap report for Hermes review packets."""
import argparse
import json
import os
from datetime import datetime


EXTERNAL_CONTEXT_FILE = os.environ.get(
    "EXTERNAL_MARKET_CONTEXT_REPORT_FILE",
    "/tmp/external_market_context_report.json",
)
WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
REPORT_FILE = os.environ.get("EVENT_CATALYST_REPORT_FILE", "/tmp/event_catalyst_report.json")
MIN_CATALYST_IMPACT_SCORE = float(os.environ.get("EVENT_CATALYST_MIN_IMPACT_SCORE", "0.7"))
CATALYST_CATEGORIES = {"news", "macro", "capital_flow", "event"}


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


def normalize_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace(";", ",").replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = [value]
    result = []
    seen = set()
    for item in raw_items:
        text = str(item).strip().upper()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def symbols_from_watchlist_payload(payload, market):
    candidates = [
        payload.get(market),
        payload.get(market.lower()),
        payload.get(f"{market}_WATCHLIST"),
        payload.get(f"{market.lower()}_watchlist"),
    ]
    for parent_key in ("markets", "watchlists"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            item = parent.get(market) or parent.get(market.lower())
            if isinstance(item, dict):
                candidates.append(item.get("symbols"))
            else:
                candidates.append(item)
    for candidate in candidates:
        symbols = normalize_list(candidate)
        if symbols:
            return symbols
    return []


def load_watchlist(path=WATCHLIST_FILE):
    warnings = []
    payload = load_json_file(path)
    if not payload:
        warnings.append(f"watchlist_missing_or_invalid:{path}")
    watchlist = {
        "HK": symbols_from_watchlist_payload(payload, "HK"),
        "US": symbols_from_watchlist_payload(payload, "US"),
    }
    return watchlist, warnings


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def item_is_fresh(item):
    return item.get("stale") is False


def item_category(item):
    return str(item.get("category") or "news").strip().lower()


def item_sentiment(item):
    return str(item.get("sentiment") or "unknown").strip().lower()


def market_watch_symbols(watchlist, markets):
    matched = []
    for market in markets:
        matched.extend(watchlist.get(market) or [])
    return matched


def catalyst_candidate(item, watchlist, min_impact_score=MIN_CATALYST_IMPACT_SCORE):
    if item_category(item) not in CATALYST_CATEGORIES:
        return None
    if not item_is_fresh(item):
        return None
    impact_score = as_float(item.get("impact_score"), 0.0) or 0.0
    if impact_score < min_impact_score:
        return None

    item_symbols = set(normalize_list(item.get("symbols") or item.get("tickers")))
    item_markets = set(normalize_list(item.get("markets") or item.get("market")))
    watch_symbols = {
        market: set(normalize_list(symbols))
        for market, symbols in (watchlist or {}).items()
    }
    all_watch_symbols = set()
    for symbols in watch_symbols.values():
        all_watch_symbols.update(symbols)

    matched_symbols = sorted(item_symbols & all_watch_symbols)
    matched_markets = sorted(market for market in item_markets if market in watch_symbols)
    category = item_category(item)
    if item_symbols and not matched_symbols and category not in ("macro", "capital_flow"):
        return None
    if not matched_symbols and not matched_markets:
        return None

    scope = "symbol" if matched_symbols else "market"
    market_symbol_count = len(market_watch_symbols(watchlist, matched_markets))
    return {
        "id": item.get("id"),
        "scope": scope,
        "category": category,
        "source": item.get("source"),
        "title": item.get("title"),
        "summary": item.get("summary"),
        "published_at": item.get("published_at"),
        "age_minutes": item.get("age_minutes"),
        "sentiment": item_sentiment(item),
        "impact_score": round(impact_score, 4),
        "matched_symbols": matched_symbols[:20],
        "matched_markets": matched_markets,
        "market_watch_symbol_count": market_symbol_count if matched_markets else 0,
        "url": item.get("url"),
        "tags": item.get("tags") or [],
        "hermes_use": (
            "symbol_specific_review"
            if matched_symbols
            else "market_wide_review_for_watchlist_symbols"
        ),
    }


def build_recommendations(status, candidates):
    recs = []
    negative = [item for item in candidates if item.get("sentiment") == "negative"]
    positive = [item for item in candidates if item.get("sentiment") == "positive"]
    if status == "MISSING":
        return [
            "wire_external_context_report_before_event_catalyst_review",
            "hermes_should_not_claim_watchlist_event_awareness_without_catalysts",
        ]
    if status == "STALE":
        return ["refresh_external_context_before_event_catalyst_review"]
    if negative:
        recs.append("require_hermes_explicit_risk_note_for_negative_watchlist_catalysts")
    if positive:
        recs.append("allow_hermes_to_consider_positive_catalysts_but_keep_intake_gates_authoritative")
    if not candidates:
        recs.append("no_fresh_high_impact_watchlist_catalysts_detected")
    if not recs:
        recs.append("event_catalysts_available_require_hermes_to_discuss_relevant_items")
    return recs


def build_report(
    external_context=None,
    watchlist=None,
    external_context_file=EXTERNAL_CONTEXT_FILE,
    watchlist_file=WATCHLIST_FILE,
    min_impact_score=MIN_CATALYST_IMPACT_SCORE,
):
    warnings = []
    if external_context is None:
        external_context = load_json_file(external_context_file)
        if not external_context:
            warnings.append(f"external_context_missing_or_invalid:{external_context_file}")
    if watchlist is None:
        watchlist, watch_warnings = load_watchlist(watchlist_file)
        warnings.extend(watch_warnings)

    external_status = str((external_context or {}).get("status") or "MISSING").upper()
    external_schema = (external_context or {}).get("schema")
    raw_items = (external_context or {}).get("items")
    items = raw_items if isinstance(raw_items, list) else []
    watchlist_symbol_count = sum(len(symbols or []) for symbols in (watchlist or {}).values())

    if external_status == "FAIL":
        status = "FAIL"
    elif external_schema != "external_market_context_report_v1":
        status = "MISSING"
    elif external_status in ("MISSING",):
        status = "MISSING"
    elif watchlist_symbol_count == 0:
        status = "MISSING"
    elif external_status == "STALE":
        status = "STALE"
    else:
        candidates = [
            candidate
            for candidate in (
                catalyst_candidate(item, watchlist, min_impact_score=min_impact_score)
                for item in items
                if isinstance(item, dict)
            )
            if candidate
        ]
        status = "RISK" if any(item.get("sentiment") == "negative" for item in candidates) else "OK"
        payload = build_payload(
            status=status,
            external_context=external_context,
            watchlist=watchlist,
            candidates=candidates,
            warnings=warnings,
            external_context_file=external_context_file,
            watchlist_file=watchlist_file,
            min_impact_score=min_impact_score,
        )
        return payload

    candidates = []
    return build_payload(
        status=status,
        external_context=external_context or {},
        watchlist=watchlist,
        candidates=candidates,
        warnings=warnings,
        external_context_file=external_context_file,
        watchlist_file=watchlist_file,
        min_impact_score=min_impact_score,
    )


def build_payload(
    status,
    external_context,
    watchlist,
    candidates,
    warnings,
    external_context_file,
    watchlist_file,
    min_impact_score,
):
    candidates = sorted(
        candidates,
        key=lambda item: (
            0 if item.get("sentiment") == "negative" else 1,
            -as_float(item.get("impact_score"), 0.0),
            item.get("published_at") or "",
        ),
    )
    symbol_candidates = [item for item in candidates if item.get("scope") == "symbol"]
    market_candidates = [item for item in candidates if item.get("scope") == "market"]
    negative_candidates = [item for item in candidates if item.get("sentiment") == "negative"]
    positive_candidates = [item for item in candidates if item.get("sentiment") == "positive"]
    return {
        "schema": "event_catalyst_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "external_context_file": external_context_file,
            "watchlist_file": watchlist_file,
            "min_impact_score": min_impact_score,
            "depends_on_external_context_status": external_context.get("status"),
        },
        "summary": {
            "candidate_count": len(candidates),
            "symbol_candidate_count": len(symbol_candidates),
            "market_candidate_count": len(market_candidates),
            "negative_candidate_count": len(negative_candidates),
            "positive_candidate_count": len(positive_candidates),
            "watchlist_symbol_count": sum(len(symbols or []) for symbols in (watchlist or {}).values()),
            "watchlist_markets": sorted((watchlist or {}).keys()),
            "external_context_status": external_context.get("status"),
            "external_fresh_item_count": (external_context.get("summary") or {}).get("fresh_item_count"),
        },
        "candidates": candidates[:50],
        "recommendations": build_recommendations(status, candidates),
        "warnings": warnings + list(external_context.get("warnings") or []),
        "hermes_use": [
            "Use candidates as watchlist-linked news, macro, capital-flow, or event catalysts.",
            "Negative candidates require explicit risk notes before supporting new exposure.",
            "This report is read-only and must not be treated as an execution signal.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Event catalyst report {payload['generated_at']} status={payload['status']}",
        (
            f"candidates={summary.get('candidate_count')} "
            f"symbol={summary.get('symbol_candidate_count')} "
            f"market={summary.get('market_candidate_count')} "
            f"negative={summary.get('negative_candidate_count')}"
        ),
    ]
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-context-file", default=EXTERNAL_CONTEXT_FILE)
    parser.add_argument("--watchlist-file", default=WATCHLIST_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--min-impact-score", type=float, default=MIN_CATALYST_IMPACT_SCORE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        external_context_file=args.external_context_file,
        watchlist_file=args.watchlist_file,
        min_impact_score=args.min_impact_score,
    )
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
