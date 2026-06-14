#!/usr/bin/env python3
"""Read-only external news/macro/event context for Hermes review packets."""
import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime


INPUT_FILE = os.environ.get("EXTERNAL_MARKET_CONTEXT_INPUT_FILE", "/tmp/external_market_context_inputs.json")
INPUT_JSONL_FILE = os.environ.get(
    "EXTERNAL_MARKET_CONTEXT_INPUT_JSONL_FILE",
    "/tmp/external_market_context_inputs.jsonl",
)
REPORT_FILE = os.environ.get("EXTERNAL_MARKET_CONTEXT_REPORT_FILE", "/tmp/external_market_context_report.json")
MAX_ITEM_AGE_MINUTES = float(os.environ.get("EXTERNAL_CONTEXT_MAX_ITEM_AGE_MINUTES", "180"))
HIGH_IMPACT_MIN_SCORE = float(os.environ.get("EXTERNAL_CONTEXT_HIGH_IMPACT_MIN_SCORE", "0.7"))
VALID_CATEGORIES = {"news", "macro", "capital_flow", "event", "sentiment"}
VALID_SENTIMENTS = {"positive", "negative", "neutral", "mixed", "unknown"}
TRUSTED_PROVIDER_EXACT = {
    "broker",
    "broker_feed",
    "capital_flow_snapshot",
    "exchange",
    "manual_operator",
    "official_macro",
    "wudao",
    "wudao_mcp",
}
TRUSTED_PROVIDER_PREFIXES = (
    "broker_",
    "cailian",
    "capital_flow_",
    "cls_",
    "exchange_",
    "futu_",
    "ibkr_",
    "interactive_brokers_",
    "northbound_",
    "official_macro_",
    "southbound_",
    "wudao_",
    "wudao_mcp_",
)
FALLBACK_PROVIDER_EXACT = {
    "infohub_public_rss_bridge",
    "rss",
}
FALLBACK_PROVIDER_PREFIXES = (
    "cnbc_top_news",
    "google_news_",
    "infohub_",
    "marketwatch_",
    "public_rss_",
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


def normalize_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def tag_values(tags, prefix):
    values = []
    marker = f"{prefix}:"
    for tag in tags or []:
        tag = str(tag)
        if tag.startswith(marker):
            values.append(tag[len(marker):])
    return values


def source_token(value):
    token = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip().lower())
    return re.sub(r"_+", "_", token).strip("_")


def item_source_tokens(item):
    tokens = set()
    for key in ("provider", "source", "producer"):
        token = source_token(item.get(key))
        if token:
            tokens.add(token)
    for tag in item.get("tags") or []:
        tag = str(tag)
        if ":" not in tag:
            continue
        prefix, value = tag.split(":", 1)
        if prefix.strip().lower() in {"provider", "source", "producer"}:
            token = source_token(value)
            if token:
                tokens.add(token)
    return tokens


def token_matches(token, exact, prefixes):
    return token in exact or any(token.startswith(prefix) for prefix in prefixes)


def is_trusted_provider_item(item):
    return any(
        token_matches(token, TRUSTED_PROVIDER_EXACT, TRUSTED_PROVIDER_PREFIXES)
        for token in item_source_tokens(item)
    )


def is_fallback_provider_item(item):
    return any(
        token_matches(token, FALLBACK_PROVIDER_EXACT, FALLBACK_PROVIDER_PREFIXES)
        for token in item_source_tokens(item)
    )


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
                raw_items = loaded.get("items") or loaded.get("contexts") or []
                if isinstance(raw_items, list):
                    items.extend(item for item in raw_items if isinstance(item, dict))
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
                        if isinstance(item, dict):
                            items.append(item)
                    except json.JSONDecodeError:
                        warnings.append(f"jsonl_bad_line:{idx}")
        except Exception as exc:
            warnings.append(f"jsonl_input_read_failed:{exc}")
    elif jsonl_path and not loaded_json_input and not path:
        warnings.append(f"jsonl_input_missing:{jsonl_path}")
    return items, warnings


