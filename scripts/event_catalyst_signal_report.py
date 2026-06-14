#!/usr/bin/env python3
"""Read-only event-driven review signals for Hermes.

This report converts watchlist-linked event catalysts into review signals that
Hermes can use to support or criticize technical v5 alerts. It never writes the
v5 alert queue and never submits orders.
"""
import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime


EVENT_CATALYST_REPORT_FILE = os.environ.get("EVENT_CATALYST_REPORT_FILE", "/tmp/event_catalyst_report.json")
ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
REPORT_FILE = os.environ.get("EVENT_CATALYST_SIGNAL_REPORT_FILE", "/tmp/event_catalyst_signal_report.json")
DEFAULT_QUEUE_SCAN_LIMIT = int(os.environ.get("EVENT_CATALYST_SIGNAL_QUEUE_SCAN_LIMIT", "500"))
DEFAULT_ALERT_WINDOW_MINUTES = int(os.environ.get("EVENT_CATALYST_SIGNAL_ALERT_WINDOW_MINUTES", "240"))
DEFAULT_MAX_RELATED_ALERTS = int(os.environ.get("EVENT_CATALYST_SIGNAL_MAX_RELATED_ALERTS", "8"))
DEFAULT_SAMPLE_SCOPE_MODE = os.environ.get("EVENT_CATALYST_SIGNAL_SAMPLE_SCOPE", "current")


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


def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def load_jsonl_tail(path, limit):
    if not path or limit <= 0 or not os.path.exists(path):
        return [], [] if path and os.path.exists(path) else [f"alert_queue_missing:{path}"]
    warnings = []
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except Exception as exc:
        return [], [f"alert_queue_read_failed:{exc}"]
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except json.JSONDecodeError:
            warnings.append(f"alert_queue_bad_line:{idx}")
    return rows, warnings


def normalize_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
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


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def stable_id(parts):
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def catalyst_symbols(candidate):
    return normalize_list(candidate.get("matched_symbols"))


def catalyst_markets(candidate):
    return normalize_list(candidate.get("matched_markets"))


def alert_side(alert):
    side = str(alert.get("signal_type") or "").upper()
    return side if side in ("BUY", "SELL") else None


def alert_signal_id(alert):
    return str(alert.get("signal_id") or "").strip()


def alert_timestamp(alert):
    for key in ("generated_at", "quote_time", "time", "created_at"):
        parsed = parse_timestamp(alert.get(key))
        if parsed:
            return parsed
    return None


def catalyst_timestamp(candidate):
    return parse_timestamp(candidate.get("published_at"))


def is_directional_alert(alert):
    return alert_side(alert) in ("BUY", "SELL")


def alert_scope_key(alert):
    return {
        "strategy_config_id": alert.get("strategy_config_id"),
        "watchlist_id": alert.get("watchlist_id"),
    }


