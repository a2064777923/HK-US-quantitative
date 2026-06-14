#!/usr/bin/env python3
"""Read-only source reliability matrix for Hermes/v5 evidence inputs."""
import argparse
import json
import os
from collections import Counter
from datetime import datetime


REPORT_FILE = os.environ.get("SOURCE_RELIABILITY_REPORT_FILE", "/tmp/source_reliability_report.json")
DATA_SOURCE_INVENTORY_FILE = os.environ.get(
    "DATA_SOURCE_INVENTORY_REPORT_FILE",
    "/tmp/data_source_inventory_report.json",
)
KLINE_SOURCE_GRANULARITY_REPORT_FILE = os.environ.get(
    "KLINE_SOURCE_GRANULARITY_REPORT_FILE",
    "/tmp/kline_source_granularity_report.json",
)
DATA_HEALTH_FILE = os.environ.get("DATA_HEALTH_REPORT_FILE", "/tmp/data_health_report.json")
MARKET_CONTEXT_FILE = os.environ.get("MARKET_CONTEXT_REPORT_FILE", "/tmp/market_context_report.json")
INTRADAY_KLINE_BATCH_REPORT_FILE = os.environ.get(
    "INTRADAY_KLINE_BATCH_REPORT_FILE",
    "/tmp/intraday_kline_batch.json",
)
INTRADAY_CONTEXT_REPORT_FILE = os.environ.get("INTRADAY_CONTEXT_REPORT_FILE", "/tmp/intraday_context_report.json")
INTRADAY_TIMEFRAME_QUALITY_REPORT_FILE = os.environ.get(
    "INTRADAY_TIMEFRAME_QUALITY_REPORT_FILE",
    "/tmp/intraday_timeframe_quality_report.json",
)
INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE = os.environ.get(
    "INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE",
    "/tmp/intraday_market_session_overrides_report.json",
)
EXTERNAL_MARKET_CONTEXT_FILE = os.environ.get(
    "EXTERNAL_MARKET_CONTEXT_REPORT_FILE",
    "/tmp/external_market_context_report.json",
)
EVENT_CATALYST_REPORT_FILE = os.environ.get("EVENT_CATALYST_REPORT_FILE", "/tmp/event_catalyst_report.json")
EVENT_CATALYST_SIGNAL_REPORT_FILE = os.environ.get(
    "EVENT_CATALYST_SIGNAL_REPORT_FILE",
    "/tmp/event_catalyst_signal_report.json",
)
MARKET_SENTIMENT_REPORT_FILE = os.environ.get("MARKET_SENTIMENT_REPORT_FILE", "/tmp/market_sentiment_report.json")
FUNDAMENTALS_CONTEXT_REPORT_FILE = os.environ.get(
    "FUNDAMENTALS_CONTEXT_REPORT_FILE",
    "/tmp/fundamentals_context_report.json",
)
TRUSTED_SOURCE_PREFLIGHT_REPORT_FILE = os.environ.get(
    "TRUSTED_SOURCE_PREFLIGHT_REPORT_FILE",
    "/tmp/trusted_source_preflight_report.json",
)
TRUSTED_SOURCE_DISCOVERY_REPORT_FILE = os.environ.get(
    "TRUSTED_SOURCE_DISCOVERY_REPORT_FILE",
    "/tmp/trusted_source_discovery_report.json",
)
CRON_AUDIT_REPORT_FILE = os.environ.get("CRON_AUDIT_REPORT_FILE", "/tmp/cron_audit_report.json")
OUTCOME_REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
MAX_REPORT_AGE_MINUTES = float(os.environ.get("SOURCE_RELIABILITY_MAX_REPORT_AGE_MINUTES", "90"))


INPUTS = [
    ("data_source_inventory", DATA_SOURCE_INVENTORY_FILE, "data_source_inventory_report_v1", ("generated_at",)),
    (
        "kline_source_granularity",
        KLINE_SOURCE_GRANULARITY_REPORT_FILE,
        "kline_source_granularity_report_v1",
        ("generated_at",),
    ),
    ("data_health", DATA_HEALTH_FILE, "data_health_report_v1", ("generated_at", "checked_at")),
    ("market_context", MARKET_CONTEXT_FILE, "market_context_report_v1", ("generated_at",)),
    ("intraday_kline_batch", INTRADAY_KLINE_BATCH_REPORT_FILE, "intraday_kline_batch_report_v1", ("generated_at",)),
    ("intraday_context", INTRADAY_CONTEXT_REPORT_FILE, "intraday_context_report_v1", ("generated_at",)),
    (
        "intraday_timeframe_quality",
        INTRADAY_TIMEFRAME_QUALITY_REPORT_FILE,
        "intraday_timeframe_quality_report_v1",
        ("generated_at",),
    ),
    (
        "intraday_market_session_overrides",
        INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE,
        "intraday_market_session_overrides_report_v1",
        ("generated_at",),
    ),
    ("external_market_context", EXTERNAL_MARKET_CONTEXT_FILE, "external_market_context_report_v1", ("generated_at",)),
    ("event_catalysts", EVENT_CATALYST_REPORT_FILE, "event_catalyst_report_v1", ("generated_at",)),
    (
        "event_catalyst_signals",
        EVENT_CATALYST_SIGNAL_REPORT_FILE,
        "event_catalyst_signal_report_v1",
        ("generated_at",),
    ),
    ("market_sentiment", MARKET_SENTIMENT_REPORT_FILE, "market_sentiment_report_v1", ("generated_at",)),
    ("fundamentals_context", FUNDAMENTALS_CONTEXT_REPORT_FILE, "fundamentals_context_report_v1", ("generated_at",)),
    (
        "trusted_source_preflight",
        TRUSTED_SOURCE_PREFLIGHT_REPORT_FILE,
        "trusted_source_preflight_report_v1",
        ("generated_at",),
    ),
    (
        "trusted_source_discovery",
        TRUSTED_SOURCE_DISCOVERY_REPORT_FILE,
        "trusted_source_discovery_report_v1",
        ("generated_at",),
    ),
    ("cron_audit", CRON_AUDIT_REPORT_FILE, "cron_audit_report_v1", ("generated_at",)),
    ("rt_signal_outcome", OUTCOME_REPORT_FILE, "rt_signal_outcome_report_v1", ("generated_at",)),
]


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


