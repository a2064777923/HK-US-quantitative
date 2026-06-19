#!/usr/bin/env python3
"""Replay v5 trigger semantics on daily CSV or read-only DB snapshot data.

This is a research report, not a PnL backtest and not an execution input. It
feeds prior completed bars plus one synthetic close-time quote into
rt_signal_engine_v5, and summarizes what v5 would have emitted under its
trigger/confirmation/risk gates.
"""
import argparse
import csv
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

try:
    import rt_signal_engine_v5 as v5
except ImportError:
    from scripts import rt_signal_engine_v5 as v5


DEFAULT_DATA_DIR = os.environ.get("LOCAL_BACKTEST_DATA_DIR", "/tmp")
DEFAULT_HK_CSV = os.environ.get("V5_LOCAL_REPLAY_HK_CSV", os.path.join(DEFAULT_DATA_DIR, "hk_klines_v2.csv"))
DEFAULT_US_CSV = os.environ.get("V5_LOCAL_REPLAY_US_CSV", os.path.join(DEFAULT_DATA_DIR, "us_klines.csv"))
DEFAULT_OUTPUT_FILE = os.environ.get("V5_LOCAL_REPLAY_REPORT_FILE", "/tmp/v5_local_replay_report.json")
DEFAULT_SOURCE = os.environ.get("V5_LOCAL_REPLAY_SOURCE", "csv")
DEFAULT_DB_LOOKBACK_DAYS = int(os.environ.get("V5_LOCAL_REPLAY_DB_LOOKBACK_DAYS", "365"))
DEFAULT_MIN_HISTORY_BARS = v5.MIN_SIGNAL_HISTORY_BARS
DEFAULT_ALERT_SAMPLE_LIMIT = 50
ALERT_DENSITY_WARN_PER_100_BARS = 50.0
EXECUTION_DENSITY_WARN_PER_100_BARS = 10.0
DIRECTIONAL_CONFIRMATION_MIN_WARN_PCT = 35.0
DIRECTIONAL_DOWNGRADE_WARN_PCT = 60.0
MULTI_TRIGGER_SYMBOL_DAY_WARN_PCT = 30.0
TRIGGER_ALERT_DENSITY_WARN_PER_100_BARS = 5.0
TRIGGER_EXECUTION_DENSITY_WARN_PER_100_BARS = 2.0

STATUS_RANK = {"OK": 0, "INFO": 0, "WARN": 1, "FAIL": 2}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{datetime.now().strftime('%Y%m%d%H%M%S%f')}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def check(status, code, detail, data=None):
    return {"status": status, "code": code, "detail": detail, "data": data or {}}


def worst_status(statuses):
    status = "OK"
    for item in statuses:
        if STATUS_RANK.get(item, 0) > STATUS_RANK.get(status, 0):
            status = item
    return status


def row_date(row):
    value = row.get("dt") or row.get("date") or row.get("timestamp") or row.get("time")
    if value in (None, ""):
        return None
    return str(value)[:10]


def in_date_range(date_text, start_date=None, end_date=None):
    if not date_text:
        return False
    if start_date and date_text < start_date:
        return False
    if end_date and date_text > end_date:
        return False
    return True


def read_market_csv(path, market, start_date=None, end_date=None):
    market = str(market or "").upper()
    result = {
        "path": os.path.abspath(path),
        "market": market,
        "exists": os.path.exists(path),
        "row_count": 0,
        "valid_row_count": 0,
        "invalid_row_count": 0,
        "symbol_count": 0,
        "first_date": None,
        "last_date": None,
        "error": None,
    }
    by_symbol_date = {}
    if not result["exists"]:
        result["error"] = "file_missing"
        return {}, result

    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for raw in reader:
                result["row_count"] += 1
                date_text = row_date(raw)
                symbol = str(raw.get("symbol") or "").strip().upper()
                if not date_text or not in_date_range(date_text, start_date, end_date):
                    continue
                if not symbol or not v5.valid_watchlist_symbol(symbol, market=market):
                    result["invalid_row_count"] += 1
                    continue
                bar = v5.normalize_daily_bar(
                    raw.get("close_price") or raw.get("close"),
                    raw.get("high_price") or raw.get("high"),
                    raw.get("low_price") or raw.get("low"),
                    raw.get("volume"),
                )
                open_price = as_float(raw.get("open_price") or raw.get("open"))
                if bar is None or open_price is None or open_price <= 0:
                    result["invalid_row_count"] += 1
                    continue
                close_price, high_price, low_price, volume = bar
                if open_price > high_price or open_price < low_price:
                    result["invalid_row_count"] += 1
                    continue
                by_symbol_date[(symbol, date_text)] = {
                    "symbol": symbol,
                    "market": market,
                    "date": date_text,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                }
    except Exception as exc:
        result["error"] = str(exc)
        return {}, result

    grouped = defaultdict(list)
    for (_symbol, date_text), item in sorted(by_symbol_date.items()):
        grouped[item["symbol"]].append(item)
        result["valid_row_count"] += 1
        result["first_date"] = date_text if result["first_date"] is None else min(result["first_date"], date_text)
        result["last_date"] = date_text if result["last_date"] is None else max(result["last_date"], date_text)
    result["symbol_count"] = len(grouped)
    return dict(grouped), result


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def psql_rows(stdout):
    return [line.split("|") for line in str(stdout or "").splitlines() if line.strip()]


def db_replay_start_date(args):
    if args.start_date:
        return args.start_date
    lookback_days = max(as_int(getattr(args, "db_lookback_days", DEFAULT_DB_LOOKBACK_DAYS), DEFAULT_DB_LOOKBACK_DAYS), 45)
    return (datetime.now().date() - timedelta(days=lookback_days)).isoformat()


def db_replay_end_date(args):
    if args.end_date:
        return args.end_date
    return (datetime.now().date() - timedelta(days=1)).isoformat()


