#!/usr/bin/env python3
"""Replay v5 trigger semantics on local-only daily CSV data.

This is a research report, not a PnL backtest and not an execution input. It
reads local CSV bars, feeds prior completed bars plus a synthetic current-day
close quote into rt_signal_engine_v5, and summarizes what v5 would have
emitted under its trigger/confirmation/risk gates.
"""
import argparse
import csv
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime

try:
    import rt_signal_engine_v5 as v5
except ImportError:
    from scripts import rt_signal_engine_v5 as v5


DEFAULT_DATA_DIR = os.environ.get("LOCAL_BACKTEST_DATA_DIR", "/tmp")
DEFAULT_HK_CSV = os.environ.get("V5_LOCAL_REPLAY_HK_CSV", os.path.join(DEFAULT_DATA_DIR, "hk_klines_v2.csv"))
DEFAULT_US_CSV = os.environ.get("V5_LOCAL_REPLAY_US_CSV", os.path.join(DEFAULT_DATA_DIR, "us_klines.csv"))
DEFAULT_OUTPUT_FILE = os.environ.get("V5_LOCAL_REPLAY_REPORT_FILE", "/tmp/v5_local_replay_report.json")
DEFAULT_MIN_HISTORY_BARS = v5.MIN_SIGNAL_HISTORY_BARS
DEFAULT_ALERT_SAMPLE_LIMIT = 50

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
    execution_blocked_reasons = Counter()
    risk_geometry_reasons = Counter()
    suppressed_reasons = Counter()
    samples = []
    execution_candidate_count = 0
    confirmed_directional_count = 0
    downgraded_directional_count = 0
    alert_count = 0
    for report in symbol_reports:
        for replay_date, alert in report.get("alerts") or []:
            alert_count += 1
            signal_type = str(alert.get("signal_type") or "UNKNOWN")
            candidate_signal_type = str(alert.get("candidate_signal_type") or "UNKNOWN")
            by_signal_type[signal_type] += 1
            by_candidate_signal_type[candidate_signal_type] += 1
            by_trigger[str(alert.get("trigger") or "UNKNOWN")] += 1
            by_market[str(alert.get("market") or report.get("market") or "UNKNOWN")] += 1
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
    return {
        "alert_count": alert_count,
        "execution_candidate_count": execution_candidate_count,
        "confirmed_directional_count": confirmed_directional_count,
        "downgraded_directional_count": downgraded_directional_count,
        "by_signal_type": dict(by_signal_type),
        "by_candidate_signal_type": dict(by_candidate_signal_type),
        "by_trigger": dict(by_trigger.most_common()),
        "by_market": dict(by_market),
        "execution_blocked_reason_counts": dict(execution_blocked_reasons.most_common()),
        "risk_geometry_reason_counts": dict(risk_geometry_reasons.most_common()),
        "suppressed_directional_reason_counts": dict(suppressed_reasons.most_common()),
        "sample_alerts": samples,
    }


def data_checks(sources, total_rows, total_symbols):
    checks = [
        check(
            "OK",
            "raw_replay_data_local_only",
            "Replay consumes local CSV files and writes a local JSON report only.",
            {
                "raw_data_local_only": True,
                "commit_raw_csv_to_git": False,
                "copy_to_server_by_default": False,
            },
        )
    ]
    for market, source in sources.items():
        if not source.get("exists"):
            checks.append(check("FAIL", f"{market.lower()}_csv_missing", "Required local replay CSV is missing.", source))
        elif source.get("error"):
            checks.append(check("FAIL", f"{market.lower()}_csv_unreadable", "Required local replay CSV could not be read.", source))
        elif source.get("valid_row_count", 0) <= 0:
            checks.append(check("FAIL", f"{market.lower()}_csv_has_no_valid_rows", "CSV has no valid replay rows.", source))
        elif source.get("invalid_row_count", 0) > 0:
            checks.append(check("WARN", f"{market.lower()}_csv_invalid_rows_skipped", "Some CSV rows were skipped.", source))
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


def build_report(args):
    hk_rows, hk_source = read_market_csv(args.hk_csv, "HK", args.start_date, args.end_date)
    us_rows, us_source = read_market_csv(args.us_csv, "US", args.start_date, args.end_date)
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
    checks = data_checks(
        {market: source for market, source in (("HK", hk_source), ("US", us_source)) if market in selected_markets},
        total_rows,
        len(symbol_reports),
    )
    checks.extend(replay_checks(evaluated_bars, alert_summary, args.respect_cooldown))
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
            "read_only_inputs": True,
            "writes_output_only": True,
            "local_only": True,
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
            "message": "v5 replay evidence is useful for trigger/confirmation/risk distribution review, not for execution approval or profitability claims.",
        },
        "replay_contract": {
            "engine": "rt_signal_engine_v5",
            "indicator_model": "IncrementalIndicators with prior completed bars plus one synthetic close-time quote",
            "trigger_model": "TriggerEngine.check",
            "data_basis": "local_daily_csv_replay",
            "synthetic_quote_time": "market close timestamp generated from each CSV date",
            "respect_cooldown": bool(args.respect_cooldown),
            "min_history_bars": max(as_int(args.min_history_bars, DEFAULT_MIN_HISTORY_BARS), v5.MIN_SIGNAL_HISTORY_BARS),
            "strategy_config_id": strategy_config.get("config_id"),
            "strategy_config_source": strategy_context.get("source"),
            "strategy_config_version": strategy_context.get("version"),
            "strategy_config_warnings": strategy_context.get("warnings") or [],
        },
        "storage_policy": {
            "raw_data_local_only": True,
            "commit_raw_csv_to_git": False,
            "copy_to_server_by_default": False,
            "recommended_raw_data_use": "keep broad and fine-grained raw data locally; promote only compact validated reports into Hermes context",
        },
        "inputs": {"HK": hk_source, "US": us_source},
        "alert_summary": alert_summary,
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
        ],
        "hermes_contract": {
            "contract": "v5_replay_research_context_only",
            "allowed_use": [
                "compare v5 trigger, confirmation, WATCH downgrade, and risk-geometry distributions",
                "identify noisy triggers or repeated downgrade reasons before strategy promotion",
                "support or challenge research hypotheses alongside local backtest reliability and factor alignment",
            ],
            "forbidden_use": [
                "do not approve live or simulation execution from this replay alone",
                "do not bypass rt_order_intake, execution_readiness, source_reliability, or Hermes judgment gates",
                "do not treat daily-close replay as intraday path proof",
                "do not copy raw local CSV data to GitHub or the production server by default",
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
    if alerts.get("by_candidate_signal_type"):
        lines.append(f"Candidate types: {alerts.get('by_candidate_signal_type')}")
    warnings = [item.get("code") for item in payload.get("checks") or [] if item.get("status") in {"WARN", "FAIL"}]
    if warnings:
        lines.append("Warnings: " + ", ".join(warnings[:12]))
    lines.append("Contract: v5 replay research context only; no alert queue/order/simulation mutation.")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--hk-csv", default=DEFAULT_HK_CSV)
    parser.add_argument("--us-csv", default=DEFAULT_US_CSV)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--strategy-config-file", default=os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_FILE", ""))
    parser.add_argument("--market", action="append", choices=("HK", "US"), default=[])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
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