def load_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def report_timestamp(payload, keys):
    for key in keys:
        parsed = parse_timestamp(payload.get(key))
        if parsed:
            return key, parsed, payload.get(key)
    return None, None, None


def bounded_list(values, limit=20):
    return [str(value)[:240] for value in (values or [])[:limit]]


def as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def base_component(name, path, expected_schema, payload, now, timestamp_keys, max_age_minutes):
    reasons = []
    raw_status = payload.get("status") if payload else None
    status = str(raw_status).upper() if raw_status not in (None, "") else "UNKNOWN" if payload else "MISSING"
    schema = payload.get("schema") if payload else None
    if not payload:
        reasons.append("report_missing_or_unreadable")
    elif schema != expected_schema:
        reasons.append("schema_invalid")
    _ts_key, ts, ts_raw = report_timestamp(payload, timestamp_keys)
    age_minutes = round((now - ts).total_seconds() / 60, 2) if ts else None
    if payload and not ts:
        reasons.append("timestamp_missing")
    elif ts and (age_minutes > max_age_minutes or age_minutes < -5):
        reasons.append("report_stale")

    if status in ("FAIL", "BLOCKED"):
        reliability = "FAIL"
    elif not payload:
        reliability = "MISSING"
    elif "schema_invalid" in reasons:
        reliability = "FAIL"
    elif "timestamp_missing" in reasons or "report_stale" in reasons:
        reliability = "STALE"
    elif status == "MISSING":
        reliability = "MISSING"
    elif status in ("WARN", "RISK", "STALE", "ACTION_REQUIRED", "REVIEW", "PARTIAL", "UNRESOLVED"):
        reliability = "DEGRADED"
    else:
        reliability = "OK"

    return {
        "name": name,
        "path": path,
        "schema": schema,
        "expected_schema": expected_schema,
        "report_status": status,
        "reliability_status": reliability,
        "timestamp": ts_raw,
        "age_minutes": age_minutes,
        "source": payload.get("source") if isinstance(payload.get("source"), dict) else {},
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        "warnings": bounded_list(payload.get("warnings") or []),
        "reasons": reasons,
        "recommendations": bounded_list(payload.get("recommendations") or [], limit=12),
    }


