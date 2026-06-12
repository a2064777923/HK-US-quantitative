#!/usr/bin/env python3
"""Read-only forward outcome report for realtime v5 alerts.

This is intentionally conservative: hit analysis uses daily klines strictly
after the alert's quote date, so it does not pretend to know intraday order
sequence from a daily candle.
"""
import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime

try:
    import rt_order_intake as intake
except ImportError:
    from scripts import rt_order_intake as intake


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
DEFAULT_HORIZONS = tuple(
    int(x.strip())
    for x in os.environ.get("RT_SIGNAL_OUTCOME_HORIZONS", "1,3,5").split(",")
    if x.strip().isdigit() and int(x.strip()) > 0
)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def run_cmd(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def psql(sql, timeout=60):
    return run_cmd(
        [
            "docker",
            "exec",
            DB_CONTAINER,
            "psql",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-t",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
        timeout=timeout,
    )


def rows(stdout):
    return [line.rstrip("\n").split("\t") for line in stdout.splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def metadata_value(value, default="missing"):
    if value in (None, ""):
        return default
    return str(value)


def round_or_none(value, digits=4):
    return round(value, digits) if value is not None else None


def pct(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def average(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def load_jsonl_tail(path, limit):
    if not os.path.exists(path):
        return [], [f"missing_queue:{path}"]
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if limit and limit > 0:
        lines = lines[-limit:]

    warnings = []
    alerts = []
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


def signal_side(alert):
    return str(alert.get("signal_type", "")).upper()


def is_directional(alert):
    return signal_side(alert) in ("BUY", "SELL")


def entry_price(alert):
    return as_float(alert.get("entry_price"), as_float(alert.get("price")))


def parse_date(value):
    if not value:
        return ""
    text = str(value).strip()
    if len(text) >= 10:
        candidate = text[:10]
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
            return candidate
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return ""


def alert_signal_date(alert):
    return parse_date(alert.get("quote_time")) or parse_date(alert.get("generated_at"))


def signed_return_pct(side, entry, mark):
    if not entry or not mark or entry <= 0 or mark <= 0:
        return None
    raw = (mark / entry - 1) * 100
    return -raw if side == "SELL" else raw


def threshold_state(side, row, stop_loss, take_profit):
    high = as_float(row.get("high"))
    low = as_float(row.get("low"))
    stop = as_float(stop_loss)
    take = as_float(take_profit)
    target_hit = False
    stop_hit = False
    if high is None or low is None:
        return target_hit, stop_hit
    if side == "BUY":
        target_hit = take is not None and high >= take
        stop_hit = stop is not None and low <= stop
    elif side == "SELL":
        target_hit = take is not None and low <= take
        stop_hit = stop is not None and high >= stop
    return target_hit, stop_hit


def window_path_metrics(side, entry, window):
    highs = [as_float(row.get("high")) for row in window if as_float(row.get("high")) is not None]
    lows = [as_float(row.get("low")) for row in window if as_float(row.get("low")) is not None]
    if not entry or entry <= 0 or not highs or not lows:
        return None, None
    if side == "BUY":
        favorable = (max(highs) / entry - 1) * 100
        adverse = (1 - min(lows) / entry) * 100
    else:
        favorable = (1 - min(lows) / entry) * 100
        adverse = (max(highs) / entry - 1) * 100
    return favorable, adverse


def first_threshold_hit(side, window, stop_loss, take_profit):
    target_seen = False
    stop_seen = False
    first_hit = None
    for row in window:
        target_hit, stop_hit = threshold_state(side, row, stop_loss, take_profit)
        target_seen = target_seen or target_hit
        stop_seen = stop_seen or stop_hit
        if first_hit is None:
            if target_hit and stop_hit:
                first_hit = "ambiguous_same_day"
            elif target_hit:
                first_hit = "target"
            elif stop_hit:
                first_hit = "stop"
    return target_seen, stop_seen, first_hit


def evaluate_alert(alert, klines, horizons=DEFAULT_HORIZONS):
    side = signal_side(alert)
    sid = intake.signal_id(alert)
    symbol = str(alert.get("symbol", "")).upper()
    signal_date = alert_signal_date(alert)
    entry = entry_price(alert)
    base = {
        "signal_id": sid,
        "symbol": symbol,
        "market": alert.get("market"),
        "signal_type": side,
        "trigger": alert.get("trigger"),
        "confirmed": alert.get("confirmed"),
        "full_score": as_float(alert.get("full_score")),
        "rr_ratio": as_float(alert.get("rr_ratio")),
        "entry_price": entry,
        "stop_loss": as_float(alert.get("stop_loss")),
        "take_profit": as_float(alert.get("take_profit")),
        "signal_date": signal_date,
        "quote_time": alert.get("quote_time"),
        "generated_at": alert.get("generated_at"),
        "watchlist_id": alert.get("watchlist_id"),
        "watchlist_source": alert.get("watchlist_source"),
        "watchlist_count": alert.get("watchlist_count"),
        "strategy_config_id": alert.get("strategy_config_id"),
        "strategy_config_source": alert.get("strategy_config_source"),
        "strategy_config_version": alert.get("strategy_config_version"),
        "available_future_days": 0,
        "latest_kline_date": None,
        "status": "pending",
        "outcomes": {},
    }
    if side not in ("BUY", "SELL"):
        base["status"] = "skipped"
        base["reason"] = "not_directional"
        return base
    if not symbol:
        base["status"] = "invalid"
        base["reason"] = "missing_symbol"
        return base
    if not signal_date:
        base["status"] = "invalid"
        base["reason"] = "missing_signal_date"
        return base
    if entry is None or entry <= 0:
        base["status"] = "invalid"
        base["reason"] = "missing_entry_price"
        return base

    ordered = sorted([row for row in klines if row.get("date")], key=lambda row: row["date"])
    future = [row for row in ordered if row["date"] > signal_date]
    base["available_future_days"] = len(future)
    base["latest_kline_date"] = ordered[-1]["date"] if ordered else None
    if not ordered:
        base["status"] = "pending"
        base["reason"] = "missing_symbol_klines"
        return base
    if not future:
        base["status"] = "pending"
        base["reason"] = "no_future_daily_klines"
        return base

    any_resolved = False
    for horizon in sorted(set(horizons)):
        key = f"{horizon}d"
        if len(future) < horizon:
            base["outcomes"][key] = {
                "status": "pending",
                "available_future_days": len(future),
                "needed_future_days": horizon,
            }
            continue
        mark = future[horizon - 1]
        window = future[:horizon]
        close_return = signed_return_pct(side, entry, as_float(mark.get("close")))
        favorable, adverse = window_path_metrics(side, entry, window)
        target_hit, stop_hit, first_hit = first_threshold_hit(
            side,
            window,
            alert.get("stop_loss"),
            alert.get("take_profit"),
        )
        base["outcomes"][key] = {
            "status": "resolved",
            "mark_date": mark["date"],
            "mark_close": as_float(mark.get("close")),
            "signed_close_return_pct": round_or_none(close_return),
            "win": close_return is not None and close_return > 0,
            "max_favorable_pct": round_or_none(favorable),
            "max_adverse_pct": round_or_none(adverse),
            "target_hit": target_hit,
            "stop_hit": stop_hit,
            "first_hit": first_hit,
        }
        any_resolved = True
    if any_resolved:
        base["status"] = "resolved"
        base.pop("reason", None)
    return base


def parse_kline_rows(stdout):
    parsed = []
    for row in rows(stdout):
        if len(row) < 5:
            continue
        parsed.append(
            {
                "date": row[0],
                "open": as_float(row[1]),
                "high": as_float(row[2]),
                "low": as_float(row[3]),
                "close": as_float(row[4]),
            }
        )
    return parsed


def fetch_klines(symbol_min_dates):
    klines = {}
    warnings = []
    for symbol, min_date in sorted(symbol_min_dates.items()):
        sql = f"""
            SELECT timestamp::date, open_price, high_price, low_price, close_price
            FROM klines
            WHERE interval = 'day'
              AND symbol = '{sql_quote(symbol)}'
              AND timestamp::date >= '{sql_quote(min_date)}'::date
            ORDER BY timestamp ASC
        """
        r = psql(sql)
        if r.returncode != 0:
            warnings.append(f"kline_query_failed:{symbol}:{r.stderr.strip()}")
            klines[symbol] = []
            continue
        klines[symbol] = parse_kline_rows(r.stdout)
    return klines, warnings


def dedupe_directional_alerts(alerts):
    seen = set()
    out = []
    duplicates = 0
    for alert in alerts:
        if not is_directional(alert):
            continue
        sid = intake.signal_id(alert)
        if sid in seen:
            duplicates += 1
            continue
        seen.add(sid)
        out.append(alert)
    return out, duplicates


def symbol_min_dates(alerts):
    result = {}
    for alert in alerts:
        symbol = str(alert.get("symbol", "")).upper()
        date = alert_signal_date(alert)
        if not symbol or not date:
            continue
        result[symbol] = min(result.get(symbol, date), date)
    return result


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


def horizon_metrics(evaluations, horizon_key):
    resolved = []
    pending = 0
    for item in evaluations:
        outcome = (item.get("outcomes") or {}).get(horizon_key)
        if not outcome:
            pending += 1
            continue
        if outcome.get("status") == "resolved":
            resolved.append(outcome)
        else:
            pending += 1
    returns = [outcome.get("signed_close_return_pct") for outcome in resolved]
    favorable = [outcome.get("max_favorable_pct") for outcome in resolved]
    adverse = [outcome.get("max_adverse_pct") for outcome in resolved]
    avg_favorable = average(favorable)
    avg_adverse = average(adverse)
    first_hits = Counter(outcome.get("first_hit") or "none" for outcome in resolved)
    return {
        "resolved_count": len(resolved),
        "pending_count": pending,
        "avg_signed_close_return_pct": average(returns),
        "avg_max_favorable_pct": avg_favorable,
        "avg_max_adverse_pct": avg_adverse,
        "favorable_to_adverse_ratio": round(avg_favorable / avg_adverse, 4)
        if avg_favorable is not None and avg_adverse not in (None, 0)
        else None,
        "win_rate_pct": pct(len([x for x in returns if x is not None and x > 0]), len([x for x in returns if x is not None])),
        "target_hit_rate_pct": pct(len([x for x in resolved if x.get("target_hit")]), len(resolved)),
        "stop_hit_rate_pct": pct(len([x for x in resolved if x.get("stop_hit")]), len(resolved)),
        "first_hit_counts": dict(first_hits),
    }


def value_counts(items, field, default="missing"):
    return dict(Counter(metadata_value(item.get(field), default=default) for item in items))


def summarize_groups(evaluations, key_fn, horizons, metadata_fn=None):
    grouped = defaultdict(list)
    for item in evaluations:
        key = key_fn(item)
        if key:
            grouped[key].append(item)
    rows_out = []
    for key, items in grouped.items():
        row = {
            "key": key,
            "count": len(items),
            "confirmed_count": len([item for item in items if item.get("confirmed") is True]),
            "avg_full_score": average([item.get("full_score") for item in items]),
            "avg_rr_ratio": average([item.get("rr_ratio") for item in items]),
            "horizons": {},
        }
        if metadata_fn is not None:
            row.update(metadata_fn(items))
        for horizon in horizons:
            row["horizons"][f"{horizon}d"] = horizon_metrics(items, f"{horizon}d")
        rows_out.append(row)
    return sorted(rows_out, key=lambda row: (-row["count"], row["key"]))


def strategy_group_metadata(items):
    return {
        "source_counts": value_counts(items, "strategy_config_source"),
        "version_counts": value_counts(items, "strategy_config_version"),
    }


def watchlist_group_metadata(items):
    return {
        "source_counts": value_counts(items, "watchlist_source"),
        "watchlist_count_values": value_counts(items, "watchlist_count"),
    }


def strategy_trigger_group_metadata(items):
    first = items[0] if items else {}
    return {
        "strategy_config_id": metadata_value(first.get("strategy_config_id")),
        "strategy_config_source_counts": value_counts(items, "strategy_config_source"),
        "strategy_config_version_counts": value_counts(items, "strategy_config_version"),
        "trigger_key": f"{first.get('signal_type')}:{first.get('trigger') or 'UNKNOWN'}",
    }


def build_recommendations(payload):
    recs = []
    h1 = payload["overall"]["horizons"].get("1d", {})
    resolved_1d = h1.get("resolved_count", 0)
    if resolved_1d == 0:
        recs.append("outcome_sample_not_ready_keep_collecting_daily_klines")
    elif resolved_1d < 30:
        recs.append("outcome_sample_below_30_keep_shadow_mode")
    elif h1.get("avg_signed_close_return_pct") is not None and h1["avg_signed_close_return_pct"] <= 0:
        recs.append("one_day_average_return_non_positive_review_v5_filters")

    for row in payload["by_trigger"]:
        metric = row["horizons"].get("1d", {})
        if row["count"] >= 5 and metric.get("resolved_count", 0) >= 5:
            avg_return = metric.get("avg_signed_close_return_pct")
            if avg_return is not None and avg_return < 0:
                recs.append(f"negative_1d_trigger:{row['key']}")
            if metric.get("stop_hit_rate_pct", 0) > metric.get("target_hit_rate_pct", 0):
                recs.append(f"stop_hits_exceed_targets:{row['key']}")

    if not recs:
        recs.append("continue_shadow_observation_before_enabling_alert_sim")
    return recs


def primary_horizon_key(horizons):
    return "1d" if 1 in horizons else f"{horizons[0]}d"


def report_status(evaluated_count, primary_metric):
    resolved = primary_metric.get("resolved_count", 0)
    avg_return = primary_metric.get("avg_signed_close_return_pct")
    if evaluated_count == 0:
        return "NO_SIGNALS"
    if resolved == 0:
        return "PENDING"
    if resolved < 30:
        return "INSUFFICIENT_SAMPLE"
    if avg_return is not None and avg_return <= 0:
        return "WARN"
    return "OK"


def build_report(alerts, klines_by_symbol=None, horizons=DEFAULT_HORIZONS, sample_scope_mode="current"):
    horizons = tuple(sorted(set(int(h) for h in horizons if int(h) > 0))) or DEFAULT_HORIZONS
    scoped_alerts, sample_scope = apply_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    directional, duplicate_count = dedupe_directional_alerts(scoped_alerts)
    fetch_warnings = []
    if klines_by_symbol is None:
        klines_by_symbol, fetch_warnings = fetch_klines(symbol_min_dates(directional))
    evaluations = [
        evaluate_alert(alert, klines_by_symbol.get(str(alert.get("symbol", "")).upper(), []), horizons=horizons)
        for alert in directional
    ]
    overall_horizons = {f"{horizon}d": horizon_metrics(evaluations, f"{horizon}d") for horizon in horizons}
    pending_reasons = Counter(item.get("reason", "none") for item in evaluations if item.get("status") != "resolved")
    raw_alert_count = len(scoped_alerts)
    directional_alert_count = len([alert for alert in scoped_alerts if is_directional(alert)])
    evaluated_signal_count = len(evaluations)
    resolved_signal_count = len([item for item in evaluations if item.get("status") == "resolved"])
    pending_or_invalid_count = len([item for item in evaluations if item.get("status") != "resolved"])
    primary_horizon = primary_horizon_key(horizons)
    primary_horizon_metric = overall_horizons.get(primary_horizon, {})
    payload = {
        "schema": "rt_signal_outcome_report_v1",
        "generated_at": now_iso(),
        "sample_scope": sample_scope,
        "status": report_status(evaluated_signal_count, primary_horizon_metric),
        "raw_alert_count": raw_alert_count,
        "directional_alert_count": directional_alert_count,
        "evaluated_signal_count": evaluated_signal_count,
        "duplicate_signal_count": duplicate_count,
        "resolved_signal_count": resolved_signal_count,
        "pending_signal_count": pending_or_invalid_count,
        "pending_or_invalid_count": pending_or_invalid_count,
        "pending_reasons": dict(pending_reasons),
        "primary_horizon": primary_horizon,
        "primary_horizon_metric": primary_horizon_metric,
        "source": {
            "alert_queue_file": ALERT_QUEUE_FILE,
            "price_source": "klines daily rows after alert quote_date",
            "hit_analysis_note": "daily bars cannot order same-day stop/target hits; ambiguous_same_day means both levels touched in one daily candle",
            "horizons": list(horizons),
        },
        "counts": {
            "raw_alert_count": raw_alert_count,
            "directional_alert_count": directional_alert_count,
            "evaluated_signal_count": evaluated_signal_count,
            "duplicate_signal_count": duplicate_count,
            "by_signal_type": dict(Counter(signal_side(alert) or "UNKNOWN" for alert in scoped_alerts)),
            "missing_watchlist_metadata_count": len(
                [item for item in evaluations if not item.get("watchlist_id") or not item.get("watchlist_source")]
            ),
            "missing_strategy_config_metadata_count": len(
                [
                    item
                    for item in evaluations
                    if not item.get("strategy_config_id") or not item.get("strategy_config_source")
                ]
            ),
        },
        "overall": {
            "resolved_signal_count": resolved_signal_count,
            "pending_or_invalid_count": pending_or_invalid_count,
            "pending_reasons": dict(pending_reasons),
            "horizons": overall_horizons,
        },
        "by_confirmation": summarize_groups(
            evaluations,
            lambda item: "confirmed" if item.get("confirmed") is True else "unconfirmed",
            horizons,
        ),
        "by_trigger": summarize_groups(
            evaluations,
            lambda item: f"{item.get('signal_type')}:{item.get('trigger') or 'UNKNOWN'}",
            horizons,
        ),
        "by_strategy_config": summarize_groups(
            evaluations,
            lambda item: metadata_value(item.get("strategy_config_id")),
            horizons,
            metadata_fn=strategy_group_metadata,
        ),
        "by_watchlist": summarize_groups(
            evaluations,
            lambda item: metadata_value(item.get("watchlist_id")),
            horizons,
            metadata_fn=watchlist_group_metadata,
        ),
        "by_strategy_config_trigger": summarize_groups(
            evaluations,
            lambda item: (
                f"{metadata_value(item.get('strategy_config_id'))}|"
                f"{item.get('signal_type')}:{item.get('trigger') or 'UNKNOWN'}"
            ),
            horizons,
            metadata_fn=strategy_trigger_group_metadata,
        ),
        "by_symbol": summarize_groups(evaluations, lambda item: item.get("symbol"), horizons),
        "evaluations": evaluations,
        "recent_evaluations": evaluations[-50:],
        "warnings": fetch_warnings,
    }
    payload["recommendations"] = build_recommendations(payload)
    payload["primary_recommendation"] = payload["recommendations"][0] if payload["recommendations"] else None
    return payload


def build_text_report(payload):
    counts = payload["counts"]
    overall = payload["overall"]
    h1 = overall["horizons"].get("1d", {})
    scope = payload.get("sample_scope") or {}
    lines = [
        f"Signal outcome report {payload['generated_at']}",
        (
            f"sample_scope={scope.get('mode')} strategy_config={scope.get('strategy_config_id')} "
            f"watchlist={scope.get('watchlist_id')} excluded={scope.get('excluded_alert_count', 0)}"
        ),
        (
            f"raw={counts['raw_alert_count']} directional={counts['directional_alert_count']} "
            f"evaluated={counts['evaluated_signal_count']} duplicates={counts['duplicate_signal_count']}"
        ),
        (
            f"1d resolved={h1.get('resolved_count', 0)} pending={h1.get('pending_count', 0)} "
            f"avg={h1.get('avg_signed_close_return_pct')} win={h1.get('win_rate_pct')}%"
        ),
    ]
    if overall["pending_reasons"]:
        lines.append("Pending: " + ", ".join(f"{k}={v}" for k, v in overall["pending_reasons"].items()))
    top = payload["by_trigger"][:8]
    if top:
        lines.append("Top triggers:")
        for row in top:
            metric = row["horizons"].get("1d", {})
            lines.append(
                f"  {row['key']}: n={row['count']} resolved={metric.get('resolved_count', 0)} "
                f"avg1d={metric.get('avg_signed_close_return_pct')} win={metric.get('win_rate_pct')}%"
            )
    top_configs = payload.get("by_strategy_config", [])[:5]
    if top_configs:
        lines.append("Top strategy configs:")
        for row in top_configs:
            metric = row["horizons"].get("1d", {})
            versions = ",".join(sorted((row.get("version_counts") or {}).keys()))
            lines.append(
                f"  {row['key']}: n={row['count']} resolved={metric.get('resolved_count', 0)} "
                f"avg1d={metric.get('avg_signed_close_return_pct')} versions={versions}"
            )
    top_watchlists = payload.get("by_watchlist", [])[:5]
    if top_watchlists:
        lines.append("Top watchlists:")
        for row in top_watchlists:
            metric = row["horizons"].get("1d", {})
            sources = ",".join(sorted((row.get("source_counts") or {}).keys()))
            lines.append(
                f"  {row['key']}: n={row['count']} resolved={metric.get('resolved_count', 0)} "
                f"avg1d={metric.get('avg_signed_close_return_pct')} sources={sources}"
            )
    lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_horizons(value):
    parsed = []
    for item in str(value).split(","):
        item = item.strip()
        if item.isdigit() and int(item) > 0:
            parsed.append(int(item))
    return tuple(parsed) or DEFAULT_HORIZONS


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-file", default=ALERT_QUEUE_FILE)
    parser.add_argument("--scan-limit", type=int, default=5000)
    parser.add_argument("--horizons", default=",".join(str(x) for x in DEFAULT_HORIZONS))
    parser.add_argument("--sample-scope", choices=("current", "all"), default="current")
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    alerts, warnings = load_jsonl_tail(args.queue_file, args.scan_limit)
    payload = build_report(alerts, horizons=parse_horizons(args.horizons), sample_scope_mode=args.sample_scope)
    payload["warnings"].extend(warnings)
    if args.output:
        save_json_atomic(args.output, payload)

    text = build_text_report(payload)
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