def infer_current_sample_scope(alerts, sample_scope_mode=DEFAULT_SAMPLE_SCOPE_MODE):
    if sample_scope_mode == "all":
        return {
            "mode": "all_scanned_alerts",
            "strategy_config_id": None,
            "watchlist_id": None,
            "latest_signal_id": None,
        }
    for alert in reversed(alerts or []):
        if not is_directional_alert(alert):
            continue
        strategy_config_id = alert.get("strategy_config_id")
        watchlist_id = alert.get("watchlist_id")
        if strategy_config_id and watchlist_id:
            return {
                "mode": "latest_strategy_config_and_watchlist",
                "strategy_config_id": str(strategy_config_id),
                "watchlist_id": str(watchlist_id),
                "latest_signal_id": alert_signal_id(alert) or None,
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


def event_delta_minutes(candidate, alert):
    event_time = catalyst_timestamp(candidate)
    alert_time = alert_timestamp(alert)
    if not event_time or not alert_time:
        return None
    return round((alert_time - event_time).total_seconds() / 60.0, 2)


def alert_within_event_window(candidate, alert, window_minutes):
    if not window_minutes or window_minutes <= 0:
        return True
    delta = event_delta_minutes(candidate, alert)
    if delta is None:
        return False
    return abs(delta) <= window_minutes


def dedupe_alerts(alerts):
    by_key = {}
    for alert in alerts or []:
        key = alert_signal_id(alert)
        if not key:
            key = "|".join(
                [
                    str(alert.get("market") or "").upper(),
                    str(alert.get("symbol") or "").upper(),
                    str(alert.get("signal_type") or "").upper(),
                    str(alert.get("trigger") or ""),
                    str(alert.get("generated_at") or ""),
                ]
            )
        existing = by_key.get(key)
        if not existing or (alert_timestamp(alert) or datetime.min) >= (alert_timestamp(existing) or datetime.min):
            by_key[key] = alert
    return list(by_key.values())


def sort_related_alerts(candidate, alerts):
    event_time = catalyst_timestamp(candidate)

    def sort_key(alert):
        alert_time = alert_timestamp(alert)
        if event_time and alert_time:
            return (abs((alert_time - event_time).total_seconds()), -(alert_time.timestamp()))
        if alert_time:
            return (float("inf"), -(alert_time.timestamp()))
        return (float("inf"), 0)

    return sorted(alerts or [], key=sort_key)


def apply_sample_scope(alerts, sample_scope_mode=DEFAULT_SAMPLE_SCOPE_MODE):
    scope = infer_current_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    scoped = [alert for alert in alerts or [] if alert_matches_scope(alert, scope)]
    directional = [alert for alert in alerts or [] if is_directional_alert(alert)]
    scoped_directional = [alert for alert in scoped if is_directional_alert(alert)]
    scope.update(
        {
            "raw_alert_count_before_filter": len(alerts or []),
            "raw_alert_count": len(scoped),
            "excluded_alert_count": len(alerts or []) - len(scoped),
            "directional_alert_count_before_filter": len(directional),
            "directional_alert_count": len(scoped_directional),
            "excluded_directional_alert_count": len(directional) - len(scoped_directional),
        }
    )
    return scoped, scope


def relevance_reason(candidate, alert):
    symbols = set(catalyst_symbols(candidate))
    markets = set(catalyst_markets(candidate))
    symbol = str(alert.get("symbol") or "").upper()
    market = str(alert.get("market") or "").upper()
    if symbol and symbol in symbols:
        return "symbol_match"
    if not symbols and market and market in markets:
        return "market_match"
    return ""


def relevant_alerts(
    candidate,
    alerts,
    sample_scope=None,
    alert_window_minutes=DEFAULT_ALERT_WINDOW_MINUTES,
    max_related_alerts=DEFAULT_MAX_RELATED_ALERTS,
):
    symbols = set(catalyst_symbols(candidate))
    markets = set(catalyst_markets(candidate))
    result = []
    for alert in alerts or []:
        if not is_directional_alert(alert):
            continue
        if sample_scope is not None and not alert_matches_scope(alert, sample_scope):
            continue
        if not alert_within_event_window(candidate, alert, alert_window_minutes):
            continue
        symbol = str(alert.get("symbol") or "").upper()
        market = str(alert.get("market") or "").upper()
        if symbol and symbol in symbols:
            result.append(alert)
        elif not symbols and market and market in markets:
            result.append(alert)
    result = sort_related_alerts(candidate, dedupe_alerts(result))
    if max_related_alerts and max_related_alerts > 0:
        return result[:max_related_alerts]
    return result


def event_direction(candidate):
    sentiment = str(candidate.get("sentiment") or "unknown").lower()
    if sentiment == "positive":
        return "positive_catalyst"
    if sentiment == "negative":
        return "negative_catalyst"
    if sentiment == "mixed":
        return "mixed_catalyst"
    return "context_catalyst"


def review_signal_type(candidate, related_alerts):
    sentiment = str(candidate.get("sentiment") or "unknown").lower()
    sides = {alert_side(alert) for alert in related_alerts if alert_side(alert)}
    if sentiment == "positive":
        if "BUY" in sides:
            return "SUPPORT_BUY_REVIEW"
        if "SELL" in sides:
            return "CHALLENGE_SELL_REVIEW"
        return "POSITIVE_CATALYST_REVIEW"
    if sentiment == "negative":
        if "BUY" in sides:
            return "CHALLENGE_BUY_REVIEW"
        if "SELL" in sides:
            return "SUPPORT_SELL_REVIEW"
        return "NEGATIVE_CATALYST_REVIEW"
    if "BUY" in sides or "SELL" in sides:
        return "MIXED_CATALYST_REVIEW"
    return "CONTEXT_CATALYST_REVIEW"


def split_related_alerts_for_review(candidate, related_alerts):
    related_alerts = list(related_alerts or [])
    if not related_alerts:
        return [related_alerts]
    sentiment = str(candidate.get("sentiment") or "unknown").lower()
    if sentiment not in ("positive", "negative"):
        return [related_alerts]
    grouped = []
    for side in ("BUY", "SELL"):
        side_alerts = [alert for alert in related_alerts if alert_side(alert) == side]
        if side_alerts:
            grouped.append(side_alerts)
    return grouped or [related_alerts]


def review_priority(candidate, related_alerts):
    impact = as_float(candidate.get("impact_score"), 0.0) or 0.0
    sentiment = str(candidate.get("sentiment") or "").lower()
    if sentiment == "negative" and related_alerts:
        return "critical"
    if sentiment == "negative" or impact >= 0.9:
        return "high"
    if related_alerts:
        return "medium"
    return "low"


def build_signal(candidate, related_alerts):
    related_ids = [str(alert.get("signal_id") or "") for alert in related_alerts if alert.get("signal_id")]
    symbols = catalyst_symbols(candidate)
    markets = catalyst_markets(candidate)
    signal_type = review_signal_type(candidate, related_alerts)
    event_id = candidate.get("id") or stable_id(candidate)
    signal_id = "event:{date}:{event_id}:{digest}".format(
        date=str(candidate.get("published_at") or now_iso())[:10].replace("-", ""),
        event_id=str(event_id)[:40],
        digest=stable_id({"event_id": event_id, "related": related_ids, "type": signal_type}),
    )
    return {
        "schema": "event_catalyst_signal_v1",
        "signal_id": signal_id,
        "source": "event_catalyst_signal_report",
        "event_catalyst_id": event_id,
        "review_signal_type": signal_type,
        "direction": event_direction(candidate),
        "priority": review_priority(candidate, related_alerts),
        "scope": candidate.get("scope"),
        "symbols": symbols,
        "markets": markets,
        "sentiment": candidate.get("sentiment"),
        "impact_score": candidate.get("impact_score"),
        "title": candidate.get("title"),
        "summary": candidate.get("summary"),
        "published_at": candidate.get("published_at"),
        "age_minutes": candidate.get("age_minutes"),
        "url": candidate.get("url"),
        "related_v5_signal_ids": related_ids,
        "related_v5_alerts": [
            {
                "signal_id": alert.get("signal_id"),
                "symbol": alert.get("symbol"),
                "market": alert.get("market"),
                "signal_type": alert.get("signal_type"),
                "trigger": alert.get("trigger"),
                "confirmed": alert.get("confirmed"),
                "full_score": alert.get("full_score"),
                "generated_at": alert.get("generated_at"),
                "event_delta_minutes": event_delta_minutes(candidate, alert),
                "relevance_reason": relevance_reason(candidate, alert),
                **alert_scope_key(alert),
            }
            for alert in related_alerts
        ],
        "execution_candidate": False,
        "eligible_for_order_intake": False,
        "hermes_instruction": (
            "Use this event signal to support, challenge, or contextualize technical v5 alerts. "
            "Do not submit orders from this signal."
        ),
    }


def build_signals_for_candidate(candidate, related_alerts):
    return [build_signal(candidate, group) for group in split_related_alerts_for_review(candidate, related_alerts)]


def build_recommendations(status, signals):
    if status == "MISSING":
        return ["wire_event_catalyst_report_before_event_signal_review"]
    if status == "FAIL":
        return ["repair_event_catalyst_report_before_event_signal_review"]
    negative = [row for row in signals if row.get("direction") == "negative_catalyst"]
    challenge_buy = [row for row in signals if row.get("review_signal_type") == "CHALLENGE_BUY_REVIEW"]
    support_buy = [row for row in signals if row.get("review_signal_type") == "SUPPORT_BUY_REVIEW"]
    recs = []
    if challenge_buy:
        recs.append("require_hermes_to_challenge_related_buy_signals_with_negative_event_context")
    if negative:
        recs.append("require_explicit_event_risk_notes_before_new_exposure")
    if support_buy:
        recs.append("positive_event_signals_may_support_but_not_override_technical_and_readiness_gates")
    if not signals:
        recs.append("no_event_driven_review_signals_detected")
    if not recs:
        recs.append("event_driven_review_signals_available_for_hermes_context")
    return recs


def report_status(event_catalysts, signals):
    event_status = str((event_catalysts or {}).get("status") or "MISSING").upper()
    if event_status == "FAIL":
        return "FAIL"
    if event_status in ("MISSING", "STALE"):
        return event_status
    if any(row.get("review_signal_type") == "CHALLENGE_BUY_REVIEW" for row in signals):
        return "RISK"
    if signals:
        return "OK"
    return "OK"


def build_report(
    event_catalysts=None,
    alerts=None,
    queue_file=ALERT_QUEUE_FILE,
    scan_limit=DEFAULT_QUEUE_SCAN_LIMIT,
    alert_window_minutes=DEFAULT_ALERT_WINDOW_MINUTES,
    max_related_alerts=DEFAULT_MAX_RELATED_ALERTS,
    sample_scope_mode=DEFAULT_SAMPLE_SCOPE_MODE,
):
    warnings = []
    if event_catalysts is None:
        event_catalysts = load_json_file(EVENT_CATALYST_REPORT_FILE)
        if not event_catalysts:
            warnings.append(f"event_catalyst_report_missing_or_invalid:{EVENT_CATALYST_REPORT_FILE}")
    if alerts is None:
        alerts, queue_warnings = load_jsonl_tail(queue_file, scan_limit)
        warnings.extend(queue_warnings)
    candidates = event_catalysts.get("candidates") if isinstance(event_catalysts.get("candidates"), list) else []
    _scoped_alerts, sample_scope = apply_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    timestampless_directional_count = len(
        [alert for alert in alerts or [] if is_directional_alert(alert) and not alert_timestamp(alert)]
    )
    if timestampless_directional_count:
        warnings.append(f"directional_alerts_missing_timestamps:{timestampless_directional_count}")
    signals = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        related = relevant_alerts(
            candidate,
            alerts,
            sample_scope=sample_scope,
            alert_window_minutes=alert_window_minutes,
            max_related_alerts=max_related_alerts,
        )
        signals.extend(build_signals_for_candidate(candidate, related))
    signals = sorted(
        signals,
        key=lambda item: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(item.get("priority"), 9),
            -as_float(item.get("impact_score"), 0.0),
            item.get("published_at") or "",
        ),
    )
    status = report_status(event_catalysts, signals)
    by_type = Counter(row["review_signal_type"] for row in signals)
    by_priority = Counter(row["priority"] for row in signals)
    related_signal_ids = {
        signal_id
        for row in signals
        for signal_id in (row.get("related_v5_signal_ids") or [])
        if signal_id
    }
    related_count = len(related_signal_ids)
    return {
        "schema": "event_catalyst_signal_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "writes_alert_queue": False,
            "changes_strategy": False,
            "changes_watchlists": False,
            "event_catalyst_report_file": EVENT_CATALYST_REPORT_FILE,
            "alert_queue_file": queue_file,
            "alert_queue_scan_limit": scan_limit,
            "alert_event_window_minutes": alert_window_minutes,
            "max_related_alerts_per_event": max_related_alerts,
            "sample_scope_mode": sample_scope_mode,
        },
        "summary": {
            "event_catalyst_status": event_catalysts.get("status"),
            "candidate_count": len(candidates),
            "signal_count": len(signals),
            "related_v5_signal_count": related_count,
            "by_review_signal_type": dict(by_type),
            "by_priority": dict(by_priority),
            "alert_sample_scope": sample_scope,
            "timestampless_directional_alert_count": timestampless_directional_count,
        },
        "signals": signals[:50],
        "recommendations": build_recommendations(status, signals),
        "warnings": warnings + list(event_catalysts.get("warnings") or []),
        "hermes_use": [
            "These are event-driven review signals, not order signals.",
            "Use related_v5_signal_ids to support or challenge technical alerts in the same packet.",
            "A negative event signal related to a BUY should force explicit opposing_factors/risk_notes before any approval.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Event catalyst signal report {payload['generated_at']} status={payload['status']}",
        (
            f"signals={summary.get('signal_count')} "
            f"related_v5={summary.get('related_v5_signal_count')} "
            f"types={summary.get('by_review_signal_type')}"
        ),
    ]
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-catalyst-file", default=EVENT_CATALYST_REPORT_FILE)
    parser.add_argument("--queue-file", default=ALERT_QUEUE_FILE)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_QUEUE_SCAN_LIMIT)
    parser.add_argument("--alert-window-minutes", type=int, default=DEFAULT_ALERT_WINDOW_MINUTES)
    parser.add_argument("--max-related-alerts", type=int, default=DEFAULT_MAX_RELATED_ALERTS)
    parser.add_argument("--sample-scope", choices=("current", "all"), default=DEFAULT_SAMPLE_SCOPE_MODE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global EVENT_CATALYST_REPORT_FILE
    EVENT_CATALYST_REPORT_FILE = args.event_catalyst_file
    payload = build_report(
        queue_file=args.queue_file,
        scan_limit=args.scan_limit,
        alert_window_minutes=args.alert_window_minutes,
        max_related_alerts=args.max_related_alerts,
        sample_scope_mode=args.sample_scope,
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
