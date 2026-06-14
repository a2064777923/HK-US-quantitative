#!/usr/bin/env python3
"""Read-only data freshness and integrity report for QuantMind/Hermes."""
import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
REPORT_FILE = os.environ.get("DATA_HEALTH_REPORT_FILE", "/tmp/data_health_report.json")
SIGNAL_MODEL_VERSION = os.environ.get("QM_SIGNAL_MODEL_VERSION", "signal_v4")
SIGNAL_FEATURE_VERSION = os.environ.get("QM_SIGNAL_FEATURE_VERSION", "v4_full")
MIN_LATEST_COVERAGE_PCT = float(os.environ.get("DATA_HEALTH_MIN_LATEST_COVERAGE_PCT", "80"))
MIN_HISTORY_60D_COVERAGE_PCT = float(os.environ.get("DATA_HEALTH_MIN_HISTORY_60D_COVERAGE_PCT", "70"))
SIGNAL_STALE_WARN_DAYS = int(os.environ.get("DATA_HEALTH_SIGNAL_STALE_WARN_DAYS", "1"))
DAILY_SIGNAL_READY_TIME = os.environ.get("DATA_HEALTH_DAILY_SIGNAL_READY_TIME", "16:15")
SIGNAL_ENGINE_COMMAND = os.environ.get("DATA_HEALTH_SIGNAL_ENGINE_COMMAND", "/usr/bin/python3 /root/signal_engine_v4.py")
DATA_HEALTH_COMMAND = os.environ.get(
    "DATA_HEALTH_REPORT_COMMAND",
    "/usr/bin/python3 /root/data_health_report.py --output /tmp/data_health_report.json --text",
)
SYSTEM_HEALTH_COMMAND = os.environ.get(
    "DATA_HEALTH_SYSTEM_HEALTH_COMMAND",
    "/usr/bin/python3 /root/system_health_check.py --output /tmp/quantmind_system_health.json",
)
READINESS_REFRESH_COMMAND = os.environ.get(
    "DATA_HEALTH_READINESS_REFRESH_COMMAND",
    "/usr/bin/python3 /root/readiness_refresh.py --skip-network-producers --output /tmp/readiness_refresh_report.json --text",
)
KLINE_DAILY_GAP_REPAIR_COMMAND = os.environ.get(
    "DATA_HEALTH_KLINE_DAILY_GAP_REPAIR_COMMAND",
    "/usr/bin/python3 /root/kline_daily_gap_repair.py --output /tmp/kline_daily_gap_repair.json --text",
)

SEVERITY = {"OK": 0, "WARN": 1, "FAIL": 2}
_COLUMN_CACHE = {}


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


def run_cmd(args, timeout=90):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def psql(sql, timeout=90):
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