def apply_data_health(component, payload):
    markets = payload.get("markets") if isinstance(payload.get("markets"), dict) else {}
    source_counts = Counter()
    missing_source = 0
    repair_source = 0
    for market_payload in markets.values():
        if not isinstance(market_payload, dict):
            continue
        source_quality = market_payload.get("source_quality") if isinstance(market_payload.get("source_quality"), dict) else {}
        source_counts.update(source_quality.get("daily_latest_source_counts") or {})
        missing_source += int(source_quality.get("missing_daily_latest_source_count") or 0)
        repair_source += int(source_quality.get("repair_daily_latest_count") or 0)
    component["coverage"] = {
        "daily_latest_source_counts": dict(source_counts),
        "missing_daily_latest_source_count": missing_source,
        "repair_daily_latest_count": repair_source,
    }
    if missing_source:
        component["reasons"].append("daily_latest_data_source_missing")
    if repair_source:
        component["reasons"].append("daily_latest_contains_repair_sources")
    if missing_source or repair_source:
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_data_source_inventory(component, payload):
    summary = component.get("summary") or {}
    weaknesses = payload.get("weaknesses") if isinstance(payload.get("weaknesses"), list) else []
    error_count = int(summary.get("error_weakness_count") or 0)
    warning_count = int(summary.get("warning_weakness_count") or 0)
    weakness_codes = sorted({row.get("code") for row in weaknesses if isinstance(row, dict) and row.get("code")})
    if weakness_codes:
        component["coverage"] = {
            "table_status_counts": summary.get("table_status_counts") if isinstance(summary.get("table_status_counts"), dict) else {},
            "context_file_status_counts": (
                summary.get("context_file_status_counts")
                if isinstance(summary.get("context_file_status_counts"), dict)
                else {}
            ),
            "present_input_payload_file_count": int(summary.get("present_input_payload_file_count") or 0),
            "kline_source_counts": summary.get("kline_source_counts") if isinstance(summary.get("kline_source_counts"), dict) else {},
            "weakness_codes": weakness_codes,
        }
    if error_count:
        component["reasons"].append("data_source_inventory_errors")
        component["reliability_status"] = "FAIL"
    elif warning_count:
        component["reasons"].append("data_source_inventory_weaknesses")
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_kline_source_granularity(component, payload):
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    proposal = payload.get("proposal") if isinstance(payload.get("proposal"), dict) else {}
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    report_status = str(component.get("report_status") or "").upper()
    action_count = int(summary.get("proposal_action_count") or proposal.get("action_count") or 0)
    column_exists = bool(summary.get("source_granularity_column_exists"))
    component["coverage"] = {
        "source_granularity_column_exists": column_exists,
        "proposal_action_count": action_count,
        "estimated_backfill_row_count": int(summary.get("estimated_backfill_row_count") or 0),
        "unmapped_missing_granularity_group_count": int(
            summary.get("unmapped_missing_granularity_group_count") or len(issues)
        ),
        "proposal_hash": proposal.get("proposal_hash"),
        "apply_command_available": bool(proposal.get("apply_command")),
    }
    if report_status in ("FAIL", "BLOCKED"):
        component["reasons"].append("kline_source_granularity_report_failed")
        component["reliability_status"] = "FAIL"
    if not column_exists:
        component["reasons"].append("kline_source_granularity_column_missing")
    if action_count:
        component["reasons"].append("kline_source_granularity_backfill_proposal_pending")
    if issues:
        component["reasons"].append("kline_source_granularity_unmapped_sources")
    if report_status in ("ACTION_REQUIRED", "REVIEW", "WARN") or action_count or issues or not column_exists:
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_market_context(component, payload):
    markets = payload.get("markets") if isinstance(payload.get("markets"), dict) else {}
    native_rows = []
    missing_or_incomplete = []
    conflicts = []
    fallback_only = []
    for market, summary in sorted(markets.items()):
        if not isinstance(summary, dict):
            continue
        native = summary.get("native_index_context")
        if not isinstance(native, dict):
            missing_or_incomplete.append(market)
            native_rows.append({"market": market, "status": "MISSING", "alignment": "incomplete"})
            continue
        status = str(native.get("status") or "MISSING").upper()
        alignment = native.get("alignment")
        primary_index = native.get("primary_index") if isinstance(native.get("primary_index"), dict) else {}
        native_rows.append(
            {
                "market": market,
                "status": status,
                "alignment": alignment,
                "primary_symbol": primary_index.get("symbol"),
                "primary_source": primary_index.get("source"),
                "provider_grade": primary_index.get("provider_grade"),
                "latest_lag_days_vs_stock_pool": native.get("latest_lag_days_vs_stock_pool"),
            }
        )
        if status != "OK" or alignment == "incomplete":
            missing_or_incomplete.append(market)
        if alignment == "conflicts_with_breadth":
            conflicts.append(market)
        if status == "OK" and primary_index.get("provider_grade") == "public_fallback":
            fallback_only.append(market)
    if native_rows:
        component["native_index_context"] = native_rows
    if missing_or_incomplete:
        component["reasons"].append("market_context_native_index_missing_or_incomplete")
    if conflicts:
        component["reasons"].append("market_context_native_index_conflicts_with_breadth")
    if fallback_only:
        component["reasons"].append("market_context_native_index_public_fallback_only")
    if missing_or_incomplete or conflicts:
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")
    if fallback_only:
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_fundamentals(component):
    summary = component.get("summary") or {}
    fallback_count = int(summary.get("fallback_item_count") or 0)
    partial_count = int(summary.get("partial_item_count") or 0)
    fetch_failed_count = int(summary.get("producer_fetch_failed_count") or 0)
    if fallback_count:
        component["reasons"].append("fundamentals_fallback_provider_used")
    if partial_count:
        component["reasons"].append("fundamentals_partial_metric_coverage")
    if fetch_failed_count:
        component["reasons"].append("fundamentals_primary_provider_fetch_failed")
    if fallback_count or partial_count or fetch_failed_count:
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_external_context(component):
    summary = component.get("summary") or {}
    if not summary:
        return
    fallback_count = int(summary.get("fallback_rss_item_count") or 0)
    trusted_count = int(summary.get("trusted_provider_item_count") or 0)
    fetch_failed_count = int(summary.get("producer_fetch_failed_count") or 0)
    capital_flow_count = int(summary.get("capital_flow_item_count") or 0)
    fallback_positive_high_count = int(summary.get("fallback_positive_high_impact_count") or 0)
    unknown_positive_high_count = int(summary.get("unknown_positive_high_impact_count") or 0)
    has_capital_flow_coverage = "capital_flow_item_count" in summary
    if fallback_count and not trusted_count:
        component["reasons"].append("external_context_only_public_fallback_sources")
    if fallback_positive_high_count:
        component["reasons"].append("external_context_positive_high_impact_public_fallback")
    if unknown_positive_high_count:
        component["reasons"].append("external_context_positive_high_impact_unknown_provider")
    if fetch_failed_count:
        component["reasons"].append("external_context_provider_fetch_failed")
    if has_capital_flow_coverage and not capital_flow_count:
        component["reasons"].append("external_context_capital_flow_missing")
    if (
        (fallback_count and not trusted_count)
        or fallback_positive_high_count
        or unknown_positive_high_count
        or fetch_failed_count
        or (has_capital_flow_coverage and not capital_flow_count)
    ):
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_intraday_context(component):
    summary = component.get("summary") or {}
    source = component.get("source") or {}
    warnings = component.get("warnings") or []
    missing_symbols = int(summary.get("missing_symbol_count") or 0)
    stale_symbols = int(summary.get("stale_symbol_count") or 0)
    closed_symbols = int(summary.get("closed_symbol_count") or 0)
    ok_symbols = int(summary.get("ok_symbol_count") or 0)
    degraded_quality_symbols = int(summary.get("quality_degraded_symbol_count") or 0)
    large_gap_symbols = int(summary.get("large_gap_symbol_count") or 0)
    invalid_ohlc_symbols = int(summary.get("invalid_ohlc_symbol_count") or 0)
    bad_timestamp_symbols = int(summary.get("bad_timestamp_symbol_count") or 0)
    duplicate_timestamp_symbols = int(summary.get("duplicate_timestamp_symbol_count") or 0)
    missing_granularity_symbols = int(summary.get("missing_source_granularity_symbol_count") or 0)
    low_fidelity_source_symbols = int(summary.get("low_fidelity_source_symbol_count") or 0)
    snapshot_like_symbols = int(summary.get("snapshot_like_symbol_count") or 0)
    full_ohlc_symbols = int(summary.get("full_ohlc_symbol_count") or 0)
    component["coverage"] = {
        "ok_symbol_count": ok_symbols,
        "closed_symbol_count": closed_symbols,
        "stale_symbol_count": stale_symbols,
        "missing_symbol_count": missing_symbols,
        "quality_degraded_symbol_count": degraded_quality_symbols,
        "large_gap_symbol_count": large_gap_symbols,
        "invalid_ohlc_symbol_count": invalid_ohlc_symbols,
        "bad_timestamp_symbol_count": bad_timestamp_symbols,
        "duplicate_timestamp_symbol_count": duplicate_timestamp_symbols,
        "missing_source_granularity_symbol_count": missing_granularity_symbols,
        "low_fidelity_source_symbol_count": low_fidelity_source_symbols,
        "snapshot_like_symbol_count": snapshot_like_symbols,
        "full_ohlc_symbol_count": full_ohlc_symbols,
        "market_session_overrides_file": source.get("market_session_overrides_file"),
    }
    if stale_symbols:
        component["reasons"].append("intraday_context_stale_symbols")
    if closed_symbols:
        component["reasons"].append("intraday_context_market_closed")
    if missing_symbols:
        component["reasons"].append("intraday_context_missing_symbols")
    if degraded_quality_symbols:
        component["reasons"].append("intraday_context_quality_degraded_symbols")
    if large_gap_symbols:
        component["reasons"].append("intraday_context_large_minute_gaps")
    if invalid_ohlc_symbols or bad_timestamp_symbols or duplicate_timestamp_symbols:
        component["reasons"].append("intraday_context_invalid_minute_rows")
    if missing_granularity_symbols:
        component["reasons"].append("intraday_context_source_granularity_missing")
    if low_fidelity_source_symbols:
        component["reasons"].append("intraday_context_low_fidelity_minute_source")
    if snapshot_like_symbols:
        component["reasons"].append("intraday_context_snapshot_like_minute_rows")
    if any(str(warning).startswith("intraday_market_session_overrides_file_") for warning in warnings):
        component["reasons"].append("intraday_market_session_overrides_unavailable")

    # A generated report with missing minute coverage is degraded context, not a broken safety contract.
    if (
        component.get("report_status") == "MISSING"
        and component.get("reliability_status") == "MISSING"
        and "report_missing_or_unreadable" not in component["reasons"]
    ):
        component["reliability_status"] = "DEGRADED"
    if (
        stale_symbols
        or closed_symbols
        or missing_symbols
        or degraded_quality_symbols
        or large_gap_symbols
        or invalid_ohlc_symbols
        or bad_timestamp_symbols
        or duplicate_timestamp_symbols
        or missing_granularity_symbols
        or low_fidelity_source_symbols
        or snapshot_like_symbols
        or "intraday_market_session_overrides_unavailable" in component["reasons"]
    ):
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_intraday_timeframe_quality(component, payload):
    summary = component.get("summary") or {}
    source = component.get("source") or {}
    policy = payload.get("decision_policy") if isinstance(payload.get("decision_policy"), dict) else {}
    timeframes = summary.get("timeframes") if isinstance(summary.get("timeframes"), dict) else {}
    limited_symbols = int(summary.get("limited_timeframe_symbol_count") or 0)
    missing_timeframe_symbols = int(summary.get("missing_timeframe_symbol_count") or 0)
    conflict_symbols = int(summary.get("conflict_symbol_count") or 0)
    low_fidelity_symbols = int(summary.get("low_fidelity_symbol_count") or 0)
    snapshot_like_symbols = int(summary.get("snapshot_like_symbol_count") or 0)
    missing_granularity_symbols = int(summary.get("missing_source_granularity_symbol_count") or 0)
    closed_symbols = int(summary.get("closed_symbol_count") or 0)
    stale_symbols = int(summary.get("stale_symbol_count") or 0)
    degraded_symbols = int(summary.get("degraded_symbol_count") or 0)
    missing_symbols = int(summary.get("missing_symbol_count") or 0)
    symbol_count = int(summary.get("symbol_count") or 0)
    decision_use_counts = summary.get("decision_use_counts") if isinstance(summary.get("decision_use_counts"), dict) else {}
    soft_confirmation_symbols = int(summary.get("soft_confirmation_eligible_symbol_count") or 0)
    cap_or_challenge_symbols = int(summary.get("cap_or_challenge_only_symbol_count") or 0)
    diagnostic_only_symbols = int(summary.get("diagnostic_only_symbol_count") or 0)
    decision_use_counts_present = any(
        key in summary
        for key in (
            "decision_use_counts",
            "soft_confirmation_eligible_symbol_count",
            "cap_or_challenge_only_symbol_count",
            "diagnostic_only_symbol_count",
        )
    )
    component["coverage"] = {
        "symbol_count": symbol_count,
        "degraded_symbol_count": degraded_symbols,
        "missing_symbol_count": missing_symbols,
        "limited_timeframe_symbol_count": limited_symbols,
        "missing_timeframe_symbol_count": missing_timeframe_symbols,
        "conflict_symbol_count": conflict_symbols,
        "low_fidelity_symbol_count": low_fidelity_symbols,
        "snapshot_like_symbol_count": snapshot_like_symbols,
        "missing_source_granularity_symbol_count": missing_granularity_symbols,
        "closed_symbol_count": closed_symbols,
        "stale_symbol_count": stale_symbols,
        "decision_use_counts_present": decision_use_counts_present,
        "decision_use_counts": decision_use_counts,
        "soft_confirmation_eligible_symbol_count": soft_confirmation_symbols,
        "cap_or_challenge_only_symbol_count": cap_or_challenge_symbols,
        "diagnostic_only_symbol_count": diagnostic_only_symbols,
        "timeframes": timeframes,
        "input_file": source.get("input_file"),
        "decision_policy_present": bool(policy),
        "confidence_use": policy.get("confidence_use"),
        "may_raise_confidence": policy.get("may_raise_confidence"),
        "can_override_daily_gates": policy.get("can_override_daily_gates"),
        "execution_permission": policy.get("execution_permission"),
    }
    unsafe_source_contract = (
        source.get("writes_database")
        or source.get("submits_orders")
        or source.get("changes_strategy")
        or source.get("changes_crontab")
    )
    unsafe_decision_policy = (
        policy.get("can_override_daily_gates")
        or policy.get("execution_permission")
        or (
            policy.get("may_raise_confidence")
            and policy.get("requires_forward_evidence_before_confidence_raise") is False
        )
    )
    if unsafe_source_contract or unsafe_decision_policy:
        component["reasons"].append("intraday_timeframe_quality_safety_contract_unsafe")
        component["reliability_status"] = "FAIL"
    if limited_symbols:
        component["reasons"].append("intraday_timeframe_coverage_limited")
    if missing_timeframe_symbols or missing_symbols:
        component["reasons"].append("intraday_timeframe_coverage_missing")
    if conflict_symbols:
        component["reasons"].append("intraday_timeframe_conflicts")
    if low_fidelity_symbols:
        component["reasons"].append("intraday_timeframe_low_fidelity_minute_source")
    if snapshot_like_symbols:
        component["reasons"].append("intraday_timeframe_snapshot_like_minute_rows")
    if missing_granularity_symbols:
        component["reasons"].append("intraday_timeframe_source_granularity_missing")
    if closed_symbols:
        component["reasons"].append("intraday_timeframe_market_closed")
    if stale_symbols:
        component["reasons"].append("intraday_timeframe_stale_symbols")
    if degraded_symbols:
        component["reasons"].append("intraday_timeframe_quality_degraded_symbols")
    if symbol_count and not decision_use_counts_present:
        component["reasons"].append("intraday_timeframe_decision_use_counts_missing")
    elif diagnostic_only_symbols:
        component["reasons"].append("intraday_timeframe_diagnostic_only_symbols")
    elif symbol_count and soft_confirmation_symbols <= 0:
        component["reasons"].append("intraday_timeframe_no_soft_confirmation_symbols")
    if any(reason.startswith("intraday_timeframe_") for reason in component["reasons"]):
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_intraday_market_session_overrides(component):
    summary = component.get("summary") or {}
    warnings = component.get("warnings") or []
    error_count = int(summary.get("error_count") or 0)
    warning_count = int(summary.get("warning_count") or 0)
    component["coverage"] = {
        "ok_market_count": int(summary.get("ok_market_count") or 0),
        "warning_market_count": int(summary.get("warning_market_count") or 0),
        "failed_market_count": int(summary.get("failed_market_count") or 0),
        "warning_count": warning_count,
        "error_count": error_count,
    }
    if error_count:
        component["reasons"].append("intraday_market_session_overrides_invalid")
        component["reliability_status"] = "FAIL"
    elif warning_count or warnings:
        component["reasons"].append("intraday_market_session_overrides_incomplete")
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_intraday_kline_batch(component):
    summary = component.get("summary") or {}
    source = component.get("source") or {}
    status = component.get("report_status")
    action_count = int(summary.get("action_count") or 0)
    planned_rows = int(summary.get("planned_row_count") or 0)
    unresolved = int(summary.get("unresolved_count") or 0)
    sparse_us = int(summary.get("sparse_us_action_count") or 0)
    invalid_rows = int(summary.get("invalid_source_row_count") or 0)
    component["coverage"] = {
        "action_count": action_count,
        "planned_row_count": planned_rows,
        "unresolved_count": unresolved,
        "sparse_us_action_count": sparse_us,
        "invalid_source_row_count": invalid_rows,
        "provider_contract": source.get("provider_contract"),
    }
    if source and (
        source.get("submits_orders")
        or source.get("changes_strategy")
        or source.get("changes_alert_queue")
        or source.get("changes_crontab")
        or source.get("repairs_daily_klines")
    ):
        component["reasons"].append("intraday_kline_batch_safety_contract_unsafe")
        component["reliability_status"] = "FAIL"
    if source.get("provider_contract") == "unofficial_public_web_endpoint_unversioned_best_effort":
        component["reasons"].append("intraday_kline_batch_unofficial_public_provider")
    if status == "ACTIONABLE" and action_count:
        component["reasons"].append("intraday_kline_batch_apply_pending")
    if status in ("PARTIAL", "UNRESOLVED") or unresolved:
        component["reasons"].append("intraday_kline_batch_unresolved_symbols")
    if sparse_us:
        component["reasons"].append("intraday_kline_batch_sparse_us_rows")
    if invalid_rows:
        component["reasons"].append("intraday_kline_batch_invalid_source_rows")
    if (
        "intraday_kline_batch_unofficial_public_provider" in component["reasons"]
        or "intraday_kline_batch_apply_pending" in component["reasons"]
        or "intraday_kline_batch_unresolved_symbols" in component["reasons"]
        or "intraday_kline_batch_sparse_us_rows" in component["reasons"]
        or "intraday_kline_batch_invalid_source_rows" in component["reasons"]
    ):
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def outcome_primary_metric(payload):
    metric = payload.get("primary_horizon_metric")
    if isinstance(metric, dict):
        return metric
    horizons = payload.get("horizon_metrics") if isinstance(payload.get("horizon_metrics"), list) else []
    primary = str(payload.get("primary_horizon") or "")
    for row in horizons:
        if isinstance(row, dict) and str(row.get("horizon_days")) == primary:
            return row
    return horizons[0] if horizons and isinstance(horizons[0], dict) else {}


