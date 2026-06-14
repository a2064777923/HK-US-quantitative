#!/usr/bin/env python3
"""Read-only preflight for trusted Hermes context source payloads.

This validates external-event, market-sentiment, and fundamentals inputs before
operators append or promote them into the normal Hermes context reports. It
does not write ingest JSONL files, change strategy config, write alerts, edit
cron, repair data, or submit orders.
"""
import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime

try:
    import external_market_context_report as external_report
    import fundamentals_context_report as fundamentals_report
    import market_sentiment_report as sentiment_report
except ImportError:
    from scripts import external_market_context_report as external_report
    from scripts import fundamentals_context_report as fundamentals_report
    from scripts import market_sentiment_report as sentiment_report


REPORT_FILE = os.environ.get("TRUSTED_SOURCE_PREFLIGHT_REPORT_FILE", "/tmp/trusted_source_preflight_report.json")
EXTERNAL_INPUT_FILE = os.environ.get("EXTERNAL_MARKET_CONTEXT_INPUT_FILE", "/tmp/external_market_context_inputs.json")
EXTERNAL_INPUT_JSONL_FILE = os.environ.get(
    "EXTERNAL_MARKET_CONTEXT_INPUT_JSONL_FILE",
    "/tmp/external_market_context_inputs.jsonl",
)
MARKET_SENTIMENT_INPUT_FILE = os.environ.get("MARKET_SENTIMENT_INPUT_FILE", "/tmp/market_sentiment_inputs.json")
MARKET_SENTIMENT_INPUT_JSONL_FILE = os.environ.get(
    "MARKET_SENTIMENT_INPUT_JSONL_FILE",
    "/tmp/market_sentiment_inputs.jsonl",
)
FUNDAMENTALS_INPUT_FILE = os.environ.get("FUNDAMENTALS_CONTEXT_INPUT_FILE", "/tmp/fundamentals_context_inputs.json")
FUNDAMENTALS_INPUT_JSONL_FILE = os.environ.get(
    "FUNDAMENTALS_CONTEXT_INPUT_JSONL_FILE",
    "/tmp/fundamentals_context_inputs.jsonl",
)

MIN_TRUSTED_EXTERNAL_ITEMS = int(os.environ.get("TRUSTED_SOURCE_MIN_EXTERNAL_ITEMS", "1"))
MIN_FULL_FUNDAMENTALS_ITEMS = int(os.environ.get("TRUSTED_SOURCE_MIN_FULL_FUNDAMENTALS_ITEMS", "1"))

TRUSTED_SENTIMENT_EXACT = {
    "broker",
    "broker_feed",
    "capital_flow_snapshot",
    "cboe_vix",
    "exchange",
    "manual_operator",
    "official_macro",
    "vix_snapshot",
    "wudao",
    "wudao_mcp",
}
TRUSTED_SENTIMENT_PREFIXES = (
    "broker_",
    "capital_flow_",
    "cboe_",
    "exchange_",
    "futu_",
    "ibkr_",
    "interactive_brokers_",
    "northbound_",
    "official_macro_",
    "southbound_",
    "vix_",
    "wudao_",
    "wudao_mcp_",
)
FALLBACK_SENTIMENT_EXACT = {
    "infohub_public_rss_bridge",
    "public_api",
    "yahoo_chart",
}
FALLBACK_SENTIMENT_PREFIXES = (
    "infohub_",
    "public_",
    "yahoo_",
)

TRUSTED_FUNDAMENTALS_EXACT = {
    "broker_fundamentals_snapshot",
    "exchange_filing_snapshot",
    "manual_operator",
    "official_filing",
    "official_filings",
    "vendor_fundamentals_snapshot",
    "wudao_fundamentals",
}
TRUSTED_FUNDAMENTALS_PREFIXES = (
    "bloomberg_",
    "broker_",
    "exchange_",
    "factset_",
    "futu_",
    "ibkr_",
    "interactive_brokers_",
    "morningstar_",
    "official_filing",
    "refinitiv_",
    "vendor_",
    "wudao_",
)
FALLBACK_FUNDAMENTALS_EXACT = {
    "tencent_quote_snapshot",
    "yahoo_quote_summary",
}
FALLBACK_FUNDAMENTALS_PREFIXES = (
    "public_",
    "tencent_",
    "yahoo_",
)


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


