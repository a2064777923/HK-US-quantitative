#!/usr/bin/env python3
"""Read-only market sentiment and risk-appetite context for Hermes."""
import argparse
import json
import os
from collections import Counter
from datetime import datetime


INPUT_FILE = os.environ.get("MARKET_SENTIMENT_INPUT_FILE", "/tmp/market_sentiment_inputs.json")
INPUT_JSONL_FILE = os.environ.get("MARKET_SENTIMENT_INPUT_JSONL_FILE", "/tmp/market_sentiment_inputs.jsonl")
REPORT_FILE = os.environ.get("MARKET_SENTIMENT_REPORT_FILE", "/tmp/market_sentiment_report.json")
MAX_ITEM_AGE_MINUTES = float(os.environ.get("MARKET_SENTIMENT_MAX_ITEM_AGE_MINUTES", "180"))
RISK_ON_MIN_SCORE = float(os.environ.get("MARKET_SENTIMENT_RISK_ON_MIN_SCORE", "0.25"))
RISK_OFF_MAX_SCORE = float(os.environ.get("MARKET_SENTIMENT_RISK_OFF_MAX_SCORE", "-0.25"))
VALID_INDICATOR_TYPES = {
    "volatility",
    "capital_flow",
    "breadth",
    "risk_appetite",
    "funding",
    "social_sentiment",
    "macro",
}
VALID_DIRECTIONS = {"risk_on", "risk_off", "neutral", "mixed", "unknown"}


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
        raw = value
    elif isinstance(value, str):
        raw = value.replace(";", ",").split(",")
    else:
        raw = [value]
    result = []
    for item in raw:
        text = str(item).strip().upper()
        if text:
            result.append(text)
    return result


def load_indicators(path=INPUT_FILE, jsonl_path=INPUT_JSONL_FILE):
    indicators = []
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
                raw = loaded.get("indicators") or loaded.get("items") or []
                if isinstance(raw, list):
                    indicators.extend(item for item in raw if isinstance(item, dict))
                else:
                    warnings.append("json_input_indicators_not_list")
            elif isinstance(loaded, list):
                indicators.extend(item for item in loaded if isinstance(item, dict))
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
                        indicators.append(item)
        except Exception as exc:
            warnings.append(f"jsonl_input_read_failed:{exc}")
    elif jsonl_path and not loaded_json_input and not path:
        warnings.append(f"jsonl_input_missing:{jsonl_path}")
    return indicators, warnings


def normalize_indicator(item, now=None, max_age_minutes=MAX_ITEM_AGE_MINUTES):
    now = now or datetime.now()
    indicator_type = str(item.get("indicator_type") or item.get("type") or "macro").strip().lower()
    if indicator_type not in VALID_INDICATOR_TYPES:
        indicator_type = "macro"
    direction = str(item.get("direction") or "unknown").strip().lower()
    if direction not in VALID_DIRECTIONS:
        direction = "unknown"
    observed_at = item.get("observed_at") or item.get("published_at") or item.get("time") or item.get("created_at")
    parsed_time = parse_timestamp(observed_at)
    age_minutes = None
    stale = True
    if parsed_time:
        age_minutes = round((now - parsed_time).total_seconds() / 60, 2)
        stale = age_minutes > max_age_minutes or age_minutes < -5
    score = as_float(item.get("score"), 0.0)
    if score is None:
        score = 0.0
    score = max(min(score, 1.0), -1.0)
    value = as_float(item.get("value"))
    previous_value = as_float(item.get("previous_value"))
    change = as_float(item.get("change"))
    if change is None and value is not None and previous_value is not None:
        change = value - previous_value
    return {
        "id": str(item.get("id") or item.get("name") or item.get("title") or indicator_type)[:160],
        "indicator_type": indicator_type,
        "name": str(item.get("name") or item.get("title") or indicator_type)[:160],
        "source": str(item.get("source") or "unknown"),
        "observed_at": observed_at,
        "age_minutes": age_minutes,
        "stale": stale,
        "markets": normalize_list(item.get("markets") or item.get("market")),
        "direction": direction,
        "score": round(score, 4),
        "value": value,
        "previous_value": previous_value,
        "change": change,
        "unit": item.get("unit"),
        "summary": str(item.get("summary") or item.get("description") or "")[:500],
        "tags": normalize_list(item.get("tags")),
    }