def read_market_db(symbols, market, start_date=None, end_date=None):
    market = str(market or "").upper()
    symbols = v5.normalize_symbol_list(symbols, market=market)
    result = {
        "path": "db:klines",
        "source_mode": "db_klines",
        "market": market,
        "exists": True,
        "row_count": 0,
        "valid_row_count": 0,
        "invalid_row_count": 0,
        "requested_symbol_count": len(symbols),
        "symbol_count": 0,
        "first_date": None,
        "last_date": None,
        "error": None,
    }
    if not symbols:
        result["error"] = "no_symbols"
        return {}, result
    start_date = start_date or "1900-01-01"
    end_date = end_date or "2999-12-31"
    symbol_sql = ",".join(sql_literal(symbol) for symbol in symbols)
    raw = v5.db(
        f"""
        SELECT symbol,
               substring(("timestamp")::text from 1 for 10) AS dt,
               open_price, high_price, low_price, close_price,
               COALESCE(volume, 0)
        FROM klines
        WHERE interval = 'day'
          AND symbol IN ({symbol_sql})
          AND substring(("timestamp")::text from 1 for 10) BETWEEN {sql_literal(start_date)} AND {sql_literal(end_date)}
        ORDER BY symbol, "timestamp"
        """
    )
    by_symbol_date = {}
    for parts in psql_rows(raw):
        result["row_count"] += 1
        if len(parts) < 7:
            result["invalid_row_count"] += 1
            continue
        symbol = str(parts[0] or "").strip().upper()
        date_text = str(parts[1] or "")[:10]
        if not symbol or not date_text or not v5.valid_watchlist_symbol(symbol, market=market):
            result["invalid_row_count"] += 1
            continue
        bar = v5.normalize_daily_bar(parts[5], parts[3], parts[4], parts[6])
        open_price = as_float(parts[2])
        if bar is None or open_price is None or open_price <= 0:
            result["invalid_row_count"] += 1
            continue
        close_price, high_price, low_price, volume = bar
        if open_price > high_price or open_price < low_price:
            result["invalid_row_count"] += 1
            continue
        by_symbol_date[(symbol, date_text)] = {
            "symbol": symbol,
            "market": market,
            "date": date_text,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }

    grouped = defaultdict(list)
    for (_symbol, date_text), item in sorted(by_symbol_date.items()):
        grouped[item["symbol"]].append(item)
        result["valid_row_count"] += 1
        result["first_date"] = date_text if result["first_date"] is None else min(result["first_date"], date_text)
        result["last_date"] = date_text if result["last_date"] is None else max(result["last_date"], date_text)
    result["symbol_count"] = len(grouped)
    if result["row_count"] <= 0:
        result["error"] = "no_db_rows"
    return dict(grouped), result


def load_replay_inputs(args):
    selected_markets = set(args.market or ["HK", "US"])
    if replay_source_mode(args) == "db":
        hk_watchlist, us_watchlist, watchlist_context = v5.load_watchlists()
        start_date = db_replay_start_date(args)
        end_date = db_replay_end_date(args)
        hk_rows, hk_source = ({}, {"market": "HK", "source_mode": "db_klines", "skipped": True})
        us_rows, us_source = ({}, {"market": "US", "source_mode": "db_klines", "skipped": True})
        if "HK" in selected_markets:
            hk_rows, hk_source = read_market_db(hk_watchlist, "HK", start_date, end_date)
        if "US" in selected_markets:
            us_rows, us_source = read_market_db(us_watchlist, "US", start_date, end_date)
        return hk_rows, us_rows, hk_source, us_source, {
            "source_mode": "db",
            "watchlist_context": watchlist_context,
            "db_date_range": {"start": start_date, "end": end_date},
        }

    hk_rows, hk_source = read_market_csv(args.hk_csv, "HK", args.start_date, args.end_date)
    us_rows, us_source = read_market_csv(args.us_csv, "US", args.start_date, args.end_date)
    return hk_rows, us_rows, hk_source, us_source, {"source_mode": "csv", "watchlist_context": None}


def clear_realtime(indicators):
    for name in ("rt_close", "rt_high", "rt_low", "rt_volume", "rt_updated_at"):
        setattr(indicators, name, None)


def quote_time_for_date(market, date_text):
    return f"{date_text} 16:00:00"