def source_token(value):
    token = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip().lower())
    return re.sub(r"_+", "_", token).strip("_")


def token_matches(token, exact, prefixes):
    return token in exact or any(token.startswith(prefix) for prefix in prefixes)


def tag_tokens(tags):
    tokens = set()
    for tag in tags or []:
        text = str(tag)
        if ":" not in text:
            continue
        prefix, value = text.split(":", 1)
        if prefix.strip().lower() in {"provider", "source", "producer", "provider_symbol"}:
            token = source_token(value)
            if token:
                tokens.add(token)
    return tokens


def generic_source_tokens(item):
    tokens = set()
    for key in ("provider", "source", "producer"):
        token = source_token(item.get(key))
        if token:
            tokens.add(token)
    tokens.update(tag_tokens(item.get("tags") or []))
    return tokens


def fundamentals_source_tokens(item):
    tokens = generic_source_tokens(item)
    token = source_token(item.get("source"))
    if token:
        tokens.add(token)
    return tokens


def is_trusted_sentiment(item):
    return any(token_matches(token, TRUSTED_SENTIMENT_EXACT, TRUSTED_SENTIMENT_PREFIXES) for token in generic_source_tokens(item))


def is_fallback_sentiment(item):
    return any(token_matches(token, FALLBACK_SENTIMENT_EXACT, FALLBACK_SENTIMENT_PREFIXES) for token in generic_source_tokens(item))


def is_trusted_fundamentals(item):
    return any(
        token_matches(token, TRUSTED_FUNDAMENTALS_EXACT, TRUSTED_FUNDAMENTALS_PREFIXES)
        for token in fundamentals_source_tokens(item)
    )


def is_fallback_fundamentals(item):
    return any(
        token_matches(token, FALLBACK_FUNDAMENTALS_EXACT, FALLBACK_FUNDAMENTALS_PREFIXES)
        for token in fundamentals_source_tokens(item)
    )


def bounded(values, limit=20):
    return [str(value)[:240] for value in (values or [])[:limit]]


def load_json_collection(path, list_keys):
    if not path or not os.path.exists(path):
        return [], [f"json_input_missing:{path}"] if path else []
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception as exc:
        return [], [f"json_input_read_failed:{path}:{exc}"]
    warnings = []
    if isinstance(loaded, dict):
        producer_warnings = loaded.get("warnings")
        if isinstance(producer_warnings, list):
            warnings.extend(f"producer_warning:{str(warning).strip()}" for warning in producer_warnings if str(warning).strip())
        for key in list_keys:
            raw = loaded.get(key)
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)], warnings
        return [loaded], warnings
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)], warnings
    return [], ["json_input_invalid_type"]


def load_jsonl_collection(path):
    if not path or not os.path.exists(path):
        return [], []
    items = []
    warnings = []
    try:
        with open(path, encoding="utf-8") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    warnings.append(f"jsonl_bad_line:{idx}")
                    continue
                if isinstance(loaded, dict):
                    items.append(loaded)
    except Exception as exc:
        warnings.append(f"jsonl_input_read_failed:{path}:{exc}")
    return items, warnings


def load_inputs(path, jsonl_path, list_keys):
    items, warnings = load_json_collection(path, list_keys)
    jsonl_items, jsonl_warnings = load_jsonl_collection(jsonl_path)
    items.extend(jsonl_items)
    warnings.extend(jsonl_warnings)
    return items, warnings


def status_from_reasons(item_count, invalid_count, warn_reasons):
    if invalid_count:
        return "FAIL"
    if item_count == 0:
        return "MISSING"
    if warn_reasons:
        return "WARN"
    return "OK"