def normalize_item(item, now=None, max_age_minutes=MAX_ITEM_AGE_MINUTES):
    now = now or datetime.now()
    category = str(item.get("category") or item.get("type") or "news").strip().lower()
    if category not in VALID_CATEGORIES:
        category = "news"
    sentiment = str(item.get("sentiment") or "unknown").strip().lower()
    if sentiment not in VALID_SENTIMENTS:
        sentiment = "unknown"
    published_at = item.get("published_at") or item.get("time") or item.get("created_at")
    parsed_time = parse_timestamp(published_at)
    age_minutes = None
    stale = True
    if parsed_time:
        age_minutes = round((now - parsed_time).total_seconds() / 60, 2)
        stale = age_minutes > max_age_minutes or age_minutes < -5
    impact_score = as_float(item.get("impact_score"), as_float(item.get("score"), 0.0)) or 0.0
    symbols = [symbol.upper() for symbol in normalize_list(item.get("symbols") or item.get("tickers"))]
    markets = [market.upper() for market in normalize_list(item.get("markets") or item.get("market"))]
    title = str(item.get("title") or item.get("headline") or item.get("summary") or "").strip()
    tags = normalize_list(item.get("tags"))
    providers = tag_values(tags, "provider")
    producer_values = tag_values(tags, "producer")
    provider = str(item.get("provider") or (providers[0] if providers else "") or item.get("source") or "unknown")
    if is_trusted_provider_item(item):
        provider_grade = "trusted"
    elif is_fallback_provider_item(item):
        provider_grade = "public_fallback"
    else:
        provider_grade = "unknown"
    return {
        "id": str(item.get("id") or item.get("url") or title)[:160],
        "category": category,
        "source": str(item.get("source") or "unknown"),
        "provider": provider,
        "provider_grade": provider_grade,
        "producer": str(item.get("producer") or (producer_values[0] if producer_values else provider)),
        "title": title[:240],
        "summary": str(item.get("summary") or item.get("body") or "")[:500],
        "published_at": published_at,
        "age_minutes": age_minutes,
        "stale": stale,
        "sentiment": sentiment,
        "impact_score": round(impact_score, 4),
        "markets": markets,
        "symbols": symbols,
        "url": item.get("url"),
        "tags": tags,
    }