def apply_rt_signal_outcome(component, payload):
    summary = payload.get("intraday_sequence_summary")
    summary = summary if isinstance(summary, dict) else {}
    metric = outcome_primary_metric(payload)
    effective_first_hit_counts = (
        metric.get("effective_first_hit_counts")
        if isinstance(metric.get("effective_first_hit_counts"), dict)
        else {}
    )
    low_fidelity = as_int(
        summary.get(
            "low_fidelity_count",
            effective_first_hit_counts.get("ambiguous_intraday_low_fidelity", 0),
        )
    )
    missing = as_int(summary.get("missing_count"))
    same_minute_ambiguous = as_int(summary.get("ambiguous_count"))
    unresolved = as_int(summary.get("unresolved_count"))
    ambiguous_daily = as_int(summary.get("ambiguous_daily_count"))
    resolved = as_int(summary.get("resolved_count"))
    unresolved_rate = as_float(metric.get("effective_unresolved_first_hit_rate_pct"))
    component["coverage"] = {
        "ambiguous_daily_count": ambiguous_daily,
        "resolved_count": resolved,
        "missing_count": missing,
        "same_minute_ambiguous_count": same_minute_ambiguous,
        "unresolved_count": unresolved,
        "low_fidelity_count": low_fidelity,
        "status_counts": summary.get("status_counts") if isinstance(summary.get("status_counts"), dict) else {},
        "first_hit_counts": summary.get("first_hit_counts") if isinstance(summary.get("first_hit_counts"), dict) else {},
        "effective_first_hit_counts": effective_first_hit_counts,
        "effective_unresolved_first_hit_rate_pct": unresolved_rate,
    }
    if low_fidelity:
        component["reasons"].append("outcome_intraday_path_low_fidelity")
    if missing:
        component["reasons"].append("outcome_intraday_path_missing_minute_rows")
    if same_minute_ambiguous:
        component["reasons"].append("outcome_intraday_path_same_minute_ambiguous")
    if unresolved:
        component["reasons"].append("outcome_intraday_path_unresolved")
    if unresolved_rate is not None and unresolved_rate >= 25:
        component["reasons"].append("outcome_intraday_path_high_unresolved_rate")
    if any(reason.startswith("outcome_intraday_path_") for reason in component["reasons"]):
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_cron(component):
    summary = component.get("summary") or {}
    missing = int(summary.get("missing_required_job_count") or 0)
    dangerous = int(summary.get("dangerous_enabled_count") or 0)
    if dangerous:
        component["reasons"].append("dangerous_execution_cron_enabled")
        component["reliability_status"] = "FAIL"
    elif missing:
        component["reasons"].append("required_read_only_cron_jobs_missing")
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_trusted_source_preflight(component):
    summary = component.get("summary") or {}
    status = component.get("report_status")
    failed = int(summary.get("failed_component_count") or 0)
    weak = int(summary.get("warning_or_missing_component_count") or 0)
    if status == "FAIL" or failed:
        component["reasons"].append("trusted_source_preflight_failed")
        component["reliability_status"] = "FAIL"
    elif status in ("WARN", "MISSING") or weak:
        component["reasons"].append("trusted_source_preflight_not_clean")
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def apply_trusted_source_discovery(component, payload):
    status = component.get("report_status")
    capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), list) else []
    critical_capabilities = {
        row.get("capability"): row
        for row in capabilities
        if row.get("capability")
        in (
            "trusted_event_context",
            "capital_flow_context",
            "market_sentiment_context",
            "full_fundamentals_context",
        )
    }
    missing = [
        name
        for name, row in critical_capabilities.items()
        if row.get("status") == "MISSING"
    ]
    configured_unverified = [
        name
        for name, row in critical_capabilities.items()
        if row.get("status") == "CONFIGURED_UNVERIFIED"
    ]
    if status in ("FAIL", "BLOCKED"):
        component["reasons"].append("trusted_source_discovery_failed")
        component["reliability_status"] = "FAIL"
    if missing:
        component["reasons"].append("trusted_source_capabilities_missing")
        component["missing_capabilities"] = sorted(missing)
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")
    if configured_unverified:
        component["reasons"].append("trusted_source_capabilities_configured_but_unverified")
        component["configured_unverified_capabilities"] = sorted(configured_unverified)
        component["reliability_status"] = worse_reliability(component["reliability_status"], "DEGRADED")