def validate_external(raw_items, now=None, warnings=None):
    now = now or datetime.now()
    warnings = list(warnings or [])
    normalized = [external_report.normalize_item(item, now=now) for item in raw_items if isinstance(item, dict)]
    invalid = []
    for item in normalized:
        reasons = []
        if not item.get("title"):
            reasons.append("missing_title")
        if not item.get("published_at"):
            reasons.append("missing_published_at")
        elif external_report.parse_timestamp(item.get("published_at")) is None:
            reasons.append("invalid_published_at")
        if item.get("impact_score") is None or item.get("impact_score") < 0 or item.get("impact_score") > 1:
            reasons.append("impact_score_out_of_range")
        if reasons:
            invalid.append({"id": item.get("id"), "title": item.get("title"), "reasons": reasons})

    trusted = [item for item in normalized if external_report.is_trusted_provider_item(item)]
    fallback = [item for item in normalized if external_report.is_fallback_provider_item(item)]
    fresh = [item for item in normalized if item.get("stale") is False]
    fresh_trusted = [item for item in trusted if item.get("stale") is False]
    categories = Counter(item.get("category") or "news" for item in normalized)
    trusted_categories = Counter(item.get("category") or "news" for item in fresh_trusted)
    warn_reasons = []
    if warnings:
        warn_reasons.append("producer_warnings_present")
    if normalized and not fresh:
        warn_reasons.append("external_items_stale")
    if len(fresh_trusted) < MIN_TRUSTED_EXTERNAL_ITEMS:
        warn_reasons.append("trusted_external_item_count_below_minimum")
    if fallback and not trusted:
        warn_reasons.append("external_context_only_public_fallback_sources")
    if categories.get("macro", 0) == 0:
        warn_reasons.append("macro_context_missing")
    if categories.get("capital_flow", 0) == 0:
        warn_reasons.append("capital_flow_context_missing")
    if not any(item.get("symbols") for item in normalized):
        warn_reasons.append("watchlist_symbol_context_missing")
    status = status_from_reasons(len(normalized), len(invalid), warn_reasons)
    return {
        "name": "external_market_context_inputs",
        "status": status,
        "item_count": len(normalized),
        "fresh_item_count": len(fresh),
        "trusted_item_count": len(trusted),
        "fresh_trusted_item_count": len(fresh_trusted),
        "fallback_item_count": len(fallback),
        "by_category": dict(categories),
        "trusted_fresh_by_category": dict(trusted_categories),
        "invalid_items": invalid[:20],
        "warnings": bounded(warnings),
        "reasons": sorted(set(warn_reasons + (["invalid_external_items"] if invalid else []))),
        "recommendations": recommendations_for_external(status, warn_reasons, invalid),
    }


def recommendations_for_external(status, reasons, invalid):
    recs = []
    if status == "MISSING":
        return ["provide_wudao_broker_or_infohub_external_context_payload_before_preflight"]
    if invalid:
        recs.append("fix_external_context_payload_schema_before_ingest")
    if "trusted_external_item_count_below_minimum" in reasons or "external_context_only_public_fallback_sources" in reasons:
        recs.append("wire_wudao_mcp_broker_or_official_macro_provider_before_claiming_trusted_event_awareness")
    if "capital_flow_context_missing" in reasons:
        recs.append("add_northbound_southbound_or_broker_capital_flow_context")
    if "macro_context_missing" in reasons:
        recs.append("add_official_macro_calendar_or_policy_context")
    if "watchlist_symbol_context_missing" in reasons:
        recs.append("include_watchlist_symbol_mapping_for_symbol_specific_event_review")
    if "external_items_stale" in reasons:
        recs.append("refresh_external_context_payload_before_ingest")
    if "producer_warnings_present" in reasons:
        recs.append("resolve_external_context_producer_warnings")
    if not recs:
        recs.append("external_trusted_source_preflight_passed")
    return sorted(set(recs))


