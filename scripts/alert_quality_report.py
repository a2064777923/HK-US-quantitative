#!/usr/bin/env python3
"""Read-only session quality report for realtime v5 alerts."""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

try:
    import hermes_review_packet
    import rt_order_intake as intake
except ImportError:
    from scripts import hermes_review_packet
    from scripts import rt_order_intake as intake


ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
REPORT_FILE = os.environ.get("ALERT_QUALITY_REPORT_FILE", "/tmp/rt_alert_quality_report.json")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_jsonl_tail(path, limit):
    if not os.path.exists(path):
        return [], [f"missing_queue:{path}"]
    warnings = []
    alerts = []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()[-limit:] if limit and limit > 0 else f.readlines()
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"bad_jsonl_line:{idx}")
            continue
        if isinstance(item, dict):
            alerts.append(item)
    return alerts, warnings


def load_packet(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def alert_price(alert):
    return as_float(alert.get("price"), as_float(alert.get("entry_price")))


def signed_move_pct(alert, mark_price):
    start = alert_price(alert)
    if not start or not mark_price or start <= 0 or mark_price <= 0:
        return None
    raw = (mark_price / start - 1) * 100
    side = str(alert.get("signal_type", "")).upper()
    if side == "SELL":
        raw = -raw
    if side not in ("BUY", "SELL"):
        return None
    return raw


def attach_queue_marks(alerts):
    latest_by_symbol = {}
    marked = []
    for idx, alert in enumerate(alerts):
        symbol = str(alert.get("symbol", "")).upper()
        price = alert_price(alert)
        if symbol and price and price > 0:
            latest_by_symbol[symbol] = {
                "price": price,
                "index": idx,
                "generated_at": alert.get("generated_at"),
                "signal_id": intake.signal_id(alert),
            }
    for idx, alert in enumerate(alerts):
        symbol = str(alert.get("symbol", "")).upper()
        mark = latest_by_symbol.get(symbol)
        if not mark or mark["index"] <= idx:
            marked.append({**alert, "_mark": None})
            continue
        move = signed_move_pct(alert, mark["price"])
        marked.append(
            {
                **alert,
                "_mark": {
                    "price": mark["price"],
                    "generated_at": mark["generated_at"],
                    "signal_id": mark["signal_id"],
                    "signed_move_pct": round(move, 4) if move is not None else None,
                    "source": "latest_later_alert_in_queue",
                },
            }
        )
    return marked


def average(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def rate(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def validation_reasons(alerts):
    result = {}
    for alert in alerts:
        result[intake.signal_id(alert)] = intake.validate_alert(alert)
    return result


def packet_review_maps(packet):
    items = packet.get("review_items") if isinstance(packet, dict) else []
    by_id = {}
    reason_counts = Counter()
    status_counts = Counter()
    eligible_count = 0
    for item in items or []:
        sid = item.get("signal_id")
        if not sid:
            continue
        by_id[sid] = item
        if item.get("eligible_for_approval"):
            eligible_count += 1
        intake_payload = item.get("intake") or {}
        status_counts[intake_payload.get("status", "unknown")] += 1
        for reason in item.get("blocking_reasons") or []:
            reason_counts[reason] += 1
    return {
        "by_id": by_id,
        "eligible_count": eligible_count,
        "reason_counts": reason_counts,
        "status_counts": status_counts,
        "count": len(by_id),
    }


def trigger_key(alert):
    return (str(alert.get("signal_type", "UNKNOWN")).upper(), str(alert.get("trigger", "UNKNOWN")))


def infer_current_sample_scope(alerts, sample_scope_mode="current"):
    if sample_scope_mode == "all":
        return {
            "mode": "all_scanned_alerts",
            "strategy_config_id": None,
            "watchlist_id": None,
            "latest_signal_id": None,
        }
    for alert in reversed(alerts):
        if not hermes_review_packet.is_directional(alert):
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
    all_directional = [alert for alert in alerts if hermes_review_packet.is_directional(alert)]
    scoped_directional = [alert for alert in scoped if hermes_review_packet.is_directional(alert)]
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


def summarize_triggers(directional, validations, packet_maps):
    grouped = defaultdict(list)
    for alert in directional:
        grouped[trigger_key(alert)].append(alert)

    rows = []
    for (side, trigger), items in grouped.items():
        signal_ids = [intake.signal_id(alert) for alert in items]
        confirmed = [alert for alert in items if alert.get("confirmed") is True]
        validation_pass = [sid for sid in signal_ids if not validations.get(sid)]
        packet_items = [packet_maps["by_id"].get(sid) for sid in signal_ids if packet_maps["by_id"].get(sid)]
        eligible = [item for item in packet_items if item.get("eligible_for_approval")]
        moves = [
            (alert.get("_mark") or {}).get("signed_move_pct")
            for alert in items
            if alert.get("_mark") is not None
        ]
        rows.append(
            {
                "signal_type": side,
                "trigger": trigger,
                "count": len(items),
                "confirmed_count": len(confirmed),
                "confirmed_rate_pct": rate(len(confirmed), len(items)),
                "validation_pass_count": len(validation_pass),
                "validation_pass_rate_pct": rate(len(validation_pass), len(items)),
                "packet_review_count": len(packet_items),
                "packet_eligible_count": len(eligible),
                "avg_full_score": average([as_float(alert.get("full_score")) for alert in items]),
                "avg_rr_ratio": average([as_float(alert.get("rr_ratio")) for alert in items]),
                "marked_count": len([m for m in moves if m is not None]),
                "avg_signed_move_pct": average(moves),
            }
        )
    return sorted(rows, key=lambda row: (-row["count"], row["signal_type"], row["trigger"]))


def symbol_conflicts(directional):
    sides = defaultdict(Counter)
    for alert in directional:
        symbol = str(alert.get("symbol", "")).upper()
        side = str(alert.get("signal_type", "")).upper()
        if symbol and side in ("BUY", "SELL"):
            sides[symbol][side] += 1
    conflicts = []
    for symbol, counts in sides.items():
        if counts.get("BUY", 0) and counts.get("SELL", 0):
            conflicts.append({"symbol": symbol, "buy_count": counts["BUY"], "sell_count": counts["SELL"]})
    return sorted(conflicts, key=lambda item: (-(item["buy_count"] + item["sell_count"]), item["symbol"]))


def build_recommendations(summary):
    recs = []
    total = summary["counts"]["total_alerts"]
    watch = summary["counts"]["by_signal_type"].get("WATCH", 0)
    missing_watchlist = summary["directional_quality"].get("missing_watchlist_metadata_count", 0)
    missing_strategy = summary["directional_quality"].get("missing_strategy_config_metadata_count", 0)
    if total and watch / total > 0.5:
        recs.append("watch_alerts_dominate_queue_review_packet_should_filter_directional_alerts")
    if missing_watchlist:
        recs.append("directional_alerts_missing_watchlist_metadata_restart_v5_with_configured_watchlist")
    if missing_strategy:
        recs.append("directional_alerts_missing_strategy_config_metadata_restart_v5_with_configured_strategy")
    if summary["directional_quality"]["validation_pass_rate_pct"] < 50 and summary["counts"]["directional_alerts"]:
        recs.append("directional_validation_pass_rate_below_50pct_review_v5_confirmation_thresholds")
    if summary["packet_review"]["eligible_rate_pct"] < 25 and summary["packet_review"]["review_item_count"]:
        recs.append("packet_eligible_rate_below_25pct_keep_alert_sim_disabled")
    noisy = [
        row
        for row in summary["trigger_quality"]
        if row["count"] >= 3 and row["avg_signed_move_pct"] is not None and row["avg_signed_move_pct"] < 0
    ]
    for row in noisy[:5]:
        recs.append(f"negative_marked_move:{row['signal_type']}:{row['trigger']}")
    if not recs:
        recs.append("continue_shadow_observation_collect_more_session_data")
    return recs


def report_status(summary):
    total = summary.get("total_alert_count", 0)
    recommendations = summary.get("recommendations") or []
    if total == 0:
        return "NO_SIGNALS"
    if recommendations == ["continue_shadow_observation_collect_more_session_data"]:
        return "OK"
    return "WARN"


def build_report(alerts, packet=None, sample_scope_mode="current"):
    packet = packet or {}
    scoped_alerts, sample_scope = apply_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    marked_alerts = attach_queue_marks(scoped_alerts)
    directional = [alert for alert in marked_alerts if hermes_review_packet.is_directional(alert)]
    validations = validation_reasons(directional)
    packet_maps = packet_review_maps(packet)

    by_type = Counter(str(alert.get("signal_type", "UNKNOWN")).upper() or "UNKNOWN" for alert in marked_alerts)
    by_market = Counter(str(alert.get("market", "UNKNOWN")).upper() or "UNKNOWN" for alert in marked_alerts)
    by_watchlist_source = Counter(str(alert.get("watchlist_source") or "missing") for alert in marked_alerts)
    by_watchlist_id = Counter(str(alert.get("watchlist_id") or "missing") for alert in marked_alerts)
    by_strategy_config_source = Counter(str(alert.get("strategy_config_source") or "missing") for alert in marked_alerts)
    by_strategy_config_id = Counter(str(alert.get("strategy_config_id") or "missing") for alert in marked_alerts)
    validation_fail_reasons = Counter()
    for reasons in validations.values():
        for reason in reasons:
            validation_fail_reasons[reason] += 1

    confirmed_directional = [alert for alert in directional if alert.get("confirmed") is True]
    missing_watchlist_metadata = [
        alert
        for alert in directional
        if not alert.get("watchlist_id") or not alert.get("watchlist_source")
    ]
    missing_strategy_metadata = [
        alert
        for alert in directional
        if not alert.get("strategy_config_id") or not alert.get("strategy_config_source")
    ]
    validation_pass_count = len([sid for sid, reasons in validations.items() if not reasons])
    marked_moves = [
        (alert.get("_mark") or {}).get("signed_move_pct")
        for alert in directional
        if alert.get("_mark") is not None
    ]
    packet_review_count = packet_maps["count"]
    total_alert_count = len(marked_alerts)
    directional_alert_count = len(directional)
    watch_alert_count = by_type.get("WATCH", 0)
    confirmed_directional_count = len(confirmed_directional)
    symbol_conflict_rows = symbol_conflicts(directional)

    summary = {
        "schema": "alert_quality_report_v1",
        "generated_at": now_iso(),
        "sample_scope": sample_scope,
        "total_alert_count": total_alert_count,
        "directional_alert_count": directional_alert_count,
        "watch_alert_count": watch_alert_count,
        "confirmed_directional_count": confirmed_directional_count,
        "validation_pass_count": validation_pass_count,
        "packet_review_item_count": packet_review_count,
        "packet_eligible_count": packet_maps["eligible_count"],
        "symbol_conflict_count": len(symbol_conflict_rows),
        "source": {
            "queue_mark_source": "latest later same-symbol alert in scanned JSONL; diagnostic only",
            "packet_generated_at": packet.get("generated_at"),
        },
        "counts": {
            "total_alerts": total_alert_count,
            "directional_alerts": directional_alert_count,
            "by_signal_type": dict(by_type),
            "by_market": dict(by_market),
            "by_watchlist_source": dict(by_watchlist_source),
            "by_watchlist_id": dict(by_watchlist_id),
            "by_strategy_config_source": dict(by_strategy_config_source),
            "by_strategy_config_id": dict(by_strategy_config_id),
        },
        "directional_quality": {
            "confirmed_count": confirmed_directional_count,
            "confirmed_rate_pct": rate(confirmed_directional_count, directional_alert_count),
            "missing_watchlist_metadata_count": len(missing_watchlist_metadata),
            "missing_watchlist_metadata_rate_pct": rate(len(missing_watchlist_metadata), directional_alert_count),
            "missing_strategy_config_metadata_count": len(missing_strategy_metadata),
            "missing_strategy_config_metadata_rate_pct": rate(len(missing_strategy_metadata), directional_alert_count),
            "validation_pass_count": validation_pass_count,
            "validation_pass_rate_pct": rate(validation_pass_count, directional_alert_count),
            "avg_full_score": average([as_float(alert.get("full_score")) for alert in directional]),
            "avg_rr_ratio": average([as_float(alert.get("rr_ratio")) for alert in directional]),
            "marked_count": len([m for m in marked_moves if m is not None]),
            "avg_signed_move_pct": average(marked_moves),
            "positive_mark_rate_pct": rate(len([m for m in marked_moves if m is not None and m > 0]), len([m for m in marked_moves if m is not None])),
            "validation_fail_reasons": dict(validation_fail_reasons),
        },
        "packet_review": {
            "review_item_count": packet_review_count,
            "eligible_count": packet_maps["eligible_count"],
            "eligible_rate_pct": rate(packet_maps["eligible_count"], packet_review_count),
            "status_counts": dict(packet_maps["status_counts"]),
            "blocking_reasons": dict(packet_maps["reason_counts"]),
        },
        "trigger_quality": summarize_triggers(directional, validations, packet_maps),
        "symbol_conflicts": symbol_conflict_rows,
    }
    summary["recommendations"] = build_recommendations(summary)
    summary["primary_recommendation"] = summary["recommendations"][0] if summary["recommendations"] else None
    summary["status"] = report_status(summary)
    return summary


def build_text_report(payload):
    counts = payload["counts"]
    dq = payload["directional_quality"]
    pr = payload["packet_review"]
    scope = payload.get("sample_scope") or {}
    lines = [
        f"Alert quality report {payload['generated_at']}",
        (
            f"sample_scope={scope.get('mode')} strategy_config={scope.get('strategy_config_id')} "
            f"watchlist={scope.get('watchlist_id')} excluded={scope.get('excluded_alert_count', 0)}"
        ),
        f"alerts={counts['total_alerts']} directional={counts['directional_alerts']} types={counts['by_signal_type']}",
        f"watchlist_sources={counts.get('by_watchlist_source', {})}",
        f"strategy_config_sources={counts.get('by_strategy_config_source', {})}",
        (
            f"directional confirmed={dq['confirmed_count']} ({dq['confirmed_rate_pct']:.1f}%) "
            f"missing_watchlist={dq.get('missing_watchlist_metadata_count', 0)} "
            f"validation_pass={dq['validation_pass_count']} ({dq['validation_pass_rate_pct']:.1f}%) "
            f"avg_score={dq['avg_full_score']} avg_rr={dq['avg_rr_ratio']}"
        ),
        (
            f"packet review_items={pr['review_item_count']} eligible={pr['eligible_count']} "
            f"({pr['eligible_rate_pct']:.1f}%)"
        ),
    ]
    top = payload["trigger_quality"][:8]
    if top:
        lines.append("Top triggers:")
        for row in top:
            lines.append(
                f"  {row['signal_type']} {row['trigger']}: n={row['count']} "
                f"confirmed={row['confirmed_rate_pct']:.0f}% eligible={row['packet_eligible_count']} "
                f"avg_move={row['avg_signed_move_pct']}"
            )
    if payload["symbol_conflicts"]:
        conflicts = payload["symbol_conflicts"][:8]
        lines.append("Conflicts: " + ", ".join(f"{x['symbol']} B{x['buy_count']}/S{x['sell_count']}" for x in conflicts))
    lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-file", default=ALERT_QUEUE_FILE)
    parser.add_argument("--packet-file", default=PACKET_FILE)
    parser.add_argument("--scan-limit", type=int, default=2000)
    parser.add_argument("--sample-scope", choices=("current", "all"), default="current")
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--send-feishu", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    alerts, warnings = load_jsonl_tail(args.queue_file, args.scan_limit)
    packet = load_packet(args.packet_file)
    payload = build_report(alerts, packet, sample_scope_mode=args.sample_scope)
    payload["warnings"] = warnings
    if args.output:
        save_json_atomic(args.output, payload)

    text = build_text_report(payload)
    if args.send_feishu:
        try:
            from feishu_notify import send_feishu_message

            send_feishu_message(text)
        except Exception as exc:
            payload["feishu_error"] = str(exc)
            if args.output:
                save_json_atomic(args.output, payload)

    if args.text:
        print(text)
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