def synthetic_quote(row, previous_close=None):
    previous_close = previous_close if previous_close and previous_close > 0 else row["close"]
    return {
        "price": row["close"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "prev_close": previous_close,
        "volume": row["volume"],
        "volume_unit": "shares",
        "amount": 0,
        "change_pct": (row["close"] / previous_close - 1.0) * 100.0 if previous_close else 0,
        "time": quote_time_for_date(row["market"], row["date"]),
        "market": row["market"],
    }


def rounded_score(value):
    value = as_float(value)
    return round(value, 6) if value is not None else None


def distribution(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    midpoint = len(values) // 2
    if len(values) % 2:
        median = values[midpoint]
    else:
        median = (values[midpoint - 1] + values[midpoint]) / 2
    return {
        "count": len(values),
        "mean": round(sum(values) / len(values), 6),
        "median": round(median, 6),
        "min": round(values[0], 6),
        "max": round(values[-1], 6),
    }


def ratio_pct(numerator, denominator):
    numerator = as_float(numerator, 0) or 0
    denominator = as_float(denominator, 0) or 0
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100.0, 2)


def alert_sample(alert, replay_date):
    return {
        "signal_id": alert.get("signal_id"),
        "symbol": alert.get("symbol"),
        "market": alert.get("market"),
        "replay_date": replay_date,
        "trigger": alert.get("trigger"),
        "signal_type": alert.get("signal_type"),
        "candidate_signal_type": alert.get("candidate_signal_type"),
        "execution_candidate": alert.get("execution_candidate"),
        "confirmed": alert.get("confirmed"),
        "full_score": alert.get("full_score"),
        "risk_geometry_valid": alert.get("risk_geometry_valid"),
        "risk_geometry_reason": alert.get("risk_geometry_reason"),
        "execution_blocked_reasons": alert.get("execution_blocked_reasons") or [],
        "suppressed_directional_reason": alert.get("suppressed_directional_reason"),
        "candidate_rr_ratio": alert.get("candidate_rr_ratio"),
        "min_rr_ratio": alert.get("min_rr_ratio"),
    }


def alert_factor_categories(alert):
    candidate_signal_type = str(alert.get("candidate_signal_type") or "").upper()
    if candidate_signal_type not in ("BUY", "SELL"):
        return []
    categories = set()
    for contribution in v5.normalize_score_contributions(alert.get("factor_contributions")):
        direction = str(contribution.get("direction") or "").upper()
        category = str(contribution.get("category") or "").strip().lower()
        if direction == candidate_signal_type and category:
            categories.add(category)
    return sorted(categories)


def replay_symbol(symbol, rows, args, strategy_config, strategy_context):
    indicators = v5.IncrementalIndicators(symbol)
    trigger = v5.TriggerEngine(strategy_config=strategy_config, strategy_context=strategy_context)
    min_history = max(as_int(args.min_history_bars, DEFAULT_MIN_HISTORY_BARS), v5.MIN_SIGNAL_HISTORY_BARS)
    max_bars = as_int(args.max_bars_per_symbol, 0)
    rows = sorted(rows, key=lambda item: item["date"])
    if max_bars > 0:
        rows = rows[-max_bars:]

    alerts = []
    scores = []
    full_reason_counts = Counter()
    evaluated_bars = 0
    skipped_bars = 0
    for row in rows:
        history_count = v5.indicator_history_bar_count(indicators)
        if history_count >= min_history:
            quote = synthetic_quote(row, previous_close=indicators.closes[-1] if indicators.closes else None)
            if indicators.update_realtime(row["close"], row["high"], row["low"], row["volume"]):
                score, reasons = indicators.get_score(quote)
                score = rounded_score(score)
                if score is not None:
                    scores.append(score)
                for reason in reasons or []:
                    full_reason_counts[str(reason)] += 1
                before = len(trigger.alerts)
                if not args.respect_cooldown:
                    trigger.cooldowns = {}
                trigger.check(symbol, indicators, quote)
                for alert in trigger.alerts[before:]:
                    alerts.append((row["date"], dict(alert)))
                evaluated_bars += 1
            else:
                skipped_bars += 1
            clear_realtime(indicators)
        else:
            skipped_bars += 1
        indicators._update(row["close"], row["high"], row["low"], row["volume"])

    return {
        "symbol": symbol,
        "market": rows[0]["market"] if rows else None,
        "row_count": len(rows),
        "first_date": rows[0]["date"] if rows else None,
        "last_date": rows[-1]["date"] if rows else None,
        "evaluated_bars": evaluated_bars,
        "skipped_bars": skipped_bars,
        "alert_count": len(alerts),
        "execution_candidate_count": sum(1 for _date, alert in alerts if alert.get("execution_candidate") is True),
        "score_distribution": distribution(scores),
        "score_values": scores,
        "full_reason_counts": dict(full_reason_counts.most_common(20)),
        "alerts": alerts,
    }


def summarize_alerts(symbol_reports, alert_sample_limit=DEFAULT_ALERT_SAMPLE_LIMIT):
    by_signal_type = Counter()
    by_candidate_signal_type = Counter()
    by_trigger = Counter()
    by_market = Counter()
    by_factor_category = Counter()
    execution_blocked_reasons = Counter()
    risk_geometry_reasons = Counter()
    suppressed_reasons = Counter()
    samples = []
    execution_candidate_count = 0
    confirmed_directional_count = 0
    downgraded_directional_count = 0
    alert_count = 0
    symbol_day_alert_counts = Counter()
    for report in symbol_reports:
        for replay_date, alert in report.get("alerts") or []:
            alert_count += 1
            signal_type = str(alert.get("signal_type") or "UNKNOWN")
            candidate_signal_type = str(alert.get("candidate_signal_type") or "UNKNOWN")
            by_signal_type[signal_type] += 1
            by_candidate_signal_type[candidate_signal_type] += 1
            by_trigger[str(alert.get("trigger") or "UNKNOWN")] += 1
            by_market[str(alert.get("market") or report.get("market") or "UNKNOWN")] += 1
            by_factor_category.update(f"{candidate_signal_type}:{category}" for category in alert_factor_categories(alert))
            symbol_day_key = (
                str(alert.get("market") or report.get("market") or "UNKNOWN"),
                str(alert.get("symbol") or report.get("symbol") or "UNKNOWN"),
                str(replay_date or ""),
            )
            symbol_day_alert_counts[symbol_day_key] += 1
            if alert.get("execution_candidate") is True:
                execution_candidate_count += 1
            if candidate_signal_type in ("BUY", "SELL") and alert.get("confirmed") is True:
                confirmed_directional_count += 1
            if candidate_signal_type in ("BUY", "SELL") and signal_type != candidate_signal_type:
                downgraded_directional_count += 1
            for reason in alert.get("execution_blocked_reasons") or []:
                execution_blocked_reasons[str(reason)] += 1
            if alert.get("risk_geometry_reason"):
                risk_geometry_reasons[str(alert.get("risk_geometry_reason"))] += 1
            if alert.get("suppressed_directional_reason"):
                suppressed_reasons[str(alert.get("suppressed_directional_reason"))] += 1
            if len(samples) < alert_sample_limit:
                samples.append(alert_sample(alert, replay_date))
    multi_alert_symbol_days = [count for count in symbol_day_alert_counts.values() if count > 1]
    return {
        "alert_count": alert_count,
        "execution_candidate_count": execution_candidate_count,
        "confirmed_directional_count": confirmed_directional_count,
        "downgraded_directional_count": downgraded_directional_count,
        "by_signal_type": dict(by_signal_type),
        "by_candidate_signal_type": dict(by_candidate_signal_type),
        "by_trigger": dict(by_trigger.most_common()),
        "by_market": dict(by_market),
        "by_factor_category": dict(by_factor_category.most_common()),
        "execution_blocked_reason_counts": dict(execution_blocked_reasons.most_common()),
        "risk_geometry_reason_counts": dict(risk_geometry_reasons.most_common()),
        "suppressed_directional_reason_counts": dict(suppressed_reasons.most_common()),
        "alerted_symbol_day_count": len(symbol_day_alert_counts),
        "multi_alert_symbol_day_count": len(multi_alert_symbol_days),
        "max_alerts_per_symbol_day": max(symbol_day_alert_counts.values()) if symbol_day_alert_counts else 0,
        "avg_alerts_per_alerted_symbol_day": round(alert_count / len(symbol_day_alert_counts), 4)
        if symbol_day_alert_counts
        else None,
        "sample_alerts": samples,
    }


def replay_quality_assessment(evaluated_bars, symbol_count, alert_summary):
    alert_summary = alert_summary if isinstance(alert_summary, dict) else {}
    candidate_counts = alert_summary.get("by_candidate_signal_type") or {}
    directional_candidate_count = (candidate_counts.get("BUY") or 0) + (candidate_counts.get("SELL") or 0)
    alert_count = alert_summary.get("alert_count") or 0
    execution_candidate_count = alert_summary.get("execution_candidate_count") or 0
    confirmed_directional_count = alert_summary.get("confirmed_directional_count") or 0
    downgraded_directional_count = alert_summary.get("downgraded_directional_count") or 0
    alerted_symbol_day_count = alert_summary.get("alerted_symbol_day_count") or 0
    multi_alert_symbol_day_count = alert_summary.get("multi_alert_symbol_day_count") or 0
    top_trigger = None
    by_trigger = alert_summary.get("by_trigger") or {}
    if by_trigger:
        trigger, count = next(iter(by_trigger.items()))
        top_trigger = {
            "trigger": trigger,
            "count": count,
            "pct_of_alerts": ratio_pct(count, alert_count),
        }
    metrics = {
        "evaluated_bars": evaluated_bars,
        "symbol_count": symbol_count,
        "alert_rate_per_100_bars": ratio_pct(alert_count, evaluated_bars),
        "execution_candidate_rate_per_100_bars": ratio_pct(execution_candidate_count, evaluated_bars),
        "directional_candidate_count": directional_candidate_count,
        "directional_confirmation_ratio_pct": ratio_pct(confirmed_directional_count, directional_candidate_count),
        "directional_downgrade_ratio_pct": ratio_pct(downgraded_directional_count, directional_candidate_count),
        "execution_candidate_ratio_pct": ratio_pct(execution_candidate_count, directional_candidate_count),
        "alerted_symbol_day_count": alerted_symbol_day_count,
        "multi_alert_symbol_day_count": multi_alert_symbol_day_count,
        "multi_alert_symbol_day_ratio_pct": ratio_pct(multi_alert_symbol_day_count, alerted_symbol_day_count),
        "max_alerts_per_symbol_day": alert_summary.get("max_alerts_per_symbol_day"),
        "avg_alerts_per_alerted_symbol_day": alert_summary.get("avg_alerts_per_alerted_symbol_day"),
        "top_trigger": top_trigger,
    }
    checks = []
    if evaluated_bars <= 0:
        checks.append(check("FAIL", "replay_quality_no_evaluated_bars", "No evaluated bars are available for replay quality assessment."))
    else:
        alert_rate = metrics["alert_rate_per_100_bars"]
        if alert_rate is not None and alert_rate > ALERT_DENSITY_WARN_PER_100_BARS:
            checks.append(
                check(
                    "WARN",
                    "replay_alert_density_high",
                    "v5 replay emits alerts on a large share of evaluated symbol-days; treat this as a noise-control issue before promotion.",
                    {"alert_rate_per_100_bars": alert_rate},
                )
            )
        else:
            checks.append(
                check(
                    "OK",
                    "replay_alert_density_not_high",
                    "Replay alert density is not high under the current warning threshold.",
                    {"alert_rate_per_100_bars": alert_rate},
                )
            )

        execution_rate = metrics["execution_candidate_rate_per_100_bars"]
        if execution_rate is not None and execution_rate > EXECUTION_DENSITY_WARN_PER_100_BARS:
            checks.append(
                check(
                    "WARN",
                    "execution_candidate_density_high",
                    "v5 replay emits executable candidates too frequently for this to support promotion without further outcome and cost validation.",
                    {"execution_candidate_rate_per_100_bars": execution_rate},
                )
            )

        downgrade_ratio = metrics["directional_downgrade_ratio_pct"]
        confirmation_ratio = metrics["directional_confirmation_ratio_pct"]
        if (
            confirmation_ratio is not None
            and confirmation_ratio < DIRECTIONAL_CONFIRMATION_MIN_WARN_PCT
            and directional_candidate_count > 0
        ):
            checks.append(
                check(
                    "WARN",
                    "directional_confirmation_ratio_low",
                    "Only a minority of directional trigger candidates pass full-score confirmation; use replay as trigger-noise evidence, not promotion support.",
                    {"directional_confirmation_ratio_pct": confirmation_ratio},
                )
            )

        if downgrade_ratio is not None and downgrade_ratio > DIRECTIONAL_DOWNGRADE_WARN_PCT:
            checks.append(
                check(
                    "WARN",
                    "directional_downgrade_ratio_high",
                    "Most directional trigger candidates are downgraded to WATCH; the trigger layer is noisy relative to the full-score confirmation layer.",
                    {"directional_downgrade_ratio_pct": downgrade_ratio},
                )
            )

        multi_alert_ratio = metrics["multi_alert_symbol_day_ratio_pct"]
        if multi_alert_ratio is not None and multi_alert_ratio > MULTI_TRIGGER_SYMBOL_DAY_WARN_PCT:
            checks.append(
                check(
                    "WARN",
                    "multi_trigger_symbol_day_ratio_high",
                    "Many alerted symbol-days emit more than one trigger; Hermes should treat repeated same-day triggers as correlated evidence, not independent confirmation.",
                    {"multi_alert_symbol_day_ratio_pct": multi_alert_ratio},
                )
            )

        if execution_candidate_count <= 0:
            checks.append(
                check(
                    "INFO",
                    "no_execution_candidates_in_replay",
                    "Replay emitted no execution candidates in this scope; useful for diagnostics but not execution evidence.",
                )
            )

    status = worst_status([item["status"] for item in checks]) if checks else "OK"
    return {
        "schema": "v5_local_replay_quality_v1",
        "status": status,
        "thresholds": {
            "alert_density_warn_per_100_bars": ALERT_DENSITY_WARN_PER_100_BARS,
            "execution_density_warn_per_100_bars": EXECUTION_DENSITY_WARN_PER_100_BARS,
            "directional_confirmation_min_warn_pct": DIRECTIONAL_CONFIRMATION_MIN_WARN_PCT,
            "directional_downgrade_warn_pct": DIRECTIONAL_DOWNGRADE_WARN_PCT,
            "multi_trigger_symbol_day_warn_pct": MULTI_TRIGGER_SYMBOL_DAY_WARN_PCT,
        },
        "metrics": metrics,
        "checks": checks,
        "hermes_use": [
            "Use replay_quality to cap confidence when alert density, execution-candidate density, downgrade ratio, or same-day trigger stacking is high.",
            "Do not treat multiple same-symbol same-day replay triggers as independent evidence.",
            "Promotion still requires forward outcome, simulation, cost, and source-quality validation.",
        ],
    }


def trigger_group_quality_row(group, counts, denominator_bars, total_alerts):
    market, candidate_signal_type, trigger_name = group
    alert_count = counts.get("alert_count", 0)
    execution_candidate_count = counts.get("execution_candidate_count", 0)
    confirmed_directional_count = counts.get("confirmed_directional_count", 0)
    downgraded_directional_count = counts.get("downgraded_directional_count", 0)
    directional = candidate_signal_type in ("BUY", "SELL")
    metrics = {
        "denominator_bars": denominator_bars,
        "alert_count": alert_count,
        "pct_of_all_alerts": ratio_pct(alert_count, total_alerts),
        "alert_rate_per_100_bars": ratio_pct(alert_count, denominator_bars),
        "execution_candidate_count": execution_candidate_count,
        "execution_candidate_rate_per_100_bars": ratio_pct(execution_candidate_count, denominator_bars),
        "confirmed_directional_count": confirmed_directional_count,
        "downgraded_directional_count": downgraded_directional_count,
        "directional_confirmation_ratio_pct": ratio_pct(confirmed_directional_count, alert_count)
        if directional
        else None,
        "directional_downgrade_ratio_pct": ratio_pct(downgraded_directional_count, alert_count)
        if directional
        else None,
        "execution_candidate_ratio_pct": ratio_pct(execution_candidate_count, alert_count)
        if directional
        else None,
    }
    reasons = []
    alert_rate = metrics["alert_rate_per_100_bars"]
    if alert_rate is not None and alert_rate > TRIGGER_ALERT_DENSITY_WARN_PER_100_BARS:
        reasons.append("trigger_replay_alert_density_high")
    execution_rate = metrics["execution_candidate_rate_per_100_bars"]
    if execution_rate is not None and execution_rate > TRIGGER_EXECUTION_DENSITY_WARN_PER_100_BARS:
        reasons.append("trigger_execution_candidate_density_high")
    confirmation_ratio = metrics["directional_confirmation_ratio_pct"]
    if directional and confirmation_ratio is not None and confirmation_ratio < DIRECTIONAL_CONFIRMATION_MIN_WARN_PCT:
        reasons.append("trigger_directional_confirmation_ratio_low")
    downgrade_ratio = metrics["directional_downgrade_ratio_pct"]
    if directional and downgrade_ratio is not None and downgrade_ratio > DIRECTIONAL_DOWNGRADE_WARN_PCT:
        reasons.append("trigger_directional_downgrade_ratio_high")

    return {
        "market": market,
        "candidate_signal_type": candidate_signal_type,
        "trigger": trigger_name,
        "key": f"{market}:{candidate_signal_type}:{trigger_name}",
        "status": "WARN" if reasons else "OK",
        "reasons": reasons,
        "metrics": metrics,
    }


def factor_group_quality_row(group, counts, denominator_bars, total_alerts):
    row = trigger_group_quality_row(group, counts, denominator_bars, total_alerts)
    row.pop("trigger", None)
    row["factor_category"] = group[2]
    row["key"] = f"{group[0]}:{group[1]}:{group[2]}"
    row["reasons"] = [
        reason.replace("trigger_", "factor_")
        for reason in row.get("reasons") or []
    ]
    return row


def market_quality_row(market, counts, denominator_bars):
    candidate_counts = counts.get("candidate_counts") or Counter()
    directional_candidate_count = (candidate_counts.get("BUY") or 0) + (candidate_counts.get("SELL") or 0)
    metrics = {
        "denominator_bars": denominator_bars,
        "alert_count": counts.get("alert_count", 0),
        "alert_rate_per_100_bars": ratio_pct(counts.get("alert_count", 0), denominator_bars),
        "execution_candidate_count": counts.get("execution_candidate_count", 0),
        "execution_candidate_rate_per_100_bars": ratio_pct(counts.get("execution_candidate_count", 0), denominator_bars),
        "directional_candidate_count": directional_candidate_count,
        "directional_confirmation_ratio_pct": ratio_pct(counts.get("confirmed_directional_count", 0), directional_candidate_count),
        "directional_downgrade_ratio_pct": ratio_pct(counts.get("downgraded_directional_count", 0), directional_candidate_count),
    }
    reasons = []
    if metrics["alert_rate_per_100_bars"] is not None and metrics["alert_rate_per_100_bars"] > ALERT_DENSITY_WARN_PER_100_BARS:
        reasons.append("market_replay_alert_density_high")
    if (
        metrics["execution_candidate_rate_per_100_bars"] is not None
        and metrics["execution_candidate_rate_per_100_bars"] > EXECUTION_DENSITY_WARN_PER_100_BARS
    ):
        reasons.append("market_execution_candidate_density_high")
    if (
        metrics["directional_confirmation_ratio_pct"] is not None
        and metrics["directional_confirmation_ratio_pct"] < DIRECTIONAL_CONFIRMATION_MIN_WARN_PCT
        and directional_candidate_count > 0
    ):
        reasons.append("market_directional_confirmation_ratio_low")
    if (
        metrics["directional_downgrade_ratio_pct"] is not None
        and metrics["directional_downgrade_ratio_pct"] > DIRECTIONAL_DOWNGRADE_WARN_PCT
    ):
        reasons.append("market_directional_downgrade_ratio_high")
    return {
        "market": market,
        "status": "WARN" if reasons else "OK",
        "reasons": reasons,
        "metrics": metrics,
    }


def replay_breakdown(symbol_reports, total_evaluated_bars, alert_summary):
    market_bars = Counter()
    market_counts = defaultdict(lambda: {"candidate_counts": Counter()})
    trigger_counts = defaultdict(Counter)
    factor_counts = defaultdict(Counter)
    total_alerts = (alert_summary or {}).get("alert_count") or 0
    for report in symbol_reports:
        market = str(report.get("market") or "UNKNOWN")
        market_bars[market] += report.get("evaluated_bars") or 0
        market_counts[market]
        for _replay_date, alert in report.get("alerts") or []:
            candidate_signal_type = str(alert.get("candidate_signal_type") or "UNKNOWN").upper()
            trigger_name = str(alert.get("trigger") or "UNKNOWN")
            alert_market = str(alert.get("market") or market)
            group = (alert_market, candidate_signal_type, trigger_name)
            trigger_counts[group]["alert_count"] += 1
            market_counts[alert_market]["alert_count"] = market_counts[alert_market].get("alert_count", 0) + 1
            market_counts[alert_market]["candidate_counts"][candidate_signal_type] += 1
            factor_categories = alert_factor_categories(alert)
            for factor_category in factor_categories:
                factor_group = (alert_market, candidate_signal_type, factor_category)
                factor_counts[factor_group]["alert_count"] += 1
            if alert.get("execution_candidate") is True:
                trigger_counts[group]["execution_candidate_count"] += 1
                market_counts[alert_market]["execution_candidate_count"] = (
                    market_counts[alert_market].get("execution_candidate_count", 0) + 1
                )
                for factor_category in factor_categories:
                    factor_counts[(alert_market, candidate_signal_type, factor_category)]["execution_candidate_count"] += 1
            if candidate_signal_type in ("BUY", "SELL") and alert.get("confirmed") is True:
                trigger_counts[group]["confirmed_directional_count"] += 1
                market_counts[alert_market]["confirmed_directional_count"] = (
                    market_counts[alert_market].get("confirmed_directional_count", 0) + 1
                )
                for factor_category in factor_categories:
                    factor_counts[(alert_market, candidate_signal_type, factor_category)]["confirmed_directional_count"] += 1
            if candidate_signal_type in ("BUY", "SELL") and str(alert.get("signal_type") or "").upper() != candidate_signal_type:
                trigger_counts[group]["downgraded_directional_count"] += 1
                market_counts[alert_market]["downgraded_directional_count"] = (
                    market_counts[alert_market].get("downgraded_directional_count", 0) + 1
                )
                for factor_category in factor_categories:
                    factor_counts[(alert_market, candidate_signal_type, factor_category)]["downgraded_directional_count"] += 1

    trigger_groups = [
        trigger_group_quality_row(
            group,
            counts,
            market_bars.get(group[0]) or total_evaluated_bars,
            total_alerts,
        )
        for group, counts in trigger_counts.items()
    ]
    trigger_groups = sorted(
        trigger_groups,
        key=lambda row: (
            0 if row["status"] == "WARN" else 1,
            -(row["metrics"].get("alert_count") or 0),
            row["key"],
        ),
    )
    market_quality = [
        market_quality_row(market, counts, market_bars.get(market) or 0)
        for market, counts in market_counts.items()
    ]
    market_quality = sorted(market_quality, key=lambda row: row["market"])
    factor_groups = [
        factor_group_quality_row(
            group,
            counts,
            market_bars.get(group[0]) or total_evaluated_bars,
            total_alerts,
        )
        for group, counts in factor_counts.items()
    ]
    factor_groups = sorted(
        factor_groups,
        key=lambda row: (
            0 if row["status"] == "WARN" else 1,
            -(row["metrics"].get("alert_count") or 0),
            row["key"],
        ),
    )
    return {
        "schema": "v5_local_replay_breakdown_v1",
        "market_quality": market_quality,
        "trigger_groups": trigger_groups,
        "factor_groups": factor_groups,
        "top_noisy_triggers": [row for row in trigger_groups if row["status"] == "WARN"][:12],
        "top_noisy_factor_groups": [row for row in factor_groups if row["status"] == "WARN"][:12],
        "summary": {
            "trigger_group_count": len(trigger_groups),
            "warn_trigger_group_count": sum(1 for row in trigger_groups if row["status"] == "WARN"),
            "factor_group_count": len(factor_groups),
            "warn_factor_group_count": sum(1 for row in factor_groups if row["status"] == "WARN"),
            "market_count": len(market_quality),
            "warn_market_count": sum(1 for row in market_quality if row["status"] == "WARN"),
        },
        "hermes_use": [
            "Use trigger_groups to identify which v5 triggers are noisy in local replay before proposing threshold changes.",
            "Use factor_groups to see which factor categories are overrepresented in replay alerts before changing multi-factor logic.",
            "Use market_quality to check whether HK and US behave differently before applying a global trigger policy.",
            "Do not treat repeated trigger groups as independent evidence without forward outcome and simulation validation.",
        ],
    }


def source_check_prefix(source):
    return "db_klines" if (source or {}).get("source_mode") == "db_klines" else "csv"


def data_checks(sources, total_rows, total_symbols):
    checks = [
        check(
            "OK",
            "replay_data_read_only",
            "Replay consumes read-only daily bars and writes a compact JSON report only.",
            {
                "raw_data_local_only": not any(source_check_prefix(source) == "db_klines" for source in sources.values()),
                "server_db_snapshot_read_only": any(
                    source_check_prefix(source) == "db_klines" for source in sources.values()
                ),
                "commit_raw_csv_to_git": False,
                "copy_to_server_by_default": False,
            },
        )
    ]
    for market, source in sources.items():
        prefix = source_check_prefix(source)
        if not source.get("exists"):
            checks.append(
                check(
                    "FAIL",
                    f"{market.lower()}_{prefix}_missing",
                    "Required replay source is missing.",
                    source,
                )
            )
        elif source.get("error"):
            checks.append(
                check(
                    "FAIL",
                    f"{market.lower()}_{prefix}_unreadable",
                    "Required replay source could not be read.",
                    source,
                )
            )
        elif source.get("valid_row_count", 0) <= 0:
            checks.append(
                check(
                    "FAIL",
                    f"{market.lower()}_{prefix}_has_no_valid_rows",
                    "Replay source has no valid rows.",
                    source,
                )
            )
        elif source.get("invalid_row_count", 0) > 0:
            checks.append(
                check(
                    "WARN",
                    f"{market.lower()}_{prefix}_invalid_rows_skipped",
                    "Some replay rows were skipped.",
                    source,
                )
            )
    if total_rows <= 0 or total_symbols <= 0:
        checks.append(check("FAIL", "no_replay_dataset", "No valid local replay rows were available."))
    return checks


def replay_checks(evaluated_bars, alert_summary, respect_cooldown):
    checks = []
    if evaluated_bars <= 0:
        checks.append(check("FAIL", "no_v5_replay_bars_evaluated", "No bars had enough prior history for v5 replay."))
    else:
        checks.append(check("OK", "v5_replay_bars_evaluated", "At least one bar was replayed through v5 semantics.", {"evaluated_bars": evaluated_bars}))
    checks.append(
        check(
            "WARN",
            "daily_close_synthetic_quote_not_intraday_path",
            "Replay uses completed daily rows as synthetic close-time quotes; it is not true intraday path reconstruction.",
        )
    )
    if not respect_cooldown:
        checks.append(
            check(
                "INFO",
                "cooldown_not_modeled_by_default",
                "Signal cooldown is reset during replay so historical distributions are not suppressed by wall-clock runtime.",
            )
        )
    if alert_summary.get("alert_count", 0) <= 0:
        checks.append(check("INFO", "no_v5_alerts_emitted", "No v5 trigger alerts were emitted in this replay scope."))
    return checks


def replay_source_mode(args):
    source = str(getattr(args, "source", DEFAULT_SOURCE) or DEFAULT_SOURCE).strip().lower()
    return source if source in {"csv", "db"} else "csv"


def source_label(source):
    return "db" if (source or {}).get("source_mode") == "db_klines" else "csv"


def source_data_basis(source_mode):
    if source_mode == "db":
        return "server_db_completed_daily_klines_snapshot"
    return "local_daily_csv_replay"


def source_quote_time_description(source_mode):
    if source_mode == "db":
        return "market close timestamp generated from each DB daily row date"
    return "market close timestamp generated from each CSV date"


def build_report(args):
    hk_rows, us_rows, hk_source, us_source, replay_source = load_replay_inputs(args)
    source_mode = replay_source_mode(args)
    grouped = {}
    selected_markets = set(args.market or ["HK", "US"])
    if "HK" in selected_markets:
        grouped.update({("HK", symbol): rows for symbol, rows in hk_rows.items()})
    if "US" in selected_markets:
        grouped.update({("US", symbol): rows for symbol, rows in us_rows.items()})

    ordered_items = sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1]))
    max_symbols = as_int(args.max_symbols, 0)
    if max_symbols > 0:
        ordered_items = ordered_items[:max_symbols]

    strategy_config_file = args.strategy_config_file or None
    strategy_config, strategy_context = v5.load_strategy_config(env={}, file_path=strategy_config_file)
    symbol_reports = []
    for (_market, symbol), rows in ordered_items:
        symbol_reports.append(replay_symbol(symbol, rows, args, strategy_config, strategy_context))

    total_rows = sum(report["row_count"] for report in symbol_reports)
    evaluated_bars = sum(report["evaluated_bars"] for report in symbol_reports)
    skipped_bars = sum(report["skipped_bars"] for report in symbol_reports)
    score_values = []
    for report in symbol_reports:
        score_values.extend(report.get("score_values") or [])
    alert_summary = summarize_alerts(symbol_reports, alert_sample_limit=args.alert_sample_limit)
    replay_quality = replay_quality_assessment(evaluated_bars, len(symbol_reports), alert_summary)
    breakdown = replay_breakdown(symbol_reports, evaluated_bars, alert_summary)
    checks = data_checks(
        {market: source for market, source in (("HK", hk_source), ("US", us_source)) if market in selected_markets},
        total_rows,
        len(symbol_reports),
    )
    checks.extend(replay_checks(evaluated_bars, alert_summary, args.respect_cooldown))
    checks.extend(replay_quality.get("checks") or [])
    check_status = worst_status([item["status"] for item in checks])
    overall_status = "INSUFFICIENT_REPLAY_DATA" if check_status == "FAIL" else "V5_REPLAY_RESEARCH_ONLY"

    return {
        "schema": "v5_local_replay_report_v1",
        "generated_at": now_iso(),
        "source": {
            "source_files": {
                "hk_csv": os.path.abspath(args.hk_csv),
                "us_csv": os.path.abspath(args.us_csv),
                "strategy_config_file": os.path.abspath(args.strategy_config_file)
                if args.strategy_config_file
                else None,
            },
            "source_mode": replay_source.get("source_mode"),
            "db_date_range": replay_source.get("db_date_range"),
            "db_completed_daily_policy": (
                "DB daily replay excludes same-day incomplete daily rows by default; live analysis still uses "
                "realtime quotes and intraday context separately"
            )
            if source_mode == "db"
            else None,
            "watchlist_context": replay_source.get("watchlist_context"),
            "read_only_inputs": True,
            "writes_output_only": True,
            "local_only": source_mode == "csv",
            "uses_existing_db_snapshot": source_mode == "db",
            "uses_credentials": False,
            "mutates_server": False,
            "mutates_git": False,
            "writes_alert_queue": False,
            "submits_orders": False,
            "changes_v5": False,
            "changes_order_intake": False,
            "changes_simulation": False,
        },
        "summary": {
            "overall_status": overall_status,
            "promotion_ready": False,
            "hermes_use": "v5_replay_research_context_only",
            "market_count": len({report.get("market") for report in symbol_reports if report.get("market")}),
            "symbol_count": len(symbol_reports),
            "total_row_count": total_rows,
            "evaluated_bars": evaluated_bars,
            "skipped_bars": skipped_bars,
            "alert_count": alert_summary["alert_count"],
            "execution_candidate_count": alert_summary["execution_candidate_count"],
            "downgraded_directional_count": alert_summary["downgraded_directional_count"],
            "replay_quality_status": replay_quality["status"],
            "warn_trigger_group_count": (breakdown.get("summary") or {}).get("warn_trigger_group_count"),
            "message": "v5 replay evidence is useful for trigger/confirmation/risk distribution review, not for execution approval or profitability claims.",
        },
        "replay_contract": {
            "engine": "rt_signal_engine_v5",
            "indicator_model": "IncrementalIndicators with prior completed bars plus one synthetic close-time quote",
            "trigger_model": "TriggerEngine.check",
            "data_basis": source_data_basis(source_mode),
            "source_mode": replay_source.get("source_mode"),
            "synthetic_quote_time": source_quote_time_description(source_mode),
            "respect_cooldown": bool(args.respect_cooldown),
            "min_history_bars": max(as_int(args.min_history_bars, DEFAULT_MIN_HISTORY_BARS), v5.MIN_SIGNAL_HISTORY_BARS),
            "strategy_config_id": strategy_config.get("config_id"),
            "strategy_config_source": strategy_context.get("source"),
            "strategy_config_version": strategy_context.get("version"),
            "strategy_config_warnings": strategy_context.get("warnings") or [],
        },
        "storage_policy": {
            "raw_data_local_only": source_mode == "csv",
            "server_db_snapshot_read_only": source_mode == "db",
            "commit_raw_csv_to_git": False,
            "copy_to_server_by_default": False,
            "recommended_raw_data_use": "keep broad and fine-grained raw data locally; promote only compact validated reports into Hermes context",
        },
        "inputs": {"HK": hk_source, "US": us_source},
        "alert_summary": alert_summary,
        "replay_quality": replay_quality,
        "replay_breakdown": breakdown,
        "score_summary": distribution(score_values),
        "symbols": [
            {
                key: value
                for key, value in report.items()
                if key not in ("alerts", "full_reason_counts", "score_values")
            }
            for report in symbol_reports
        ],
        "symbol_full_reason_counts": {
            report["symbol"]: report.get("full_reason_counts") or {}
            for report in symbol_reports
            if report.get("full_reason_counts")
        },
        "checks": checks,
        "limitations": [
            "daily_close_synthetic_quote_only_not_true_intraday_path",
            "current_day_high_low_volume_are_completed_bar_values",
            "no_pnl_trade_lifecycle_or_slippage_model",
            "no_market_session_freshness_replay",
            "cooldown_not_modeled_unless_respect_cooldown_is_enabled",
            "local_csv_source_quality_must_still_be_cross_validated_before_institutional_claims",
            "server_db_snapshot_source_quality_must_still_be_cross_validated_before_institutional_claims",
        ],
        "hermes_contract": {
            "contract": "v5_replay_research_context_only",
            "allowed_use": [
                "compare v5 trigger, confirmation, WATCH downgrade, and risk-geometry distributions",
                "flag high replay alert density, high downgrade ratio, or same-day trigger stacking before promotion",
                "identify noisy trigger groups by market before proposing v5 threshold or trigger changes",
                "identify noisy triggers or repeated downgrade reasons before strategy promotion",
                "support or challenge research hypotheses alongside local backtest reliability and factor alignment",
            ],
            "forbidden_use": [
                "do not approve live or simulation execution from this replay alone",
                "do not bypass rt_order_intake, execution_readiness, source_reliability, or Hermes judgment gates",
                "do not treat daily-close replay as intraday path proof",
                "do not copy raw local CSV data to GitHub or the production server by default",
                "do not treat server DB replay as live execution readiness or data-health authority",
            ],
        },
    }