def validate_sentiment(raw_items, now=None, warnings=None):
    now = now or datetime.now()
    warnings = list(warnings or [])
    normalized = [sentiment_report.normalize_indicator(item, now=now) for item in raw_items if isinstance(item, dict)]
    invalid = []
    for raw, item in zip([item for item in raw_items if isinstance(item, dict)], normalized):
        reasons = []
        raw_type = str(raw.get("indicator_type") or raw.get("type") or "macro").strip().lower()
        raw_direction = str(raw.get("direction") or "unknown").strip().lower()
        if not item.get("name"):
            reasons.append("missing_name")
        if not item.get("observed_at"):
            reasons.append("missing_observed_at")
        elif sentiment_report.parse_timestamp(item.get("observed_at")) is None:
            reasons.append("invalid_observed_at")
        if raw_type not in sentiment_report.VALID_INDICATOR_TYPES:
            reasons.append("invalid_indicator_type")
        if raw_direction not in sentiment_report.VALID_DIRECTIONS:
            reasons.append("invalid_direction")
        if sentiment_report.as_float(raw.get("score")) is not None and not -1 <= sentiment_report.as_float(raw.get("score")) <= 1:
            reasons.append("score_out_of_range")
        if reasons:
            invalid.append({"id": item.get("id"), "name": item.get("name"), "reasons": reasons})

    trusted = [item for item in normalized if is_trusted_sentiment(item)]
    fallback = [item for item in normalized if is_fallback_sentiment(item)]
    fresh = [item for item in normalized if item.get("stale") is False]
    types = Counter(item.get("indicator_type") or "macro" for item in normalized)
    warn_reasons = []
    if warnings:
        warn_reasons.append("producer_warnings_present")
    if normalized and not fresh:
        warn_reasons.append("sentiment_indicators_stale")
    if normalized and not trusted:
        warn_reasons.append("sentiment_only_public_fallback_sources")
    if types.get("volatility", 0) == 0:
        warn_reasons.append("volatility_context_missing")
    if types.get("capital_flow", 0) == 0:
        warn_reasons.append("capital_flow_sentiment_missing")
    status = status_from_reasons(len(normalized), len(invalid), warn_reasons)
    return {
        "name": "market_sentiment_inputs",
        "status": status,
        "indicator_count": len(normalized),
        "fresh_indicator_count": len(fresh),
        "trusted_indicator_count": len(trusted),
        "fallback_indicator_count": len(fallback),
        "by_type": dict(types),
        "invalid_indicators": invalid[:20],
        "warnings": bounded(warnings),
        "reasons": sorted(set(warn_reasons + (["invalid_market_sentiment_indicators"] if invalid else []))),
        "recommendations": recommendations_for_sentiment(status, warn_reasons, invalid),
    }


def recommendations_for_sentiment(status, reasons, invalid):
    recs = []
    if status == "MISSING":
        return ["provide_vix_capital_flow_or_risk_appetite_payload_before_preflight"]
    if invalid:
        recs.append("fix_market_sentiment_payload_schema_before_ingest")
    if "sentiment_only_public_fallback_sources" in reasons:
        recs.append("wire_broker_northbound_cboe_or_official_macro_sentiment_provider")
    if "volatility_context_missing" in reasons:
        recs.append("add_vix_or_equivalent_volatility_context")
    if "capital_flow_sentiment_missing" in reasons:
        recs.append("add_northbound_southbound_etf_or_broker_flow_indicator")
    if "sentiment_indicators_stale" in reasons:
        recs.append("refresh_market_sentiment_payload_before_ingest")
    if "producer_warnings_present" in reasons:
        recs.append("resolve_market_sentiment_producer_warnings")
    if not recs:
        recs.append("market_sentiment_trusted_source_preflight_passed")
    return sorted(set(recs))


def validate_fundamentals(raw_items, now=None, warnings=None):
    now = now or datetime.now()
    warnings = list(warnings or [])
    normalized = [fundamentals_report.normalize_item(item, now=now) for item in raw_items if isinstance(item, dict)]
    invalid = []
    for item in normalized:
        reasons = []
        if not item.get("symbol"):
            reasons.append("missing_symbol")
        if not item.get("as_of"):
            reasons.append("missing_as_of")
        elif fundamentals_report.parse_timestamp(item.get("as_of")) is None:
            reasons.append("invalid_as_of")
        if reasons:
            invalid.append({"symbol": item.get("symbol"), "source": item.get("source"), "reasons": reasons})

    trusted = [item for item in normalized if is_trusted_fundamentals(item)]
    fallback = [item for item in normalized if is_fallback_fundamentals(item)]
    full = [item for item in normalized if (item.get("fundamental_completeness") or {}).get("level") == "full"]
    trusted_full = [item for item in full if is_trusted_fundamentals(item)]
    fresh = [item for item in normalized if item.get("stale") is False]
    completeness = Counter((item.get("fundamental_completeness") or {}).get("level") or "unknown" for item in normalized)
    warn_reasons = []
    if warnings:
        warn_reasons.append("producer_warnings_present")
    if normalized and not fresh:
        warn_reasons.append("fundamentals_items_stale")
    if len(trusted_full) < MIN_FULL_FUNDAMENTALS_ITEMS:
        warn_reasons.append("trusted_full_fundamentals_count_below_minimum")
    if fallback:
        warn_reasons.append("fundamentals_public_or_partial_fallback_sources_present")
    if completeness.get("partial", 0) or completeness.get("empty", 0):
        warn_reasons.append("partial_fundamentals_present")
    status = status_from_reasons(len(normalized), len(invalid), warn_reasons)
    return {
        "name": "fundamentals_context_inputs",
        "status": status,
        "item_count": len(normalized),
        "fresh_item_count": len(fresh),
        "trusted_item_count": len(trusted),
        "fallback_item_count": len(fallback),
        "full_item_count": len(full),
        "trusted_full_item_count": len(trusted_full),
        "completeness_counts": dict(completeness),
        "invalid_items": invalid[:20],
        "warnings": bounded(warnings),
        "reasons": sorted(set(warn_reasons + (["invalid_fundamentals_items"] if invalid else []))),
        "recommendations": recommendations_for_fundamentals(status, warn_reasons, invalid),
    }