def dedupe_items(items):
    deduped = []
    seen = set()
    for item in items:
        key = (
            item.get("id"),
            item.get("published_at"),
            item.get("title"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def summarize(items):
    by_category = Counter(item["category"] for item in items)
    by_sentiment = Counter(item["sentiment"] for item in items)
    by_source = Counter(item["source"] for item in items)
    by_provider = Counter(item.get("provider") or item.get("source") or "unknown" for item in items)
    by_producer = Counter(item.get("producer") or "unknown" for item in items)
    high_impact = [item for item in items if item["impact_score"] >= HIGH_IMPACT_MIN_SCORE and not item["stale"]]
    negative_high = [item for item in high_impact if item["sentiment"] == "negative"]
    positive_high = [item for item in high_impact if item["sentiment"] == "positive"]
    market_counts = defaultdict(int)
    symbol_counts = defaultdict(int)
    for item in items:
        for market in item.get("markets") or ["UNKNOWN"]:
            market_counts[market] += 1
        for symbol in item.get("symbols") or []:
            symbol_counts[symbol] += 1
    trusted_provider_item_count = len([item for item in items if is_trusted_provider_item(item)])
    fallback_rss_item_count = len([item for item in items if is_fallback_provider_item(item)])
    fallback_high_impact = [
        item for item in high_impact if item.get("provider_grade") == "public_fallback" or is_fallback_provider_item(item)
    ]
    unknown_high_impact = [
        item for item in high_impact if item.get("provider_grade") == "unknown"
    ]
    fallback_positive_high = [
        item for item in positive_high if item.get("provider_grade") == "public_fallback" or is_fallback_provider_item(item)
    ]
    unknown_positive_high = [
        item for item in positive_high if item.get("provider_grade") == "unknown"
    ]
    producer_fetch_failed_count = len(
        [
            warning for item in items for warning in []
        ]
    )
    return {
        "item_count": len(items),
        "fresh_item_count": len([item for item in items if not item["stale"]]),
        "stale_item_count": len([item for item in items if item["stale"]]),
        "high_impact_count": len(high_impact),
        "negative_high_impact_count": len(negative_high),
        "positive_high_impact_count": len(positive_high),
        "by_category": dict(by_category),
        "by_sentiment": dict(by_sentiment),
        "by_provider": dict(by_provider),
        "by_producer": dict(by_producer),
        "top_sources": dict(by_source.most_common(8)),
        "top_markets": dict(sorted(market_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]),
        "top_symbols": dict(sorted(symbol_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]),
        "trusted_provider_item_count": trusted_provider_item_count,
        "fallback_rss_item_count": fallback_rss_item_count,
        "fallback_high_impact_count": len(fallback_high_impact),
        "unknown_high_impact_count": len(unknown_high_impact),
        "fallback_positive_high_impact_count": len(fallback_positive_high),
        "unknown_positive_high_impact_count": len(unknown_positive_high),
        "macro_item_count": by_category.get("macro", 0),
        "capital_flow_item_count": by_category.get("capital_flow", 0),
        "watchlist_symbol_item_count": len([item for item in items if item.get("symbols")]),
        "producer_fetch_failed_count": producer_fetch_failed_count,
    }


def recommendations(status, summary, items, warnings=None):
    warnings = warnings or []
    recs = []
    if status == "MISSING":
        return [
            "wire_wudao_or_infohub_external_context_inputs",
            "hermes_should_not_claim_news_macro_awareness_without_external_context",
        ]
    if summary["negative_high_impact_count"]:
        recs.append("require_hermes_explicit_risk_note_for_negative_high_impact_events")
    if summary["positive_high_impact_count"]:
        recs.append("allow_hermes_to_consider_positive_catalysts_but_keep_intake_gates_authoritative")
    if summary["stale_item_count"] and not summary["fresh_item_count"]:
        recs.append("refresh_external_context_before_trade_judgment")
    if not any(item["category"] == "macro" for item in items):
        recs.append("macro_context_missing_add_index_rates_fx_or_policy_summary")
    if not any(item["category"] == "capital_flow" for item in items):
        recs.append("capital_flow_context_missing_add_northbound_or_market_flow_summary")
    if summary.get("trusted_provider_item_count", 0) == 0:
        recs.append("external_context_only_public_fallback_wire_wudao_infohub_or_broker_structured_feed")
    if summary.get("fallback_positive_high_impact_count", 0):
        recs.append("positive_high_impact_public_fallback_requires_source_limit_acknowledgement")
    if summary.get("unknown_positive_high_impact_count", 0):
        recs.append("positive_high_impact_unknown_provider_requires_source_limit_acknowledgement")
    if any(str(warning).startswith("producer_warning:fetch_failed:") for warning in warnings):
        recs.append("fix_external_context_provider_fetch_failures")
    if not recs:
        recs.append("external_context_available_require_hermes_to_discuss_relevant_items")
    return recs


def build_report(items=None, now=None, warnings=None, input_file=INPUT_FILE, input_jsonl_file=INPUT_JSONL_FILE):
    now = now or datetime.now()
    warnings = list(warnings or [])
    if items is None:
        raw_items, load_warnings = load_items(path=input_file, jsonl_path=input_jsonl_file)
        warnings.extend(load_warnings)
    else:
        raw_items = items
    normalized = dedupe_items([normalize_item(item, now=now) for item in raw_items if isinstance(item, dict)])
    summary = summarize(normalized)
    summary["producer_fetch_failed_count"] = len(
        [warning for warning in warnings if str(warning).startswith("producer_warning:fetch_failed:")]
    )
    if not normalized:
        status = "MISSING"
    elif summary["fresh_item_count"] == 0:
        status = "STALE"
    elif summary["negative_high_impact_count"]:
        status = "RISK"
    else:
        status = "OK"
    payload = {
        "schema": "external_market_context_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "input_file": input_file,
            "input_jsonl_file": input_jsonl_file,
            "max_item_age_minutes": MAX_ITEM_AGE_MINUTES,
            "high_impact_min_score": HIGH_IMPACT_MIN_SCORE,
            "expected_producers": [
                "wudao_mcp_flash_news",
                "infohub_macro_summary",
                "capital_flow_snapshot",
                "event_catalyst_detector",
            ],
        },
        "summary": summary,
        "items": sorted(normalized, key=lambda item: (item["stale"], -item["impact_score"], item["published_at"] or ""))[:50],
        "recommendations": recommendations(status, summary, normalized, warnings=warnings),
        "warnings": warnings,
        "hermes_use": [
            "Use this as external news, macro, capital-flow, event, and sentiment context.",
            "Do not treat this report as an execution signal; rt_order_intake gates remain authoritative.",
            "When status is MISSING or STALE, Hermes must state that current-event awareness is incomplete.",
        ],
    }
    return payload


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"External market context report {payload['generated_at']} status={payload['status']}",
        (
            f"items={summary.get('item_count')} fresh={summary.get('fresh_item_count')} "
            f"high_impact={summary.get('high_impact_count')} "
            f"negative_high={summary.get('negative_high_impact_count')}"
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