def text_report(payload):
    summary = payload.get("summary") or {}
    alerts = payload.get("alert_summary") or {}
    lines = [
        f"v5 local replay: {summary.get('overall_status')}",
        f"Hermes use: {summary.get('hermes_use')} promotion_ready={summary.get('promotion_ready')}",
        (
            "Replay scope: "
            f"symbols={summary.get('symbol_count')} rows={summary.get('total_row_count')} "
            f"evaluated={summary.get('evaluated_bars')} skipped={summary.get('skipped_bars')}"
        ),
        (
            "Alerts: "
            f"total={alerts.get('alert_count')} execution_candidates={alerts.get('execution_candidate_count')} "
            f"downgraded_directionals={alerts.get('downgraded_directional_count')}"
        ),
    ]
    quality = payload.get("replay_quality") or {}
    metrics = quality.get("metrics") or {}
    lines.append(
        "Quality: "
        f"status={quality.get('status')} "
        f"alert_rate_per_100={metrics.get('alert_rate_per_100_bars')} "
        f"execution_rate_per_100={metrics.get('execution_candidate_rate_per_100_bars')} "
        f"confirmation_ratio={metrics.get('directional_confirmation_ratio_pct')} "
        f"downgrade_ratio={metrics.get('directional_downgrade_ratio_pct')}"
    )
    if alerts.get("by_candidate_signal_type"):
        lines.append(f"Candidate types: {alerts.get('by_candidate_signal_type')}")
    breakdown = payload.get("replay_breakdown") or {}
    noisy = breakdown.get("top_noisy_triggers") or []
    if noisy:
        sample = [
            f"{row.get('key')}:{(row.get('metrics') or {}).get('alert_rate_per_100_bars')}/100"
            for row in noisy[:5]
        ]
        lines.append("Top noisy triggers: " + ", ".join(sample))
    noisy_factors = breakdown.get("top_noisy_factor_groups") or []
    if noisy_factors:
        sample = [
            f"{row.get('key')}:{(row.get('metrics') or {}).get('alert_rate_per_100_bars')}/100"
            for row in noisy_factors[:5]
        ]
        lines.append("Top noisy factors: " + ", ".join(sample))
    warnings = [item.get("code") for item in payload.get("checks") or [] if item.get("status") in {"WARN", "FAIL"}]
    if warnings:
        lines.append("Warnings: " + ", ".join(warnings[:12]))
    lines.append("Contract: v5 replay research context only; no alert queue/order/simulation mutation.")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["csv", "db"], default=DEFAULT_SOURCE)
    parser.add_argument("--hk-csv", default=DEFAULT_HK_CSV)
    parser.add_argument("--us-csv", default=DEFAULT_US_CSV)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--strategy-config-file", default=os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_FILE", ""))
    parser.add_argument("--market", action="append", choices=("HK", "US"), default=[])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--db-lookback-days", type=int, default=DEFAULT_DB_LOOKBACK_DAYS)
    parser.add_argument("--min-history-bars", type=int, default=DEFAULT_MIN_HISTORY_BARS)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--max-bars-per-symbol", type=int, default=0)
    parser.add_argument("--alert-sample-limit", type=int, default=DEFAULT_ALERT_SAMPLE_LIMIT)
    parser.add_argument("--respect-cooldown", action="store_true")
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    payload = build_report(args)
    save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(text_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