def recommendations_for_fundamentals(status, reasons, invalid):
    recs = []
    if status == "MISSING":
        return ["provide_broker_vendor_or_official_fundamentals_payload_before_preflight"]
    if invalid:
        recs.append("fix_fundamentals_payload_schema_before_ingest")
    if "trusted_full_fundamentals_count_below_minimum" in reasons:
        recs.append("wire_broker_vendor_or_official_fundamentals_provider")
    if "fundamentals_public_or_partial_fallback_sources_present" in reasons:
        recs.append("treat_yahoo_or_tencent_fundamentals_as_partial_until_broker_vendor_coverage_exists")
    if "partial_fundamentals_present" in reasons:
        recs.append("fill_pb_ps_roe_growth_and_leverage_metrics_before_claiming_full_fundamentals")
    if "fundamentals_items_stale" in reasons:
        recs.append("refresh_fundamentals_payload_before_buy_review")
    if "producer_warnings_present" in reasons:
        recs.append("resolve_fundamentals_producer_warnings")
    if not recs:
        recs.append("fundamentals_trusted_source_preflight_passed")
    return sorted(set(recs))


def classify_overall(components):
    statuses = [component.get("status") for component in components]
    if "FAIL" in statuses:
        return "FAIL"
    if all(status == "MISSING" for status in statuses):
        return "MISSING"
    if any(status in ("WARN", "MISSING") for status in statuses):
        return "WARN"
    return "OK"


