#!/usr/bin/env python3
"""Build a single review packet for Hermes trade judgment.

The packet is review-only. It may run rt_order_intake in dry-run mode to
produce sizing/rejection context, but it never submits simulation orders.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

try:
    import portfolio_report
    import rt_order_intake as intake
    import stock_universe_hygiene_promote as universe_promote
    import system_health_check
except ImportError:
    try:
        from scripts import portfolio_report
        from scripts import rt_order_intake as intake
        from scripts import stock_universe_hygiene_promote as universe_promote
        from scripts import system_health_check
    except ImportError:
        universe_promote = None
        from scripts import portfolio_report
        from scripts import rt_order_intake as intake
        from scripts import system_health_check


PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
PACKET_ARCHIVE_DIR = os.environ.get("HERMES_REVIEW_PACKET_ARCHIVE_DIR", "/tmp/hermes_review_packet_archive")
PACKET_ARCHIVE_MAX_FILES = int(os.environ.get("HERMES_REVIEW_PACKET_ARCHIVE_MAX_FILES", "360"))
PACKET_ARCHIVE_MAX_AGE_HOURS = float(os.environ.get("HERMES_REVIEW_PACKET_ARCHIVE_MAX_AGE_HOURS", "24"))
PACKET_ARCHIVE_MAX_BYTES = int(os.environ.get("HERMES_REVIEW_PACKET_ARCHIVE_MAX_BYTES", str(1024 * 1024 * 1024)))
JUDGMENT_SCHEMA = os.environ.get("HERMES_JUDGMENT_SCHEMA", "hermes_trade_judgment_v1")
POSITION_JUDGMENT_SCHEMA = os.environ.get("HERMES_POSITION_JUDGMENT_SCHEMA", "hermes_position_judgment_v1")
POSITION_JUDGMENT_FILE = os.environ.get("HERMES_POSITION_JUDGMENT_FILE", "/tmp/hermes_position_judgments.jsonl")
OUTCOME_REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
ALERT_QUALITY_REPORT_FILE = os.environ.get("ALERT_QUALITY_REPORT_FILE", "/tmp/rt_alert_quality_report.json")
ALERT_EVENT_STORE_REPORT_FILE = os.environ.get("RT_ALERT_EVENT_STORE_REPORT_FILE", "/tmp/rt_alert_event_store_report.json")
JUDGMENT_EVENT_STORE_REPORT_FILE = os.environ.get(
    "HERMES_JUDGMENT_EVENT_STORE_REPORT_FILE",
    "/tmp/hermes_judgment_event_store_report.json",
)
INTAKE_EVENT_STORE_REPORT_FILE = os.environ.get(
    "RT_ORDER_INTAKE_EVENT_STORE_REPORT_FILE",
    "/tmp/rt_order_intake_event_store_report.json",
)
OUTCOME_EVENT_STORE_REPORT_FILE = os.environ.get(
    "RT_SIGNAL_OUTCOME_EVENT_STORE_REPORT_FILE",
    "/tmp/rt_signal_outcome_event_store_report.json",
)
STRATEGY_REVIEW_REPORT_FILE = os.environ.get("STRATEGY_REVIEW_REPORT_FILE", "/tmp/strategy_review_report.json")
STRATEGY_LEARNING_REPORT_FILE = os.environ.get("STRATEGY_LEARNING_REPORT_FILE", "/tmp/strategy_learning_report.json")
EXECUTION_READINESS_REPORT_FILE = os.environ.get("EXECUTION_READINESS_REPORT_FILE", "/tmp/execution_readiness_report.json")
SIMULATION_PERFORMANCE_REPORT_FILE = os.environ.get(
    "SIMULATION_PERFORMANCE_REPORT_FILE",
    "/tmp/simulation_performance_report.json",
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
CRON_AUDIT_REPORT_FILE = os.environ.get("CRON_AUDIT_REPORT_FILE", "/tmp/cron_audit_report.json")
SOURCE_RELIABILITY_REPORT_FILE = os.environ.get(
    "SOURCE_RELIABILITY_REPORT_FILE",
    "/tmp/source_reliability_report.json",
)
OPERATOR_ACTION_QUEUE_REPORT_FILE = os.environ.get(
    "OPERATOR_ACTION_QUEUE_REPORT_FILE",
    "/tmp/operator_action_queue_report.json",
)
MARKET_CONTEXT_FILE = os.environ.get("MARKET_CONTEXT_REPORT_FILE", "/tmp/market_context_report.json")
DATA_HEALTH_REPORT_FILE = os.environ.get("DATA_HEALTH_REPORT_FILE", "/tmp/data_health_report.json")
DATA_SOURCE_INVENTORY_REPORT_FILE = os.environ.get(
    "DATA_SOURCE_INVENTORY_REPORT_FILE",
    "/tmp/data_source_inventory_report.json",
)
KLINE_SOURCE_GRANULARITY_REPORT_FILE = os.environ.get(
    "KLINE_SOURCE_GRANULARITY_REPORT_FILE",
    "/tmp/kline_source_granularity_report.json",
)
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
KLINE_DAILY_GAP_REPAIR_FILE = os.environ.get("KLINE_DAILY_GAP_REPAIR_FILE", "/tmp/kline_daily_gap_repair.json")
KLINE_GAP_SOURCE_DIAGNOSTIC_FILE = os.environ.get(
    "KLINE_GAP_SOURCE_DIAGNOSTIC_FILE",
    "/tmp/kline_gap_source_diagnostic_report.json",
)
KLINE_GAP_ALTERNATE_PROVIDER_PROBE_FILE = os.environ.get(
    "KLINE_GAP_ALTERNATE_PROVIDER_PROBE_FILE",
    "/tmp/kline_gap_alternate_provider_probe.json",
)
KLINE_GAP_ALTERNATE_PROVIDER_REPAIR_PLAN_FILE = os.environ.get(
    "KLINE_GAP_ALTERNATE_PROVIDER_REPAIR_PLAN_FILE",
    "/tmp/kline_gap_alternate_provider_repair_plan.json",
)
UNIVERSE_REPORT_FILE = os.environ.get("UNIVERSE_RANK_REPORT_FILE", "/tmp/universe_rank_report.json")
WATCHLIST_DIFF_REPORT_FILE = os.environ.get("WATCHLIST_DIFF_REPORT_FILE", "/tmp/watchlist_diff_report.json")
UNIVERSE_HYGIENE_REPORT_FILE = os.environ.get("UNIVERSE_HYGIENE_REPORT_FILE", "/tmp/universe_hygiene_report.json")
JUDGMENT_AUDIT_FILE = os.environ.get("HERMES_JUDGMENT_AUDIT_FILE", "/tmp/hermes_judgment_audit_report.json")
POSITION_JUDGMENT_AUDIT_FILE = os.environ.get(
    "HERMES_POSITION_JUDGMENT_AUDIT_FILE",
    "/tmp/hermes_position_judgment_audit_report.json",
)
DEFAULT_REVIEW_LIMIT = int(os.environ.get("HERMES_REVIEW_LIMIT", "20"))
DEFAULT_QUEUE_SCAN_LIMIT = int(os.environ.get("HERMES_QUEUE_SCAN_LIMIT", "500"))
MAX_READINESS_REPORT_AGE_HOURS = int(
    os.environ.get(
        "HERMES_MAX_READINESS_REPORT_AGE_HOURS",
        os.environ.get("RT_ORDER_MAX_READINESS_REPORT_AGE_HOURS", "2"),
    )
)
INTRADAY_ALIGNMENT_ALIASES = {
    "conflicting_intraday_context": "conflicting_timeframes",
    "insufficient_intraday_context": "neutral_or_insufficient",
    "missing_minute_rows_before_signal": "unavailable_or_stale",
    "missing_signal_timestamp_or_symbol": "unavailable_or_stale",
    "missing_intraday_signal_context": "unavailable_or_stale",
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def parse_timestamp(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


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


def safe_file_stem(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))[:120]


def empty_prune_result():
    return {"deleted_count": 0, "deleted_bytes": 0, "kept_count": 0, "kept_bytes": 0, "errors": []}


def prune_packet_archive(
    archive_dir=PACKET_ARCHIVE_DIR,
    max_files=PACKET_ARCHIVE_MAX_FILES,
    max_age_hours=PACKET_ARCHIVE_MAX_AGE_HOURS,
    max_bytes=PACKET_ARCHIVE_MAX_BYTES,
    now=None,
):
    if not archive_dir:
        return empty_prune_result()
    try:
        max_files = int(max_files)
    except (TypeError, ValueError):
        max_files = PACKET_ARCHIVE_MAX_FILES
    try:
        max_age_hours = float(max_age_hours)
    except (TypeError, ValueError):
        max_age_hours = PACKET_ARCHIVE_MAX_AGE_HOURS
    try:
        max_bytes = int(max_bytes)
    except (TypeError, ValueError):
        max_bytes = PACKET_ARCHIVE_MAX_BYTES
    if max_files <= 0 and max_age_hours <= 0 and max_bytes <= 0:
        return empty_prune_result()
    archive_root = os.path.realpath(archive_dir)
    if not os.path.isdir(archive_root):
        return empty_prune_result()
    entries = []
    errors = []
    for name in os.listdir(archive_root):
        if not name.endswith(".json"):
            continue
        path = os.path.realpath(os.path.join(archive_root, name))
        if not path.startswith(archive_root + os.sep):
            continue
        try:
            stat = os.stat(path)
        except OSError as exc:
            errors.append(f"stat_failed:{name}:{exc}")
            continue
        if not os.path.isfile(path):
            continue
        entries.append({"path": path, "mtime": stat.st_mtime, "size": stat.st_size})
    entries = sorted(entries, key=lambda item: item["mtime"], reverse=True)
    now_ts = (now or datetime.now()).timestamp()
    cutoff = now_ts - max_age_hours * 3600 if max_age_hours > 0 else None
    deleted = 0
    deleted_bytes = 0
    kept = 0
    kept_bytes = 0
    for index, item in enumerate(entries):
        too_many = max_files > 0 and index >= max_files
        too_old = cutoff is not None and item["mtime"] < cutoff
        too_large = max_bytes > 0 and kept > 0 and kept_bytes + item["size"] > max_bytes
        if too_many or too_old or too_large:
            try:
                os.remove(item["path"])
                deleted += 1
                deleted_bytes += item["size"]
            except OSError as exc:
                errors.append(f"delete_failed:{os.path.basename(item['path'])}:{exc}")
        else:
            kept += 1
            kept_bytes += item["size"]
    return {
        "deleted_count": deleted,
        "deleted_bytes": deleted_bytes,
        "kept_count": kept,
        "kept_bytes": kept_bytes,
        "errors": errors[:20],
    }


def archive_packet(packet, archive_dir=PACKET_ARCHIVE_DIR):
    if not archive_dir:
        return ""
    packet_id = safe_file_stem(packet.get("packet_id"))
    if not packet_id:
        return ""
    os.makedirs(archive_dir, exist_ok=True)
    prune_packet_archive(archive_dir)
    path = os.path.join(archive_dir, f"{packet_id}.json")
    save_json_atomic(path, packet)
    prune_packet_archive(archive_dir)
    return path


def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except Exception as exc:
        return {"status": "missing", "path": path, "error": str(exc)}
    return {"status": "invalid", "path": path}


def packet_id_for(alerts, health_payload):
    signal_ids = [intake.signal_id(alert) for alert in alerts]
    seed = {
        "signal_ids": signal_ids,
        "health_checked_at": health_payload.get("checked_at"),
        "health_status": health_payload.get("status"),
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def is_directional(alert):
    return str(alert.get("signal_type", "")).upper() in ("BUY", "SELL")


def infer_current_sample_scope(alerts, sample_scope_mode="current"):
    if sample_scope_mode == "all":
        return {
            "mode": "all_scanned_alerts",
            "strategy_config_id": None,
            "watchlist_id": None,
            "latest_signal_id": None,
        }
    for alert in reversed(alerts):
        if not is_directional(alert):
            continue
        strategy_config_id = alert.get("strategy_config_id")
        watchlist_id = alert.get("watchlist_id")
        if strategy_config_id and watchlist_id:
            return {
                "mode": "latest_strategy_config_and_watchlist",
                "strategy_config_id": str(strategy_config_id),
                "watchlist_id": str(watchlist_id),
                "latest_signal_id": intake.signal_id(alert),
            }
    return {
        "mode": "all_scanned_alerts",
        "strategy_config_id": None,
        "watchlist_id": None,
        "latest_signal_id": None,
    }


def alert_matches_scope(alert, scope):
    if (scope or {}).get("mode") != "latest_strategy_config_and_watchlist":
        return True
    return (
        str(alert.get("strategy_config_id") or "") == scope.get("strategy_config_id")
        and str(alert.get("watchlist_id") or "") == scope.get("watchlist_id")
    )


def apply_sample_scope(alerts, sample_scope_mode="current"):
    scope = infer_current_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    scoped = [alert for alert in alerts if alert_matches_scope(alert, scope)]
    all_directional = [alert for alert in alerts if is_directional(alert)]
    scoped_directional = [alert for alert in scoped if is_directional(alert)]
    scope.update(
        {
            "raw_alert_count_before_filter": len(alerts),
            "raw_alert_count": len(scoped),
            "excluded_alert_count": len(alerts) - len(scoped),
            "directional_alert_count_before_filter": len(all_directional),
            "directional_alert_count": len(scoped_directional),
            "excluded_directional_alert_count": len(all_directional) - len(scoped_directional),
        }
    )
    return scoped, scope


def alert_selection_stats(source_alerts, review_alerts, sample_scope=None):
    by_type = {}
    directional = 0
    confirmed_directional = 0
    unconfirmed_directional = 0
    for alert in source_alerts:
        side = str(alert.get("signal_type", "UNKNOWN")).upper() or "UNKNOWN"
        by_type[side] = by_type.get(side, 0) + 1
        if side in ("BUY", "SELL"):
            directional += 1
            if alert.get("confirmed") is True:
                confirmed_directional += 1
            else:
                unconfirmed_directional += 1
    return {
        "source_alert_count": len(source_alerts),
        "review_alert_count": len(review_alerts),
        "directional_count": directional,
        "confirmed_directional_count": confirmed_directional,
        "unconfirmed_directional_count": unconfirmed_directional,
        "directional_not_selected_count": max(directional - len(review_alerts), 0),
        "by_signal_type": by_type,
        "review_signal_ids": [intake.signal_id(alert) for alert in review_alerts],
        "sample_scope": sample_scope or infer_current_sample_scope(source_alerts),
    }


def select_review_alerts(
    alerts,
    limit=DEFAULT_REVIEW_LIMIT,
    include_watch=False,
    include_unconfirmed=False,
    sample_scope_mode="current",
):
    scoped_alerts, _scope = apply_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    if include_watch:
        candidates = scoped_alerts
    else:
        candidates = [
            alert
            for alert in scoped_alerts
            if is_directional(alert) and (include_unconfirmed or alert.get("confirmed") is True)
        ]
    return candidates[-limit:] if limit and limit > 0 else candidates


def load_source_alerts(alert_json=None, alert_file=None, queue_file=None, scan_limit=DEFAULT_QUEUE_SCAN_LIMIT):
    args = type(
        "Args",
        (),
        {
            "alert_json": alert_json,
            "alert_file": alert_file,
            "queue_file": queue_file,
            "limit": scan_limit,
        },
    )()
    return intake.load_alerts_from_args(args)


def load_alerts(
    alert_json=None,
    alert_file=None,
    queue_file=None,
    limit=DEFAULT_REVIEW_LIMIT,
    scan_limit=DEFAULT_QUEUE_SCAN_LIMIT,
    include_watch=False,
    include_unconfirmed=False,
    sample_scope_mode="current",
):
    source_alerts = load_source_alerts(alert_json, alert_file, queue_file, scan_limit)
    return select_review_alerts(
        source_alerts,
        limit=limit,
        include_watch=include_watch,
        include_unconfirmed=include_unconfirmed,
        sample_scope_mode=sample_scope_mode,
    )


def run_intake_dry_runs(alerts, state_file, judgment_file):
    state = intake.load_state(state_file)
    results = []
    for alert in alerts:
        results.append(intake.process_alert(alert, "dry-run", state, state_file, judgment_file))
    return results


def alert_summary(alert):
    return {
        "signal_id": intake.signal_id(alert),
        "source": alert.get("source"),
        "symbol": alert.get("symbol"),
        "market": alert.get("market"),
        "signal_type": alert.get("signal_type"),
        "trigger": alert.get("trigger"),
        "confirmed": alert.get("confirmed"),
        "full_score": alert.get("full_score"),
        "rr_ratio": alert.get("rr_ratio"),
        "entry_price": alert.get("entry_price"),
        "stop_loss": alert.get("stop_loss"),
        "take_profit": alert.get("take_profit"),
        "generated_at": alert.get("generated_at"),
        "quote_time": alert.get("quote_time") or alert.get("time"),
    }


def review_item(alert, result, health_status):
    reasons = []
    if health_status == "FAIL":
        reasons.append("system_health_fail")
    if result.get("status") != "dry_run":
        reasons.append(f"intake_status_{result.get('status', 'unknown')}")
    if not result.get("plan"):
        reasons.append("no_order_plan")
    reasons.extend(result.get("reasons") or [])
    strategy_gate = result.get("strategy_evidence") or {}
    if strategy_gate.get("would_block_execute") or strategy_gate.get("status") == "REJECTED":
        reasons.append("strategy_evidence_would_block_execute")
        for reason in strategy_gate.get("reasons") or []:
            reasons.append(f"strategy_evidence:{reason}")
    conflict_gate = result.get("symbol_conflict") or {}
    if conflict_gate.get("would_block_execute") or conflict_gate.get("status") == "REJECTED":
        reasons.append("symbol_conflict_would_block_execute")
        for reason in conflict_gate.get("reasons") or []:
            reasons.append(f"symbol_conflict:{reason}")
    readiness_gate = result.get("execution_readiness") or {}
    if readiness_gate.get("would_block_execute") or readiness_gate.get("status") == "REJECTED":
        reasons.append("execution_readiness_would_block_execute")
        for reason in readiness_gate.get("reasons") or []:
            reasons.append(f"execution_readiness:{reason}")

    eligible = not reasons
    return {
        "signal_id": result.get("signal_id") or intake.signal_id(alert),
        "eligible_for_approval": eligible,
        "recommended_judgment": "approve_or_reduce_allowed_after_llm_review" if eligible else "reject_or_hold",
        "blocking_reasons": reasons,
        "alert": alert_summary(alert),
        "intake": result,
    }


def is_non_actionable_observation(alert, result):
    side = str((alert or {}).get("signal_type", "")).upper()
    reasons = set((result or {}).get("reasons") or [])
    if (result or {}).get("status") != "rejected":
        return False
    if "alert_too_old" in reasons:
        return True
    return side == "SELL" and "sell_without_position" in reasons


def non_actionable_reason(alert, result):
    side = str((alert or {}).get("signal_type", "")).upper()
    reasons = set((result or {}).get("reasons") or [])
    if "alert_too_old" in reasons:
        return "alert_too_old"
    if side == "SELL" and "sell_without_position" in reasons:
        return "sell_without_position"
    return "non_actionable_rejected_alert"


def non_actionable_observation(alert, result):
    reason = non_actionable_reason(alert, result)
    return {
        "signal_id": result.get("signal_id") or intake.signal_id(alert),
        "reason": reason,
        "recommended_use": "observation_only_no_trade_judgment_required",
        "alert": alert_summary(alert),
        "intake": {
            "status": result.get("status"),
            "reasons": result.get("reasons") or [],
            "signal_id": result.get("signal_id") or intake.signal_id(alert),
            "plan": result.get("plan"),
        },
    }


def review_item_suppression_summary(alert_result_pairs, actionable_pairs, observation_pairs):
    selected_count = len(alert_result_pairs or [])
    review_item_count = len(actionable_pairs or [])
    observation_count = len(observation_pairs or [])
    reason_counter = {}
    examples = []
    for alert, result in observation_pairs or []:
        reason = non_actionable_reason(alert, result)
        reason_counter[reason] = reason_counter.get(reason, 0) + 1
        if len(examples) < 20:
            examples.append(
                {
                    "signal_id": result.get("signal_id") or intake.signal_id(alert),
                    "symbol": (alert or {}).get("symbol"),
                    "signal_type": (alert or {}).get("signal_type"),
                    "trigger": (alert or {}).get("trigger"),
                    "reason": reason,
                    "generated_at": (alert or {}).get("generated_at"),
                    "quote_time": (alert or {}).get("quote_time") or (alert or {}).get("time"),
                }
            )
    if selected_count == 0:
        status = "NO_SELECTED_ALERTS"
    elif review_item_count == 0 and observation_count == selected_count:
        status = "ALL_SELECTED_ALERTS_SUPPRESSED"
    elif observation_count:
        status = "PARTIAL_SUPPRESSION"
    else:
        status = "HAS_REVIEW_ITEMS"

    recommendations = []
    if status == "NO_SELECTED_ALERTS":
        recommendations.append("check_alert_queue_for_confirmed_directional_current_scope_signals")
    if reason_counter.get("alert_too_old"):
        recommendations.append("wait_for_fresh_confirmed_alerts_or_run_packet_during_market_session")
    if reason_counter.get("sell_without_position"):
        recommendations.append("treat_sell_without_position_as_position_observation_only")
    if status == "HAS_REVIEW_ITEMS":
        recommendations.append("continue_hermes_review_for_review_items")
    if not recommendations:
        recommendations.append("inspect_non_actionable_observations_before_expecting_trade_judgments")

    return {
        "schema": "hermes_review_item_suppression_summary_v1",
        "read_only": True,
        "submits_orders": False,
        "status": status,
        "selected_alert_count": selected_count,
        "review_item_count": review_item_count,
        "non_actionable_observation_count": observation_count,
        "reason_counts": [
            {"key": key, "count": count}
            for key, count in sorted(reason_counter.items(), key=lambda item: (-item[1], item[0]))
        ],
        "examples": examples,
        "recommendations": recommendations,
    }


def normalize_market(value):
    text = str(value or "").strip().upper()
    aliases = {
        "HKEX": "HK",
        "HKG": "HK",
        "HONGKONG": "HK",
        "NYSE": "US",
        "NASDAQ": "US",
        "AMEX": "US",
        "USA": "US",
    }
    return aliases.get(text, text)


def normalize_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = [value]
    normalized = []
    for raw in raw_items:
        text = str(raw or "").strip()
        if text:
            normalized.append(text)
    return normalized


def symbol_tokens(value, market=None):
    text = str(value or "").strip().upper()
    if not text:
        return set()
    tokens = {text}
    base = text
    for suffix in (".HK", ".US", ".NYSE", ".NASDAQ", ".AMEX"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            tokens.add(base)
            break
    if base.isdigit():
        stripped = base.lstrip("0") or "0"
        tokens.add(stripped)
        tokens.add(base.zfill(4))
        tokens.add(base.zfill(5))
        if normalize_market(market) == "HK":
            tokens.add(f"{base.zfill(4)}.HK")
            tokens.add(f"{base.zfill(5)}.HK")
    return {token for token in tokens if token}


def list_symbol_tokens(values, market=None):
    tokens = set()
    for value in normalize_list(values):
        tokens.update(symbol_tokens(value, market=market))
    return tokens


def item_symbol_tokens(item, market=None):
    values = []
    for key in ("symbols", "symbol", "tickers", "ticker", "matched_symbols"):
        values.extend(normalize_list((item or {}).get(key)))
    return list_symbol_tokens(values, market=market)


def item_markets(item):
    values = []
    for key in ("markets", "market", "matched_markets"):
        values.extend(normalize_list((item or {}).get(key)))
    return {normalize_market(value) for value in values if normalize_market(value)}


def is_global_context(item):
    markets = item_markets(item)
    return not markets or "GLOBAL" in markets or "ALL" in markets


def context_relevance(alert, item, symbol_key="symbols", market_key="markets", allow_global=False):
    symbol = (alert or {}).get("symbol")
    market = normalize_market((alert or {}).get("market"))
    alert_symbols = symbol_tokens(symbol, market=market)
    item_symbols = item_symbol_tokens(item, market=market)
    markets = item_markets(item)
    if alert_symbols and item_symbols and alert_symbols & item_symbols:
        return "symbol"
    if market and market in markets:
        return "market"
    category = str((item or {}).get("category") or "").lower()
    if allow_global and (is_global_context(item) or category in ("macro", "capital_flow", "sentiment")):
        return "global"
    if symbol_key and market_key:
        return None
    return None


def context_sort_key(item):
    stale_rank = 1 if (item or {}).get("stale") is True else 0
    sentiment = str((item or {}).get("sentiment") or "").lower()
    direction = str((item or {}).get("direction") or "").lower()
    review_type = str((item or {}).get("review_signal_type") or "").upper()
    risk_rank = 0 if sentiment == "negative" or direction.startswith("negative") or "CHALLENGE" in review_type else 1
    try:
        impact = float((item or {}).get("impact_score") or 0.0)
    except Exception:
        impact = 0.0
    age = (item or {}).get("age_minutes")
    try:
        age_rank = float(age)
    except Exception:
        age_rank = 10**9
    return (stale_rank, risk_rank, -impact, age_rank, str((item or {}).get("published_at") or ""))


def numeric_value(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def bool_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y"):
        return True
    if text in ("0", "false", "no", "n"):
        return False
    return default


def compact_context_item(item, relevance=None, extra_keys=None):
    keys = [
        "id",
        "signal_id",
        "event_catalyst_id",
        "review_signal_type",
        "priority",
        "scope",
        "category",
        "source",
        "provider",
        "producer",
        "title",
        "name",
        "sentiment",
        "direction",
        "impact_score",
        "score",
        "published_at",
        "observed_at",
        "age_minutes",
        "stale",
        "markets",
        "matched_markets",
        "symbols",
        "matched_symbols",
        "related_v5_signal_ids",
        "url",
    ]
    for key in extra_keys or []:
        if key not in keys:
            keys.append(key)
    compact = {"relevance": relevance} if relevance else {}
    for key in keys:
        if key in (item or {}) and (item or {}).get(key) not in (None, "", [], {}):
            compact[key] = (item or {}).get(key)
    summary = str((item or {}).get("summary") or "").strip()
    if summary:
        compact["summary"] = summary[:240]
    return compact


def relevant_external_context(alert, payload, limit=5):
    items = (payload or {}).get("items")
    rows = []
    for item in items if isinstance(items, list) else []:
        relevance = context_relevance(alert, item, allow_global=True)
        if relevance:
            rows.append(compact_context_item(item, relevance=relevance))
    return sorted(rows, key=context_sort_key)[:limit]


def relevant_event_catalysts(alert, payload, limit=5):
    candidates = (payload or {}).get("candidates")
    rows = []
    for item in candidates if isinstance(candidates, list) else []:
        relevance = context_relevance(alert, item, allow_global=True)
        if relevance:
            rows.append(compact_context_item(item, relevance=relevance))
    return sorted(rows, key=context_sort_key)[:limit]


def relevant_event_catalyst_signals(alert, payload, limit=5):
    signals = (payload or {}).get("signals")
    signal_id = str((alert or {}).get("signal_id") or "")
    rows = []
    for item in signals if isinstance(signals, list) else []:
        related_ids = {str(value) for value in ((item or {}).get("related_v5_signal_ids") or [])}
        if signal_id and signal_id in related_ids:
            relevance = "direct_signal"
        else:
            relevance = context_relevance(alert, item, allow_global=False)
        if relevance:
            rows.append(compact_context_item(item, relevance=relevance))
    return sorted(rows, key=context_sort_key)[:limit]


def relevant_market_sentiment(alert, payload, limit=5):
    indicators = (payload or {}).get("indicators")
    rows = []
    for item in indicators if isinstance(indicators, list) else []:
        relevance = context_relevance(alert, item, allow_global=True)
        if relevance:
            rows.append(compact_context_item(item, relevance=relevance))
    return sorted(rows, key=context_sort_key)[:limit]


def relevant_fundamentals(alert, payload, limit=3):
    items = (payload or {}).get("items")
    rows = []
    for item in items if isinstance(items, list) else []:
        relevance = context_relevance(alert, item, allow_global=False)
        if relevance == "symbol":
            rows.append(
                compact_context_item(
                    item,
                    relevance=relevance,
                    extra_keys=[
                        "symbol",
                        "market",
                        "as_of",
                        "age_days",
                        "currency",
                        "market_cap",
                        "pe_ttm",
                        "pb",
                        "ps",
                        "roe_pct",
                        "revenue_growth_pct",
                        "earnings_growth_pct",
                        "dividend_yield_pct",
                        "debt_to_equity",
                        "valuation_flags",
                        "fundamental_completeness",
                    ],
                )
            )
    return sorted(rows, key=context_sort_key)[:limit]


def source_limit_summary(trusted_source_preflight_payload, source_reliability_payload):
    preflight = trusted_source_preflight_payload if isinstance(trusted_source_preflight_payload, dict) else {}
    reliability = source_reliability_payload if isinstance(source_reliability_payload, dict) else {}
    preflight_components = [
        {
            "name": component.get("name"),
            "status": component.get("status"),
            "reasons": component.get("reasons") or component.get("warnings") or [],
        }
        for component in preflight.get("components") or []
        if str(component.get("status") or "").upper() in ("WARN", "FAIL", "MISSING", "STALE")
    ]
    reliability_components = []
    for component in reliability.get("components") or []:
        if str(component.get("reliability_status") or component.get("status") or "").upper() not in (
            "DEGRADED",
            "STALE",
            "MISSING",
            "FAIL",
            "WARN",
        ):
            continue
        row = {
            "name": component.get("name"),
            "reliability_status": component.get("reliability_status") or component.get("status"),
            "reasons": component.get("reasons") or [],
        }
        if component.get("name") == "market_context" and component.get("native_index_context"):
            row["native_index_context"] = component.get("native_index_context")
        if component.get("name") == "rt_signal_outcome":
            coverage = component.get("coverage") if isinstance(component.get("coverage"), dict) else {}
            row["intraday_path_fidelity"] = {
                "ambiguous_daily_count": coverage.get("ambiguous_daily_count"),
                "resolved_count": coverage.get("resolved_count"),
                "missing_count": coverage.get("missing_count"),
                "same_minute_ambiguous_count": coverage.get("same_minute_ambiguous_count"),
                "unresolved_count": coverage.get("unresolved_count"),
                "low_fidelity_count": coverage.get("low_fidelity_count"),
                "effective_unresolved_first_hit_rate_pct": coverage.get("effective_unresolved_first_hit_rate_pct"),
            }
        reliability_components.append(row)
    return {
        "trusted_source_preflight_status": preflight.get("status"),
        "source_reliability_status": reliability.get("status"),
        "trusted_source_problem_components": preflight_components[:8],
        "source_reliability_problem_components": reliability_components[:8],
        "trusted_source_recommendations": (preflight.get("recommendations") or [])[:8],
        "source_reliability_recommendations": (reliability.get("recommendations") or [])[:8],
    }


def fundamentals_limit_required(fundamental_items):
    for item in fundamental_items or []:
        flags = set(item.get("valuation_flags") or [])
        completeness = item.get("fundamental_completeness") or {}
        source = str(item.get("source") or "").lower()
        if item.get("stale") is True:
            return True
        if "partial_fundamentals" in flags:
            return True
        if completeness.get("level") in ("partial", "empty"):
            return True
        if source in ("tencent_quote_snapshot", "yahoo_quote_snapshot"):
            return True
    return False


def fundamentals_support_available(fundamental_items):
    for item in fundamental_items or []:
        flags = set(item.get("valuation_flags") or [])
        completeness = item.get("fundamental_completeness") or {}
        if item.get("stale") is not True and completeness.get("level") == "full" and not flags:
            return True
    return False


def market_context_for_alert(alert, payload):
    market = normalize_market((alert or {}).get("market"))
    symbol = str((alert or {}).get("symbol") or "").strip()
    if market not in ("HK", "US"):
        market = "HK" if symbol[:1].isdigit() and len(symbol) == 5 else "US"
    report_status = (payload or {}).get("status")
    markets = (payload or {}).get("markets") if isinstance((payload or {}).get("markets"), dict) else {}
    summary = markets.get(market) if isinstance(markets.get(market), dict) else {}
    if not summary:
        return {
            "schema": "hermes_review_item_market_context_digest_v1",
            "status": "MISSING",
            "report_status": report_status,
            "market": market,
            "regime": None,
            "risk_level": None,
            "notes": ["market_context_missing_for_signal_market"],
            "breadth": {},
            "native_index_context": {},
            "cross_market": {},
        }
    native = summary.get("native_index_context") if isinstance(summary.get("native_index_context"), dict) else {}
    primary_index = native.get("primary_index") if isinstance(native.get("primary_index"), dict) else {}
    compact_native = {
        "status": native.get("status"),
        "source": native.get("source"),
        "breadth_regime": native.get("breadth_regime"),
        "index_direction": native.get("index_direction"),
        "alignment": native.get("alignment"),
        "latest_lag_days_vs_stock_pool": native.get("latest_lag_days_vs_stock_pool"),
        "available_index_count": native.get("available_index_count"),
        "primary_index": {
            key: primary_index.get(key)
            for key in (
                "symbol",
                "name",
                "latest_date",
                "history_days",
                "latest_close",
                "above_ma20",
                "above_ma50",
                "return_1d_pct",
                "return_5d_pct",
                "return_20d_pct",
                "volatility_20d_pct",
                "source_table",
                "source",
                "provider_grade",
            )
            if primary_index.get(key) not in (None, "", [], {})
        },
        "notes": native.get("notes") or [],
    }
    cross = summary.get("cross_market") if isinstance(summary.get("cross_market"), dict) else {}
    compact_cross = {
        key: cross.get(key)
        for key in (
            "status",
            "source",
            "sentiment_status",
            "breadth_regime",
            "sentiment_direction",
            "sentiment_score",
            "alignment",
            "notes",
        )
        if cross.get(key) not in (None, "", [], {})
    }
    return {
        "schema": "hermes_review_item_market_context_digest_v1",
        "status": str(report_status or "OK").upper(),
        "report_status": report_status,
        "market": market,
        "latest_date": summary.get("latest_date"),
        "regime": summary.get("regime"),
        "risk_level": summary.get("risk_level"),
        "notes": summary.get("notes") or [],
        "breadth": summary.get("breadth") or {},
        "returns": summary.get("returns") or {},
        "native_index_context": compact_native,
        "cross_market": compact_cross,
    }


def intraday_context_for_alert(alert, payload):
    market = normalize_market((alert or {}).get("market"))
    symbol_set = symbol_tokens((alert or {}).get("symbol"), market=market)
    report_status = (payload or {}).get("status")
    granularity_policy = (payload or {}).get("granularity_policy") if isinstance((payload or {}).get("granularity_policy"), dict) else {}
    markets = (payload or {}).get("markets") if isinstance((payload or {}).get("markets"), dict) else {}
    market_payload = markets.get(market) if isinstance(markets.get(market), dict) else {}
    if not market_payload:
        return {
            "schema": "hermes_review_item_intraday_context_digest_v1",
            "status": "MISSING",
            "report_status": report_status,
            "market": market,
            "symbol": (alert or {}).get("symbol"),
            "granularity_policy": granularity_policy,
            "notes": ["intraday_context_missing_for_signal_market"],
        }
    matched = None
    for item in market_payload.get("symbols") or []:
        item_tokens = symbol_tokens(item.get("symbol"), market=market)
        if symbol_set and item_tokens and symbol_set & item_tokens:
            matched = item
            break
    if not matched:
        return {
            "schema": "hermes_review_item_intraday_context_digest_v1",
            "status": "MISSING",
            "report_status": report_status,
            "market": market,
            "symbol": (alert or {}).get("symbol"),
            "market_latest_timestamp": market_payload.get("latest_timestamp"),
            "market_breadth": market_payload.get("breadth") or {},
            "granularity_policy": granularity_policy,
            "notes": ["intraday_context_missing_for_signal_symbol"],
        }
    keys = [
        "symbol",
        "market",
        "status",
        "point_count",
        "latest_timestamp",
        "latest_age_minutes",
        "latest_price",
        "session",
        "latest_5m",
        "latest_15m",
        "latest_30m",
        "latest_60m",
        "multi_timeframe_confirmation",
        "rolling_windows",
        "data_sources",
        "source_granularities",
        "quality",
        "warnings",
        "hermes_notes",
    ]
    compact = {key: matched.get(key) for key in keys if matched.get(key) not in (None, "", [], {})}
    compact.update(
        {
            "schema": "hermes_review_item_intraday_context_digest_v1",
            "report_status": report_status,
            "market_session": matched.get("market_session") or market_payload.get("market_session") or {},
            "market_breadth": market_payload.get("breadth") or {},
            "granularity_policy": granularity_policy,
        }
    )
    return compact


def intraday_market_session_overrides_for_alert(alert, payload):
    market = normalize_market((alert or {}).get("market"))
    if not isinstance(payload, dict) or not payload:
        return {
            "schema": "hermes_review_item_intraday_market_session_overrides_digest_v1",
            "status": "MISSING",
            "report_status": "MISSING",
            "market": market,
            "warnings": ["intraday_market_session_overrides_report_missing"],
            "recommendations": ["review_intraday_market_session_override_coverage_for_holidays_and_half_days"],
        }

    report_status = str(payload.get("status") or "MISSING").upper()
    markets = payload.get("markets") if isinstance(payload.get("markets"), dict) else {}
    market_payload = markets.get(market) if isinstance(markets.get(market), dict) else {}
    market_status = str(market_payload.get("status") or "").upper()
    effective_status = market_status or report_status
    if report_status == "FAIL":
        effective_status = "FAIL"
    elif report_status in ("WARN", "MISSING", "STALE", "INVALID") and effective_status == "OK":
        effective_status = "WARN"
    if not effective_status:
        effective_status = "MISSING"

    warnings = []
    warnings.extend(str(value) for value in payload.get("warnings") or [] if str(value).strip())
    warnings.extend(str(value) for value in market_payload.get("warnings") or [] if str(value).strip())
    errors = []
    errors.extend(str(value) for value in payload.get("errors") or [] if str(value).strip())
    errors.extend(str(value) for value in market_payload.get("errors") or [] if str(value).strip())
    return {
        "schema": "hermes_review_item_intraday_market_session_overrides_digest_v1",
        "status": effective_status,
        "report_status": report_status,
        "market": market,
        "market_status": market_status or None,
        "coverage_until": market_payload.get("coverage_until"),
        "future_entry_count": market_payload.get("future_entry_count"),
        "warnings": warnings[:8],
        "errors": errors[:8],
        "recommendations": (payload.get("recommendations") or [])[:8],
    }


def intraday_minute_producer_summary(payload):
    if not isinstance(payload, dict) or not payload:
        return {
            "schema": "hermes_review_item_intraday_minute_producer_digest_v1",
            "status": "MISSING",
            "notes": ["intraday_minute_producer_report_missing"],
        }
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    apply_contract = payload.get("apply_contract") if isinstance(payload.get("apply_contract"), dict) else {}
    status = str(payload.get("status") or "MISSING").upper()
    action_count = int(numeric_value(summary.get("action_count"), 0) or 0)
    unresolved_count = int(numeric_value(summary.get("unresolved_count"), 0) or 0)
    sparse_us_action_count = int(numeric_value(summary.get("sparse_us_action_count"), 0) or 0)
    invalid_source_row_count = int(numeric_value(summary.get("invalid_source_row_count"), 0) or 0)
    provider_contract = source.get("provider_contract")
    notes = []
    if status in ("ACTIONABLE", "PARTIAL") and action_count:
        notes.append("intraday_minute_apply_pending")
    if status in ("PARTIAL", "UNRESOLVED") or unresolved_count:
        notes.append("intraday_minute_unresolved_symbols")
    if provider_contract == "unofficial_public_web_endpoint_unversioned_best_effort":
        notes.append("intraday_minute_public_fallback_provider")
    if sparse_us_action_count:
        notes.append("intraday_minute_sparse_us_rows")
    if invalid_source_row_count:
        notes.append("intraday_minute_invalid_source_rows")
    if apply_contract.get("dry_run_default") is True:
        notes.append("intraday_minute_producer_dry_run_default")
    return {
        "schema": "hermes_review_item_intraday_minute_producer_digest_v1",
        "status": status,
        "mode": payload.get("mode"),
        "plan_hash": payload.get("plan_hash"),
        "provider": source.get("provider"),
        "provider_contract": provider_contract,
        "dry_run_default": source.get("dry_run_default") if source.get("dry_run_default") is not None else apply_contract.get("dry_run_default"),
        "manual_apply_command_available": bool(apply_contract.get("manual_apply_command")),
        "action_count": action_count,
        "planned_row_count": int(numeric_value(summary.get("planned_row_count"), 0) or 0),
        "unresolved_count": unresolved_count,
        "sparse_us_action_count": sparse_us_action_count,
        "invalid_source_row_count": invalid_source_row_count,
        "notes": notes,
    }


def intraday_timeframe_policy_summary(payload):
    if not isinstance(payload, dict) or not payload:
        return {
            "schema": "hermes_review_item_intraday_timeframe_policy_digest_v1",
            "status": "MISSING",
            "decision_policy_present": False,
            "confidence_use": "diagnostic_only",
            "may_raise_confidence": False,
            "requires_forward_evidence_before_confidence_raise": True,
            "can_override_daily_gates": False,
            "execution_permission": False,
            "allowed_effects": [],
            "reason_codes": ["intraday_timeframe_quality_missing"],
            "requires_judgment_acknowledgement": True,
        }

    policy = payload.get("decision_policy") if isinstance(payload.get("decision_policy"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    report_status = str(payload.get("status") or "MISSING").upper()
    if policy:
        reason_codes = normalize_list(policy.get("reason_codes"))
        confidence_use = str(policy.get("confidence_use") or "diagnostic_only")
        allowed_effects = normalize_list(policy.get("allowed_effects"))
        may_raise_confidence = bool_value(policy.get("may_raise_confidence"))
        requires_forward_evidence = bool_value(
            policy.get("requires_forward_evidence_before_confidence_raise"),
            default=True,
        )
        can_override_daily_gates = bool_value(policy.get("can_override_daily_gates"))
        execution_permission = bool_value(policy.get("execution_permission"))
        timeframe_roles = policy.get("timeframe_roles") if isinstance(policy.get("timeframe_roles"), dict) else {}
    else:
        reason_codes = ["intraday_timeframe_policy_missing"]
        confidence_use = "diagnostic_only"
        allowed_effects = []
        may_raise_confidence = False
        requires_forward_evidence = True
        can_override_daily_gates = False
        execution_permission = False
        timeframe_roles = {}
    if report_status not in ("", "OK") and f"intraday_timeframe_quality_{report_status.lower()}" not in reason_codes:
        reason_codes.append(f"intraday_timeframe_quality_{report_status.lower()}")
    requires_ack = (
        confidence_use in ("cap_or_challenge_only", "diagnostic_only")
        or bool(reason_codes)
        or may_raise_confidence
        or can_override_daily_gates
        or execution_permission
    )
    return {
        "schema": "hermes_review_item_intraday_timeframe_policy_digest_v1",
        "status": report_status,
        "decision_policy_present": bool(policy),
        "confidence_use": confidence_use,
        "may_raise_confidence": may_raise_confidence,
        "requires_forward_evidence_before_confidence_raise": requires_forward_evidence,
        "can_override_daily_gates": can_override_daily_gates,
        "execution_permission": execution_permission,
        "allowed_effects": allowed_effects,
        "timeframe_roles": timeframe_roles,
        "reason_codes": reason_codes[:12],
        "summary": {
            "symbol_count": summary.get("symbol_count"),
            "degraded_symbol_count": summary.get("degraded_symbol_count"),
            "limited_timeframe_symbol_count": summary.get("limited_timeframe_symbol_count"),
            "missing_timeframe_symbol_count": summary.get("missing_timeframe_symbol_count"),
            "conflict_symbol_count": summary.get("conflict_symbol_count"),
            "low_fidelity_symbol_count": summary.get("low_fidelity_symbol_count"),
            "snapshot_like_symbol_count": summary.get("snapshot_like_symbol_count"),
            "missing_source_granularity_symbol_count": summary.get("missing_source_granularity_symbol_count"),
        },
        "recommendations": normalize_list(payload.get("recommendations"))[:8],
        "requires_judgment_acknowledgement": requires_ack,
    }


def intraday_timeframe_decision_for_alert(alert, payload):
    market = normalize_market((alert or {}).get("market"))
    symbol_set = symbol_tokens((alert or {}).get("symbol"), market=market)
    report_status = str((payload or {}).get("status") or "MISSING").upper()
    markets = (payload or {}).get("markets") if isinstance((payload or {}).get("markets"), dict) else {}
    market_payload = markets.get(market) if isinstance(markets.get(market), dict) else {}
    base = {
        "schema": "hermes_review_item_intraday_timeframe_decision_v1",
        "report_status": report_status,
        "market": market,
        "symbol": (alert or {}).get("symbol"),
    }
    if not market_payload:
        return {
            **base,
            "matched": False,
            "status": "MISSING",
            "decision_use": "diagnostic_only",
            "allowed_effects": [],
            "reasons": ["intraday_timeframe_quality_missing_for_signal_market"],
        }
    matched = None
    for item in market_payload.get("symbols") or []:
        item_tokens = symbol_tokens(item.get("symbol"), market=item.get("market") or market)
        if symbol_set and item_tokens and symbol_set & item_tokens:
            matched = item
            break
    if not matched:
        return {
            **base,
            "matched": False,
            "status": "MISSING",
            "decision_use": "diagnostic_only",
            "allowed_effects": [],
            "market_status": market_payload.get("status"),
            "market_symbol_count": market_payload.get("symbol_count"),
            "reasons": ["intraday_timeframe_quality_missing_for_signal_symbol"],
        }
    quality = matched.get("quality") if isinstance(matched.get("quality"), dict) else {}
    return {
        **base,
        "matched": True,
        "status": matched.get("status"),
        "source_status": matched.get("source_status"),
        "decision_use": matched.get("decision_use") or "diagnostic_only",
        "allowed_effects": normalize_list(matched.get("allowed_effects")),
        "alignment": matched.get("alignment"),
        "dominant_direction": matched.get("dominant_direction"),
        "limited_timeframes": matched.get("limited_timeframes") or [],
        "missing_timeframes": matched.get("missing_timeframes") or [],
        "reasons": normalize_list(matched.get("reasons")),
        "quality": {
            "status": quality.get("status"),
            "valid_point_count": quality.get("valid_point_count"),
            "full_ohlc_row_count": quality.get("full_ohlc_row_count"),
            "low_fidelity_point_count": quality.get("low_fidelity_point_count"),
            "snapshot_like_row_count": quality.get("snapshot_like_row_count"),
            "missing_source_granularity_count": quality.get("missing_source_granularity_count"),
        },
    }


def intraday_signal_evidence(alert, intraday_context):
    context = intraday_context if isinstance(intraday_context, dict) else {}
    side = str((alert or {}).get("signal_type") or "").upper()
    status = str(context.get("status") or "MISSING").upper()
    notes = set(normalize_list(context.get("hermes_notes")) + normalize_list(context.get("notes")))
    mtf = context.get("multi_timeframe_confirmation") if isinstance(context.get("multi_timeframe_confirmation"), dict) else {}
    quality = context.get("quality") if isinstance(context.get("quality"), dict) else {}

    support_codes = []
    challenge_codes = []
    conflict_codes = []
    quality_codes = []
    limit_codes = []

    def add_unique(target, code):
        text = str(code or "").strip()
        if text and text not in target:
            target.append(text)

    if status in ("MISSING", "STALE"):
        add_unique(limit_codes, f"intraday_context_{status.lower()}")
    if status == "CLOSED":
        add_unique(limit_codes, "intraday_market_not_open_last_session_only")

    alignment = str(mtf.get("alignment") or "").strip()
    if side == "BUY":
        if mtf.get("buy_confirmation") is True:
            add_unique(support_codes, "multi_timeframe_supports_buy")
        if "intraday_session_up_supports_buy_review" in notes:
            add_unique(support_codes, "session_up_supports_buy")
        if "intraday_session_down_against_new_buy_review" in notes:
            add_unique(challenge_codes, "session_down_challenges_buy")
        if "intraday_multi_timeframe_bearish_challenges_buy_review" in notes:
            add_unique(challenge_codes, "multi_timeframe_bearish_challenges_buy")
    elif side == "SELL":
        if mtf.get("sell_confirmation") is True:
            add_unique(support_codes, "multi_timeframe_supports_sell")
        if "intraday_session_up_supports_buy_review" in notes:
            add_unique(challenge_codes, "session_up_challenges_sell")
        if "intraday_multi_timeframe_bullish_challenges_sell_review" in notes:
            add_unique(challenge_codes, "multi_timeframe_bullish_challenges_sell")

    for code in mtf.get("contradictions") or []:
        add_unique(conflict_codes, code)
    if "intraday_timeframes_conflicting_requires_disclosure" in notes:
        add_unique(conflict_codes, "intraday_timeframes_conflicting_requires_disclosure")
    for code in (
        "intraday_5m_window_coverage_limited_requires_disclosure",
        "intraday_15m_window_coverage_limited_requires_disclosure",
        "intraday_30m_window_coverage_limited_requires_disclosure",
        "intraday_60m_window_coverage_limited_requires_disclosure",
    ):
        if code in notes:
            add_unique(limit_codes, code)

    quality_status = str(quality.get("status") or "").upper()
    if quality_status and quality_status != "OK":
        add_unique(quality_codes, f"intraday_quality_{quality_status.lower()}")
    for code in quality.get("notes") or []:
        add_unique(quality_codes, code)
    if "intraday_context_quality_degraded_requires_disclosure" in notes:
        add_unique(quality_codes, "intraday_context_quality_degraded_requires_disclosure")

    if challenge_codes:
        evidence_alignment = "challenges_signal"
    elif conflict_codes:
        evidence_alignment = "conflicting_timeframes"
    elif support_codes and (quality_codes or limit_codes):
        evidence_alignment = "supports_with_limits"
    elif support_codes:
        evidence_alignment = "supports_signal"
    elif status in ("MISSING", "STALE"):
        evidence_alignment = "unavailable_or_stale"
    elif quality_codes or limit_codes:
        evidence_alignment = "limited_context"
    else:
        evidence_alignment = "neutral_or_insufficient"

    codes = support_codes + challenge_codes + conflict_codes + quality_codes + limit_codes
    return {
        "schema": "hermes_review_item_intraday_signal_evidence_v1",
        "read_only": True,
        "submits_orders": False,
        "changes_strategy": False,
        "signal_type": side,
        "status": status,
        "alignment": evidence_alignment,
        "timeframe_alignment": alignment or None,
        "dominant_direction": mtf.get("dominant_direction"),
        "support_codes": support_codes,
        "challenge_codes": challenge_codes,
        "conflict_codes": conflict_codes,
        "quality_codes": quality_codes,
        "limit_codes": limit_codes,
        "codes": codes,
        "requires_judgment_acknowledgement": evidence_alignment != "neutral_or_insufficient",
        "instruction": (
            "Use 60m/30m/15m/5m as intraday confirmation, contradiction, timing, or quality evidence only; "
            "use 1m mainly for execution/path/postmortem diagnostics. Daily K-lines remain the forward-return authority."
        ),
    }


def daily_gap_source_for_alert(alert, payload):
    market = normalize_market((alert or {}).get("market"))
    symbol_set = symbol_tokens((alert or {}).get("symbol"), market=market)
    report_status = (payload or {}).get("status")
    if not isinstance(payload, dict) or not payload:
        return {
            "schema": "hermes_review_item_daily_gap_source_digest_v1",
            "status": "MISSING",
            "report_status": report_status,
            "market": market,
            "symbol": (alert or {}).get("symbol"),
            "matched": False,
            "notes": ["kline_gap_source_diagnostic_report_missing"],
        }
    matched = None
    for item in payload.get("classifications") or []:
        item_tokens = symbol_tokens(item.get("symbol"), market=item.get("market") or market)
        if symbol_set and item_tokens and symbol_set & item_tokens:
            matched = item
            break
    if not matched:
        return {
            "schema": "hermes_review_item_daily_gap_source_digest_v1",
            "status": "OK",
            "report_status": report_status,
            "market": market,
            "symbol": (alert or {}).get("symbol"),
            "matched": False,
            "summary": (payload or {}).get("summary") or {},
            "notes": ["signal_symbol_not_in_unresolved_daily_gap_classifications"],
        }
    exposure = matched.get("exposure") if isinstance(matched.get("exposure"), dict) else {}
    notes = ["signal_symbol_has_unresolved_daily_gap_source_issue"]
    if exposure.get("in_current_v5_watchlist"):
        notes.append("unresolved_daily_gap_symbol_is_current_v5_watchlist_member")
    if exposure.get("has_open_position"):
        notes.append("unresolved_daily_gap_symbol_has_open_position")
    return {
        "schema": "hermes_review_item_daily_gap_source_digest_v1",
        "status": "ACTION_REQUIRED",
        "report_status": report_status,
        "market": matched.get("market") or market,
        "symbol": matched.get("symbol") or (alert or {}).get("symbol"),
        "matched": True,
        "category": matched.get("category"),
        "confidence": matched.get("confidence"),
        "recommended_action": matched.get("recommended_action"),
        "reason": matched.get("reason"),
        "latest_daily_date": matched.get("latest_daily_date"),
        "target_end_date": matched.get("target_end_date"),
        "latest_source_date": matched.get("latest_source_date"),
        "source_lag_days_vs_target": matched.get("source_lag_days_vs_target"),
        "daily_lag_days_vs_target": matched.get("daily_lag_days_vs_target"),
        "hygiene": matched.get("hygiene") or {},
        "exposure": exposure,
        "notes": notes,
    }


def context_attention_items(
    alert,
    market_context,
    intraday_context,
    intraday_timeframe_policy,
    intraday_market_session_overrides,
    intraday_minute_producer,
    daily_gap_source,
    external_market_context_payload,
    external_items,
    catalyst_items,
    event_catalyst_payload,
    event_catalyst_signal_payload,
    catalyst_signal_items,
    market_sentiment_payload,
    sentiment_items,
    fundamentals_context_payload,
    fundamental_items,
    source_limits,
):
    attention = []
    side = str((alert or {}).get("signal_type") or "").upper()
    market_notes = set((market_context or {}).get("notes") or [])
    native_context = (market_context or {}).get("native_index_context") or {}
    native_primary = native_context.get("primary_index") or {}
    cross_market = (market_context or {}).get("cross_market") or {}
    market_context_status = str((market_context or {}).get("status") or "MISSING").upper()
    if market_context_status != "OK":
        attention.append("market_context_missing_for_signal_market")
    if market_context_status in ("MISSING", "STALE", "FAIL", "INVALID"):
        attention.append("market_context_coverage_limit_requires_acknowledgement")
    if side == "BUY" and (market_context or {}).get("regime") == "risk_off":
        attention.append("risk_off_market_context_requires_exception_for_buy")
    if side == "BUY" and "buy_signals_against_weak_breadth" in market_notes:
        attention.append("buy_signal_against_weak_breadth_requires_explicit_review")
    if str(native_context.get("status") or "").upper() not in ("", "OK"):
        attention.append("native_index_context_incomplete_requires_disclosure")
    if native_context.get("alignment") == "conflicts_with_breadth":
        attention.append("native_index_conflicts_with_breadth_requires_discussion")
    if native_primary.get("provider_grade") == "public_fallback":
        attention.append("native_index_public_fallback_requires_source_limit_acknowledgement")
    if cross_market.get("alignment") == "conflicts_with_breadth":
        attention.append("cross_market_conflicts_with_breadth_requires_discussion")
    intraday_status = str((intraday_context or {}).get("status") or "").upper()
    intraday_notes = set((intraday_context or {}).get("hermes_notes") or (intraday_context or {}).get("notes") or [])
    if intraday_status in ("MISSING", "STALE"):
        attention.append("intraday_context_missing_or_stale_requires_disclosure")
    if side == "BUY" and "intraday_session_down_against_new_buy_review" in intraday_notes:
        attention.append("intraday_context_challenges_buy_requires_discussion")
    if side == "SELL" and "intraday_session_up_supports_buy_review" in intraday_notes:
        attention.append("intraday_context_challenges_sell_requires_discussion")
    if side == "BUY" and "intraday_multi_timeframe_bearish_challenges_buy_review" in intraday_notes:
        attention.append("intraday_context_challenges_buy_requires_discussion")
    if side == "SELL" and "intraday_multi_timeframe_bullish_challenges_sell_review" in intraday_notes:
        attention.append("intraday_context_challenges_sell_requires_discussion")
    if "intraday_timeframes_conflicting_requires_disclosure" in intraday_notes:
        attention.append("intraday_context_timeframe_conflict_requires_disclosure")
    if intraday_notes & {
        "intraday_5m_window_coverage_limited_requires_disclosure",
        "intraday_15m_window_coverage_limited_requires_disclosure",
        "intraday_30m_window_coverage_limited_requires_disclosure",
        "intraday_60m_window_coverage_limited_requires_disclosure",
    }:
        attention.append("intraday_timeframe_coverage_limited_requires_disclosure")
    if "intraday_context_quality_degraded_requires_disclosure" in intraday_notes:
        attention.append("intraday_context_quality_degraded_requires_disclosure")
    if "intraday_market_not_open_requires_session_context" in intraday_notes:
        attention.append("intraday_market_not_open_requires_session_context")
    timeframe_policy = intraday_timeframe_policy if isinstance(intraday_timeframe_policy, dict) else {}
    if timeframe_policy.get("requires_judgment_acknowledgement"):
        attention.append("intraday_timeframe_policy_requires_acknowledgement")
    if (
        timeframe_policy.get("may_raise_confidence")
        or timeframe_policy.get("can_override_daily_gates")
        or timeframe_policy.get("execution_permission")
    ):
        attention.append("intraday_timeframe_policy_safety_limit_requires_rejection")
    producer_status = str((intraday_minute_producer or {}).get("status") or "").upper()
    producer_notes = set((intraday_minute_producer or {}).get("notes") or [])
    if producer_status in ("MISSING", "STALE", "FAIL", "INVALID", "ACTIONABLE", "PARTIAL", "UNRESOLVED") or producer_notes & {
        "intraday_minute_apply_pending",
        "intraday_minute_unresolved_symbols",
        "intraday_minute_public_fallback_provider",
        "intraday_minute_sparse_us_rows",
        "intraday_minute_invalid_source_rows",
        "intraday_minute_producer_dry_run_default",
    }:
        attention.append("intraday_minute_producer_limit_requires_acknowledgement")
    calendar_status = str((intraday_market_session_overrides or {}).get("status") or "").upper()
    calendar_report_status = str((intraday_market_session_overrides or {}).get("report_status") or "").upper()
    if (
        calendar_status in ("WARN", "FAIL", "MISSING", "STALE", "INVALID")
        or calendar_report_status in ("WARN", "FAIL", "MISSING", "STALE", "INVALID")
        or (intraday_market_session_overrides or {}).get("warnings")
        or (intraday_market_session_overrides or {}).get("errors")
    ):
        attention.append("intraday_market_session_overrides_limit_requires_disclosure")
    if (daily_gap_source or {}).get("matched"):
        attention.append("signal_symbol_unresolved_daily_gap_requires_rejection_or_hold")
        exposure = (daily_gap_source or {}).get("exposure") or {}
        if exposure.get("in_current_v5_watchlist"):
            attention.append("signal_symbol_unresolved_daily_gap_watchlist_member_requires_review")
        if exposure.get("has_open_position"):
            attention.append("signal_symbol_unresolved_daily_gap_open_position_requires_position_review")
    external_status = str((external_market_context_payload or {}).get("status") or "MISSING").upper()
    if external_status in ("MISSING", "STALE", "RISK", "FAIL", "INVALID"):
        attention.append("external_market_context_coverage_limit_requires_acknowledgement")
    if not external_items:
        attention.append("external_market_context_no_relevant_item_found")
    if any(str(item.get("sentiment") or "").lower() == "negative" for item in catalyst_items):
        attention.append("negative_event_catalyst_requires_explicit_risk_note")
    event_catalyst_status = str((event_catalyst_payload or {}).get("status") or "MISSING").upper()
    if event_catalyst_status in ("MISSING", "STALE", "RISK", "FAIL", "INVALID"):
        attention.append("event_catalyst_coverage_limit_requires_acknowledgement")
    event_signal_status = str((event_catalyst_signal_payload or {}).get("status") or "MISSING").upper()
    if event_signal_status in ("MISSING", "STALE", "RISK", "FAIL", "INVALID"):
        attention.append("event_catalyst_signal_coverage_limit_requires_acknowledgement")
    if any(str(item.get("review_signal_type") or "").upper() == "CHALLENGE_BUY_REVIEW" for item in catalyst_signal_items):
        attention.append("event_catalyst_signal_challenges_buy_requires_acknowledgement")
    sentiment_status = str((market_sentiment_payload or {}).get("status") or "MISSING").upper()
    if sentiment_status in ("MISSING", "STALE", "RISK", "FAIL", "INVALID"):
        attention.append("market_sentiment_coverage_limit_requires_acknowledgement")
    if any(
        str(item.get("direction") or "").lower() in ("risk_off", "negative")
        or numeric_value(item.get("score"), default=0.0) < 0
        for item in sentiment_items
    ):
        attention.append("market_sentiment_risk_or_negative_requires_confidence_check")
    if side == "BUY" and any(
        str(item.get("direction") or "").lower() in ("risk_on", "positive")
        and numeric_value(item.get("score"), default=0.0) >= 0.25
        and item.get("stale") is not True
        for item in sentiment_items
    ):
        attention.append("market_sentiment_support_requires_acknowledgement")
    fundamentals_status = str((fundamentals_context_payload or {}).get("status") or "MISSING").upper()
    if fundamentals_status in ("MISSING", "STALE", "RISK", "FAIL", "INVALID"):
        attention.append("fundamentals_context_coverage_limit_requires_acknowledgement")
    if side == "BUY" and not fundamental_items:
        attention.append("fundamentals_context_missing_for_buy_symbol")
    if side == "BUY" and fundamentals_limit_required(fundamental_items):
        attention.append("fundamentals_context_limit_requires_acknowledgement")
    if side == "BUY" and fundamentals_support_available(fundamental_items):
        attention.append("fundamentals_context_support_requires_acknowledgement")
    if str(source_limits.get("trusted_source_preflight_status") or "").upper() in ("WARN", "FAIL", "MISSING", "STALE"):
        attention.append("trusted_source_preflight_limit_requires_disclosure")
    if str(source_limits.get("source_reliability_status") or "").upper() in ("DEGRADED", "STALE", "MISSING", "FAIL"):
        attention.append("source_reliability_limit_requires_acknowledgement")
    deduped = []
    for item in attention:
        if item not in deduped:
            deduped.append(item)
    return deduped


def context_digest_for_item(
    item,
    market_context_payload,
    intraday_kline_batch_payload,
    intraday_context_payload,
    intraday_timeframe_quality_payload,
    intraday_market_session_overrides_payload,
    external_market_context_payload,
    event_catalyst_payload,
    event_catalyst_signal_payload,
    market_sentiment_payload,
    fundamentals_context_payload,
    trusted_source_preflight_payload,
    source_reliability_payload,
    kline_gap_source_diagnostic_payload,
):
    alert = item.get("alert") or {}
    market_context = market_context_for_alert(alert, market_context_payload)
    intraday_context = intraday_context_for_alert(alert, intraday_context_payload)
    intraday_timeframe_policy = intraday_timeframe_policy_summary(intraday_timeframe_quality_payload)
    intraday_timeframe_decision = intraday_timeframe_decision_for_alert(alert, intraday_timeframe_quality_payload)
    intraday_market_session_overrides = intraday_market_session_overrides_for_alert(
        alert,
        intraday_market_session_overrides_payload,
    )
    intraday_minute_producer = intraday_minute_producer_summary(intraday_kline_batch_payload)
    external_items = relevant_external_context(alert, external_market_context_payload)
    catalyst_items = relevant_event_catalysts(alert, event_catalyst_payload)
    catalyst_signal_items = relevant_event_catalyst_signals(alert, event_catalyst_signal_payload)
    sentiment_items = relevant_market_sentiment(alert, market_sentiment_payload)
    fundamental_items = relevant_fundamentals(alert, fundamentals_context_payload)
    source_limits = source_limit_summary(trusted_source_preflight_payload, source_reliability_payload)
    intraday_evidence = intraday_signal_evidence(alert, intraday_context)
    daily_gap_source = daily_gap_source_for_alert(alert, kline_gap_source_diagnostic_payload)
    return {
        "schema": "hermes_review_item_context_digest_v1",
        "read_only": True,
        "submits_orders": False,
        "changes_strategy": False,
        "changes_alert_queue": False,
        "signal_id": item.get("signal_id"),
        "symbol": alert.get("symbol"),
        "market": alert.get("market"),
        "matching": {
            "symbol_tokens": sorted(symbol_tokens(alert.get("symbol"), market=alert.get("market"))),
            "market": normalize_market(alert.get("market")),
            "rules": [
                "prefer exact or normalized symbol match",
                "then same-market context",
                "then global macro/capital-flow/sentiment context",
                "event_catalyst_signals.related_v5_signal_ids is a direct match",
            ],
        },
        "market_context": market_context,
        "intraday_context": intraday_context,
        "intraday_timeframe_policy": intraday_timeframe_policy,
        "intraday_timeframe_decision": intraday_timeframe_decision,
        "intraday_signal_evidence": intraday_evidence,
        "intraday_minute_producer": intraday_minute_producer,
        "intraday_market_session_overrides": intraday_market_session_overrides,
        "external_market_context": {
            "status": (external_market_context_payload or {}).get("status"),
            "relevant_item_count": len(external_items),
            "items": external_items,
        },
        "event_catalysts": {
            "status": (event_catalyst_payload or {}).get("status"),
            "relevant_candidate_count": len(catalyst_items),
            "negative_candidate_count": len(
                [row for row in catalyst_items if str(row.get("sentiment") or "").lower() == "negative"]
            ),
            "positive_candidate_count": len(
                [row for row in catalyst_items if str(row.get("sentiment") or "").lower() == "positive"]
            ),
            "candidates": catalyst_items,
        },
        "event_catalyst_signals": {
            "status": (event_catalyst_signal_payload or {}).get("status"),
            "relevant_signal_count": len(catalyst_signal_items),
            "challenge_buy_count": len(
                [
                    row
                    for row in catalyst_signal_items
                    if str(row.get("review_signal_type") or "").upper() == "CHALLENGE_BUY_REVIEW"
                ]
            ),
            "signals": catalyst_signal_items,
        },
        "market_sentiment": {
            "status": (market_sentiment_payload or {}).get("status"),
            "relevant_indicator_count": len(sentiment_items),
            "indicators": sentiment_items,
        },
        "fundamentals_context": {
            "status": (fundamentals_context_payload or {}).get("status"),
            "relevant_item_count": len(fundamental_items),
            "limit_acknowledgement_required": fundamentals_limit_required(fundamental_items),
            "support_acknowledgement_required": fundamentals_support_available(fundamental_items),
            "items": fundamental_items,
        },
        "daily_gap_source_diagnostic": daily_gap_source,
        "source_limits": source_limits,
        "required_judgment_attention": context_attention_items(
            alert,
            market_context,
            intraday_context,
            intraday_timeframe_policy,
            intraday_market_session_overrides,
            intraday_minute_producer,
            daily_gap_source,
            external_market_context_payload,
            external_items,
            catalyst_items,
            event_catalyst_payload,
            event_catalyst_signal_payload,
            catalyst_signal_items,
            market_sentiment_payload,
            sentiment_items,
            fundamentals_context_payload,
            fundamental_items,
            source_limits,
        ),
    }


def attach_context_digests_to_items(
    items,
    market_context_payload,
    intraday_kline_batch_payload,
    intraday_context_payload,
    intraday_timeframe_quality_payload,
    intraday_market_session_overrides_payload,
    external_market_context_payload,
    event_catalyst_payload,
    event_catalyst_signal_payload,
    market_sentiment_payload,
    fundamentals_context_payload,
    trusted_source_preflight_payload,
    source_reliability_payload,
    kline_gap_source_diagnostic_payload,
):
    for item in items:
        item["context_digest"] = context_digest_for_item(
            item,
            market_context_payload,
            intraday_kline_batch_payload,
            intraday_context_payload,
            intraday_timeframe_quality_payload,
            intraday_market_session_overrides_payload,
            external_market_context_payload,
            event_catalyst_payload,
            event_catalyst_signal_payload,
            market_sentiment_payload,
            fundamentals_context_payload,
            trusted_source_preflight_payload,
            source_reliability_payload,
            kline_gap_source_diagnostic_payload,
        )
    return items


def alert_like_from_position_review_item(item):
    return {
        "signal_id": item.get("review_id"),
        "symbol": item.get("symbol"),
        "market": item.get("market"),
        "signal_type": "POSITION",
        "trigger": item.get("recommended_action") or "position_review",
    }


def position_context_attention_items(
    item,
    market_context,
    intraday_context,
    intraday_market_session_overrides,
    external_items,
    catalyst_items,
    catalyst_signal_items,
    sentiment_items,
    fundamental_items,
    source_limits,
):
    attention = []
    action = str((item or {}).get("recommended_action") or "").lower()
    urgency = str((item or {}).get("urgency") or "").lower()
    market_notes = set((market_context or {}).get("notes") or [])
    native_context = (market_context or {}).get("native_index_context") or {}
    cross_market = (market_context or {}).get("cross_market") or {}
    intraday_status = str((intraday_context or {}).get("status") or "").upper()
    intraday_notes = set((intraday_context or {}).get("hermes_notes") or (intraday_context or {}).get("notes") or [])

    if urgency == "high":
        attention.append("high_urgency_position_requires_contextual_rationale")
    if action in ("exit_review", "reduce_or_exit_review", "take_profit_or_trailing_stop_review"):
        attention.append("position_exit_or_reduce_review_requires_contextual_rationale")
    if (market_context or {}).get("status") != "OK":
        attention.append("position_market_context_missing_for_holding_market")
    if (market_context or {}).get("regime") == "risk_off":
        attention.append("position_risk_off_market_context_requires_discussion")
    if "buy_signals_against_weak_breadth" in market_notes:
        attention.append("position_weak_breadth_requires_discussion")
    if str(native_context.get("status") or "").upper() not in ("", "OK"):
        attention.append("position_native_index_context_incomplete_requires_disclosure")
    if native_context.get("alignment") == "conflicts_with_breadth":
        attention.append("position_native_index_conflicts_with_breadth_requires_discussion")
    if cross_market.get("alignment") == "conflicts_with_breadth":
        attention.append("position_cross_market_conflict_requires_discussion")
    if intraday_status in ("MISSING", "STALE"):
        attention.append("position_intraday_context_missing_or_stale_requires_disclosure")
    if "intraday_context_quality_degraded_requires_disclosure" in intraday_notes:
        attention.append("position_intraday_context_quality_degraded_requires_disclosure")
    if "intraday_market_not_open_requires_session_context" in intraday_notes:
        attention.append("position_intraday_market_not_open_requires_session_context")
    calendar_status = str((intraday_market_session_overrides or {}).get("status") or "").upper()
    calendar_report_status = str((intraday_market_session_overrides or {}).get("report_status") or "").upper()
    if (
        calendar_status in ("WARN", "FAIL", "MISSING", "STALE", "INVALID")
        or calendar_report_status in ("WARN", "FAIL", "MISSING", "STALE", "INVALID")
        or (intraday_market_session_overrides or {}).get("warnings")
        or (intraday_market_session_overrides or {}).get("errors")
    ):
        attention.append("position_intraday_market_session_overrides_limit_requires_disclosure")
    if any(str(row.get("sentiment") or "").lower() == "negative" for row in external_items):
        attention.append("position_negative_external_context_requires_discussion")
    if any(str(row.get("sentiment") or "").lower() == "negative" for row in catalyst_items):
        attention.append("position_negative_event_catalyst_requires_discussion")
    if any(str(row.get("review_signal_type") or "").upper() == "CHALLENGE_BUY_REVIEW" for row in catalyst_signal_items):
        attention.append("position_event_catalyst_signal_challenge_requires_discussion")
    if any(
        str(row.get("direction") or "").lower() in ("risk_off", "negative")
        or numeric_value(row.get("score"), default=0.0) < 0
        for row in sentiment_items
    ):
        attention.append("position_market_sentiment_risk_requires_discussion")
    if not fundamental_items:
        attention.append("position_fundamentals_context_missing_for_holding_symbol")
    elif fundamentals_limit_required(fundamental_items):
        attention.append("position_fundamentals_context_limit_requires_discussion")
    if str(source_limits.get("trusted_source_preflight_status") or "").upper() in ("WARN", "FAIL", "MISSING", "STALE"):
        attention.append("position_trusted_source_preflight_limit_requires_disclosure")
    if str(source_limits.get("source_reliability_status") or "").upper() in ("DEGRADED", "STALE", "MISSING", "FAIL"):
        attention.append("position_source_reliability_limit_requires_discussion")

    deduped = []
    for value in attention:
        if value not in deduped:
            deduped.append(value)
    return deduped


def position_context_digest_for_item(
    item,
    market_context_payload,
    intraday_context_payload,
    intraday_market_session_overrides_payload,
    external_market_context_payload,
    event_catalyst_payload,
    event_catalyst_signal_payload,
    market_sentiment_payload,
    fundamentals_context_payload,
    trusted_source_preflight_payload,
    source_reliability_payload,
):
    alert_like = alert_like_from_position_review_item(item)
    market_context = market_context_for_alert(alert_like, market_context_payload)
    intraday_context = intraday_context_for_alert(alert_like, intraday_context_payload)
    intraday_market_session_overrides = intraday_market_session_overrides_for_alert(
        alert_like,
        intraday_market_session_overrides_payload,
    )
    external_items = relevant_external_context(alert_like, external_market_context_payload)
    catalyst_items = relevant_event_catalysts(alert_like, event_catalyst_payload)
    catalyst_signal_items = relevant_event_catalyst_signals(alert_like, event_catalyst_signal_payload)
    sentiment_items = relevant_market_sentiment(alert_like, market_sentiment_payload)
    fundamental_items = relevant_fundamentals(alert_like, fundamentals_context_payload)
    source_limits = source_limit_summary(trusted_source_preflight_payload, source_reliability_payload)
    return {
        "schema": "hermes_position_review_context_digest_v1",
        "read_only": True,
        "advisory_only": True,
        "submits_orders": False,
        "changes_strategy": False,
        "changes_alert_queue": False,
        "review_id": item.get("review_id"),
        "portfolio_id": item.get("portfolio_id"),
        "role": item.get("role"),
        "symbol": item.get("symbol"),
        "market": item.get("market"),
        "recommended_action": item.get("recommended_action"),
        "urgency": item.get("urgency"),
        "matching": {
            "symbol_tokens": sorted(symbol_tokens(item.get("symbol"), market=item.get("market"))),
            "market": normalize_market(item.get("market")),
            "rules": [
                "prefer exact or normalized position symbol match",
                "then same-market context",
                "then global macro/capital-flow/sentiment context",
                "event_catalyst_signals.related_v5_signal_ids may be empty for position reviews",
            ],
        },
        "market_context": market_context,
        "intraday_context": intraday_context,
        "intraday_market_session_overrides": intraday_market_session_overrides,
        "external_market_context": {
            "status": (external_market_context_payload or {}).get("status"),
            "relevant_item_count": len(external_items),
            "items": external_items,
        },
        "event_catalysts": {
            "status": (event_catalyst_payload or {}).get("status"),
            "relevant_candidate_count": len(catalyst_items),
            "negative_candidate_count": len(
                [row for row in catalyst_items if str(row.get("sentiment") or "").lower() == "negative"]
            ),
            "positive_candidate_count": len(
                [row for row in catalyst_items if str(row.get("sentiment") or "").lower() == "positive"]
            ),
            "candidates": catalyst_items,
        },
        "event_catalyst_signals": {
            "status": (event_catalyst_signal_payload or {}).get("status"),
            "relevant_signal_count": len(catalyst_signal_items),
            "challenge_buy_count": len(
                [
                    row
                    for row in catalyst_signal_items
                    if str(row.get("review_signal_type") or "").upper() == "CHALLENGE_BUY_REVIEW"
                ]
            ),
            "signals": catalyst_signal_items,
        },
        "market_sentiment": {
            "status": (market_sentiment_payload or {}).get("status"),
            "relevant_indicator_count": len(sentiment_items),
            "indicators": sentiment_items,
        },
        "fundamentals_context": {
            "status": (fundamentals_context_payload or {}).get("status"),
            "relevant_item_count": len(fundamental_items),
            "limit_acknowledgement_required": fundamentals_limit_required(fundamental_items),
            "support_acknowledgement_available": fundamentals_support_available(fundamental_items),
            "items": fundamental_items,
        },
        "source_limits": source_limits,
        "position_attention": position_context_attention_items(
            item,
            market_context,
            intraday_context,
            intraday_market_session_overrides,
            external_items,
            catalyst_items,
            catalyst_signal_items,
            sentiment_items,
            fundamental_items,
            source_limits,
        ),
    }


def position_judgment_allowed_decisions(item):
    role = str((item or {}).get("role") or "").strip().lower()
    if role == "user":
        return ["hold", "watch"]
    return ["hold", "watch", "reduce", "exit", "trail_stop"]


def position_attention_codes_from_item(item):
    digest = (item or {}).get("context_digest") if isinstance((item or {}).get("context_digest"), dict) else {}
    codes = digest.get("position_attention") if isinstance(digest.get("position_attention"), list) else []
    deduped = []
    for code in codes:
        text = str(code or "").strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def position_judgment_template_for_item(item, packet_id, judgment_file):
    item = item if isinstance(item, dict) else {}
    attention_codes = position_attention_codes_from_item(item)
    allowed_decisions = position_judgment_allowed_decisions(item)
    role = str(item.get("role") or "").strip().lower()
    instructions = [
        "Draft only: Hermes must review the position_review item and replace every placeholder before appending.",
        "Append only one completed JSON object per review_id to the judgment file.",
        "Set advisory_only=true and submits_orders=false; this path must not submit orders or change portfolios.",
    ]
    if role == "user":
        instructions.append("For user role, machine-readable decision must stay hold/watch; put manual reduce/exit advice in risk_notes.")
    if attention_codes:
        instructions.append("Copy every required_position_attention_code into position_attention_codes and explain one effect per code.")
    return {
        "schema": "hermes_position_judgment_template_v1",
        "template_only": True,
        "ready_to_append_without_hermes_review": False,
        "judgment_file": judgment_file,
        "allowed_decisions": allowed_decisions,
        "required_position_attention_codes": attention_codes,
        "instructions": instructions,
        "draft_jsonl_object": {
            "schema": POSITION_JUDGMENT_SCHEMA,
            "packet_id": packet_id,
            "review_id": item.get("review_id"),
            "portfolio_id": item.get("portfolio_id"),
            "role": item.get("role"),
            "symbol": item.get("symbol"),
            "decision": "<choose one: " + "|".join(allowed_decisions) + ">",
            "confidence": "<0.0-1.0 after Hermes review>",
            "reviewed_at": "<ISO-8601 datetime when Hermes completes review>",
            "reviewer": "hermes",
            "advisory_only": True,
            "submits_orders": False,
            "max_exit_quantity": "<optional positive number only when decision=reduce|exit>",
            "supporting_factors": ["<facts supporting the advisory decision>"],
            "opposing_factors": ["<facts against the advisory decision>"],
            "risk_notes": ["<position risk, stale data, market/news/fundamental/intraday/source limits>"],
            "context_review": {
                "position_context_reviewed": "<set true only after reviewing context_digest>",
                "portfolio_risk_reviewed": "<set true only after reviewing portfolio_risk and execution_policy>",
                "market_context_reviewed": "<set true only after reviewing market_context/source limits>",
                "external_context_reviewed": "<set true only after reviewing news/events/sentiment/fundamentals>",
                "intraday_context_reviewed": "<set true only after reviewing intraday/session context>",
                "notes": ["<how context changed hold/watch/reduce/exit/trail_stop advice>"],
            },
            "position_attention_acknowledged": (
                "<set true after reviewing all position_attention codes>" if attention_codes else False
            ),
            "position_attention_codes": attention_codes,
            "position_attention_notes": [
                "<how holding-specific attention changed the advisory decision>"
            ] if attention_codes else [],
            "position_attention_effects": [
                {
                    "code": code,
                    "effect": "<specific holding-risk interpretation for this code>",
                    "decision_impact": "<how this code changed the advisory decision>",
                }
                for code in attention_codes
            ],
            "follow_up": ["<optional manual follow-up items>"],
        },
    }


def attach_position_judgment_templates(position_review_payload, packet_id, judgment_file):
    payload = position_review_payload if isinstance(position_review_payload, dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        item["position_judgment_template"] = position_judgment_template_for_item(
            item,
            packet_id,
            judgment_file,
        )
        count += 1
    if payload:
        payload["position_judgment_template_summary"] = {
            "schema": "portfolio_position_judgment_template_summary_v1",
            "template_only": True,
            "ready_to_append_without_hermes_review": False,
            "judgment_file": judgment_file,
            "template_count": count,
        }
    return payload


def enrich_position_review_with_context(
    position_review_payload,
    market_context_payload,
    intraday_context_payload,
    intraday_market_session_overrides_payload,
    external_market_context_payload,
    event_catalyst_payload,
    event_catalyst_signal_payload,
    market_sentiment_payload,
    fundamentals_context_payload,
    trusted_source_preflight_payload,
    source_reliability_payload,
):
    payload = copy.deepcopy(position_review_payload) if isinstance(position_review_payload, dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        item["context_digest"] = position_context_digest_for_item(
            item,
            market_context_payload,
            intraday_context_payload,
            intraday_market_session_overrides_payload,
            external_market_context_payload,
            event_catalyst_payload,
            event_catalyst_signal_payload,
            market_sentiment_payload,
            fundamentals_context_payload,
            trusted_source_preflight_payload,
            source_reliability_payload,
        )
    if payload:
        payload["context_enrichment"] = {
            "schema": "portfolio_position_review_context_enrichment_v1",
            "read_only": True,
            "advisory_only": True,
            "submits_orders": False,
            "item_context_digest_count": len([item for item in items if isinstance(item, dict) and item.get("context_digest")]),
        }
    return payload


def portfolio_risk_blocking_reasons(portfolio_payload, alert=None):
    risk_payload = (portfolio_payload or {}).get("portfolio_risk") or {}
    side = str((alert or {}).get("signal_type", "")).upper()
    reasons = []
    for risk in risk_payload.get("reports") or []:
        if risk.get("role") != "simulation":
            continue
        flags = set(risk.get("risk_flags") or [])
        if risk.get("risk_level") == "critical":
            reasons.append("simulation_portfolio_risk_critical")
            for flag in flags:
                reasons.append(f"portfolio_risk:{flag}")
        if risk.get("trade_position_reconciliation_status") == "FAIL":
            reasons.append("portfolio_risk:trade_position_reconciliation_failed")
        if side == "BUY" and "exit_pressure_above_30pct" in flags:
            reasons.append("portfolio_risk:exit_pressure_requires_review_before_new_buy")
    deduped = []
    seen = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return deduped


def apply_portfolio_risk_to_items(items, portfolio_payload):
    for item in items:
        reasons = portfolio_risk_blocking_reasons(portfolio_payload, item.get("alert") or {})
        if reasons:
            merge_blocking_reasons([item], reasons)
    return items


def data_health_blocking_reasons(data_health_payload):
    if not isinstance(data_health_payload, dict):
        return []
    if data_health_payload.get("schema") != "data_health_report_v1":
        return []
    if data_health_payload.get("status") != "FAIL":
        return []
    reasons = ["data_health_fail"]
    for market, summary in sorted((data_health_payload.get("markets") or {}).items()):
        if summary.get("status") == "FAIL":
            for reason in summary.get("failures") or []:
                reasons.append(f"data_health:{market}:{reason}")
    return reasons


def apply_data_health_to_items(items, data_health_payload):
    return merge_blocking_reasons(items, data_health_blocking_reasons(data_health_payload))


def execution_readiness_blocking_reasons(execution_readiness_payload):
    if not isinstance(execution_readiness_payload, dict):
        return ["execution_readiness_missing"]
    if execution_readiness_payload.get("schema") != "execution_readiness_report_v1":
        return ["execution_readiness_schema_invalid"]
    status = execution_readiness_payload.get("status")
    ready = execution_readiness_payload.get("ready_for_execute")
    reasons = []
    generated_at = parse_timestamp(execution_readiness_payload.get("generated_at"))
    if not generated_at:
        reasons.append("execution_readiness_generated_at_missing")
    else:
        age = datetime.now() - generated_at
        if age.total_seconds() < -300:
            reasons.append("execution_readiness_generated_at_in_future")
        elif age > timedelta(hours=MAX_READINESS_REPORT_AGE_HOURS):
            reasons.append("execution_readiness_stale")
    if status != "READY":
        reasons.append(f"execution_readiness_status_{str(status or 'missing').lower()}")
    if ready is not True:
        reasons.append("execution_readiness_ready_for_execute_false")
    return reasons


def apply_execution_readiness_to_items(items, execution_readiness_payload):
    return merge_blocking_reasons(
        items,
        execution_readiness_blocking_reasons(execution_readiness_payload),
    )


def simulation_performance_blocking_reasons(simulation_performance_payload, alert=None):
    side = str((alert or {}).get("signal_type") or "").upper()
    if side != "BUY":
        return []
    if not isinstance(simulation_performance_payload, dict):
        return []
    if simulation_performance_payload.get("schema") != "simulation_performance_report_v1":
        return []
    if str(simulation_performance_payload.get("status") or "").upper() != "FAIL":
        return []
    reasons = ["simulation_performance_fail"]
    for reason in simulation_performance_payload.get("reason_codes") or []:
        text = str(reason or "").strip()
        if text:
            reasons.append(f"simulation_performance:{text}")
    remediation = simulation_performance_payload.get("remediation_plan")
    if isinstance(remediation, dict) and remediation.get("proposal_hash"):
        reasons.append(f"simulation_performance_remediation:{remediation.get('proposal_hash')}")
    return reasons


def apply_simulation_performance_to_items(items, simulation_performance_payload):
    for item in items:
        reasons = simulation_performance_blocking_reasons(
            simulation_performance_payload,
            item.get("alert") or {},
        )
        if reasons:
            merge_blocking_reasons([item], reasons)
    return items


def daily_gap_source_blocking_reasons(alert, kline_gap_source_diagnostic_payload):
    digest = daily_gap_source_for_alert(alert, kline_gap_source_diagnostic_payload)
    if not digest.get("matched"):
        return []
    reasons = ["daily_gap_source_unresolved_symbol"]
    category = digest.get("category")
    if category:
        reasons.append(f"daily_gap_source:{category}")
    exposure = digest.get("exposure") or {}
    if exposure.get("in_current_v5_watchlist"):
        reasons.append("daily_gap_source:current_v5_watchlist_member")
    if exposure.get("has_open_position"):
        reasons.append("daily_gap_source:open_position")
    return reasons


def apply_daily_gap_source_to_items(items, kline_gap_source_diagnostic_payload):
    for item in items:
        reasons = daily_gap_source_blocking_reasons(
            item.get("alert") or {},
            kline_gap_source_diagnostic_payload,
        )
        if reasons:
            merge_blocking_reasons([item], reasons)
    return items


def merge_blocking_reasons(items, reasons):
    if not reasons:
        return items
    for item in items:
        merged = list(item.get("blocking_reasons") or [])
        for reason in reasons:
            if reason not in merged:
                merged.append(reason)
        item["blocking_reasons"] = merged
        item["eligible_for_approval"] = False
        item["recommended_judgment"] = "reject_or_hold"
    return items


def judgment_contract(judgment_file):
    return {
        "judgment_file": judgment_file,
        "schema": JUDGMENT_SCHEMA,
        "append_jsonl_object": {
            "schema": JUDGMENT_SCHEMA,
            "packet_id": "<copy from packet_id>",
            "signal_id": "<copy from review_items[].signal_id>",
            "decision": "approve|reject|reduce|hold",
            "confidence": "0.0-1.0",
            "reviewed_at": "ISO-8601 datetime",
            "reviewer": "hermes",
            "supporting_factors": ["facts supporting the decision"],
            "opposing_factors": ["facts against the decision"],
            "risk_notes": ["position sizing, stale data, event risk, market condition"],
            "max_quantity": "required only when decision=reduce",
            "context_review": {
                "technical_signal_reviewed": "required true for approve/reduce",
                "portfolio_risk_reviewed": "required true for approve/reduce",
                "strategy_evidence_reviewed": "required true for approve/reduce",
                "data_health_reviewed": "required true for approve/reduce",
                "execution_readiness_reviewed": "required true for approve/reduce",
                "market_context_reviewed": "required true for approve/reduce",
                "intraday_context_reviewed": "required true for approve/reduce",
                "external_market_context_reviewed": "required true for approve/reduce",
                "event_catalysts_reviewed": "required true for approve/reduce",
                "event_catalyst_signals_reviewed": "required true for approve/reduce",
                "market_sentiment_reviewed": "required true for approve/reduce",
                "fundamentals_context_reviewed": "required true for approve/reduce",
                "source_reliability_reviewed": "required true for approve/reduce",
                "simulation_performance_reviewed": "required true for approve/reduce",
                "cron_wiring_reviewed": "required true for approve/reduce",
                "notes": ["short explanation of how conflicting context changed confidence or sizing"],
            },
            "external_market_context_risk_acknowledged": "required true when approving/reducing BUY with relevant negative external_market_context items",
            "external_market_context_ids": "required list of relevant external_market_context.items[].id values when external context risk is present",
            "external_market_context_notes": "required notes explaining how negative news, macro, capital-flow, event, or sentiment context affected confidence, sizing, or rejection",
            "external_market_context_support_acknowledged": "required true when approving/reducing BUY with relevant positive high-impact external_market_context items",
            "external_market_context_support_ids": "required list of relevant positive external_market_context.items[].id values used as support",
            "external_market_context_support_notes": "required notes explaining how positive news, macro, capital-flow, event, or sentiment context affected confidence, sizing, or rejection",
            "market_context_coverage_acknowledged": "required true when context_digest.required_judgment_attention includes market_context_coverage_limit_requires_acknowledgement",
            "market_context_coverage_status": "required reviewed context_digest.market_context.status when market context coverage acknowledgement is required",
            "market_context_coverage_notes": "required notes explaining how missing/stale/risky/failed market regime, breadth, native-index, or cross-market context limited confidence or prevented using absence of risk_off as evidence",
            "external_market_context_coverage_acknowledged": "required true when context_digest.required_judgment_attention includes external_market_context_coverage_limit_requires_acknowledgement",
            "external_market_context_coverage_status": "required reviewed context_digest.external_market_context.status when external context coverage acknowledgement is required",
            "external_market_context_coverage_notes": "required notes explaining how missing/stale/risky/failed news, macro, event, or capital-flow coverage limited confidence or prevented using absence of external items as evidence",
            "event_catalyst_risk_acknowledged": "required true when approving/reducing BUY with relevant negative event_catalysts",
            "event_catalyst_ids": "required list of relevant event_catalysts.candidates[].id when event_catalyst_risk_acknowledged=true",
            "event_catalyst_signal_ids": "required list of relevant event_catalyst_signals.signals[].signal_id for CHALLENGE_BUY_REVIEW on this BUY",
            "event_catalyst_risk_notes": "required event-specific risk notes when relevant negative event_catalysts exist",
            "event_catalyst_coverage_acknowledged": "required true when context_digest.required_judgment_attention includes event_catalyst_coverage_limit_requires_acknowledgement",
            "event_catalyst_coverage_status": "required reviewed context_digest.event_catalysts.status when event catalyst coverage acknowledgement is required",
            "event_catalyst_coverage_notes": "required notes explaining how missing/stale/risky/failed event-catalyst coverage limited confidence or prevented using absence of event catalysts as evidence",
            "event_catalyst_signal_coverage_acknowledged": "required true when context_digest.required_judgment_attention includes event_catalyst_signal_coverage_limit_requires_acknowledgement",
            "event_catalyst_signal_coverage_status": "required reviewed context_digest.event_catalyst_signals.status when event signal coverage acknowledgement is required",
            "event_catalyst_signal_coverage_notes": "required notes explaining how missing/stale/risky/failed event-catalyst signal coverage limited confidence or prevented using absence of event signals as evidence",
            "event_catalyst_support_acknowledged": "required true when approving/reducing BUY with relevant SUPPORT_BUY_REVIEW event_catalyst_signals",
            "event_catalyst_support_signal_ids": "required list of relevant event_catalyst_signals.signals[].signal_id for SUPPORT_BUY_REVIEW on this BUY",
            "event_catalyst_support_notes": "required notes explaining how positive event-catalyst support affected confidence or sizing without overriding gates",
            "fundamentals_context_limit_acknowledged": "required true when approving/reducing BUY with partial_fundamentals or fallback fundamentals for this symbol",
            "fundamentals_context_symbols": "required list of relevant fundamentals_context.items[].symbol values when fundamentals are partial/fallback",
            "fundamentals_context_missing_metrics": "required list of missing metrics considered from fundamentals_context.items[].fundamental_completeness.missing_metrics",
            "fundamentals_context_notes": "required notes explaining how partial/fallback fundamentals affected confidence, sizing, or rejection",
            "fundamentals_context_coverage_acknowledged": "required true when context_digest.required_judgment_attention includes fundamentals_context_coverage_limit_requires_acknowledgement",
            "fundamentals_context_coverage_status": "required reviewed context_digest.fundamentals_context.status when fundamentals coverage acknowledgement is required",
            "fundamentals_context_coverage_notes": "required notes explaining how missing/stale/risky/failed valuation, profitability, growth, dividend, or leverage coverage limited confidence or prevented treating absence of fundamentals as neutral",
            "fundamentals_context_support_acknowledged": "required true when approving/reducing BUY with full fresh fundamentals used as support",
            "fundamentals_context_support_symbols": "required list of relevant fundamentals_context.items[].symbol values used as support",
            "fundamentals_context_support_metrics": "required list of valuation/profitability/growth/leverage metrics used as support",
            "fundamentals_context_support_notes": "required notes explaining how full fundamentals affected confidence or sizing without overriding gates",
            "market_sentiment_risk_acknowledged": "required true when approving/reducing BUY with relevant market_sentiment risk_off or negative indicators",
            "market_sentiment_indicator_ids": "required list of relevant market_sentiment.indicators[].id values when sentiment risk is present",
            "market_sentiment_notes": "required notes explaining how risk-off or negative sentiment affected confidence, sizing, or rejection",
            "market_sentiment_coverage_acknowledged": "required true when context_digest.required_judgment_attention includes market_sentiment_coverage_limit_requires_acknowledgement",
            "market_sentiment_coverage_status": "required reviewed context_digest.market_sentiment.status when market sentiment coverage acknowledgement is required",
            "market_sentiment_coverage_notes": "required notes explaining how missing/stale/risky/failed volatility, capital-flow, or risk-appetite sentiment coverage limited confidence or prevented using absence of sentiment indicators as evidence",
            "market_sentiment_support_acknowledged": "required true when approving/reducing BUY with relevant market_sentiment risk_on or positive support indicators",
            "market_sentiment_support_indicator_ids": "required list of relevant market_sentiment.indicators[].id values used as support",
            "market_sentiment_support_notes": "required notes explaining how quantified risk-on or positive sentiment affected confidence or sizing without overriding gates",
            "source_reliability_limit_acknowledged": "required true when approving/reducing while source_reliability.status is DEGRADED, STALE, MISSING, or FAIL",
            "source_reliability_components": "required list of source_reliability.components[].name values considered when source reliability is degraded",
            "source_reliability_reasons": "required list of source_reliability component reasons or report recommendations considered when source reliability is degraded",
            "source_reliability_notes": "required notes explaining how source-quality limits affected confidence, sizing, or rejection",
            "simulation_performance_acknowledged": "required true when approving/reducing while simulation_performance.status is WARN or FAIL",
            "simulation_performance_status": "required reviewed top-level simulation_performance.status when simulation performance is WARN or FAIL",
            "simulation_performance_reason_codes": "required list of simulation_performance.reason_codes considered when simulation performance is WARN or FAIL",
            "simulation_performance_notes": "required notes explaining how realized simulation performance affected confidence, sizing, rejection, or hold",
            "hermes_alpha_evidence_acknowledged": "required true when strategy_learning_brief.hermes_alpha_evidence.status is INSUFFICIENT, NEGATIVE, MISSING, or INVALID, or when the evidence object is absent",
            "hermes_alpha_evidence_status": "required reviewed strategy_learning_brief.hermes_alpha_evidence.status when weak or missing alpha evidence acknowledgement is required",
            "hermes_alpha_evidence_reasons": "required list containing at least one reviewed strategy_learning_brief.hermes_alpha_evidence.reasons[] value when present",
            "hermes_alpha_evidence_notes": "required notes explaining why Hermes is approving/reducing even though audit-pass approval alpha is unproven or negative",
            "intraday_context_acknowledged": "required true when context_digest.required_judgment_attention includes intraday missing/stale, contradiction, rolling-window coverage, timeframe-policy, market-session, calendar-override, minute-producer, or minute-quality notes",
            "intraday_context_status": "required reviewed context_digest.intraday_context.status when intraday acknowledgement is required",
            "intraday_context_notes": "required notes explaining how intraday confirmation, contradiction, missing/stale minute coverage, timeframe-policy limits, producer/apply/source limits, market-session state, calendar-override limits, or degraded minute-bar quality affected confidence, sizing, or rejection",
            "intraday_signal_evidence_acknowledged": "required true when context_digest.intraday_signal_evidence.requires_judgment_acknowledgement=true",
            "intraday_signal_evidence_alignment": "required copy of context_digest.intraday_signal_evidence.alignment when intraday evidence requires acknowledgement",
            "intraday_signal_evidence_codes": "required list containing at least one reviewed context_digest.intraday_signal_evidence.codes[] value when present",
            "intraday_signal_evidence_notes": "required notes explaining whether 5m/15m/30m/60m/session evidence supported, challenged, conflicted, had incomplete rolling-window coverage, or was too low-quality to strengthen the judgment",
        },
        "hard_rules": [
            "Do not approve when eligible_for_approval is false.",
            "Do not approve if system health status is FAIL.",
            "Do not approve for execute when execution_readiness is missing, stale, BLOCKED, WARN, or ready_for_execute is false.",
            "Do not approve unconfirmed alerts or alerts rejected by intake.",
            "Do not approve for execute when strategy_evidence is missing, unresolved, or below the configured outcome thresholds.",
            "Do not approve for execute when symbol_conflict indicates an opposite same-symbol alert in the current queue scope.",
            "Do not approve when portfolio_risk reports critical simulation data integrity failures.",
            "Do not approve new BUY exposure when simulation_performance.status=FAIL; use reject/hold until later simulation evidence proves recovery.",
            "Do not approve when data_health status is FAIL or when the relevant market data is stale, incomplete, or internally inconsistent.",
            "Do not approve or reduce when review_items[].context_digest.daily_gap_source_diagnostic.matched=true; use reject or hold until the symbol's daily K-line source, mapping, active-universe, watchlist, and exposure issues are manually reviewed and resolved.",
            "Do not approve new BUY exposure while portfolio_risk shows unresolved exit_pressure_above_30pct.",
            "Any approve/reduce judgment must include context_review with all required *_reviewed fields true.",
            "Use market_context to reduce or reject new BUY approvals in risk_off regimes unless there is a specific documented exception with market_regime_exception=true.",
            "When review_items[].context_digest.required_judgment_attention includes market_context_coverage_limit_requires_acknowledgement, do not approve/reduce unless market_context_coverage_acknowledged=true, market_context_coverage_status matches the reviewed market_context.status, and market_context_coverage_notes explains why missing/stale/risky/failed market regime, breadth, native-index, or cross-market context was not treated as evidence that the market is benign.",
            "Do not approve or reduce a BUY with relevant negative external_market_context items unless external_market_context_risk_acknowledged=true, external_market_context_ids references the item ids, and external_market_context_notes explains how the news/macro/capital-flow risk affected confidence, sizing, or rejection.",
            "Do not use relevant positive high-impact external_market_context items to support a BUY unless external_market_context_support_acknowledged=true, external_market_context_support_ids references the item ids, and external_market_context_support_notes explains how the news/macro/capital-flow support affected confidence or sizing; this support must not override readiness, health, data, portfolio, or intake gates.",
            "If positive high-impact external_market_context support comes from public_fallback or unknown provider evidence, Hermes must also acknowledge the source_reliability limitation and avoid treating the item as broker, official, or vendor-grade evidence.",
            "When review_items[].context_digest.required_judgment_attention includes external_market_context_coverage_limit_requires_acknowledgement, do not approve/reduce unless external_market_context_coverage_acknowledged=true, external_market_context_coverage_status matches the reviewed external_market_context.status, and external_market_context_coverage_notes explains why missing/stale/risky/failed external context coverage was not treated as evidence that no news, macro, event, or capital-flow risk exists.",
            "Do not approve or reduce a BUY with relevant negative event_catalysts unless event_catalyst_risk_acknowledged=true, event_catalyst_ids references the candidate ids, and event_catalyst_risk_notes explains the event risk.",
            "When review_items[].context_digest.required_judgment_attention includes event_catalyst_coverage_limit_requires_acknowledgement, do not approve/reduce unless event_catalyst_coverage_acknowledged=true, event_catalyst_coverage_status matches the reviewed event_catalysts.status, and event_catalyst_coverage_notes explains why missing/stale/risky/failed event-catalyst coverage was not treated as evidence that no watchlist event risk exists.",
            "When review_items[].context_digest.required_judgment_attention includes event_catalyst_signal_coverage_limit_requires_acknowledgement, do not approve/reduce unless event_catalyst_signal_coverage_acknowledged=true, event_catalyst_signal_coverage_status matches the reviewed event_catalyst_signals.status, and event_catalyst_signal_coverage_notes explains why missing/stale/risky/failed event-signal coverage was not treated as evidence that no event risk exists.",
            "Do not approve or reduce a BUY challenged by event_catalyst_signals.CHALLENGE_BUY_REVIEW unless event_catalyst_risk_acknowledged=true, event_catalyst_signal_ids references the review signal ids, and event_catalyst_risk_notes explains why the challenge is accepted, reduced, or overridden.",
            "Do not use event_catalyst_signals.SUPPORT_BUY_REVIEW to support a BUY unless event_catalyst_support_acknowledged=true, event_catalyst_support_signal_ids references the review signal ids, and event_catalyst_support_notes explains how the positive event support affected confidence or sizing; support must not override technical, readiness, health, data, portfolio, source-reliability, or intake gates.",
            "When market_context.markets.<market>.cross_market.alignment=conflicts_with_breadth, any approve/reduce BUY judgment must explicitly discuss both breadth and cross-market sentiment/index/VIX evidence in supporting_factors, opposing_factors, risk_notes, context_review.notes, or market_regime_exception_reason.",
            "When review_items[].context_digest.market_context.native_index_context.alignment=conflicts_with_breadth, any approve/reduce BUY judgment must explicitly discuss both stock-pool breadth and native index evidence.",
            "When native index evidence is provider_grade=public_fallback, any approve/reduce judgment that relies on it must acknowledge the source_reliability limitation instead of treating it as broker, vendor, or official data.",
            "Use position_review to evaluate existing holdings before adding exposure.",
            "Use reduce instead of approve when the plan is directionally valid but sizing is too aggressive.",
            "Use reject or hold when context is incomplete, stale, contradictory, or outside the strategy mandate.",
            "Do not approve or reduce a BUY with relevant market_sentiment risk_off or negative indicators unless market_sentiment_risk_acknowledged=true, market_sentiment_indicator_ids references the indicator ids, and market_sentiment_notes explains how sentiment risk affected confidence, sizing, or rejection.",
            "When review_items[].context_digest.required_judgment_attention includes market_sentiment_coverage_limit_requires_acknowledgement, do not approve/reduce unless market_sentiment_coverage_acknowledged=true, market_sentiment_coverage_status matches the reviewed market_sentiment.status, and market_sentiment_coverage_notes explains why missing/stale/risky/failed sentiment coverage was not treated as evidence that volatility, capital-flow, or risk appetite is normal.",
            "Do not use relevant market_sentiment risk_on or positive indicators to support a BUY unless market_sentiment_support_acknowledged=true, market_sentiment_support_indicator_ids references the indicator ids, and market_sentiment_support_notes explains how quantified sentiment support affected confidence or sizing; support must not override technical, readiness, health, data, portfolio, source-reliability, or intake gates.",
            "Use fundamentals_context to discuss valuation, profitability, growth, dividend, and leverage context. If missing or stale, Hermes must state that fundamental awareness is incomplete before any approve/reduce.",
            "When review_items[].context_digest.required_judgment_attention includes fundamentals_context_coverage_limit_requires_acknowledgement, do not approve/reduce unless fundamentals_context_coverage_acknowledged=true, fundamentals_context_coverage_status matches the reviewed fundamentals_context.status, and fundamentals_context_coverage_notes explains why missing/stale/risky/failed fundamentals coverage was not treated as evidence that valuation, profitability, growth, dividend, or leverage risk is neutral.",
            "Do not approve or reduce a BUY with partial_fundamentals or fallback fundamentals for the same symbol unless fundamentals_context_limit_acknowledged=true, fundamentals_context_symbols references the symbol, fundamentals_context_missing_metrics lists at least one missing metric, and fundamentals_context_notes explains the coverage limit.",
            "Do not use full fresh fundamentals_context as BUY support unless fundamentals_context_support_acknowledged=true, fundamentals_context_support_symbols references the symbol, fundamentals_context_support_metrics lists the specific valuation/profitability/growth/leverage metrics used, and fundamentals_context_support_notes explains how the metrics affected confidence or sizing; support must not override technical, readiness, health, data, portfolio, source-reliability, or intake gates.",
            "When source_reliability.status is DEGRADED, STALE, MISSING, or FAIL, do not approve/reduce unless source_reliability_limit_acknowledged=true, source_reliability_components and source_reliability_reasons reference the degraded evidence, and source_reliability_notes explains how source limitations affected confidence, sizing, or rejection.",
            "When simulation_performance.status is WARN or FAIL, do not approve/reduce unless simulation_performance_acknowledged=true, simulation_performance_status matches the reviewed report status, simulation_performance_reason_codes references the report reason_codes when present, and simulation_performance_notes explains how realized simulation performance affected confidence, sizing, or rejection.",
            "When strategy_learning_brief.hermes_alpha_evidence.status is INSUFFICIENT, NEGATIVE, MISSING, or INVALID, or the evidence object is absent, do not approve/reduce unless hermes_alpha_evidence_acknowledged=true, hermes_alpha_evidence_status matches the reviewed status, hermes_alpha_evidence_reasons references at least one reviewed reason when present, and hermes_alpha_evidence_notes explains why the LLM layer is not being treated as proven alpha.",
            "When review_items[].context_digest.required_judgment_attention includes intraday_context_missing_or_stale_requires_disclosure, intraday_context_challenges_buy_requires_discussion, intraday_context_challenges_sell_requires_discussion, intraday_context_timeframe_conflict_requires_disclosure, intraday_timeframe_coverage_limited_requires_disclosure, intraday_timeframe_policy_requires_acknowledgement, intraday_context_quality_degraded_requires_disclosure, intraday_minute_producer_limit_requires_acknowledgement, intraday_market_not_open_requires_session_context, or intraday_market_session_overrides_limit_requires_disclosure, do not approve/reduce unless intraday_context_acknowledged=true, intraday_context_status matches the reviewed status when known, and intraday_context_notes explains how same-session or last-session minute evidence, rolling-window coverage limits, timeframe-policy limits, producer/apply/source limits, market-session state, calendar-override limits, and minute-bar quality affected the decision.",
            "When review_items[].context_digest.required_judgment_attention includes intraday_timeframe_policy_safety_limit_requires_rejection, use reject or hold until intraday_timeframe_quality_report.py no longer claims confidence raise, execution permission, or daily-gate override authority.",
            "When review_items[].context_digest.intraday_signal_evidence.requires_judgment_acknowledgement=true, do not approve/reduce unless intraday_signal_evidence_acknowledged=true, intraday_signal_evidence_alignment copies the reviewed alignment, intraday_signal_evidence_codes references at least one reviewed support/challenge/conflict/quality/limit code when present, and intraday_signal_evidence_notes explains how 5m/15m/30m/60m/session evidence changed confidence, sizing, rejection, or hold logic.",
        ],
    }


def position_judgment_contract(judgment_file):
    return {
        "judgment_file": judgment_file,
        "schema": POSITION_JUDGMENT_SCHEMA,
        "append_jsonl_object": {
            "schema": POSITION_JUDGMENT_SCHEMA,
            "packet_id": "<copy from packet_id>",
            "review_id": "<copy from position_review.items[].review_id>",
            "portfolio_id": "<copy from position_review.items[].portfolio_id>",
            "role": "<copy from position_review.items[].role>",
            "symbol": "<copy from position_review.items[].symbol>",
            "decision": "hold|watch|reduce|exit|trail_stop",
            "confidence": "0.0-1.0",
            "reviewed_at": "ISO-8601 datetime",
            "reviewer": "hermes",
            "advisory_only": True,
            "submits_orders": False,
            "max_exit_quantity": "optional advisory cap only when decision=reduce|exit",
            "supporting_factors": ["facts supporting the decision"],
            "opposing_factors": ["facts against the decision"],
            "risk_notes": ["position risk, stale data, concentration, market condition, external context, fundamentals, or intraday path risk"],
            "context_review": {
                "position_context_reviewed": "required true after reviewing position_review.items[].context_digest when present",
                "portfolio_risk_reviewed": "required true after reviewing the position, portfolio_risk, and advisory execution_policy",
                "market_context_reviewed": "required true after reviewing market_context and source limits relevant to the holding",
                "external_context_reviewed": "required true after reviewing external_market_context, event_catalysts, event_catalyst_signals, market_sentiment, and fundamentals context relevant to the holding",
                "intraday_context_reviewed": "required true after reviewing intraday_context and intraday_market_session_overrides relevant to the holding",
                "notes": ["short explanation of how context changed hold/reduce/exit/watch advice"],
            },
            "position_attention_acknowledged": "required true when position_review.items[].context_digest.position_attention is non-empty",
            "position_attention_codes": "required list copying every reviewed position_review.items[].context_digest.position_attention[] code",
            "position_attention_notes": "required notes explaining how holding-specific attention changed hold/watch/reduce/exit/trail-stop advice",
            "position_attention_effects": [
                {
                    "code": "<one copied position_attention code>",
                    "effect": "specific holding-risk interpretation for this code",
                    "decision_impact": "how this code changed hold/watch/reduce/exit/trail_stop advice",
                }
            ],
            "follow_up": ["optional manual follow-up items"],
        },
        "hard_rules": [
            "Position judgments are advisory review artifacts only.",
            "Position judgments do not approve trades and must not be consumed by rt_order_intake.py.",
            "Always set advisory_only=true and submits_orders=false.",
            "Copy packet_id and review_id exactly so audits can resolve the packet and position_review item reviewed.",
            "Review position_review.items[].context_digest before writing hold, watch, reduce, exit, or trail_stop advice; negative external context, risk-off sentiment, partial fundamentals, stale intraday data, or source-reliability limits must be reflected in supporting_factors, opposing_factors, risk_notes, or follow_up.",
            "When position_review.items[].context_digest.position_attention is non-empty, set position_attention_acknowledged=true, copy all attention codes into position_attention_codes, explain the overall effect in position_attention_notes, and include one position_attention_effects[] object per reviewed code with code, effect, and decision_impact.",
            "For user role, keep machine-readable decisions to hold or watch; put manual reduce/exit advice only in risk_notes.",
            "For simulation role, reduce/exit/trail_stop remains advisory and still requires a separate gated execution path.",
            "Do not call the simulation API from this judgment path.",
        ],
    }


def merged_outcome_maturity(learning_maturity, outcome_report_payload):
    maturity = dict(learning_maturity) if isinstance(learning_maturity, dict) else {}
    report_maturity = (
        outcome_report_payload.get("outcome_maturity")
        if isinstance(outcome_report_payload, dict) and isinstance(outcome_report_payload.get("outcome_maturity"), dict)
        else {}
    )
    if not maturity:
        return dict(report_maturity)
    for key, value in report_maturity.items():
        if maturity.get(key) in (None, "", [], {}):
            maturity[key] = value
    return maturity


def normalize_intraday_signal_alignment(value):
    text = str(value or "unavailable_or_stale").strip() or "unavailable_or_stale"
    return INTRADAY_ALIGNMENT_ALIASES.get(text, text)


def strategy_learning_intraday_alignment_brief(strategy_learning_payload):
    payload = strategy_learning_payload if isinstance(strategy_learning_payload, dict) else {}
    effect = (
        payload.get("intraday_alignment_effect")
        if isinstance(payload.get("intraday_alignment_effect"), dict)
        else {}
    )
    rows = (
        payload.get("by_intraday_signal_alignment")
        if isinstance(payload.get("by_intraday_signal_alignment"), list)
        else []
    )
    groups = []
    by_key = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = normalize_intraday_signal_alignment(row.get("key"))
        if not key:
            continue
        compact = {
            "key": key,
            "count": row.get("count"),
            "resolved_count": row.get("resolved_count"),
            "pending_or_missing_count": row.get("pending_or_missing_count"),
            "avg_signed_return_pct": row.get("avg_signed_return_pct"),
            "win_rate_pct": row.get("win_rate_pct"),
        }
        groups.append(compact)
        by_key[key] = compact

    minimum_resolved_sample = effect.get("minimum_sample") or 5
    challenge = by_key.get("challenges_signal") or {}
    challenge_resolved_count = challenge.get("resolved_count") or 0
    challenge_avg = challenge.get("avg_signed_return_pct")
    if effect.get("hermes_note"):
        hermes_note = effect.get("hermes_note")
    elif challenge_resolved_count >= minimum_resolved_sample and challenge_avg is not None:
        if challenge_avg <= 0:
            hermes_note = "challenged_intraday_alignment_has_negative_forward_return_review_hold_or_reduce_rule"
        else:
            hermes_note = "challenged_intraday_alignment_has_positive_forward_return_review_labeling_before_blocking"
    elif groups:
        hermes_note = "intraday_alignment_samples_below_threshold_keep_collecting_before_using_as_hard_rule"
    else:
        hermes_note = "intraday_alignment_learning_not_available_for_legacy_or_empty_report"

    return {
        "read_only": True,
        "submits_orders": False,
        "source": "strategy_learning.by_intraday_signal_alignment",
        "minimum_resolved_sample": minimum_resolved_sample,
        "minimum_sample_met": any((row.get("resolved_count") or 0) >= minimum_resolved_sample for row in groups),
        "evidence_status": effect.get("status") or ("INSUFFICIENT" if groups else "MISSING"),
        "evidence_reasons": effect.get("reasons") or [],
        "support_vs_challenge_delta_pct": effect.get("support_vs_challenge_delta_pct"),
        "policy": effect.get("policy") or "keep_alignment_read_only_until_forward_evidence_is_available",
        "supports_signal_like": effect.get("supports_signal_like") or by_key.get("supports_signal") or {},
        "groups": groups[:8],
        "supports_signal": by_key.get("supports_signal") or {},
        "challenges_signal": challenge,
        "hermes_note": hermes_note,
    }


def number_or_none(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def hermes_alpha_evidence_summary(effective_judgment_effect, judgment_audit_coverage, minimum_sample=5):
    effect = effective_judgment_effect if isinstance(effective_judgment_effect, dict) else {}
    coverage = judgment_audit_coverage if isinstance(judgment_audit_coverage, dict) else {}
    approved = effect.get("approved_or_reduced") if isinstance(effect.get("approved_or_reduced"), dict) else {}
    rejected = effect.get("rejected_or_held") if isinstance(effect.get("rejected_or_held"), dict) else {}
    sample_filter = effect.get("sample_filter") or "raw_judgment_decision"
    approved_count = int(number_or_none(approved.get("resolved_count")) or 0)
    rejected_count = int(number_or_none(rejected.get("resolved_count")) or 0)
    approved_avg = number_or_none(approved.get("avg_signed_return_pct"))
    rejected_avg = number_or_none(rejected.get("avg_signed_return_pct"))
    delta = round(approved_avg - rejected_avg, 6) if approved_avg is not None and rejected_avg is not None else None
    reasons = []
    if sample_filter != "judgment_audit_status_PASS":
        reasons.append("audit_pass_judgment_effect_missing_raw_effect_only")
    if coverage.get("audit_report_available") is False:
        reasons.append("judgment_audit_report_missing")
    if coverage.get("audit_report_truncated"):
        reasons.append("judgment_audit_report_truncated")
    if coverage.get("approved_or_reduced_audit_fail_or_missing_count"):
        reasons.append("approved_or_reduced_audit_fail_or_missing_present")
    if approved_count < minimum_sample:
        reasons.append("approved_or_reduced_audit_pass_sample_below_minimum")
    if rejected_count < minimum_sample:
        reasons.append("rejected_or_held_audit_pass_sample_below_minimum")
    if approved_avg is None:
        reasons.append("approved_or_reduced_avg_return_missing")
    elif approved_avg <= 0:
        reasons.append("approved_or_reduced_avg_return_not_positive")
    if delta is None:
        reasons.append("approval_vs_rejection_delta_missing")
    elif delta <= 0:
        reasons.append("approved_or_reduced_not_outperforming_rejected_or_held")

    if any(reason.endswith("sample_below_minimum") for reason in reasons) or any(
        reason in reasons
        for reason in (
            "audit_pass_judgment_effect_missing_raw_effect_only",
            "judgment_audit_report_missing",
            "judgment_audit_report_truncated",
        )
    ):
        status = "INSUFFICIENT"
    elif "approved_or_reduced_avg_return_not_positive" in reasons or "approved_or_reduced_not_outperforming_rejected_or_held" in reasons:
        status = "NEGATIVE"
    else:
        status = "SUPPORTIVE"

    if status == "SUPPORTIVE":
        hermes_note = "audit_pass_approved_or_reduced_judgments_outperform_rejected_or_held_with_minimum_sample"
    elif status == "NEGATIVE":
        hermes_note = "audit_pass_hermes_approval_effect_is_not_positive_or_not_outperforming_rejections"
    else:
        hermes_note = "hermes_alpha_evidence_unproven_until_audit_pass_samples_and_coverage_are_sufficient"
    return {
        "schema": "hermes_alpha_evidence_summary_v1",
        "read_only": True,
        "submits_orders": False,
        "status": status,
        "sample_filter": sample_filter,
        "minimum_sample": minimum_sample,
        "approved_or_reduced_resolved_count": approved_count,
        "rejected_or_held_resolved_count": rejected_count,
        "approved_or_reduced_avg_signed_return_pct": approved_avg,
        "rejected_or_held_avg_signed_return_pct": rejected_avg,
        "approval_vs_rejection_delta_pct": delta,
        "reasons": reasons,
        "hermes_note": hermes_note,
    }


def strategy_learning_brief(strategy_learning_payload, watchlist_diff_payload=None, outcome_report_payload=None):
    payload = strategy_learning_payload if isinstance(strategy_learning_payload, dict) else {}
    watchlist_diff_payload = watchlist_diff_payload if isinstance(watchlist_diff_payload, dict) else {}
    outcome_report_payload = outcome_report_payload if isinstance(outcome_report_payload, dict) else {}
    intake_coverage = payload.get("intake_coverage") if isinstance(payload.get("intake_coverage"), dict) else {}
    directional_coverage = intake_coverage.get("directional") if isinstance(intake_coverage.get("directional"), dict) else {}
    watch_coverage = intake_coverage.get("watch") if isinstance(intake_coverage.get("watch"), dict) else {}
    sizing_remediation = (
        payload.get("sizing_blocker_remediation")
        if isinstance(payload.get("sizing_blocker_remediation"), dict)
        else {}
    )
    proposal = watchlist_diff_payload.get("proposal") if isinstance(watchlist_diff_payload.get("proposal"), dict) else {}
    sample_scope = payload.get("sample_scope") if isinstance(payload.get("sample_scope"), dict) else {}
    overall = payload.get("overall") if isinstance(payload.get("overall"), dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    outcome_source = source.get("outcomes") if isinstance(source.get("outcomes"), dict) else {}
    outcome_maturity = (
        outcome_source.get("outcome_maturity")
        if isinstance(outcome_source.get("outcome_maturity"), dict)
        else {}
    )
    outcome_maturity = merged_outcome_maturity(outcome_maturity, outcome_report_payload)
    judgment_effect = payload.get("judgment_effect") if isinstance(payload.get("judgment_effect"), dict) else {}
    audit_pass_judgment_effect = (
        payload.get("audit_pass_judgment_effect")
        if isinstance(payload.get("audit_pass_judgment_effect"), dict)
        else {}
    )
    effective_judgment_effect = audit_pass_judgment_effect if audit_pass_judgment_effect else judgment_effect
    judgment_audit_coverage = (
        payload.get("judgment_audit_coverage")
        if isinstance(payload.get("judgment_audit_coverage"), dict)
        else {}
    )
    context_review_effect = (
        payload.get("context_review_effect")
        if isinstance(payload.get("context_review_effect"), dict)
        else {}
    )
    context_review_quality = (
        payload.get("context_review_quality")
        if isinstance(payload.get("context_review_quality"), dict)
        else {}
    )
    approved_effect = (
        effective_judgment_effect.get("approved_or_reduced")
        if isinstance(effective_judgment_effect.get("approved_or_reduced"), dict)
        else {}
    )
    rejected_effect = (
        effective_judgment_effect.get("rejected_or_held")
        if isinstance(effective_judgment_effect.get("rejected_or_held"), dict)
        else {}
    )
    raw_approved_effect = (
        judgment_effect.get("approved_or_reduced")
        if isinstance(judgment_effect.get("approved_or_reduced"), dict)
        else {}
    )
    raw_rejected_effect = (
        judgment_effect.get("rejected_or_held")
        if isinstance(judgment_effect.get("rejected_or_held"), dict)
        else {}
    )
    context_complete_effect = (
        context_review_effect.get("approved_or_reduced_context_complete")
        if isinstance(context_review_effect.get("approved_or_reduced_context_complete"), dict)
        else {}
    )
    context_incomplete_effect = (
        context_review_effect.get("approved_or_reduced_context_incomplete")
        if isinstance(context_review_effect.get("approved_or_reduced_context_incomplete"), dict)
        else {}
    )
    context_rejected_effect = (
        context_review_effect.get("rejected_or_held")
        if isinstance(context_review_effect.get("rejected_or_held"), dict)
        else {}
    )
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    missing_symbol_diagnostics = (
        outcome_maturity.get("missing_symbol_kline_diagnostics")
        if isinstance(outcome_maturity.get("missing_symbol_kline_diagnostics"), list)
        else []
    )
    missing_symbol_status_counts = {}
    missing_symbol_source_category_counts = {}
    for item in missing_symbol_diagnostics:
        if not isinstance(item, dict):
            continue
        key = str(item.get("status") or "unknown")
        try:
            affected = int(item.get("affected_signal_count") or 1)
        except (TypeError, ValueError):
            affected = 1
        missing_symbol_status_counts[key] = missing_symbol_status_counts.get(key, 0) + affected
        source_category = item.get("daily_gap_source_category")
        if source_category:
            source_category = str(source_category)
            missing_symbol_source_category_counts[source_category] = (
                missing_symbol_source_category_counts.get(source_category, 0) + affected
            )
    daily_gap_repair_context = (
        outcome_maturity.get("daily_gap_repair_context")
        if isinstance(outcome_maturity.get("daily_gap_repair_context"), dict)
        else {}
    )
    daily_gap_source_context = (
        outcome_maturity.get("daily_gap_source_diagnostic_context")
        if isinstance(outcome_maturity.get("daily_gap_source_diagnostic_context"), dict)
        else {}
    )
    if not missing_symbol_source_category_counts:
        context_counts = daily_gap_source_context.get("category_affected_signal_counts")
        if isinstance(context_counts, dict):
            missing_symbol_source_category_counts = dict(context_counts)
    covered = sizing_remediation.get("covered_by_watchlist_removal_count") or 0
    blocker_count = sizing_remediation.get("sizing_blocker_count") or 0
    uncovered = sizing_remediation.get("uncovered_count") or 0
    if blocker_count and covered == blocker_count and not uncovered:
        remediation_status = "fully_covered_by_manual_watchlist_proposal"
    elif blocker_count and covered:
        remediation_status = "partially_covered_by_manual_watchlist_proposal"
    elif blocker_count:
        remediation_status = "not_covered_by_current_watchlist_proposal"
    else:
        remediation_status = "no_sizing_blockers_in_learning_scope"
    alpha_summary = hermes_alpha_evidence_summary(effective_judgment_effect, judgment_audit_coverage)
    return {
        "schema": "hermes_strategy_learning_brief_v1",
        "read_only": True,
        "submits_orders": False,
        "sample_scope": {
            "mode": sample_scope.get("mode"),
            "strategy_config_id": sample_scope.get("strategy_config_id"),
            "watchlist_id": sample_scope.get("watchlist_id"),
            "joined_signal_count": sample_scope.get("joined_signal_count"),
            "excluded_joined_signal_count": sample_scope.get("excluded_joined_signal_count"),
        },
        "outcome_evidence": {
            "resolved_count": overall.get("resolved_count"),
            "avg_signed_return_pct": overall.get("avg_signed_return_pct"),
            "win_rate_pct": overall.get("win_rate_pct"),
            "minimum_sample_met": (overall.get("resolved_count") or 0) >= 5,
        },
        "outcome_maturity": {
            "primary_horizon": outcome_maturity.get("primary_horizon"),
            "needed_future_days": outcome_maturity.get("needed_future_days"),
            "latest_signal_date": outcome_maturity.get("latest_signal_date"),
            "latest_kline_date": outcome_maturity.get("latest_kline_date"),
            "pending_or_invalid_count": outcome_maturity.get("pending_or_invalid_count"),
            "min_missing_future_days_for_pending": outcome_maturity.get("min_missing_future_days_for_pending"),
            "max_missing_future_days_for_pending": outcome_maturity.get("max_missing_future_days_for_pending"),
            "earliest_primary_horizon_date_for_pending": outcome_maturity.get("earliest_primary_horizon_date_for_pending"),
            "missing_symbol_kline_count": outcome_maturity.get("missing_symbol_kline_count"),
            "missing_symbol_kline_unique_symbol_count": outcome_maturity.get("missing_symbol_kline_unique_symbol_count"),
            "no_future_daily_kline_count": outcome_maturity.get("no_future_daily_kline_count"),
            "missing_symbol_kline_status_counts": missing_symbol_status_counts,
            "daily_gap_source_category_affected_signal_counts": missing_symbol_source_category_counts,
            "daily_gap_repair_context": {
                "status": daily_gap_repair_context.get("status"),
                "plan_hash": daily_gap_repair_context.get("plan_hash"),
                "actionable_missing_symbol_count": daily_gap_repair_context.get("actionable_missing_symbol_count"),
                "unresolved_missing_symbol_count": daily_gap_repair_context.get("unresolved_missing_symbol_count"),
                "not_in_repair_plan_missing_symbol_count": daily_gap_repair_context.get(
                    "not_in_repair_plan_missing_symbol_count"
                ),
            },
            "daily_gap_source_diagnostic_context": {
                "status": daily_gap_source_context.get("status"),
                "classified_missing_symbol_count": daily_gap_source_context.get("classified_missing_symbol_count"),
                "unclassified_missing_symbol_count": daily_gap_source_context.get("unclassified_missing_symbol_count"),
                "category_counts": daily_gap_source_context.get("category_counts") or {},
                "confidence_counts": daily_gap_source_context.get("confidence_counts") or {},
                "category_affected_signal_counts": daily_gap_source_context.get(
                    "category_affected_signal_counts"
                )
                or {},
                "active_universe_or_mapping_missing_symbol_count": daily_gap_source_context.get(
                    "active_universe_or_mapping_missing_symbol_count"
                ),
                "provider_lag_missing_symbol_count": daily_gap_source_context.get(
                    "provider_lag_missing_symbol_count"
                ),
            },
            "missing_symbol_kline_diagnostics": missing_symbol_diagnostics[:20],
        },
        "intake_coverage": {
            "overall_pct": intake_coverage.get("coverage_pct"),
            "directional_pct": directional_coverage.get("coverage_pct"),
            "directional_joined_signal_count": directional_coverage.get("joined_signal_count"),
            "watch_pct": watch_coverage.get("coverage_pct"),
            "watch_joined_signal_count": watch_coverage.get("joined_signal_count"),
        },
        "judgment_effect": {
            "sample_filter": effective_judgment_effect.get("sample_filter") or "raw_judgment_decision",
            "approved_or_reduced": {
                "resolved_count": approved_effect.get("resolved_count"),
                "avg_signed_return_pct": approved_effect.get("avg_signed_return_pct"),
                "win_rate_pct": approved_effect.get("win_rate_pct"),
            },
            "rejected_or_held": {
                "resolved_count": rejected_effect.get("resolved_count"),
                "avg_signed_return_pct": rejected_effect.get("avg_signed_return_pct"),
                "win_rate_pct": rejected_effect.get("win_rate_pct"),
            },
        },
        "raw_judgment_effect": {
            "sample_filter": "raw_judgment_decision",
            "approved_or_reduced": {
                "resolved_count": raw_approved_effect.get("resolved_count"),
                "avg_signed_return_pct": raw_approved_effect.get("avg_signed_return_pct"),
                "win_rate_pct": raw_approved_effect.get("win_rate_pct"),
            },
            "rejected_or_held": {
                "resolved_count": raw_rejected_effect.get("resolved_count"),
                "avg_signed_return_pct": raw_rejected_effect.get("avg_signed_return_pct"),
                "win_rate_pct": raw_rejected_effect.get("win_rate_pct"),
            },
        },
        "judgment_audit_coverage": {
            "audit_report_available": judgment_audit_coverage.get("audit_report_available"),
            "audit_report_status": judgment_audit_coverage.get("audit_report_status"),
            "audit_report_truncated": judgment_audit_coverage.get("audit_report_truncated"),
            "joined_judgment_count": judgment_audit_coverage.get("joined_judgment_count"),
            "audit_pass_count": judgment_audit_coverage.get("audit_pass_count"),
            "audit_fail_count": judgment_audit_coverage.get("audit_fail_count"),
            "audit_missing_count": judgment_audit_coverage.get("audit_missing_count"),
            "approved_or_reduced_audit_pass_count": judgment_audit_coverage.get(
                "approved_or_reduced_audit_pass_count"
            ),
            "approved_or_reduced_audit_fail_or_missing_count": judgment_audit_coverage.get(
                "approved_or_reduced_audit_fail_or_missing_count"
            ),
            "rejected_or_held_audit_pass_count": judgment_audit_coverage.get(
                "rejected_or_held_audit_pass_count"
            ),
            "rejected_or_held_audit_fail_or_missing_count": judgment_audit_coverage.get(
                "rejected_or_held_audit_fail_or_missing_count"
            ),
            "failed_reason_counts": judgment_audit_coverage.get("failed_reason_counts") or [],
        },
        "hermes_alpha_evidence": alpha_summary,
        "context_review_effect": {
            "quality": {
                "approved_or_reduced_count": context_review_quality.get("approved_or_reduced_count"),
                "complete_context_review_count": context_review_quality.get("complete_context_review_count"),
                "incomplete_context_review_count": context_review_quality.get("incomplete_context_review_count"),
                "complete_context_review_pct": context_review_quality.get("complete_context_review_pct"),
                "missing_flag_counts": context_review_quality.get("missing_flag_counts") or [],
            },
            "approved_or_reduced_context_complete": {
                "resolved_count": context_complete_effect.get("resolved_count"),
                "avg_signed_return_pct": context_complete_effect.get("avg_signed_return_pct"),
                "win_rate_pct": context_complete_effect.get("win_rate_pct"),
            },
            "approved_or_reduced_context_incomplete": {
                "resolved_count": context_incomplete_effect.get("resolved_count"),
                "avg_signed_return_pct": context_incomplete_effect.get("avg_signed_return_pct"),
                "win_rate_pct": context_incomplete_effect.get("win_rate_pct"),
            },
            "rejected_or_held": {
                "resolved_count": context_rejected_effect.get("resolved_count"),
                "avg_signed_return_pct": context_rejected_effect.get("avg_signed_return_pct"),
                "win_rate_pct": context_rejected_effect.get("win_rate_pct"),
            },
        },
        "intraday_signal_alignment": strategy_learning_intraday_alignment_brief(payload),
        "sizing_blocker_remediation": {
            "status": remediation_status,
            "sizing_blocker_count": blocker_count,
            "covered_by_watchlist_removal_count": covered,
            "uncovered_count": uncovered,
            "covered_symbols": sizing_remediation.get("covered_symbols") or [],
            "uncovered_symbols": sizing_remediation.get("uncovered_symbols") or [],
            "watchlist_proposal_hash": sizing_remediation.get("watchlist_proposal_hash"),
            "proposed_watchlist_id": proposal.get("proposed_watchlist_id"),
            "current_watchlist_id": proposal.get("current_watchlist_id"),
            "manual_review_required": proposal.get("manual_review_required", True),
            "auto_applied": proposal.get("auto_applied", False),
            "does_not_restart_services": proposal.get("does_not_restart_services", True),
            "does_not_submit_orders": proposal.get("does_not_submit_orders", True),
        },
        "recommendations": recommendations[:12],
        "hermes_use": [
            "Use this brief to decide whether a signal is ready for judgment review or only useful for diagnostics.",
            "Treat low directional intake coverage as incomplete learning evidence.",
            "Use outcome_maturity to distinguish normal waiting for future daily K-lines from missing K-line pipeline coverage.",
            "Use daily-gap source diagnostics to distinguish repairable K-line gaps from active-universe, provider, or symbol-mapping defects.",
            "Use audit-pass judgment_effect for Hermes alpha review; raw_judgment_effect is diagnostic only.",
            "Treat audit-failed or audit-missing approvals as excluded from Hermes alpha even when their forward returns are positive.",
            "Treat context-reviewed approval outcomes as unproven until the complete-context cohort outperforms rejected/held judgments on resolved forward returns.",
            "Use intraday_signal_alignment as read-only learning evidence; daily K-lines remain the forward-return authority.",
            "Treat sizing blocker remediation as manual watchlist proposal context only; do not apply or restart services from this packet.",
        ],
    }


def simulation_trade_review_brief(portfolio_payload):
    payload = portfolio_payload if isinstance(portfolio_payload, dict) else {}
    reports = payload.get("portfolio_reports") if isinstance(payload.get("portfolio_reports"), list) else []
    sim_report = {}
    for report in reports:
        if isinstance(report, dict) and str(report.get("role") or "").lower() == "simulation":
            sim_report = report
            break
    risk = payload.get("portfolio_risk") if isinstance(payload.get("portfolio_risk"), dict) else {}
    risk_reports = risk.get("reports") if isinstance(risk.get("reports"), list) else []
    sim_risk = {}
    for report in risk_reports:
        if isinstance(report, dict) and str(report.get("role") or "").lower() == "simulation":
            sim_risk = report
            break
    unrealized = sim_risk.get("unrealized_pnl") if isinstance(sim_risk.get("unrealized_pnl"), dict) else {}
    review = (
        payload.get("simulation_trade_review")
        if isinstance(payload.get("simulation_trade_review"), dict)
        else {}
    )
    return {
        "schema": "hermes_simulation_trade_review_brief_v1",
        "read_only": True,
        "submits_orders": False,
        "portfolio_id": sim_report.get("portfolio_id") or review.get("portfolio_id"),
        "total_value_hkd": sim_report.get("total_value_hkd"),
        "return_pct_vs_initial": sim_report.get("return_pct_vs_initial"),
        "unrealized_pnl_pct_of_cost": unrealized.get("unrealized_pnl_pct_of_cost"),
        "lookback_days": review.get("lookback_days"),
        "trade_count": review.get("trade_count"),
        "closed_trade_count": review.get("closed_trade_count"),
        "closed_win_rate_pct": review.get("closed_win_rate_pct"),
        "closed_pnl_hkd_est": review.get("closed_pnl_hkd_est"),
        "largest_loss": review.get("largest_loss"),
        "largest_win": review.get("largest_win"),
        "review_notes": review.get("review_notes") or [],
        "hermes_use": [
            "Use this brief as realized simulation-trade context before approving new exposure.",
            "Do not treat positive paper signal outcomes as sufficient when realized simulation trade review is weak or missing.",
            "This brief is read-only and does not submit orders.",
        ],
    }


def stock_universe_hygiene_promotion_plan(universe_hygiene_payload):
    if universe_promote is None:
        return {
            "schema": "stock_universe_hygiene_promotion_report_v1",
            "mode": "dry-run",
            "status": "unavailable",
            "reason": "stock_universe_hygiene_promote_module_unavailable",
            "applied": False,
            "selected_count": 0,
            "operator_review_plan": {
                "schema": "stock_universe_hygiene_operator_review_plan_v1",
                "status": "unavailable",
                "review_required_count": 0,
                "items": [],
                "commands": [],
            },
            "safety": {
                "read_only_payload_build": True,
                "queries_database": False,
                "does_not_submit_orders": True,
                "does_not_restart_services": True,
                "does_not_change_watchlists": True,
                "does_not_change_stock_universe": True,
            },
        }
    try:
        return universe_promote.build_plan_from_report_payload(universe_hygiene_payload)
    except Exception as exc:
        return {
            "schema": "stock_universe_hygiene_promotion_report_v1",
            "mode": "dry-run",
            "status": "error",
            "reason": str(exc),
            "applied": False,
            "selected_count": 0,
            "operator_review_plan": {
                "schema": "stock_universe_hygiene_operator_review_plan_v1",
                "status": "error",
                "review_required_count": 0,
                "items": [],
                "commands": [],
            },
            "safety": {
                "read_only_payload_build": True,
                "queries_database": False,
                "does_not_submit_orders": True,
                "does_not_restart_services": True,
                "does_not_change_watchlists": True,
                "does_not_change_stock_universe": True,
            },
        }


def build_packet(
    alerts,
    health_payload=None,
    portfolio_payload=None,
    intake_results=None,
    judgment_file=None,
    source_alerts=None,
    alert_sample_scope=None,
    strategy_evidence_payload=None,
    alert_quality_payload=None,
    alert_event_store_payload=None,
    judgment_event_store_payload=None,
    intake_event_store_payload=None,
    outcome_event_store_payload=None,
    strategy_review_payload=None,
    strategy_learning_payload=None,
    execution_readiness_payload=None,
    simulation_performance_payload=None,
    external_market_context_payload=None,
    event_catalyst_payload=None,
    event_catalyst_signal_payload=None,
    market_sentiment_payload=None,
    fundamentals_context_payload=None,
    trusted_source_preflight_payload=None,
    cron_audit_payload=None,
    source_reliability_payload=None,
    operator_action_queue_payload=None,
    market_context_payload=None,
    data_health_payload=None,
    data_source_inventory_payload=None,
    kline_source_granularity_payload=None,
    intraday_kline_batch_payload=None,
    intraday_context_payload=None,
    intraday_timeframe_quality_payload=None,
    intraday_market_session_overrides_payload=None,
    kline_daily_gap_repair_payload=None,
    kline_gap_source_diagnostic_payload=None,
    kline_gap_alternate_provider_probe_payload=None,
    kline_gap_alternate_provider_repair_plan_payload=None,
    universe_payload=None,
    watchlist_diff_payload=None,
    universe_hygiene_payload=None,
    judgment_audit_payload=None,
    position_judgment_file=None,
    position_judgment_audit_payload=None,
):
    judgment_file = judgment_file or intake.JUDGMENT_FILE
    position_judgment_file = position_judgment_file or POSITION_JUDGMENT_FILE
    health_payload = health_payload if health_payload is not None else system_health_check.build_payload()
    portfolio_payload = (
        portfolio_payload
        if portfolio_payload is not None
        else portfolio_report.build_payload(
            sim_portfolio_id=portfolio_report.SIM_PORTFOLIO_ID,
            user_portfolio_ids=portfolio_report.USER_PORTFOLIO_IDS,
        )
    )
    if intake_results is None:
        intake_results = []
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, "rt_order_intake_state.json")
            intake_results = run_intake_dry_runs(alerts, state_file, judgment_file)

    health_status = health_payload.get("status", "UNKNOWN")
    alert_result_pairs = list(zip(alerts, intake_results))
    actionable_pairs = [
        (alert, result)
        for alert, result in alert_result_pairs
        if not is_non_actionable_observation(alert, result)
    ]
    observation_pairs = [
        (alert, result)
        for alert, result in alert_result_pairs
        if is_non_actionable_observation(alert, result)
    ]
    items = [review_item(alert, result, health_status) for alert, result in actionable_pairs]
    observations = [non_actionable_observation(alert, result) for alert, result in observation_pairs]
    source_alerts = source_alerts if source_alerts is not None else alerts
    if strategy_evidence_payload is None:
        strategy_evidence_payload = load_json_file(OUTCOME_REPORT_FILE)
    if alert_quality_payload is None:
        alert_quality_payload = load_json_file(ALERT_QUALITY_REPORT_FILE)
    if alert_event_store_payload is None:
        alert_event_store_payload = load_json_file(ALERT_EVENT_STORE_REPORT_FILE)
    if judgment_event_store_payload is None:
        judgment_event_store_payload = load_json_file(JUDGMENT_EVENT_STORE_REPORT_FILE)
    if intake_event_store_payload is None:
        intake_event_store_payload = load_json_file(INTAKE_EVENT_STORE_REPORT_FILE)
    if outcome_event_store_payload is None:
        outcome_event_store_payload = load_json_file(OUTCOME_EVENT_STORE_REPORT_FILE)
    if strategy_review_payload is None:
        strategy_review_payload = load_json_file(STRATEGY_REVIEW_REPORT_FILE)
    if strategy_learning_payload is None:
        strategy_learning_payload = load_json_file(STRATEGY_LEARNING_REPORT_FILE)
    if execution_readiness_payload is None:
        execution_readiness_payload = load_json_file(EXECUTION_READINESS_REPORT_FILE)
    if simulation_performance_payload is None:
        simulation_performance_payload = load_json_file(SIMULATION_PERFORMANCE_REPORT_FILE)
    if external_market_context_payload is None:
        external_market_context_payload = load_json_file(EXTERNAL_MARKET_CONTEXT_FILE)
    if event_catalyst_payload is None:
        event_catalyst_payload = load_json_file(EVENT_CATALYST_REPORT_FILE)
    if event_catalyst_signal_payload is None:
        event_catalyst_signal_payload = load_json_file(EVENT_CATALYST_SIGNAL_REPORT_FILE)
    if market_sentiment_payload is None:
        market_sentiment_payload = load_json_file(MARKET_SENTIMENT_REPORT_FILE)
    if fundamentals_context_payload is None:
        fundamentals_context_payload = load_json_file(FUNDAMENTALS_CONTEXT_REPORT_FILE)
    if trusted_source_preflight_payload is None:
        trusted_source_preflight_payload = load_json_file(TRUSTED_SOURCE_PREFLIGHT_REPORT_FILE)
    if cron_audit_payload is None:
        cron_audit_payload = load_json_file(CRON_AUDIT_REPORT_FILE)
    if source_reliability_payload is None:
        source_reliability_payload = load_json_file(SOURCE_RELIABILITY_REPORT_FILE)
    if operator_action_queue_payload is None:
        operator_action_queue_payload = load_json_file(OPERATOR_ACTION_QUEUE_REPORT_FILE)
    if market_context_payload is None:
        market_context_payload = load_json_file(MARKET_CONTEXT_FILE)
    if data_health_payload is None:
        data_health_payload = load_json_file(DATA_HEALTH_REPORT_FILE)
    if data_source_inventory_payload is None:
        data_source_inventory_payload = load_json_file(DATA_SOURCE_INVENTORY_REPORT_FILE)
    if kline_source_granularity_payload is None:
        kline_source_granularity_payload = load_json_file(KLINE_SOURCE_GRANULARITY_REPORT_FILE)
    if intraday_kline_batch_payload is None:
        intraday_kline_batch_payload = load_json_file(INTRADAY_KLINE_BATCH_REPORT_FILE)
    if intraday_context_payload is None:
        intraday_context_payload = load_json_file(INTRADAY_CONTEXT_REPORT_FILE)
    if intraday_timeframe_quality_payload is None:
        intraday_timeframe_quality_payload = load_json_file(INTRADAY_TIMEFRAME_QUALITY_REPORT_FILE)
    if intraday_market_session_overrides_payload is None:
        intraday_market_session_overrides_payload = load_json_file(INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE)
    if kline_daily_gap_repair_payload is None:
        kline_daily_gap_repair_payload = load_json_file(KLINE_DAILY_GAP_REPAIR_FILE)
    if kline_gap_source_diagnostic_payload is None:
        kline_gap_source_diagnostic_payload = load_json_file(KLINE_GAP_SOURCE_DIAGNOSTIC_FILE)
    if kline_gap_alternate_provider_probe_payload is None:
        kline_gap_alternate_provider_probe_payload = load_json_file(KLINE_GAP_ALTERNATE_PROVIDER_PROBE_FILE)
    if kline_gap_alternate_provider_repair_plan_payload is None:
        kline_gap_alternate_provider_repair_plan_payload = load_json_file(
            KLINE_GAP_ALTERNATE_PROVIDER_REPAIR_PLAN_FILE
        )
    if universe_payload is None:
        universe_payload = load_json_file(UNIVERSE_REPORT_FILE)
    if watchlist_diff_payload is None:
        watchlist_diff_payload = load_json_file(WATCHLIST_DIFF_REPORT_FILE)
    if universe_hygiene_payload is None:
        universe_hygiene_payload = load_json_file(UNIVERSE_HYGIENE_REPORT_FILE)
    if judgment_audit_payload is None:
        judgment_audit_payload = load_json_file(JUDGMENT_AUDIT_FILE)
    if position_judgment_audit_payload is None:
        position_judgment_audit_payload = load_json_file(POSITION_JUDGMENT_AUDIT_FILE)
    universe_hygiene_promotion_plan = stock_universe_hygiene_promotion_plan(universe_hygiene_payload)
    position_review_payload = enrich_position_review_with_context(
        portfolio_payload.get("position_review", {}),
        market_context_payload,
        intraday_context_payload,
        intraday_market_session_overrides_payload,
        external_market_context_payload,
        event_catalyst_payload,
        event_catalyst_signal_payload,
        market_sentiment_payload,
        fundamentals_context_payload,
        trusted_source_preflight_payload,
        source_reliability_payload,
    )
    packet_id = packet_id_for(alerts, health_payload)
    position_review_payload = attach_position_judgment_templates(
        position_review_payload,
        packet_id,
        position_judgment_file,
    )
    items = apply_execution_readiness_to_items(items, execution_readiness_payload)
    items = apply_portfolio_risk_to_items(items, portfolio_payload)
    items = apply_data_health_to_items(items, data_health_payload)
    items = apply_simulation_performance_to_items(items, simulation_performance_payload)
    items = apply_daily_gap_source_to_items(items, kline_gap_source_diagnostic_payload)
    items = attach_context_digests_to_items(
        items,
        market_context_payload,
        intraday_kline_batch_payload,
        intraday_context_payload,
        intraday_timeframe_quality_payload,
        intraday_market_session_overrides_payload,
        external_market_context_payload,
        event_catalyst_payload,
        event_catalyst_signal_payload,
        market_sentiment_payload,
        fundamentals_context_payload,
        trusted_source_preflight_payload,
        source_reliability_payload,
        kline_gap_source_diagnostic_payload,
    )
    return {
        "schema": "hermes_signal_review_packet_v1",
        "packet_id": packet_id,
        "generated_at": now_iso(),
        "execution_safety": {
            "review_only": True,
            "submits_orders": False,
            "intake_mode": "dry-run",
            "execute_path": "rt_order_intake.py --mode execute, gated by matching Hermes judgment",
        },
        "health": health_payload,
        "portfolio_context": portfolio_payload,
        "portfolio_risk": portfolio_payload.get("portfolio_risk", {}),
        "position_review": position_review_payload,
        "market_context": market_context_payload,
        "data_health": data_health_payload,
        "data_source_inventory": data_source_inventory_payload,
        "kline_source_granularity": kline_source_granularity_payload,
        "intraday_kline_batch": intraday_kline_batch_payload,
        "intraday_context": intraday_context_payload,
        "intraday_timeframe_quality": intraday_timeframe_quality_payload,
        "intraday_market_session_overrides": intraday_market_session_overrides_payload,
        "kline_daily_gap_repair": kline_daily_gap_repair_payload,
        "kline_gap_source_diagnostic": kline_gap_source_diagnostic_payload,
        "kline_gap_alternate_provider_probe": kline_gap_alternate_provider_probe_payload,
        "kline_gap_alternate_provider_repair_plan": kline_gap_alternate_provider_repair_plan_payload,
        "universe_context": universe_payload,
        "watchlist_diff": watchlist_diff_payload,
        "universe_hygiene": universe_hygiene_payload,
        "stock_universe_hygiene_promotion_plan": universe_hygiene_promotion_plan,
        "strategy_evidence": strategy_evidence_payload,
        "alert_quality_summary": alert_quality_payload,
        "alert_event_store": alert_event_store_payload,
        "judgment_event_store": judgment_event_store_payload,
        "order_intake_event_store": intake_event_store_payload,
        "signal_outcome_event_store": outcome_event_store_payload,
        "strategy_review": strategy_review_payload,
        "strategy_learning": strategy_learning_payload,
        "strategy_learning_brief": strategy_learning_brief(
            strategy_learning_payload,
            watchlist_diff_payload,
            strategy_evidence_payload,
        ),
        "simulation_trade_review_brief": simulation_trade_review_brief(portfolio_payload),
        "execution_readiness": execution_readiness_payload,
        "simulation_performance": simulation_performance_payload,
        "external_market_context": external_market_context_payload,
        "event_catalysts": event_catalyst_payload,
        "event_catalyst_signals": event_catalyst_signal_payload,
        "market_sentiment": market_sentiment_payload,
        "fundamentals_context": fundamentals_context_payload,
        "trusted_source_preflight": trusted_source_preflight_payload,
        "cron_audit": cron_audit_payload,
        "source_reliability": source_reliability_payload,
        "operator_action_queue": operator_action_queue_payload,
        "judgment_audit": judgment_audit_payload,
        "position_judgment_audit": position_judgment_audit_payload,
        "alert_selection": alert_selection_stats(source_alerts, alerts, sample_scope=alert_sample_scope),
        "review_items": items,
        "review_item_suppression": review_item_suppression_summary(
            alert_result_pairs,
            actionable_pairs,
            observation_pairs,
        ),
        "non_actionable_observations": observations,
        "non_actionable_observation_count": len(observations),
        "judgment_contract": judgment_contract(judgment_file),
        "position_judgment_contract": position_judgment_contract(position_judgment_file),
        "operator_notes": [
            "This packet is the input for Hermes judgment, not an execution command.",
            "Hermes should copy packet_id into every judgment so audits can resolve the exact packet version reviewed.",
            "Hermes should write judgments only for review_items that it explicitly reviewed.",
            "non_actionable_observations are visible for learning and diagnostics only; Hermes should not write trade judgments for them.",
            "Hermes may write position judgments for position_review.items, but those judgments are advisory and never submit orders.",
            "position_review.items[].position_judgment_template is a draft helper only; Hermes must replace placeholders before appending to the position judgment JSONL file.",
            "Universe context is for watchlist quality review only; do not auto-apply candidate watchlists without operator review.",
            "Watchlist diff is read-only proposal context; do not replace the live watchlist without manual review and service restart planning.",
            "Universe hygiene is for active-stock data quality review only; do not deactivate symbols without operator review.",
            "Stock-universe hygiene promotion plan is dry-run context only; manual stale-symbol actions require explicit symbol, explicit allow-action, matching proposal hash, and post-apply reruns.",
            "The promotion plan embedded here does not query the database, does not apply stock changes, does not change watchlists, does not restart services, and does not submit orders.",
            "Alert quality summary is read-only session diagnostics; it does not approve execution.",
            "Alert event store status is read-only durability/audit context; missing or dry-run status should not block packet generation but means alert history still depends on JSONL retention.",
            "Judgment event store status is read-only durability/audit context; Hermes should still write judgments to the JSONL contract and let the store persist them.",
            "Order intake event store status is read-only durability/audit context; it must not be interpreted as execution permission.",
            "Signal outcome event store status is read-only durability/audit context; strategy evidence gates still read rt_signal_outcome_report.json.",
            "Strategy review is read-only trigger policy context; execute mode still requires rt_order_intake.py gates.",
            "Strategy learning is read-only cohort evidence for improving prompts, triggers, and review discipline; it does not approve execution.",
            "Strategy learning brief is a top-level summary for Hermes attention only; the full strategy_learning object remains authoritative.",
            "Simulation trade review brief is realized simulation portfolio context; it does not approve execution by itself.",
            "Execution readiness is read-only dashboard context; READY is necessary but not sufficient for execute mode.",
            "rt_order_intake.py execute mode also enforces execution_readiness.status=READY before submitting orders.",
            "Simulation performance is read-only attribution context; FAIL means recent simulation behavior does not support new exposure.",
            "External market context is read-only news/macro/event/capital-flow context; missing or stale context means Hermes must state limited current-event awareness.",
            "Event catalysts are read-only watchlist-linked external context; they do not generate or approve v5 signals.",
            "Event catalyst signals are read-only review signals; they may support or challenge technical alerts but must never be submitted to order intake.",
            "Market sentiment is read-only quantified volatility/capital-flow/risk-appetite context; missing or stale context means Hermes must state limited sentiment awareness.",
            "Fundamentals context is read-only valuation/profitability/growth/leverage context; missing or stale context means Hermes must state limited fundamental awareness.",
            "Trusted source preflight is a read-only payload validator before Wudao, broker, official, sentiment, or fundamentals payloads are trusted; WARN/MISSING means Hermes must state source coverage limits, and FAIL means the payload should not be cited as evidence.",
            "Cron audit is read-only job wiring context; missing read-only jobs explain stale reports and any dangerous enabled execution job must block approval.",
            "Source reliability is a read-only matrix of context source completeness, fallback use, freshness, cron coverage, and provenance; degraded sources must reduce confidence and be discussed before approve/reduce.",
            "Data source inventory is a read-only visibility ledger of DB tables, K-line provenance, context files, and provider input payloads; it proves what the system can see, not whether that data is alpha.",
            "K-line source granularity is a dry-run/hash-gated provenance proposal; it can label minute rows as snapshot-only or full OHLCV, but it is not a signal, does not change prices or volumes, and does not grant execution permission.",
            "Operator action queue is a read-only remediation priority list; it does not install cron, write judgments, submit orders, or change portfolios by itself.",
            "review_items[].context_digest is a read-only attention layer that maps top-level context to a specific signal; it never changes eligibility or grants execution permission.",
            "position_review.items[].context_digest is advisory-only holding context for Hermes position judgments; it does not submit orders, create trade approvals, or change portfolio records.",
            "Data health is read-only integrity context; FAIL means Hermes should reject or hold until K-line, signal, or feature-run evidence is repaired.",
            "Intraday K-line batch is a dry-run/current-session minute collection plan; ACTIONABLE means rows may be available but are not proof of DB coverage until an operator hash-confirms apply and reruns intraday_context_report.py.",
            "Intraday context is read-only minute-bar confirmation/contradiction context; it must not replace daily K-lines, create trade signals, or relax execution gates.",
            "Intraday timeframe quality is read-only 5m/15m/30m/60m confirmation-quality context; limited, conflicting, snapshot-like, or low-fidelity evidence can cap confidence but must not raise confidence or override daily/readiness gates.",
            "Intraday market-session overrides are read-only holiday and half-day validation context; WARN/FAIL means exchange-calendar awareness is incomplete and intraday session interpretation must be treated cautiously.",
            "K-line daily gap repair is dry-run/manual remediation context only; do not apply it from this packet, and treat unresolved symbols as source, mapping, or universe-review issues.",
            "K-line gap source diagnostic classifies unresolved daily-gap symbols for operator review only; it does not repair K-lines, exclude evidence, change watchlists, or change the stock universe.",
            "K-line gap alternate-provider probe compares unresolved symbols against Yahoo daily chart only as read-only evidence; it does not use alternate rows for repairs or evidence exclusion.",
            "K-line gap alternate-provider repair plan is a read-only candidate-quality report; it has no manual apply command and must not be used as a DB repair instruction.",
            "Critical simulation portfolio_risk means the portfolio state is not trustworthy enough for approval.",
            "High-urgency position_review items should be handled before new BUY exposure is approved.",
            "Simulation execution remains disabled until the bridge is switched to alert-sim and execute gates pass.",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alert-json", help="one alert JSON object or list")
    parser.add_argument("--alert-file", help="JSON file containing one alert object or list")
    parser.add_argument("--queue-file", help="JSONL alert queue; defaults to RT_ALERT_QUEUE_FILE")
    parser.add_argument("--limit", type=int, default=DEFAULT_REVIEW_LIMIT, help="max review items after filtering")
    parser.add_argument(
        "--queue-scan-limit",
        type=int,
        default=DEFAULT_QUEUE_SCAN_LIMIT,
        help="max raw JSONL tail lines to scan before selecting review items",
    )
    parser.add_argument("--include-watch", action="store_true", help="include WATCH alerts in review_items")
    parser.add_argument(
        "--include-unconfirmed",
        action="store_true",
        help="include unconfirmed BUY/SELL alerts in review_items",
    )
    parser.add_argument("--sample-scope", choices=("current", "all"), default="current")
    parser.add_argument("--state-file", default=intake.STATE_FILE)
    parser.add_argument("--judgment-file", default=intake.JUDGMENT_FILE)
    parser.add_argument("--outcome-report-file", default=OUTCOME_REPORT_FILE)
    parser.add_argument("--alert-quality-file", default=ALERT_QUALITY_REPORT_FILE)
    parser.add_argument("--alert-event-store-file", default=ALERT_EVENT_STORE_REPORT_FILE)
    parser.add_argument("--judgment-event-store-file", default=JUDGMENT_EVENT_STORE_REPORT_FILE)
    parser.add_argument("--intake-event-store-file", default=INTAKE_EVENT_STORE_REPORT_FILE)
    parser.add_argument("--outcome-event-store-file", default=OUTCOME_EVENT_STORE_REPORT_FILE)
    parser.add_argument("--strategy-review-file", default=STRATEGY_REVIEW_REPORT_FILE)
    parser.add_argument("--strategy-learning-file", default=STRATEGY_LEARNING_REPORT_FILE)
    parser.add_argument("--simulation-performance-file", default=SIMULATION_PERFORMANCE_REPORT_FILE)
    parser.add_argument("--external-market-context-file", default=EXTERNAL_MARKET_CONTEXT_FILE)
    parser.add_argument("--event-catalyst-file", default=EVENT_CATALYST_REPORT_FILE)
    parser.add_argument("--event-catalyst-signal-file", default=EVENT_CATALYST_SIGNAL_REPORT_FILE)
    parser.add_argument("--market-sentiment-file", default=MARKET_SENTIMENT_REPORT_FILE)
    parser.add_argument("--fundamentals-context-file", default=FUNDAMENTALS_CONTEXT_REPORT_FILE)
    parser.add_argument("--trusted-source-preflight-file", default=TRUSTED_SOURCE_PREFLIGHT_REPORT_FILE)
    parser.add_argument("--cron-audit-file", default=CRON_AUDIT_REPORT_FILE)
    parser.add_argument("--source-reliability-file", default=SOURCE_RELIABILITY_REPORT_FILE)
    parser.add_argument("--operator-action-queue-file", default=OPERATOR_ACTION_QUEUE_REPORT_FILE)
    parser.add_argument("--market-context-file", default=MARKET_CONTEXT_FILE)
    parser.add_argument("--data-health-file", default=DATA_HEALTH_REPORT_FILE)
    parser.add_argument("--data-source-inventory-file", default=DATA_SOURCE_INVENTORY_REPORT_FILE)
    parser.add_argument("--kline-source-granularity-file", default=KLINE_SOURCE_GRANULARITY_REPORT_FILE)
    parser.add_argument("--intraday-kline-batch-file", default=INTRADAY_KLINE_BATCH_REPORT_FILE)
    parser.add_argument("--intraday-context-file", default=INTRADAY_CONTEXT_REPORT_FILE)
    parser.add_argument("--intraday-timeframe-quality-file", default=INTRADAY_TIMEFRAME_QUALITY_REPORT_FILE)
    parser.add_argument(
        "--intraday-market-session-overrides-file",
        default=INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE,
    )
    parser.add_argument("--kline-daily-gap-repair-file", default=KLINE_DAILY_GAP_REPAIR_FILE)
    parser.add_argument("--kline-gap-source-diagnostic-file", default=KLINE_GAP_SOURCE_DIAGNOSTIC_FILE)
    parser.add_argument("--kline-gap-alternate-provider-probe-file", default=KLINE_GAP_ALTERNATE_PROVIDER_PROBE_FILE)
    parser.add_argument(
        "--kline-gap-alternate-provider-repair-plan-file",
        default=KLINE_GAP_ALTERNATE_PROVIDER_REPAIR_PLAN_FILE,
    )
    parser.add_argument("--universe-report-file", default=UNIVERSE_REPORT_FILE)
    parser.add_argument("--watchlist-diff-file", default=WATCHLIST_DIFF_REPORT_FILE)
    parser.add_argument("--universe-hygiene-file", default=UNIVERSE_HYGIENE_REPORT_FILE)
    parser.add_argument("--execution-readiness-file", default=EXECUTION_READINESS_REPORT_FILE)
    parser.add_argument("--judgment-audit-file", default=JUDGMENT_AUDIT_FILE)
    parser.add_argument("--position-judgment-file", default=POSITION_JUDGMENT_FILE)
    parser.add_argument("--position-judgment-audit-file", default=POSITION_JUDGMENT_AUDIT_FILE)
    parser.add_argument("--output", default=PACKET_FILE)
    parser.add_argument("--archive-dir", default=PACKET_ARCHIVE_DIR)
    parser.add_argument("--no-archive", action="store_true", help="do not write packet snapshot archive")
    parser.add_argument("--stdout", action="store_true", help="print packet JSON to stdout")
    parser.add_argument(
        "--ephemeral-state",
        action="store_true",
        help="do not persist dry-run decisions to the intake dry_runs ledger",
    )
    parser.add_argument("--review-days", type=int, default=30)
    parser.add_argument("--sim-portfolio-id", type=int, default=portfolio_report.SIM_PORTFOLIO_ID)
    parser.add_argument("--user-portfolio-id", action="append", type=int, default=[])
    return parser.parse_args()


def main():
    args = parse_args()
    source_alerts = load_source_alerts(args.alert_json, args.alert_file, args.queue_file, args.queue_scan_limit)
    _scoped_source_alerts, alert_sample_scope = apply_sample_scope(source_alerts, sample_scope_mode=args.sample_scope)
    alerts = select_review_alerts(
        source_alerts,
        limit=args.limit,
        include_watch=args.include_watch,
        include_unconfirmed=args.include_unconfirmed,
        sample_scope_mode=args.sample_scope,
    )
    health_payload = system_health_check.build_payload()
    portfolio_payload = portfolio_report.build_payload(
        sim_portfolio_id=args.sim_portfolio_id,
        user_portfolio_ids=args.user_portfolio_id or portfolio_report.USER_PORTFOLIO_IDS,
        review_days=args.review_days,
    )

    if args.ephemeral_state:
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, "rt_order_intake_state.json")
            intake_results = run_intake_dry_runs(alerts, state_file, args.judgment_file)
    else:
        intake_results = run_intake_dry_runs(alerts, args.state_file, args.judgment_file)

    packet = build_packet(
        alerts,
        health_payload=health_payload,
        portfolio_payload=portfolio_payload,
        intake_results=intake_results,
        judgment_file=args.judgment_file,
        source_alerts=source_alerts,
        alert_sample_scope=alert_sample_scope,
        strategy_evidence_payload=load_json_file(args.outcome_report_file),
        alert_quality_payload=load_json_file(args.alert_quality_file),
        alert_event_store_payload=load_json_file(args.alert_event_store_file),
        judgment_event_store_payload=load_json_file(args.judgment_event_store_file),
        intake_event_store_payload=load_json_file(args.intake_event_store_file),
        outcome_event_store_payload=load_json_file(args.outcome_event_store_file),
        strategy_review_payload=load_json_file(args.strategy_review_file),
        strategy_learning_payload=load_json_file(args.strategy_learning_file),
        execution_readiness_payload=load_json_file(args.execution_readiness_file),
        simulation_performance_payload=load_json_file(args.simulation_performance_file),
        external_market_context_payload=load_json_file(args.external_market_context_file),
        event_catalyst_payload=load_json_file(args.event_catalyst_file),
        event_catalyst_signal_payload=load_json_file(args.event_catalyst_signal_file),
        market_sentiment_payload=load_json_file(args.market_sentiment_file),
        fundamentals_context_payload=load_json_file(args.fundamentals_context_file),
        trusted_source_preflight_payload=load_json_file(args.trusted_source_preflight_file),
        cron_audit_payload=load_json_file(args.cron_audit_file),
        source_reliability_payload=load_json_file(args.source_reliability_file),
        operator_action_queue_payload=load_json_file(args.operator_action_queue_file),
        market_context_payload=load_json_file(args.market_context_file),
        data_health_payload=load_json_file(args.data_health_file),
        data_source_inventory_payload=load_json_file(args.data_source_inventory_file),
        kline_source_granularity_payload=load_json_file(args.kline_source_granularity_file),
        intraday_kline_batch_payload=load_json_file(args.intraday_kline_batch_file),
        intraday_context_payload=load_json_file(args.intraday_context_file),
        intraday_timeframe_quality_payload=load_json_file(args.intraday_timeframe_quality_file),
        intraday_market_session_overrides_payload=load_json_file(args.intraday_market_session_overrides_file),
        kline_daily_gap_repair_payload=load_json_file(args.kline_daily_gap_repair_file),
        kline_gap_source_diagnostic_payload=load_json_file(args.kline_gap_source_diagnostic_file),
        kline_gap_alternate_provider_probe_payload=load_json_file(args.kline_gap_alternate_provider_probe_file),
        kline_gap_alternate_provider_repair_plan_payload=load_json_file(
            args.kline_gap_alternate_provider_repair_plan_file
        ),
        universe_payload=load_json_file(args.universe_report_file),
        watchlist_diff_payload=load_json_file(args.watchlist_diff_file),
        universe_hygiene_payload=load_json_file(args.universe_hygiene_file),
        judgment_audit_payload=load_json_file(args.judgment_audit_file),
        position_judgment_file=args.position_judgment_file,
        position_judgment_audit_payload=load_json_file(args.position_judgment_audit_file),
    )
    if args.output:
        save_json_atomic(args.output, packet)
    if not args.no_archive:
        archive_packet(packet, args.archive_dir)
    if args.stdout or not args.output:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