def table_columns(table):
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    r = psql(
        f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = '{sql_quote(table)}'
        """
    )
    cols = {row[0] for row in rows(r.stdout)} if r.returncode == 0 else set()
    _COLUMN_CACHE[table] = cols
    return cols


def first_existing(table, candidates, fallback):
    cols = table_columns(table)
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return fallback


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def rate(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def max_status(statuses):
    return max(statuses or ["OK"], key=lambda status: SEVERITY.get(status, 1))


def parse_date(value):
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_hhmm_minutes(value):
    try:
        hour, minute = str(value).split(":", 1)
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return 16 * 60 + 15


def minutes_since_midnight(value):
    return value.hour * 60 + value.minute


def time_minutes_from_text(value):
    text = str(value or "")
    if len(text) < 16:
        return None
    try:
        return int(text[11:13]) * 60 + int(text[14:16])
    except ValueError:
        return None


def timestamp_text_from_quality(value):
    if not value:
        return None
    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    generated_at = payload.get("generated_at")
    return str(generated_at) if generated_at else None


def feature_run_effective_timestamp(latest):
    """Use the freshest trusted v4 generation timestamp while exposing metadata drift."""
    latest = latest or {}
    metadata_timestamp = latest.get("updated_at") or latest.get("created_at")
    quality_timestamp = latest.get("quality_generated_at") or timestamp_text_from_quality(latest.get("quality"))
    metadata_minutes = time_minutes_from_text(metadata_timestamp)
    quality_minutes = time_minutes_from_text(quality_timestamp)
    if quality_minutes is not None and (metadata_minutes is None or quality_minutes > metadata_minutes):
        return quality_timestamp, "quality_generated_at"
    if metadata_minutes is not None:
        return metadata_timestamp, "metadata_timestamp"
    if quality_minutes is not None:
        return quality_timestamp, "quality_generated_at"
    return None, "missing"


def date_lag_days(later, earlier):
    later_date = parse_date(later)
    earlier_date = parse_date(earlier)
    if not later_date or not earlier_date:
        return None
    return (later_date - earlier_date).days


def market_code(exchange):
    return "HK" if exchange == "HKEX" else "US"


def normalize_stock_rows(stock_rows, kline_rows, signal_rows):
    grouped = defaultdict(set)
    if stock_rows:
        for row in stock_rows:
            market = row.get("market") or market_code(row.get("exchange"))
            symbol = str(row.get("symbol") or "").upper()
            if market and symbol:
                grouped[market].add(symbol)
    else:
        for row in (kline_rows or []) + (signal_rows or []):
            market = row.get("market") or market_code(row.get("exchange"))
            symbol = str(row.get("symbol") or "").upper()
            if market and symbol:
                grouped[market].add(symbol)
    return {market: sorted(symbols) for market, symbols in grouped.items()}


def normalize_kline_points(kline_rows):
    points = defaultdict(list)
    for row in kline_rows or []:
        market = row.get("market") or market_code(row.get("exchange"))
        symbol = str(row.get("symbol") or "").upper()
        if not market or not symbol:
            continue
        normalized = dict(row)
        normalized["market"] = market
        normalized["symbol"] = symbol
        normalized["date"] = str(row.get("date") or row.get("latest_date") or "")[:10]
        points[(market, symbol)].append(normalized)
    return points


def duplicate_symbol_day_summary(kline_rows):
    duplicate_count = 0
    duplicate_dates = Counter()
    duplicate_examples = []
    for row in kline_rows or []:
        duplicates = as_int(row.get("duplicate_symbol_day_count"), 0)
        if duplicates <= 0:
            continue
        duplicate_count += duplicates
        date_text = str(row.get("date") or row.get("latest_date") or "")[:10]
        if date_text:
            duplicate_dates[date_text] += duplicates
        if len(duplicate_examples) < 20:
            duplicate_examples.append(
                {
                    "symbol": str(row.get("symbol") or "").upper(),
                    "date": date_text,
                    "duplicate_symbol_day_count": duplicates,
                    "raw_symbol_day_row_count": as_int(row.get("raw_symbol_day_row_count"), 0),
                }
            )
    return {
        "duplicate_symbol_day_count": duplicate_count,
        "duplicate_date_counts": dict(duplicate_dates),
        "duplicate_examples": duplicate_examples,
    }


def latest_point(points):
    dated = [row for row in points if row.get("date")]
    if not dated:
        return {}
    return sorted(dated, key=lambda row: row["date"])[-1]


def history_rows(points):
    if not points:
        return 0
    explicit = [as_int(row.get("history_rows_120d"), None) for row in points if row.get("history_rows_120d") not in (None, "")]
    if explicit:
        return max(explicit)
    return len([row for row in points if row.get("date")])


def is_repair_source(value):
    return "repair" in str(value or "").lower()


def source_family(value):
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "repair" in text:
        return "repair"
    if text.startswith("tencent"):
        return "tencent"
    return text


def build_source_quality(symbol_summaries):
    daily_sources = Counter()
    minute_sources = Counter()
    repair_latest = []
    missing_latest_source = []
    missing_minute_source = []
    source_mismatch = []
    daily_with_source = 0
    minute_with_source = 0
    for item in symbol_summaries:
        daily_source = item.get("data_source")
        minute_source = item.get("latest_minute_data_source")
        if item.get("latest_date"):
            daily_sources[str(daily_source or "missing")] += 1
            if daily_source:
                daily_with_source += 1
            else:
                missing_latest_source.append(item["symbol"])
            if is_repair_source(daily_source):
                repair_latest.append(
                    {
                        "symbol": item["symbol"],
                        "latest_daily_date": item.get("latest_date"),
                        "data_source": daily_source,
                    }
                )
        if item.get("latest_minute_date"):
            minute_sources[str(minute_source or "missing")] += 1
            if minute_source:
                minute_with_source += 1
            else:
                missing_minute_source.append(item["symbol"])
            daily_family = source_family(daily_source)
            minute_family = source_family(minute_source)
            if daily_family and minute_family and daily_family != minute_family:
                source_mismatch.append(
                    {
                        "symbol": item["symbol"],
                        "daily_source": daily_source,
                        "minute_source": minute_source,
                        "daily_source_family": daily_family,
                        "minute_source_family": minute_family,
                        "latest_daily_date": item.get("latest_date"),
                        "latest_minute_date": item.get("latest_minute_date"),
                    }
                )
    latest_count = sum(daily_sources.values())
    minute_count = sum(minute_sources.values())
    repair_count = len(repair_latest)
    return {
        "schema": "kline_source_quality_v1",
        "daily_latest_source_counts": dict(daily_sources),
        "minute_latest_source_counts": dict(minute_sources),
        "source_family_normalization": {
            "tencent": ["tencent", "tencent_hk", "tencent_us", "tencent_min"],
            "repair": ["values containing repair"],
        },
        "daily_latest_source_coverage_pct": rate(daily_with_source, latest_count),
        "minute_latest_source_coverage_pct": rate(minute_with_source, minute_count),
        "missing_daily_latest_source_count": len(missing_latest_source),
        "missing_minute_latest_source_count": len(missing_minute_source),
        "repair_daily_latest_count": repair_count,
        "repair_daily_latest_pct": rate(repair_count, latest_count),
        "daily_minute_source_mismatch_count": len(source_mismatch),
        "sample_repair_daily_latest_symbols": repair_latest[:20],
        "sample_missing_daily_latest_source_symbols": missing_latest_source[:20],
        "sample_missing_minute_latest_source_symbols": missing_minute_source[:20],
        "sample_daily_minute_source_mismatches": source_mismatch[:20],
        "notes": [
            "Counts use each active symbol's latest daily and latest minute K-line rows only.",
            "Repair data sources are expected after manual hash-confirmed repairs but should remain explainable and limited.",
        ],
    }


def latest_ohlc_errors(row):
    if not row:
        return ["missing_latest_kline"]
    errors = []
    open_price = as_float(row.get("open"))
    high = as_float(row.get("high"))
    low = as_float(row.get("low"))
    close = as_float(row.get("close"))
    for name, value in (("open", open_price), ("high", high), ("low", low), ("close", close)):
        if value is None:
            errors.append(f"missing_{name}")
        elif value <= 0:
            errors.append(f"non_positive_{name}")
    if high is not None and low is not None and high < low:
        errors.append("high_below_low")
    if high is not None and low is not None and close is not None and not (low <= close <= high):
        errors.append("close_outside_high_low")
    if high is not None and low is not None and open_price is not None and not (low <= open_price <= high):
        errors.append("open_outside_high_low")
    return errors


def signal_summary(signal_rows, market, latest_kline_date):
    rows_for_market = [row for row in signal_rows or [] if (row.get("market") or market_code(row.get("exchange"))) == market]
    if not rows_for_market:
        return {
            "status": "WARN",
            "latest_signal_date": None,
            "count": 0,
            "by_side": {},
            "lag_days_vs_latest_kline": None,
            "notes": ["missing_signal_rows"],
        }

    aggregate_rows = [row for row in rows_for_market if row.get("latest_signal_date") or row.get("signal_count") is not None]
    if aggregate_rows:
        row = sorted(aggregate_rows, key=lambda item: str(item.get("latest_signal_date") or ""))[-1]
        latest_signal_date = row.get("latest_signal_date")
        count = as_int(row.get("signal_count"))
        by_side = {
            "BUY": as_int(row.get("buy_count")),
            "HOLD": as_int(row.get("hold_count")),
            "SELL": as_int(row.get("sell_count")),
        }
    else:
        latest_signal_date = max((row.get("trade_date") for row in rows_for_market if row.get("trade_date")), default=None)
        latest_rows = [row for row in rows_for_market if row.get("trade_date") == latest_signal_date]
        counts = Counter(str(row.get("signal_side") or "UNKNOWN").upper() for row in latest_rows)
        count = len(latest_rows)
        by_side = dict(counts)

    lag_days = date_lag_days(latest_kline_date, latest_signal_date)
    notes = []
    status = "OK"
    if latest_kline_date and latest_signal_date and lag_days is not None and lag_days >= SIGNAL_STALE_WARN_DAYS:
        status = "WARN"
        notes.append("signal_rows_lag_latest_klines")
    if not latest_signal_date:
        status = "WARN"
        notes.append("missing_latest_signal_date")
    return {
        "status": status,
        "latest_signal_date": latest_signal_date,
        "count": count,
        "by_side": by_side,
        "lag_days_vs_latest_kline": lag_days,
        "notes": notes,
    }


def summarize_market(market, symbols, kline_points, signal_rows):
    symbol_summaries = []
    latest_dates = Counter()
    data_sources = Counter()
    invalid_latest = []
    duplicate_symbol_day_count = 0
    duplicate_date_counts = Counter()
    duplicate_examples = []
    for symbol in symbols:
        points = kline_points.get((market, symbol), [])
        latest = latest_point(points)
        duplicates = duplicate_symbol_day_summary(points)
        duplicate_symbol_day_count += duplicates["duplicate_symbol_day_count"]
        duplicate_date_counts.update(duplicates["duplicate_date_counts"])
        duplicate_examples.extend(duplicates["duplicate_examples"])
        latest_date = latest.get("date")
        if latest_date:
            latest_dates[latest_date] += 1
            data_sources[str(latest.get("data_source") or "missing")] += 1
        errors = [] if not latest else latest_ohlc_errors(latest)
        if errors:
            invalid_latest.append({"symbol": symbol, "latest_date": latest_date, "errors": errors[:8]})
        symbol_summaries.append(
            {
                "symbol": symbol,
                "latest_date": latest_date,
                "latest_minute_date": latest.get("latest_minute_date") or None,
                "latest_minute_data_source": latest.get("latest_minute_data_source") or None,
                "history_rows_120d": history_rows(points),
                "data_source": latest.get("data_source") or None,
                "integrity_errors": errors,
                "duplicate_symbol_day_count": duplicates["duplicate_symbol_day_count"],
            }
        )

    active_count = len(symbols)
    symbols_with_klines = len([item for item in symbol_summaries if item["latest_date"]])
    latest_date = max(latest_dates) if latest_dates else None
    latest_count = len([item for item in symbol_summaries if item["latest_date"] == latest_date])
    missing = [item["symbol"] for item in symbol_summaries if not item["latest_date"]]
    stale = [
        item["symbol"]
        for item in symbol_summaries
        if item["latest_date"] and latest_date and item["latest_date"] < latest_date
    ]
    minute_fresh_daily_stale = [
        item
        for item in symbol_summaries
        if item["latest_date"]
        and item.get("latest_minute_date")
        and latest_date
        and item["latest_date"] < latest_date
        and item["latest_minute_date"] >= latest_date
    ]
    history_60d_count = len([item for item in symbol_summaries if item["history_rows_120d"] >= 60])
    signal = signal_summary(signal_rows, market, latest_date)

    failures = []
    warnings = []
    if active_count == 0:
        failures.append("no_active_symbols")
    if active_count and symbols_with_klines == 0:
        failures.append("no_daily_klines_for_active_symbols")
    if invalid_latest:
        failures.append("invalid_latest_ohlc")
    if duplicate_symbol_day_count:
        failures.append("duplicate_daily_symbol_dates")
    latest_coverage_pct = rate(latest_count, active_count)
    history_60d_coverage_pct = rate(history_60d_count, active_count)
    if active_count and latest_coverage_pct < 50:
        failures.append("latest_kline_coverage_below_50pct")
    elif active_count and latest_coverage_pct < MIN_LATEST_COVERAGE_PCT:
        warnings.append(f"latest_kline_coverage_below_{MIN_LATEST_COVERAGE_PCT:g}pct")
    if active_count and history_60d_coverage_pct < MIN_HISTORY_60D_COVERAGE_PCT:
        warnings.append(f"history_60d_coverage_below_{MIN_HISTORY_60D_COVERAGE_PCT:g}pct")
    if missing:
        warnings.append("active_symbols_missing_daily_klines")
    if stale:
        warnings.append("active_symbols_stale_vs_market_latest")
    if minute_fresh_daily_stale:
        warnings.append("minute_fresh_daily_stale_symbols")
    if signal["status"] != "OK":
        warnings.extend(signal["notes"])
    source_quality = build_source_quality(symbol_summaries)
    if source_quality["missing_daily_latest_source_count"]:
        warnings.append("daily_latest_data_source_missing")
    if source_quality["repair_daily_latest_count"]:
        warnings.append("daily_latest_contains_repair_sources")

    status = "FAIL" if failures else "WARN" if warnings else "OK"
    return {
        "market": market,
        "status": status,
        "active_symbol_count": active_count,
        "symbols_with_day_klines": symbols_with_klines,
        "latest_date": latest_date,
        "latest_date_distribution": dict(latest_dates),
        "data_source_counts": dict(data_sources),
        "coverage": {
            "latest_date_count": latest_count,
            "latest_date_coverage_pct": latest_coverage_pct,
            "missing_kline_symbol_count": len(missing),
            "stale_vs_market_latest_count": len(stale),
            "minute_fresh_daily_stale_count": len(minute_fresh_daily_stale),
            "history_60d_count": history_60d_count,
            "history_60d_coverage_pct": history_60d_coverage_pct,
        },
        "integrity": {
            "invalid_latest_ohlc_count": len(invalid_latest),
            "invalid_latest_ohlc_examples": invalid_latest[:20],
            "duplicate_daily_symbol_date_count": duplicate_symbol_day_count,
            "duplicate_daily_symbol_date_counts_by_date": dict(duplicate_date_counts),
            "duplicate_daily_symbol_date_examples": duplicate_examples[:20],
        },
        "source_quality": source_quality,
        "signals": signal,
        "failures": failures,
        "warnings": warnings,
        "sample_missing_symbols": missing[:20],
        "sample_stale_symbols": stale[:20],
        "sample_minute_fresh_daily_stale_symbols": [
            {
                "symbol": item["symbol"],
                "latest_daily_date": item["latest_date"],
                "latest_minute_date": item.get("latest_minute_date"),
            }
            for item in minute_fresh_daily_stale[:20]
        ],
    }


def feature_run_timing_notes(latest, current_dt):
    notes = []
    trade_date = parse_date(latest.get("trade_date"))
    if not trade_date or trade_date != current_dt.date():
        return notes
    ready_minutes = parse_hhmm_minutes(DAILY_SIGNAL_READY_TIME)
    if minutes_since_midnight(current_dt) < ready_minutes:
        notes.append("current_session_before_daily_signal_ready_time")
    effective_timestamp, timestamp_source = feature_run_effective_timestamp(latest)
    effective_minutes = time_minutes_from_text(effective_timestamp)
    metadata_minutes = time_minutes_from_text(latest.get("updated_at") or latest.get("created_at"))
    quality_minutes = time_minutes_from_text(latest.get("quality_generated_at"))
    if (
        metadata_minutes is not None
        and quality_minutes is not None
        and metadata_minutes < quality_minutes
    ):
        notes.append("feature_run_metadata_timestamp_lagged_quality_generated_at")
    if timestamp_source == "quality_generated_at" and metadata_minutes is None:
        notes.append("feature_run_effective_timestamp_from_quality_generated_at")
    if effective_minutes is None:
        notes.append("latest_daily_signal_run_timestamp_missing")
    elif effective_minutes < ready_minutes:
        notes.append("latest_daily_signal_run_generated_before_full_day_cutoff")
    return notes


def feature_run_remediation(latest, status, notes, current_dt):
    latest = latest or {}
    notes = notes or []
    ready_minutes = parse_hhmm_minutes(DAILY_SIGNAL_READY_TIME)
    current_after_cutoff = minutes_since_midnight(current_dt) >= ready_minutes
    effective_timestamp, timestamp_source = feature_run_effective_timestamp(latest)
    updated_minutes = time_minutes_from_text(effective_timestamp)
    generated_before_cutoff = updated_minutes is not None and updated_minutes < ready_minutes

    remediation = {
        "schema": "signal_v4_daily_run_remediation_v1",
        "status": "not_required",
        "required_action": "none",
        "reason": "latest_feature_run_is_acceptable",
        "read_only_report": True,
        "submits_orders": False,
        "changes_crontab": False,
        "write_command_requires_operator": True,
        "current_time": current_dt.isoformat(timespec="seconds"),
        "daily_signal_ready_time": DAILY_SIGNAL_READY_TIME,
        "current_after_cutoff": current_after_cutoff,
        "latest_run_id": latest.get("run_id"),
        "latest_trade_date": latest.get("trade_date"),
        "latest_status": latest.get("status"),
        "latest_expected_count": latest.get("expected_count"),
        "latest_ready_count": latest.get("ready_count"),
        "latest_missing_count": latest.get("missing_count"),
        "latest_created_at": latest.get("created_at"),
        "latest_updated_at": latest.get("updated_at"),
        "latest_quality_generated_at": latest.get("quality_generated_at"),
        "latest_effective_generated_at": effective_timestamp,
        "latest_effective_generated_at_source": timestamp_source,
        "latest_generated_before_cutoff": generated_before_cutoff,
        "notes": notes,
        "safe_preflight_command": f"{SIGNAL_ENGINE_COMMAND} --preflight --json",
        "manual_write_command": SIGNAL_ENGINE_COMMAND,
        "post_run_verification_commands": [
            DATA_HEALTH_COMMAND,
            SYSTEM_HEALTH_COMMAND,
            READINESS_REFRESH_COMMAND,
        ],
    }

    if not latest:
        remediation.update(
            {
                "status": "operator_action_required",
                "required_action": "run_signal_engine_v4_preflight_then_operator_run_if_ok",
                "reason": "missing_signal_v4_feature_run_rows",
            }
        )
    elif "current_session_before_daily_signal_ready_time" in notes:
        remediation.update(
            {
                "status": "wait",
                "required_action": "wait_until_daily_signal_ready_time_then_run_signal_engine_v4_preflight",
                "reason": "current_session_is_before_full_day_signal_cutoff",
            }
        )
    elif "latest_daily_signal_run_generated_before_full_day_cutoff" in notes and current_after_cutoff:
        remediation.update(
            {
                "status": "operator_action_required",
                "required_action": "run_signal_engine_v4_post_close_under_operator_control",
                "reason": "latest_current_day_signal_v4_run_was_generated_before_full_day_cutoff",
            }
        )
    elif "latest_daily_signal_run_timestamp_missing" in notes:
        remediation.update(
            {
                "status": "operator_action_required",
                "required_action": "inspect_signal_v4_feature_run_timestamp_then_run_preflight",
                "reason": "latest_signal_v4_feature_run_timestamp_missing",
            }
        )
    elif status != "OK":
        remediation.update(
            {
                "status": "operator_action_required",
                "required_action": "inspect_signal_engine_v4_preflight_and_latest_feature_run",
                "reason": "latest_signal_v4_feature_run_not_ready",
            }
        )

    return remediation


def feature_run_summary(feature_run_rows, current_dt=None):
    rows_in = feature_run_rows or []
    current_dt = current_dt or datetime.now()
    if not rows_in:
        notes = ["missing_feature_run_rows"]
        return {
            "status": "WARN",
            "latest": None,
            "notes": notes,
            "daily_signal_ready_time": DAILY_SIGNAL_READY_TIME,
            "remediation": feature_run_remediation(None, "WARN", notes, current_dt),
        }
    latest = rows_in[0]
    status = "OK" if latest.get("status") == "signal_ready" and as_int(latest.get("ready_count")) > 0 else "WARN"
    notes = [] if status == "OK" else ["latest_feature_run_not_signal_ready"]
    effective_timestamp, timestamp_source = feature_run_effective_timestamp(latest)
    latest["effective_generated_at"] = effective_timestamp
    latest["effective_generated_at_source"] = timestamp_source
    timing_notes = feature_run_timing_notes(latest, current_dt)
    if timing_notes:
        blocking_timing_notes = [
            note
            for note in timing_notes
            if note
            not in (
                "feature_run_metadata_timestamp_lagged_quality_generated_at",
                "feature_run_effective_timestamp_from_quality_generated_at",
            )
        ]
        if blocking_timing_notes:
            status = "FAIL"
        notes.extend(timing_notes)
    return {
        "status": status,
        "latest": latest,
        "notes": notes,
        "daily_signal_ready_time": DAILY_SIGNAL_READY_TIME,
        "remediation": feature_run_remediation(latest, status, notes, current_dt),
    }


def build_recommendations(markets, feature_run):
    recs = []
    for market, summary in sorted(markets.items()):
        for failure in summary["failures"]:
            recs.append(f"{market}:block_execution_until_data_failure_fixed:{failure}")
        for warning in summary["warnings"]:
            recs.append(f"{market}:review_data_warning:{warning}")
    if feature_run.get("status") != "OK":
        recs.append("review_signal_v4_feature_run_before_trusting_new_daily_signals")
    if feature_run.get("status") == "FAIL":
        recs.append("block_execution_until_signal_v4_full_day_run_ready")
    if not recs:
        recs.append("data_health_ok_for_review_context")
    return recs


def daily_gap_remediation(markets):
    gap_symbols = []
    for market, summary in sorted((markets or {}).items()):
        for item in summary.get("sample_minute_fresh_daily_stale_symbols") or []:
            symbol = item.get("symbol")
            if symbol:
                gap_symbols.append(
                    {
                        "market": market,
                        "symbol": symbol,
                        "latest_daily_date": item.get("latest_daily_date"),
                        "latest_minute_date": item.get("latest_minute_date"),
                    }
                )
    status = "operator_action_required" if gap_symbols else "not_required"
    return {
        "schema": "kline_daily_gap_remediation_v1",
        "status": status,
        "read_only_report": True,
        "submits_orders": False,
        "changes_crontab": False,
        "write_command_requires_operator": True,
        "gap_symbol_count": len(gap_symbols),
        "gap_symbols": gap_symbols[:40],
        "dry_run_command": KLINE_DAILY_GAP_REPAIR_COMMAND,
        "apply_command_template": KLINE_DAILY_GAP_REPAIR_COMMAND
        + " --apply --confirm-plan-hash <plan_hash>",
        "post_run_verification_commands": [
            DATA_HEALTH_COMMAND,
            SYSTEM_HEALTH_COMMAND,
            READINESS_REFRESH_COMMAND,
        ],
    }


def fetch_kline_rows():
    data_source_expr = "k.data_source" if "data_source" in table_columns("klines") else "'missing'"
    minute_data_source_expr = "k.data_source" if "data_source" in table_columns("klines") else "'missing'"
    sql = """
        WITH active AS (
            SELECT CASE WHEN exchange = 'HKEX' THEN 'HK' ELSE 'US' END AS market,
                   exchange,
                   symbol
            FROM stocks
            WHERE is_active = true
              AND exchange IN ('HKEX','NASDAQ','NYSE')
        ),
        raw_daily AS (
            SELECT k.symbol,
                   k.timestamp::date AS trade_date,
                   count(*) AS raw_symbol_day_row_count
            FROM klines k
            JOIN active a ON a.symbol = k.symbol
            WHERE k.interval = 'day'
            GROUP BY k.symbol, k.timestamp::date
        ),
        daily_bar AS (
            SELECT DISTINCT ON (k.symbol, k.timestamp::date)
                   k.symbol,
                   k.timestamp::date AS trade_date,
                   k.open_price, k.high_price, k.low_price, k.close_price,
                   k.volume, {data_source_expr} AS data_source,
                   COALESCE(r.raw_symbol_day_row_count, 0) AS raw_symbol_day_row_count,
                   GREATEST(COALESCE(r.raw_symbol_day_row_count, 0) - 1, 0) AS duplicate_symbol_day_count
            FROM klines k
            JOIN active a ON a.symbol = k.symbol
            LEFT JOIN raw_daily r
              ON r.symbol = k.symbol
             AND r.trade_date = k.timestamp::date
            WHERE k.interval = 'day'
            ORDER BY k.symbol, k.timestamp::date, k.timestamp DESC
        ),
        latest AS (
            SELECT DISTINCT ON (a.symbol)
                   a.market, a.exchange, a.symbol,
                   d.trade_date AS latest_date,
                   d.open_price, d.high_price, d.low_price, d.close_price,
                   d.volume, d.data_source,
                   COALESCE(d.raw_symbol_day_row_count, 0) AS raw_symbol_day_row_count,
                   COALESCE(d.duplicate_symbol_day_count, 0) AS duplicate_symbol_day_count
            FROM active a
            LEFT JOIN daily_bar d
              ON d.symbol = a.symbol
            ORDER BY a.symbol, d.trade_date DESC NULLS LAST
        ),
        history AS (
            SELECT a.symbol,
                   count(d.*) FILTER (WHERE d.trade_date >= CURRENT_DATE - INTERVAL '120 days') AS history_rows_120d,
                   COALESCE(sum(d.duplicate_symbol_day_count), 0) AS duplicate_symbol_day_count_120d
            FROM active a
            LEFT JOIN daily_bar d
              ON d.symbol = a.symbol
            GROUP BY a.symbol
        ),
        minute_latest AS (
            SELECT DISTINCT ON (a.symbol)
                   a.symbol,
                   k.timestamp::date AS latest_minute_date,
                   {minute_data_source_expr} AS latest_minute_data_source
            FROM active a
            LEFT JOIN klines k
              ON k.symbol = a.symbol
             AND k.interval = 'min'
            ORDER BY a.symbol, k.timestamp DESC NULLS LAST
        )
        SELECT l.market, l.exchange, l.symbol, COALESCE(h.history_rows_120d, 0),
               l.latest_date, l.open_price, l.high_price, l.low_price, l.close_price,
               l.volume, l.data_source, m.latest_minute_date, m.latest_minute_data_source,
               COALESCE(h.duplicate_symbol_day_count_120d, 0),
               COALESCE(l.raw_symbol_day_row_count, 0),
               COALESCE(l.duplicate_symbol_day_count, 0)
        FROM latest l
        LEFT JOIN history h ON h.symbol = l.symbol
        LEFT JOIN minute_latest m ON m.symbol = l.symbol
        ORDER BY l.market, l.symbol
    """.format(data_source_expr=data_source_expr, minute_data_source_expr=minute_data_source_expr)
    r = psql(sql)
    if r.returncode != 0:
        return [], [f"kline_coverage_query_failed:{r.stderr.strip()}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 11:
            continue
        parsed.append(
            {
                "market": row[0],
                "exchange": row[1],
                "symbol": row[2],
                "history_rows_120d": as_int(row[3]),
                "date": row[4],
                "open": as_float(row[5]),
                "high": as_float(row[6]),
                "low": as_float(row[7]),
                "close": as_float(row[8]),
                "volume": as_float(row[9]),
                "data_source": row[10],
                "latest_minute_date": row[11] if len(row) > 11 else None,
                "latest_minute_data_source": row[12] if len(row) > 12 else None,
                "duplicate_symbol_day_count": as_int(row[13]) if len(row) > 13 else 0,
                "raw_symbol_day_row_count": as_int(row[14]) if len(row) > 14 else 0,
                "latest_duplicate_symbol_day_count": as_int(row[15]) if len(row) > 15 else 0,
            }
        )
    return parsed, []


def fetch_signal_rows():
    sql = f"""
        WITH scored AS (
            SELECT CASE WHEN s.exchange = 'HKEX' THEN 'HK' ELSE 'US' END AS market,
                   e.trade_date,
                   e.signal_side
            FROM engine_signal_scores e
            JOIN stocks s ON s.symbol = e.symbol
            WHERE e.model_version = '{sql_quote(SIGNAL_MODEL_VERSION)}'
              AND e.feature_version = '{sql_quote(SIGNAL_FEATURE_VERSION)}'
              AND s.exchange IN ('HKEX','NASDAQ','NYSE')
        ),
        latest AS (
            SELECT market, max(trade_date) AS latest_signal_date
            FROM scored
            GROUP BY market
        )
        SELECT s.market, l.latest_signal_date, count(*),
               count(*) FILTER (WHERE s.signal_side = 'BUY'),
               count(*) FILTER (WHERE s.signal_side = 'HOLD'),
               count(*) FILTER (WHERE s.signal_side = 'SELL')
        FROM scored s
        JOIN latest l ON l.market = s.market AND l.latest_signal_date = s.trade_date
        GROUP BY s.market, l.latest_signal_date
        ORDER BY s.market
    """
    r = psql(sql)
    if r.returncode != 0:
        return [], [f"signal_summary_query_failed:{r.stderr.strip()}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 6:
            continue
        parsed.append(
            {
                "market": row[0],
                "latest_signal_date": row[1],
                "signal_count": as_int(row[2]),
                "buy_count": as_int(row[3]),
                "hold_count": as_int(row[4]),
                "sell_count": as_int(row[5]),
            }
        )
    return parsed, []


def fetch_feature_run_rows():
    expected_expr = first_existing("engine_feature_runs", ("expected_count", "expected_symbols"), "NULL")
    ready_expr = first_existing("engine_feature_runs", ("ready_count", "ready_symbols"), "NULL")
    missing_expr = first_existing("engine_feature_runs", ("missing_count", "missing_symbols"), "NULL")
    created_expr = first_existing("engine_feature_runs", ("created_at",), "NULL")
    updated_expr = first_existing("engine_feature_runs", ("updated_at",), "NULL")
    quality_expr = "quality->>'generated_at'" if "quality" in table_columns("engine_feature_runs") else "NULL"
    sql = """
        SELECT run_id, trade_date, status, {expected_expr}, {ready_expr}, {missing_expr},
               {created_expr}, {updated_expr}, {quality_expr}
        FROM engine_feature_runs
        WHERE run_id LIKE 'signal_v4_%'
        ORDER BY trade_date DESC, run_id DESC
        LIMIT 3
    """.format(
        expected_expr=expected_expr,
        ready_expr=ready_expr,
        missing_expr=missing_expr,
        created_expr=created_expr,
        updated_expr=updated_expr,
        quality_expr=quality_expr,
    )
    r = psql(sql)
    if r.returncode != 0:
        return [], [f"feature_run_query_failed:{r.stderr.strip()}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 6:
            continue
        parsed.append(
            {
                "run_id": row[0],
                "trade_date": row[1],
                "status": row[2],
                "expected_count": as_int(row[3]),
                "ready_count": as_int(row[4]),
                "missing_count": as_int(row[5]),
                "created_at": row[6] if len(row) > 6 else None,
                "updated_at": row[7] if len(row) > 7 else None,
                "quality_generated_at": row[8] if len(row) > 8 else None,
            }
        )
    return parsed, []


def build_report(stock_rows=None, kline_rows=None, signal_rows=None, feature_run_rows=None, current_dt=None):
    warnings = []
    _COLUMN_CACHE.clear()
    if kline_rows is None:
        kline_rows, kline_warnings = fetch_kline_rows()
        warnings.extend(kline_warnings)
    if signal_rows is None:
        signal_rows, signal_warnings = fetch_signal_rows()
        warnings.extend(signal_warnings)
    if feature_run_rows is None:
        feature_run_rows, feature_warnings = fetch_feature_run_rows()
        warnings.extend(feature_warnings)

    symbols_by_market = normalize_stock_rows(stock_rows, kline_rows, signal_rows)
    kline_points = normalize_kline_points(kline_rows)
    markets = {
        market: summarize_market(market, symbols, kline_points, signal_rows)
        for market, symbols in sorted(symbols_by_market.items())
    }
    feature_run = feature_run_summary(feature_run_rows, current_dt=current_dt)
    status = max_status([summary["status"] for summary in markets.values()] + [feature_run["status"]])
    if warnings and status == "OK":
        status = "WARN"
    gap_remediation = daily_gap_remediation(markets)
    payload = {
        "schema": "data_health_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "stock_table": "stocks",
            "kline_table": "klines",
            "signal_table": "engine_signal_scores",
            "feature_run_table": "engine_feature_runs",
            "signal_model_version": SIGNAL_MODEL_VERSION,
            "signal_feature_version": SIGNAL_FEATURE_VERSION,
        },
        "markets": markets,
        "feature_run": feature_run,
        "daily_gap_remediation": gap_remediation,
        "recommendations": build_recommendations(markets, feature_run),
        "warnings": warnings,
    }
    return payload


def build_text_report(payload):
    lines = [f"Data health report {payload['generated_at']} status={payload['status']}"]
    for market, summary in sorted((payload.get("markets") or {}).items()):
        lines.append(
            f"{market}: status={summary['status']} active={summary['active_symbol_count']} "
            f"latest={summary['latest_date']} latest_coverage={summary['coverage']['latest_date_coverage_pct']}% "
            f"history60={summary['coverage']['history_60d_coverage_pct']}% "
            f"minute_fresh_daily_stale={summary['coverage'].get('minute_fresh_daily_stale_count', 0)} "
            f"invalid_ohlc={summary['integrity']['invalid_latest_ohlc_count']} "
            f"duplicate_day_rows={summary['integrity'].get('duplicate_daily_symbol_date_count', 0)} "
            f"signal_date={summary['signals']['latest_signal_date']} lag={summary['signals']['lag_days_vs_latest_kline']}"
        )
        source_quality = summary.get("source_quality") or {}
        if source_quality:
            lines.append(
                f"{market}_source_quality: daily_sources={source_quality.get('daily_latest_source_counts', {})} "
                f"repair_daily_latest={source_quality.get('repair_daily_latest_count', 0)} "
                f"missing_daily_source={source_quality.get('missing_daily_latest_source_count', 0)} "
                f"minute_sources={source_quality.get('minute_latest_source_counts', {})}"
            )
    feature = payload.get("feature_run") or {}
    latest = feature.get("latest") or {}
    lines.append(
        f"feature_run: status={feature.get('status')} run_id={latest.get('run_id')} "
        f"trade_date={latest.get('trade_date')} ready={latest.get('ready_count')}/{latest.get('expected_count')} "
        f"effective_generated_at={latest.get('effective_generated_at')}"
    )
    if feature.get("notes"):
        lines.append("feature_run_notes: " + ", ".join(feature.get("notes") or []))
    remediation = feature.get("remediation") or {}
    if remediation and remediation.get("required_action") != "none":
        lines.append(
            "feature_run_remediation: status={status} action={action} reason={reason}".format(
                status=remediation.get("status"),
                action=remediation.get("required_action"),
                reason=remediation.get("reason"),
            )
        )
        if remediation.get("safe_preflight_command"):
            lines.append("feature_run_preflight: " + remediation["safe_preflight_command"])
        if remediation.get("manual_write_command"):
            lines.append("feature_run_manual_write: " + remediation["manual_write_command"])
    gap_remediation = payload.get("daily_gap_remediation") or {}
    if gap_remediation.get("status") == "operator_action_required":
        lines.append(
            "daily_gap_remediation: gaps={count} dry_run={command}".format(
                count=gap_remediation.get("gap_symbol_count"),
                command=gap_remediation.get("dry_run_command"),
            )
        )
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    lines.append("Recommendations: " + ", ".join(payload.get("recommendations") or []))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report()
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
    return 0 if payload["status"] in ("OK", "WARN") else 2


if __name__ == "__main__":
    sys.exit(main())