def worse_reliability(current, candidate):
    order = {"OK": 0, "DEGRADED": 1, "STALE": 2, "MISSING": 3, "FAIL": 4}
    return candidate if order.get(candidate, 0) > order.get(current, 0) else current


def component_from_payload(name, path, expected_schema, payload, now, timestamp_keys, max_age_minutes):
    component = base_component(name, path, expected_schema, payload, now, timestamp_keys, max_age_minutes)
    if name == "data_source_inventory" and payload:
        apply_data_source_inventory(component, payload)
    elif name == "kline_source_granularity" and payload:
        apply_kline_source_granularity(component, payload)
    elif name == "data_health" and payload:
        apply_data_health(component, payload)
    elif name == "market_context" and payload:
        apply_market_context(component, payload)
    elif name == "intraday_kline_batch" and payload:
        apply_intraday_kline_batch(component)
    elif name == "intraday_context" and payload:
        apply_intraday_context(component)
    elif name == "intraday_timeframe_quality" and payload:
        apply_intraday_timeframe_quality(component, payload)
    elif name == "intraday_market_session_overrides" and payload:
        apply_intraday_market_session_overrides(component)
    elif name == "external_market_context":
        apply_external_context(component)
    elif name == "fundamentals_context":
        apply_fundamentals(component)
    elif name == "trusted_source_preflight":
        apply_trusted_source_preflight(component)
    elif name == "trusted_source_discovery" and payload:
        apply_trusted_source_discovery(component, payload)
    elif name == "cron_audit":
        apply_cron(component)
    elif name == "rt_signal_outcome" and payload:
        apply_rt_signal_outcome(component, payload)
    return component