def dedupe_indicators(indicators):
    deduped = []
    seen = set()
    for item in indicators:
        key = (item.get("id"), item.get("indicator_type"), item.get("observed_at"), tuple(item.get("markets") or []))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def summarize(indicators):
    fresh = [item for item in indicators if not item["stale"]]
    risk_off = [item for item in fresh if item["direction"] == "risk_off"]
    risk_on = [item for item in fresh if item["direction"] == "risk_on"]
    by_type = Counter(item["indicator_type"] for item in indicators)
    by_direction = Counter(item["direction"] for item in indicators)
    market_scores = {}
    for market in sorted({market for item in fresh for market in (item.get("markets") or ["GLOBAL"])}):
        rows = [item for item in fresh if market in (item.get("markets") or ["GLOBAL"])]
        if rows:
            market_scores[market] = round(sum(item["score"] for item in rows) / len(rows), 4)
    overall_score = round(sum(item["score"] for item in fresh) / len(fresh), 4) if fresh else None
    return {
        "indicator_count": len(indicators),
        "fresh_indicator_count": len(fresh),
        "stale_indicator_count": len([item for item in indicators if item["stale"]]),
        "risk_off_count": len(risk_off),
        "risk_on_count": len(risk_on),
        "overall_score": overall_score,
        "market_scores": market_scores,
        "by_type": dict(by_type),
        "by_direction": dict(by_direction),
    }


def classify_status(summary):
    if summary["indicator_count"] == 0:
        return "MISSING"
    if summary["fresh_indicator_count"] == 0:
        return "STALE"
    score = summary.get("overall_score")
    if summary["risk_off_count"] and (score is None or score <= RISK_OFF_MAX_SCORE):
        return "RISK"
    return "OK"


def build_recommendations(status, summary, indicators):
    if status == "MISSING":
        return [
            "wire_vix_capital_flow_or_sentiment_producer",
            "hermes_should_not_claim_quantified_sentiment_awareness_without_market_sentiment",
        ]
    if status == "STALE":
        return ["refresh_market_sentiment_before_trade_judgment"]
    recs = []
    if status == "RISK":
        recs.append("tighten_new_buy_review_when_sentiment_is_risk_off")
    if not any(item["indicator_type"] == "volatility" for item in indicators):
        recs.append("volatility_context_missing_add_vix_or_equivalent")
    if not any(item["indicator_type"] == "capital_flow" for item in indicators):
        recs.append("capital_flow_context_missing_add_northbound_or_etf_flow")
    if not recs:
        recs.append("market_sentiment_available_require_hermes_to_discuss_relevant_indicators")
    return recs


def build_report(indicators=None, now=None, warnings=None, input_file=INPUT_FILE, input_jsonl_file=INPUT_JSONL_FILE):
    now = now or datetime.now()
    warnings = list(warnings or [])
    if indicators is None:
        raw_indicators, load_warnings = load_indicators(path=input_file, jsonl_path=input_jsonl_file)
        warnings.extend(load_warnings)
    else:
        raw_indicators = indicators
    normalized = dedupe_indicators(
        [normalize_indicator(item, now=now) for item in raw_indicators if isinstance(item, dict)]
    )
    summary = summarize(normalized)
    status = classify_status(summary)
    payload = {
        "schema": "market_sentiment_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "input_file": input_file,
            "input_jsonl_file": input_jsonl_file,
            "max_item_age_minutes": MAX_ITEM_AGE_MINUTES,
            "risk_on_min_score": RISK_ON_MIN_SCORE,
            "risk_off_max_score": RISK_OFF_MAX_SCORE,
            "expected_producers": [
                "vix_or_volatility_snapshot",
                "northbound_or_market_capital_flow",
                "infohub_macro_risk_appetite_summary",
                "social_sentiment_snapshot_optional",
            ],
        },
        "summary": summary,
        "indicators": sorted(normalized, key=lambda item: (item["stale"], item["score"], item["observed_at"] or ""))[:50],
        "recommendations": build_recommendations(status, summary, normalized),
        "warnings": warnings,
        "hermes_use": [
            "Use this as quantified volatility, capital-flow, risk-appetite, and sentiment context.",
            "Do not treat this report as an execution signal; rt_order_intake gates remain authoritative.",
            "When status is MISSING or STALE, Hermes must state quantified sentiment awareness is incomplete.",
        ],
    }
    return payload


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Market sentiment report {payload['generated_at']} status={payload['status']}",
        (
            f"indicators={summary.get('indicator_count')} fresh={summary.get('fresh_indicator_count')} "
            f"score={summary.get('overall_score')} risk_off={summary.get('risk_off_count')}"
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