def build_report(
    external_items=None,
    sentiment_indicators=None,
    fundamentals_items=None,
    external_warnings=None,
    sentiment_warnings=None,
    fundamentals_warnings=None,
    now=None,
    external_input_file=EXTERNAL_INPUT_FILE,
    external_input_jsonl_file=EXTERNAL_INPUT_JSONL_FILE,
    sentiment_input_file=MARKET_SENTIMENT_INPUT_FILE,
    sentiment_input_jsonl_file=MARKET_SENTIMENT_INPUT_JSONL_FILE,
    fundamentals_input_file=FUNDAMENTALS_INPUT_FILE,
    fundamentals_input_jsonl_file=FUNDAMENTALS_INPUT_JSONL_FILE,
):
    now = now or datetime.now()
    if external_items is None:
        external_items, external_warnings = load_inputs(
            external_input_file,
            external_input_jsonl_file,
            ("items", "contexts"),
        )
    if sentiment_indicators is None:
        sentiment_indicators, sentiment_warnings = load_inputs(
            sentiment_input_file,
            sentiment_input_jsonl_file,
            ("indicators", "items"),
        )
    if fundamentals_items is None:
        fundamentals_items, fundamentals_warnings = load_inputs(
            fundamentals_input_file,
            fundamentals_input_jsonl_file,
            ("items", "fundamentals"),
        )

    components = [
        validate_external(external_items or [], now=now, warnings=external_warnings or []),
        validate_sentiment(sentiment_indicators or [], now=now, warnings=sentiment_warnings or []),
        validate_fundamentals(fundamentals_items or [], now=now, warnings=fundamentals_warnings or []),
    ]
    status = classify_overall(components)
    recommendations = sorted({rec for component in components for rec in component.get("recommendations") or []})
    return {
        "schema": "trusted_source_preflight_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "changes_crontab": False,
            "writes_ingest_files": False,
            "repairs_data": False,
            "external_input_file": external_input_file,
            "external_input_jsonl_file": external_input_jsonl_file,
            "sentiment_input_file": sentiment_input_file,
            "sentiment_input_jsonl_file": sentiment_input_jsonl_file,
            "fundamentals_input_file": fundamentals_input_file,
            "fundamentals_input_jsonl_file": fundamentals_input_jsonl_file,
        },
        "summary": {
            "component_count": len(components),
            "status_counts": dict(Counter(component["status"] for component in components)),
            "failed_component_count": len([component for component in components if component["status"] == "FAIL"]),
            "warning_or_missing_component_count": len(
                [component for component in components if component["status"] in ("WARN", "MISSING")]
            ),
        },
        "components": components,
        "recommendations": recommendations or ["trusted_source_preflight_passed"],
        "ingest_workflow": {
            "external_context_dry_run": (
                "/usr/bin/python3 /root/external_market_context_ingest.py "
                "--input-file <trusted_external_payload.json> --dry-run --text"
            ),
            "external_context_append": (
                "/usr/bin/python3 /root/external_market_context_ingest.py "
                "--input-file <trusted_external_payload.json> --text"
            ),
            "market_sentiment_dry_run": (
                "/usr/bin/python3 /root/market_sentiment_ingest.py "
                "--input-file <trusted_sentiment_payload.json> --dry-run --text"
            ),
            "market_sentiment_append": (
                "/usr/bin/python3 /root/market_sentiment_ingest.py "
                "--input-file <trusted_sentiment_payload.json> --text"
            ),
            "fundamentals_context_dry_run": (
                "/usr/bin/python3 /root/fundamentals_context_ingest.py "
                "--input-file <trusted_fundamentals_payload.json> --dry-run --text"
            ),
            "fundamentals_context_append": (
                "/usr/bin/python3 /root/fundamentals_context_ingest.py "
                "--input-file <trusted_fundamentals_payload.json> --append --text"
            ),
            "post_ingest_refresh": [
                "/usr/bin/python3 /root/external_market_context_report.py --output /tmp/external_market_context_report.json --text",
                "/usr/bin/python3 /root/market_sentiment_report.py --output /tmp/market_sentiment_report.json --text",
                "/usr/bin/python3 /root/fundamentals_context_report.py --output /tmp/fundamentals_context_report.json --text",
                "/usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text",
                "/usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text",
                "/usr/bin/python3 /root/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json --ephemeral-state",
            ],
            "operator_contract": {
                "manual_review_required": True,
                "preflight_does_not_append": True,
                "append_commands_do_not_submit_orders": True,
                "post_refresh_still_does_not_enable_execute": True,
            },
        },
        "hermes_use": [
            "Use this preflight to distinguish trusted structured source payloads from public fallback context before relying on them in judgments.",
            "OK means payload shape and minimum trusted coverage passed; it still does not approve trades or bypass readiness gates.",
            "WARN means Hermes may read the context but must explicitly state the source limitation before any approve/reduce.",
            "FAIL means operators should fix the payload before ingesting it or letting Hermes cite it as evidence.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Trusted source preflight {payload['generated_at']} status={payload['status']}",
        (
            f"components={summary.get('component_count')} "
            f"failed={summary.get('failed_component_count')} "
            f"warn_or_missing={summary.get('warning_or_missing_component_count')} "
            f"status_counts={summary.get('status_counts', {})}"
        ),
    ]
    for component in payload.get("components") or []:
        lines.append(
            "  {status} {name}: reasons={reasons}".format(
                status=component.get("status"),
                name=component.get("name"),
                reasons=",".join(component.get("reasons") or []),
            )
        )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-input-file", default=EXTERNAL_INPUT_FILE)
    parser.add_argument("--external-input-jsonl-file", default=EXTERNAL_INPUT_JSONL_FILE)
    parser.add_argument("--market-sentiment-input-file", default=MARKET_SENTIMENT_INPUT_FILE)
    parser.add_argument("--market-sentiment-input-jsonl-file", default=MARKET_SENTIMENT_INPUT_JSONL_FILE)
    parser.add_argument("--fundamentals-input-file", default=FUNDAMENTALS_INPUT_FILE)
    parser.add_argument("--fundamentals-input-jsonl-file", default=FUNDAMENTALS_INPUT_JSONL_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        external_input_file=args.external_input_file,
        external_input_jsonl_file=args.external_input_jsonl_file,
        sentiment_input_file=args.market_sentiment_input_file,
        sentiment_input_jsonl_file=args.market_sentiment_input_jsonl_file,
        fundamentals_input_file=args.fundamentals_input_file,
        fundamentals_input_jsonl_file=args.fundamentals_input_jsonl_file,
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
    return 2 if payload["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