def classify_overall(components):
    statuses = [component.get("reliability_status") for component in components]
    if "FAIL" in statuses:
        return "FAIL"
    if "MISSING" in statuses:
        return "MISSING"
    if "STALE" in statuses:
        return "STALE"
    if "DEGRADED" in statuses:
        return "DEGRADED"
    return "OK"


def build_recommendations(status, components):
    recs = []
    for component in components:
        name = component["name"]
        for reason in component.get("reasons") or []:
            if reason == "report_missing_or_unreadable":
                recs.append(f"wire_or_refresh_source_report:{name}")
            elif reason == "report_stale":
                recs.append(f"refresh_stale_source_report:{name}")
            elif reason == "schema_invalid":
                recs.append(f"fix_source_report_schema:{name}")
            elif reason == "data_source_inventory_errors":
                recs.append("fix_data_source_inventory_errors_before_claiming_data_visibility")
            elif reason == "data_source_inventory_weaknesses":
                recs.append("review_data_source_inventory_weaknesses_before_hermes_review")
            elif reason == "kline_source_granularity_report_failed":
                recs.append("fix_kline_source_granularity_report_before_claiming_intraday_provenance")
            elif reason == "kline_source_granularity_column_missing":
                recs.append("review_hash_confirmed_source_granularity_column_proposal")
            elif reason == "kline_source_granularity_backfill_proposal_pending":
                recs.append("review_hash_confirmed_source_granularity_backfill_before_full_intraday_claims")
            elif reason == "kline_source_granularity_unmapped_sources":
                recs.append("map_or_exclude_unmapped_kline_sources_before_inferring_granularity")
            elif reason == "fundamentals_primary_provider_fetch_failed":
                recs.append("fix_fundamentals_primary_provider_or_replace_with_broker_fundamentals")
            elif reason == "fundamentals_partial_metric_coverage":
                recs.append("treat_fundamentals_as_partial_until_full_metric_provider_available")
            elif reason == "required_read_only_cron_jobs_missing":
                recs.append("install_missing_read_only_cron_jobs_before_claiming_autonomous_context_refresh")
            elif reason == "dangerous_execution_cron_enabled":
                recs.append("disable_dangerous_execution_cron_before_any_trading_review")
            elif reason == "daily_latest_data_source_missing":
                recs.append("repair_or_explain_missing_daily_kline_data_source_provenance")
            elif reason == "daily_latest_contains_repair_sources":
                recs.append("verify_repaired_daily_kline_rows_before_trusting_forward_evidence")
            elif reason == "market_context_native_index_missing_or_incomplete":
                recs.append("populate_native_hk_us_index_ohlcv_before_claiming_real_index_market_regime")
            elif reason == "market_context_native_index_conflicts_with_breadth":
                recs.append("require_hermes_to_discuss_native_index_vs_stock_pool_breadth_conflict")
            elif reason == "market_context_native_index_public_fallback_only":
                recs.append("replace_public_index_snapshot_with_broker_vendor_or_official_index_feed")
            elif reason == "external_context_only_public_fallback_sources":
                recs.append("wire_structured_wudao_infohub_or_broker_context_before_claiming_full_event_awareness")
            elif reason == "external_context_provider_fetch_failed":
                recs.append("fix_external_context_provider_fetch_failures")
            elif reason == "external_context_capital_flow_missing":
                recs.append("add_capital_flow_provider_to_external_context")
            elif reason == "external_context_positive_high_impact_public_fallback":
                recs.append("positive_high_impact_public_fallback_requires_source_limit_acknowledgement")
            elif reason == "external_context_positive_high_impact_unknown_provider":
                recs.append("positive_high_impact_unknown_provider_requires_source_limit_acknowledgement")
            elif reason == "intraday_context_stale_symbols":
                recs.append("refresh_intraday_context_before_trade_judgment")
            elif reason == "intraday_context_market_closed":
                recs.append("treat_intraday_context_as_last_session_only_until_market_reopens")
            elif reason == "intraday_market_session_overrides_unavailable":
                recs.append("configure_or_fix_intraday_market_session_overrides_for_holidays_and_half_days")
            elif reason == "intraday_market_session_overrides_invalid":
                recs.append("fix_intraday_market_session_override_schema_before_trusting_calendar")
            elif reason == "intraday_market_session_overrides_incomplete":
                recs.append("review_intraday_market_session_override_coverage_for_holidays_and_half_days")
            elif reason == "intraday_context_missing_symbols":
                recs.append("wire_minute_kline_refresh_for_watchlist_symbols")
            elif reason == "intraday_context_quality_degraded_symbols":
                recs.append("review_intraday_quality_before_using_minute_path_evidence")
            elif reason == "intraday_context_large_minute_gaps":
                recs.append("refresh_or_repair_minute_kline_gap_coverage")
            elif reason == "intraday_context_invalid_minute_rows":
                recs.append("fix_invalid_intraday_kline_rows_before_trusting_path_evidence")
            elif reason == "intraday_context_source_granularity_missing":
                recs.append("persist_intraday_source_granularity_before_claiming_full_ohlcv_context")
            elif reason == "intraday_context_low_fidelity_minute_source":
                recs.append("treat_low_fidelity_intraday_sources_as_advisory_until_vendor_feed_available")
            elif reason == "intraday_context_snapshot_like_minute_rows":
                recs.append("avoid_using_snapshot_like_minute_rows_as_full_ohlcv_intraday_evidence")
            elif reason == "intraday_timeframe_quality_safety_contract_unsafe":
                recs.append("disable_or_fix_intraday_timeframe_quality_report_before_using_it")
            elif reason == "intraday_timeframe_coverage_limited":
                recs.append("do_not_raise_confidence_from_limited_30m_60m_coverage")
            elif reason == "intraday_timeframe_coverage_missing":
                recs.append("collect_more_minute_rows_before_using_all_intraday_timeframes")
            elif reason == "intraday_timeframe_conflicts":
                recs.append("require_hermes_to_discuss_intraday_timeframe_conflicts")
            elif reason == "intraday_timeframe_low_fidelity_minute_source":
                recs.append("treat_low_fidelity_intraday_timeframes_as_advisory")
            elif reason == "intraday_timeframe_snapshot_like_minute_rows":
                recs.append("avoid_using_snapshot_minute_timeframes_as_full_ohlcv_evidence")
            elif reason == "intraday_timeframe_source_granularity_missing":
                recs.append("review_source_granularity_before_full_intraday_timeframe_claims")
            elif reason == "intraday_timeframe_market_closed":
                recs.append("treat_timeframe_quality_as_last_session_only_until_market_reopens")
            elif reason == "intraday_timeframe_stale_symbols":
                recs.append("refresh_intraday_timeframe_quality_before_trade_judgment")
            elif reason == "intraday_timeframe_quality_degraded_symbols":
                recs.append("cap_hermes_confidence_when_intraday_timeframe_quality_is_degraded")
            elif reason == "intraday_timeframe_decision_use_counts_missing":
                recs.append("rerun_intraday_timeframe_quality_before_using_decision_use_contract")
            elif reason == "intraday_timeframe_diagnostic_only_symbols":
                recs.append("treat_diagnostic_only_intraday_timeframes_as_unavailable_for_confirmation")
            elif reason == "intraday_timeframe_no_soft_confirmation_symbols":
                recs.append("do_not_use_intraday_timeframes_to_raise_confidence_without_soft_confirmation_symbols")
            elif reason == "intraday_kline_batch_safety_contract_unsafe":
                recs.append("disable_or_fix_intraday_kline_batch_before_using_minute_data")
            elif reason == "intraday_kline_batch_unofficial_public_provider":
                recs.append("treat_intraday_minute_provider_as_best_effort_until_vendor_feed_available")
            elif reason == "intraday_kline_batch_apply_pending":
                recs.append("operator_review_hash_confirmed_intraday_minute_apply_before_claiming_collection")
            elif reason == "intraday_kline_batch_unresolved_symbols":
                recs.append("review_intraday_minute_provider_coverage_or_symbol_mapping")
            elif reason == "intraday_kline_batch_sparse_us_rows":
                recs.append("treat_sparse_us_minute_rows_as_execution_quality_only")
            elif reason == "intraday_kline_batch_invalid_source_rows":
                recs.append("fix_invalid_intraday_minute_source_rows_before_apply")
            elif reason == "outcome_intraday_path_low_fidelity":
                recs.append("collect_full_ohlcv_minute_path_evidence_before_claiming_intraday_path_resolution")
            elif reason == "outcome_intraday_path_missing_minute_rows":
                recs.append("collect_minute_klines_to_resolve_ambiguous_daily_outcomes")
            elif reason == "outcome_intraday_path_same_minute_ambiguous":
                recs.append("manual_review_same_minute_stop_target_order_before_learning")
            elif reason == "outcome_intraday_path_unresolved":
                recs.append("review_daily_vs_minute_threshold_mismatch_before_learning")
            elif reason == "outcome_intraday_path_high_unresolved_rate":
                recs.append("increase_full_ohlcv_minute_coverage_before_using_first_hit_rates")
            elif reason == "trusted_source_preflight_failed":
                recs.append("fix_trusted_source_payload_schema_before_ingest")
            elif reason == "trusted_source_preflight_not_clean":
                recs.append("wire_trusted_wudao_broker_or_official_payloads_before_claiming_full_context_coverage")
            elif reason == "trusted_source_discovery_failed":
                recs.append("fix_trusted_source_discovery_before_claiming_source_coverage")
            elif reason == "trusted_source_capabilities_missing":
                recs.append("configure_missing_wudao_broker_official_or_vendor_source_adapters")
            elif reason == "trusted_source_capabilities_configured_but_unverified":
                recs.append("run_dry_run_source_export_and_trusted_preflight_for_configured_adapters")
        for recommendation in component.get("recommendations") or []:
            recs.append(str(recommendation))
    if not recs:
        recs.append("source_reliability_matrix_clean")
    return sorted(set(recs))


def build_report(payloads=None, now=None, max_age_minutes=MAX_REPORT_AGE_MINUTES):
    now = now or datetime.now()
    payloads = payloads or {}
    components = []
    for name, path, expected_schema, timestamp_keys in INPUTS:
        payload = payloads.get(name)
        if payload is None:
            payload = load_json_file(path)
        components.append(
            component_from_payload(name, path, expected_schema, payload, now, timestamp_keys, max_age_minutes)
        )
    status = classify_overall(components)
    status_counts = Counter(component["reliability_status"] for component in components)
    return {
        "schema": "source_reliability_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_crontab": False,
            "changes_strategy": False,
            "repairs_data": False,
            "max_report_age_minutes": max_age_minutes,
        },
        "summary": {
            "component_count": len(components),
            "status_counts": dict(status_counts),
            "degraded_or_worse_count": len([row for row in components if row["reliability_status"] != "OK"]),
        },
        "components": components,
        "recommendations": build_recommendations(status, components),
        "hermes_use": [
            "Use this matrix to judge whether context sources are complete, degraded, stale, missing, or failed.",
            "A fresh downstream report can still be DEGRADED when it relies on fallback providers, partial metrics, missing cron, or weak provenance.",
            "Do not treat this report as an execution signal; readiness and rt_order_intake remain authoritative.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Source reliability report {payload['generated_at']} status={payload['status']}",
        (
            f"components={summary.get('component_count')} "
            f"degraded_or_worse={summary.get('degraded_or_worse_count')} "
            f"status_counts={summary.get('status_counts', {})}"
        ),
    ]
    for component in payload.get("components") or []:
        if component.get("reliability_status") != "OK":
            lines.append(
                "  {status} {name}: report_status={report_status} reasons={reasons}".format(
                    status=component.get("reliability_status"),
                    name=component.get("name"),
                    report_status=component.get("report_status"),
                    reasons=",".join(component.get("reasons") or []),
                )
            )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--max-report-age-minutes", type=float, default=MAX_REPORT_AGE_MINUTES)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(max_age_minutes=args.max_report_age_minutes)
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
    return 0 if payload["status"] in ("OK", "DEGRADED", "STALE", "MISSING") else 2


if __name__ == "__main__":
    raise SystemExit(main())
