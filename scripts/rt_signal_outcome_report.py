#!/usr/bin/env python3
"""Read-only forward outcome report for realtime v5 alerts.

This is intentionally conservative: hit analysis uses daily klines strictly
after the alert's quote date. Minute klines are used only to resolve
same-day stop/target ordering when a daily candle touched both levels, and
to classify no-lookahead intraday signal-time context for later learning.
"""
import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

try:
    import rt_order_intake as intake
except ImportError:
    from scripts import rt_order_intake as intake


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
KLINE_DAILY_GAP_REPAIR_FILE = os.environ.get("KLINE_DAILY_GAP_REPAIR_FILE", "/tmp/kline_daily_gap_repair.json")
KLINE_GAP_SOURCE_DIAGNOSTIC_FILE = os.environ.get(
    "KLINE_GAP_SOURCE_DIAGNOSTIC_FILE",
    "/tmp/kline_gap_source_diagnostic_report.json",
)
DEFAULT_HORIZONS = tuple(
    int(x.strip())
    for x in os.environ.get("RT_SIGNAL_OUTCOME_HORIZONS", "1,3,5").split(",")
    if x.strip().isdigit() and int(x.strip()) > 0
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


_TABLE_COLUMNS_CACHE = {}


def table_columns(table):
    table = str(table)
    if table in _TABLE_COLUMNS_CACHE:
        return _TABLE_COLUMNS_CACHE[table]
    sql = f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = '{sql_quote(table)}'
    """
    result = psql(sql, timeout=30)
    if result.returncode != 0:
        _TABLE_COLUMNS_CACHE[table] = set()
        return set()
    cols = {row[0] for row in rows(result.stdout) if row}
    _TABLE_COLUMNS_CACHE[table] = cols
    return cols


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


def load_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def signal_side(alert):
    return str(alert.get("signal_type", "")).upper()


def candidate_side(alert):
    return str(alert.get("candidate_signal_type") or signal_side(alert)).upper()


def is_directional(alert):
    return signal_side(alert) in ("BUY", "SELL")


def is_directional_candidate(alert):
    return candidate_side(alert) in ("BUY", "SELL")


def entry_price(alert):
    return as_float(alert.get("entry_price"), as_float(alert.get("price")))


def effective_entry_price(alert):
    return as_float(alert.get("entry_price"), as_float(alert.get("candidate_entry_price"), as_float(alert.get("price"))))


def effective_stop_loss(alert):
    return as_float(alert.get("stop_loss"), as_float(alert.get("candidate_stop_loss")))


def effective_take_profit(alert):
    return as_float(alert.get("take_profit"), as_float(alert.get("candidate_take_profit")))


def effective_rr_ratio(alert):
    return as_float(alert.get("rr_ratio"), as_float(alert.get("candidate_rr_ratio")))


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


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def quote_timestamp(alert):
    return parse_timestamp((alert or {}).get("quote_time")) or parse_timestamp((alert or {}).get("generated_at"))


def add_days_iso(day_text, days):
    try:
        day = datetime.strptime(str(day_text)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (day + timedelta(days=int(days))).isoformat()


def add_weekdays_iso(day_text, days):
    try:
        day = datetime.strptime(str(day_text)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    remaining = int(days)
    while remaining > 0:
        day = day + timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    return day.isoformat()


def calendar_pending_reason(item, needed_days):
    signal_date = item.get("signal_date")
    latest_kline = item.get("latest_kline_date")
    earliest_calendar = add_days_iso(signal_date, needed_days)
    earliest_weekday = add_weekdays_iso(signal_date, needed_days)
    if not earliest_weekday:
        return "unknown"
    if not latest_kline:
        return "missing_symbol_klines"
    if latest_kline < earliest_weekday:
        return "waiting_for_next_trading_day"
    if earliest_calendar and latest_kline < earliest_calendar:
        return "waiting_for_calendar_day"
    return "kline_gap_or_missing_symbol"


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


def first_threshold_hit_detail(side, window, stop_loss, take_profit):
    target_seen = False
    stop_seen = False
    first_hit = None
    first_hit_date = None
    for row in window:
        target_hit, stop_hit = threshold_state(side, row, stop_loss, take_profit)
        target_seen = target_seen or target_hit
        stop_seen = stop_seen or stop_hit
        if first_hit is None:
            if target_hit and stop_hit:
                first_hit = "ambiguous_same_day"
                first_hit_date = row.get("date")
            elif target_hit:
                first_hit = "target"
                first_hit_date = row.get("date")
            elif stop_hit:
                first_hit = "stop"
                first_hit_date = row.get("date")
    return {
        "target_seen": target_seen,
        "stop_seen": stop_seen,
        "first_hit": first_hit,
        "first_hit_date": first_hit_date,
    }


def first_threshold_hit(side, window, stop_loss, take_profit):
    detail = first_threshold_hit_detail(side, window, stop_loss, take_profit)
    return detail["target_seen"], detail["stop_seen"], detail["first_hit"]


def intraday_sequence_key(symbol, trade_date):
    return f"{str(symbol or '').upper()}|{str(trade_date or '')[:10]}"


FULL_OHLC_INTRADAY_SOURCES = {
    "broker_minute_ohlcv",
    "vendor_minute_ohlcv",
    "futu_minute_ohlcv",
    "ibkr_minute_ohlcv",
    "polygon_minute_ohlcv",
    "alpaca_minute_ohlcv",
    "full_minute_ohlcv",
}
FULL_OHLC_INTRADAY_GRANULARITIES = {
    "minute_ohlcv",
    "1m_ohlcv",
    "full_ohlcv",
    "broker_minute_ohlcv",
    "vendor_minute_ohlcv",
}
LOW_FIDELITY_INTRADAY_SOURCES = {
    "missing",
    "unknown",
    "tencent_minute_query",
}
LOW_FIDELITY_INTRADAY_GRANULARITIES = {
    "missing",
    "unknown",
    "minute_snapshot_price",
    "snapshot_price",
    "last_price_snapshot",
}


def normalized_text(value, default="missing"):
    return str(value if value not in (None, "") else default).strip().lower()


def intraday_row_has_full_ohlc_fidelity(row):
    source = normalized_text((row or {}).get("data_source"))
    granularity = normalized_text((row or {}).get("source_granularity"))
    return source in FULL_OHLC_INTRADAY_SOURCES or granularity in FULL_OHLC_INTRADAY_GRANULARITIES


def intraday_minute_path_fidelity(minute_rows):
    rows_in = list(minute_rows or [])
    source_counts = Counter(normalized_text(row.get("data_source")) for row in rows_in)
    granularity_counts = Counter(normalized_text(row.get("source_granularity")) for row in rows_in)
    snapshot_like_count = len(
        [
            row
            for row in rows_in
            if as_float(row.get("open")) == as_float(row.get("high"))
            and as_float(row.get("high")) == as_float(row.get("low"))
            and as_float(row.get("low")) == as_float(row.get("close"))
        ]
    )
    reasons = []
    if not rows_in:
        return {
            "schema": "intraday_minute_path_fidelity_v1",
            "status": "MISSING",
            "reasons": ["minute_rows_missing"],
            "row_count": 0,
            "source_counts": {},
            "source_granularity_counts": {},
            "snapshot_like_row_count": 0,
            "full_ohlc_row_count": 0,
        }

    low_sources = sorted(source for source in source_counts if source in LOW_FIDELITY_INTRADAY_SOURCES)
    low_granularities = sorted(
        granularity for granularity in granularity_counts if granularity in LOW_FIDELITY_INTRADAY_GRANULARITIES
    )
    if low_sources:
        reasons.append("low_fidelity_minute_sources:" + ",".join(low_sources))
    if low_granularities:
        reasons.append("low_fidelity_source_granularity:" + ",".join(low_granularities))
    if "missing" in source_counts or "unknown" in source_counts:
        reasons.append("minute_data_source_missing_or_unknown")
    if "missing" in granularity_counts or "unknown" in granularity_counts:
        reasons.append("minute_source_granularity_missing_or_unknown")

    full_ohlc_count = len([row for row in rows_in if intraday_row_has_full_ohlc_fidelity(row)])
    if full_ohlc_count != len(rows_in):
        reasons.append("minute_path_source_fidelity_unverified")
    if snapshot_like_count == len(rows_in):
        reasons.append("all_minute_rows_are_single_price_points")

    status = "FULL_OHLC" if full_ohlc_count == len(rows_in) and not reasons else "LOW_FIDELITY"
    return {
        "schema": "intraday_minute_path_fidelity_v1",
        "status": status,
        "reasons": reasons,
        "row_count": len(rows_in),
        "source_counts": dict(source_counts),
        "source_granularity_counts": dict(granularity_counts),
        "snapshot_like_row_count": snapshot_like_count,
        "full_ohlc_row_count": full_ohlc_count,
    }


def parse_intraday_kline_rows(stdout):
    parsed = []
    for row in rows(stdout):
        if len(row) < 5:
            continue
        parsed.append(
            {
                "timestamp": row[0],
                "date": parse_date(row[0]),
                "open": as_float(row[1]),
                "high": as_float(row[2]),
                "low": as_float(row[3]),
                "close": as_float(row[4]),
                "volume": as_float(row[5], 0.0) if len(row) > 5 else None,
                "data_source": row[6] if len(row) > 6 and row[6] else None,
                "source_granularity": row[7] if len(row) > 7 and row[7] else None,
            }
        )
    return sorted(parsed, key=lambda item: parse_timestamp(item.get("timestamp")) or datetime.min)


def fetch_intraday_klines(symbol_dates):
    intraday = {}
    warnings = []
    kline_columns = table_columns("klines")
    source_granularity_expr = (
        "COALESCE(source_granularity, 'missing')"
        if "source_granularity" in kline_columns
        else "'missing'"
    )
    keys = sorted(
        {
            (str(symbol or "").strip().upper(), str(trade_date or "")[:10])
            for symbol, trade_date in (symbol_dates or [])
            if str(symbol or "").strip() and str(trade_date or "")[:10]
        }
    )
    for symbol, trade_date in keys:
        sql = f"""
            SELECT timestamp,
                   open_price, high_price, low_price, close_price,
                   COALESCE(volume, 0),
                   COALESCE(data_source, 'missing'),
                   {source_granularity_expr}
            FROM klines
            WHERE interval = 'min'
              AND symbol = '{sql_quote(symbol)}'
              AND timestamp::date = '{sql_quote(trade_date)}'::date
            ORDER BY timestamp ASC
        """
        result = psql(sql)
        if result.returncode != 0:
            warnings.append(f"intraday_kline_query_failed:{symbol}:{trade_date}:{result.stderr.strip()}")
            intraday[intraday_sequence_key(symbol, trade_date)] = []
            continue
        intraday[intraday_sequence_key(symbol, trade_date)] = parse_intraday_kline_rows(result.stdout)
    return intraday, warnings


def intraday_signal_context_targets(alerts):
    targets = []
    seen = set()
    for alert in alerts or []:
        symbol = str((alert or {}).get("symbol") or "").strip().upper()
        signal_date = alert_signal_date(alert)
        cutoff = quote_timestamp(alert)
        if not symbol or not signal_date or cutoff is None:
            continue
        key = intraday_sequence_key(symbol, signal_date)
        if key in seen:
            continue
        seen.add(key)
        targets.append((symbol, signal_date, cutoff))
    return targets


def fetch_intraday_signal_context_klines(targets):
    intraday = {}
    warnings = []
    kline_columns = table_columns("klines")
    source_granularity_expr = (
        "COALESCE(source_granularity, 'missing')"
        if "source_granularity" in kline_columns
        else "'missing'"
    )
    for symbol, trade_date, cutoff in sorted(
        {
            (str(symbol or "").strip().upper(), str(trade_date or "")[:10], cutoff)
            for symbol, trade_date, cutoff in (targets or [])
            if str(symbol or "").strip() and str(trade_date or "")[:10] and cutoff is not None
        },
        key=lambda item: (item[0], item[1], item[2]),
    ):
        cutoff_text = cutoff.isoformat(sep=" ", timespec="seconds")
        sql = f"""
            SELECT timestamp,
                   open_price, high_price, low_price, close_price,
                   COALESCE(volume, 0),
                   COALESCE(data_source, 'missing'),
                   {source_granularity_expr}
            FROM klines
            WHERE interval = 'min'
              AND symbol = '{sql_quote(symbol)}'
              AND timestamp::date = '{sql_quote(trade_date)}'::date
              AND timestamp <= '{sql_quote(cutoff_text)}'::timestamp
            ORDER BY timestamp ASC
        """
        result = psql(sql)
        key = intraday_sequence_key(symbol, trade_date)
        if result.returncode != 0:
            warnings.append(f"intraday_signal_context_query_failed:{symbol}:{trade_date}:{result.stderr.strip()}")
            intraday[key] = []
            continue
        intraday[key] = parse_intraday_kline_rows(result.stdout)
    return intraday, warnings


def intraday_rows_for(intraday_klines_by_symbol_date, symbol, trade_date):
    if not isinstance(intraday_klines_by_symbol_date, dict):
        return []
    symbol = str(symbol or "").upper()
    trade_date = str(trade_date or "")[:10]
    if not symbol or not trade_date:
        return []
    flat_key = intraday_sequence_key(symbol, trade_date)
    if flat_key in intraday_klines_by_symbol_date:
        return intraday_klines_by_symbol_date.get(flat_key) or []
    tuple_key = (symbol, trade_date)
    if tuple_key in intraday_klines_by_symbol_date:
        return intraday_klines_by_symbol_date.get(tuple_key) or []
    nested = intraday_klines_by_symbol_date.get(symbol) or intraday_klines_by_symbol_date.get(symbol.lower())
    if isinstance(nested, dict):
        return nested.get(trade_date) or []
    return []


def latest_window_change(rows, minutes):
    ordered = [
        row
        for row in sorted(
            list(rows or []),
            key=lambda item: parse_timestamp(item.get("timestamp")) or datetime.min,
        )
        if parse_timestamp(row.get("timestamp")) is not None
    ]
    if not ordered:
        return None, []
    latest_ts = parse_timestamp(ordered[-1].get("timestamp"))
    if latest_ts is None:
        return None, []
    start_ts = latest_ts - timedelta(minutes=minutes)
    window = [row for row in ordered if (parse_timestamp(row.get("timestamp")) or datetime.min) > start_ts]
    if not window:
        return None, []
    first_open = as_float(window[0].get("open"), as_float(window[0].get("close")))
    last_close = as_float(window[-1].get("close"))
    if first_open in (None, 0) or last_close is None:
        return None, window
    return (last_close / first_open - 1.0) * 100.0, window


def momentum_label(change_pct):
    if change_pct is None:
        return "unknown"
    if change_pct >= 1.0:
        return "strong_up"
    if change_pct >= 0.25:
        return "up"
    if change_pct <= -1.0:
        return "strong_down"
    if change_pct <= -0.25:
        return "down"
    return "flat"


def direction_from_change(change_pct):
    label = momentum_label(change_pct)
    if label in ("strong_up", "up"):
        return "up"
    if label in ("strong_down", "down"):
        return "down"
    if label == "flat":
        return "flat"
    return "unknown"


def expected_direction(side):
    side = str(side or "").upper()
    if side == "BUY":
        return "up"
    if side == "SELL":
        return "down"
    return "unknown"


def normalize_intraday_signal_alignment(value):
    text = str(value or "unavailable_or_stale").strip() or "unavailable_or_stale"
    return INTRADAY_ALIGNMENT_ALIASES.get(text, text)


def signal_alignment_from_votes(side, votes):
    expected = expected_direction(side)
    if expected not in ("up", "down"):
        return "neutral_or_insufficient"
    opposite = "down" if expected == "up" else "up"
    directional = [vote for vote in votes if vote.get("direction") in ("up", "down")]
    if not directional:
        return "neutral_or_insufficient"
    expected_count = len([vote for vote in directional if vote.get("direction") == expected])
    opposite_count = len([vote for vote in directional if vote.get("direction") == opposite])
    if expected_count and not opposite_count:
        return "supports_signal"
    if opposite_count and not expected_count:
        return "challenges_signal"
    if expected_count and opposite_count:
        return "conflicting_timeframes"
    return "neutral_or_insufficient"


def intraday_signal_context_for_alert(alert, intraday_signal_klines_by_symbol_date=None):
    symbol = str((alert or {}).get("symbol") or "").strip().upper()
    trade_date = alert_signal_date(alert)
    cutoff = quote_timestamp(alert)
    if not symbol or not trade_date or cutoff is None:
        return {
            "schema": "intraday_signal_context_v1",
            "status": "MISSING",
            "alignment": "unavailable_or_stale",
            "row_count": 0,
            "votes": [],
        }
    rows_for_day = intraday_rows_for(intraday_signal_klines_by_symbol_date, symbol, trade_date)
    ordered = [
        row
        for row in sorted(
            list(rows_for_day or []),
            key=lambda item: parse_timestamp(item.get("timestamp")) or datetime.min,
        )
        if parse_timestamp(row.get("timestamp")) is not None and parse_timestamp(row.get("timestamp")) <= cutoff
    ]
    if not ordered:
        return {
            "schema": "intraday_signal_context_v1",
            "status": "MISSING",
            "alignment": "unavailable_or_stale",
            "symbol": symbol,
            "trade_date": trade_date,
            "cutoff_timestamp": cutoff.isoformat(timespec="seconds"),
            "row_count": 0,
            "votes": [],
        }
    session_open = as_float(ordered[0].get("open"), as_float(ordered[0].get("close")))
    session_close = as_float(ordered[-1].get("close"))
    session_change = (session_close / session_open - 1.0) * 100.0 if session_open not in (None, 0) and session_close is not None else None
    change_5m, rows_5m = latest_window_change(ordered, 5)
    change_15m, rows_15m = latest_window_change(ordered, 15)
    change_30m, rows_30m = latest_window_change(ordered, 30)
    change_60m, rows_60m = latest_window_change(ordered, 60)
    votes = [
        {
            "timeframe": "session_to_signal",
            "direction": direction_from_change(session_change),
            "momentum": momentum_label(session_change),
            "change_pct": round_or_none(session_change),
            "row_count": len(ordered),
        },
        {
            "timeframe": "latest_5m_before_signal",
            "direction": direction_from_change(change_5m),
            "momentum": momentum_label(change_5m),
            "change_pct": round_or_none(change_5m),
            "row_count": len(rows_5m),
        },
        {
            "timeframe": "latest_15m_before_signal",
            "direction": direction_from_change(change_15m),
            "momentum": momentum_label(change_15m),
            "change_pct": round_or_none(change_15m),
            "row_count": len(rows_15m),
        },
        {
            "timeframe": "latest_30m_before_signal",
            "direction": direction_from_change(change_30m),
            "momentum": momentum_label(change_30m),
            "change_pct": round_or_none(change_30m),
            "row_count": len(rows_30m),
        },
        {
            "timeframe": "latest_60m_before_signal",
            "direction": direction_from_change(change_60m),
            "momentum": momentum_label(change_60m),
            "change_pct": round_or_none(change_60m),
            "row_count": len(rows_60m),
        },
    ]
    directions = [vote["direction"] for vote in votes if vote["direction"] in ("up", "down")]
    alignment = normalize_intraday_signal_alignment(signal_alignment_from_votes(candidate_side(alert), votes))
    contradictions = []
    if len(set(directions)) > 1:
        contradictions.append("intraday_timeframes_conflict_before_signal")
    status = "OK" if alignment != "neutral_or_insufficient" else "WARN"
    if any(vote["row_count"] == 0 for vote in votes[1:]):
        status = "WARN"
    return {
        "schema": "intraday_signal_context_v1",
        "status": status,
        "symbol": symbol,
        "trade_date": trade_date,
        "cutoff_timestamp": cutoff.isoformat(timespec="seconds"),
        "latest_timestamp": ordered[-1].get("timestamp"),
        "row_count": len(ordered),
        "alignment": alignment,
        "dominant_direction": Counter(directions).most_common(1)[0][0] if directions else "unknown",
        "expected_direction": expected_direction(candidate_side(alert)),
        "votes": votes,
        "contradictions": contradictions,
        "source_note": "uses only interval='min' rows at or before the signal timestamp; does not submit orders",
    }


def threshold_sequence_from_ordered_minute_rows(side, ordered, stop_loss, take_profit):
    for row in ordered:
        target_hit, stop_hit = threshold_state(side, row, stop_loss, take_profit)
        if target_hit and stop_hit:
            return {
                "schema": "intraday_threshold_sequence_v1",
                "status": "AMBIGUOUS",
                "reason": "target_and_stop_touched_in_same_minute_bar",
                "row_count": len(ordered),
                "first_hit": "ambiguous_same_minute",
                "first_hit_timestamp": row.get("timestamp"),
                "first_hit_price_bar": {
                    "open": as_float(row.get("open")),
                    "high": as_float(row.get("high")),
                    "low": as_float(row.get("low")),
                    "close": as_float(row.get("close")),
                },
            }
        if target_hit:
            return {
                "schema": "intraday_threshold_sequence_v1",
                "status": "RESOLVED",
                "reason": "target_touched_before_stop_on_minute_bars",
                "row_count": len(ordered),
                "first_hit": "target",
                "first_hit_timestamp": row.get("timestamp"),
            }
        if stop_hit:
            return {
                "schema": "intraday_threshold_sequence_v1",
                "status": "RESOLVED",
                "reason": "stop_touched_before_target_on_minute_bars",
                "row_count": len(ordered),
                "first_hit": "stop",
                "first_hit_timestamp": row.get("timestamp"),
            }
    return {
        "schema": "intraday_threshold_sequence_v1",
        "status": "UNRESOLVED",
        "reason": "minute_rows_do_not_reproduce_daily_threshold_touch",
        "row_count": len(ordered),
        "first_hit": None,
    }


def resolve_intraday_threshold_sequence(side, minute_rows, stop_loss, take_profit):
    ordered = sorted(
        list(minute_rows or []),
        key=lambda item: parse_timestamp(item.get("timestamp")) or datetime.min,
    )
    fidelity = intraday_minute_path_fidelity(ordered)
    if not ordered:
        return {
            "schema": "intraday_threshold_sequence_v1",
            "status": "MISSING",
            "reason": "missing_intraday_rows_for_ambiguous_daily_bar",
            "row_count": 0,
            "first_hit": None,
            "source_fidelity": fidelity,
        }
    sampled = threshold_sequence_from_ordered_minute_rows(side, ordered, stop_loss, take_profit)
    if fidelity.get("status") != "FULL_OHLC":
        return {
            "schema": "intraday_threshold_sequence_v1",
            "status": "LOW_FIDELITY",
            "reason": "minute_path_source_fidelity_unverified",
            "row_count": len(ordered),
            "first_hit": None,
            "sampled_status": sampled.get("status"),
            "sampled_first_hit": sampled.get("first_hit"),
            "sampled_first_hit_timestamp": sampled.get("first_hit_timestamp"),
            "sampled_reason": sampled.get("reason"),
            "source_fidelity": fidelity,
        }
    sampled["source_fidelity"] = fidelity
    return sampled


def evaluate_alert(
    alert,
    klines,
    horizons=DEFAULT_HORIZONS,
    intraday_klines_by_symbol_date=None,
    intraday_signal_klines_by_symbol_date=None,
):
    emitted_side = signal_side(alert)
    side = candidate_side(alert)
    sid = intake.signal_id(alert)
    symbol = str(alert.get("symbol", "")).upper()
    signal_date = alert_signal_date(alert)
    entry = effective_entry_price(alert)
    base = {
        "signal_id": sid,
        "symbol": symbol,
        "market": alert.get("market"),
        "signal_type": side,
        "emitted_signal_type": emitted_side,
        "candidate_signal_type": side,
        "downgraded_directional": emitted_side != side and side in ("BUY", "SELL"),
        "trigger": alert.get("trigger"),
        "confirmed": alert.get("confirmed"),
        "full_score": as_float(alert.get("full_score")),
        "rr_ratio": effective_rr_ratio(alert),
        "entry_price": entry,
        "stop_loss": effective_stop_loss(alert),
        "take_profit": effective_take_profit(alert),
        "signal_date": signal_date,
        "quote_time": alert.get("quote_time"),
        "generated_at": alert.get("generated_at"),
        "watchlist_id": alert.get("watchlist_id"),
        "watchlist_source": alert.get("watchlist_source"),
        "watchlist_count": alert.get("watchlist_count"),
        "strategy_config_id": alert.get("strategy_config_id"),
        "strategy_config_source": alert.get("strategy_config_source"),
        "strategy_config_version": alert.get("strategy_config_version"),
        "trigger_review_mode": alert.get("trigger_review_mode"),
        "strategy_policy_shadow_only": alert.get("strategy_policy_shadow_only"),
        "strategy_policy_disabled_observation": alert.get("strategy_policy_disabled_observation"),
        "suppressed_directional_reason": alert.get("suppressed_directional_reason"),
        "execution_candidate": alert.get("execution_candidate"),
        "execution_blocked_reasons": alert.get("execution_blocked_reasons") or [],
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
    base["intraday_signal_context"] = intraday_signal_context_for_alert(
        alert,
        intraday_signal_klines_by_symbol_date,
    )

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
        first_hit_detail = first_threshold_hit_detail(
            side,
            window,
            base.get("stop_loss"),
            base.get("take_profit"),
        )
        target_hit = first_hit_detail["target_seen"]
        stop_hit = first_hit_detail["stop_seen"]
        first_hit = first_hit_detail["first_hit"]
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
            "first_hit_date": first_hit_detail["first_hit_date"],
        }
        if first_hit == "ambiguous_same_day":
            minute_rows = intraday_rows_for(
                intraday_klines_by_symbol_date,
                symbol,
                first_hit_detail["first_hit_date"],
            )
            intraday_sequence = resolve_intraday_threshold_sequence(
                side,
                minute_rows,
                base.get("stop_loss"),
                base.get("take_profit"),
            )
            intraday_sequence["symbol"] = symbol
            intraday_sequence["trade_date"] = first_hit_detail["first_hit_date"]
            base["outcomes"][key]["intraday_sequence"] = intraday_sequence
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
            WITH daily_bar AS (
                SELECT DISTINCT ON (timestamp::date)
                       timestamp::date AS trade_date,
                       open_price, high_price, low_price, close_price
                FROM klines
                WHERE interval = 'day'
                  AND symbol = '{sql_quote(symbol)}'
                  AND timestamp::date >= '{sql_quote(min_date)}'::date
                ORDER BY timestamp::date, timestamp DESC
            )
            SELECT trade_date, open_price, high_price, low_price, close_price
            FROM daily_bar
            ORDER BY trade_date ASC
        """
        r = psql(sql)
        if r.returncode != 0:
            warnings.append(f"kline_query_failed:{symbol}:{r.stderr.strip()}")
            klines[symbol] = []
            continue
        klines[symbol] = parse_kline_rows(r.stdout)
    return klines, warnings


def fetch_symbol_kline_diagnostics(symbols):
    diagnostics = {}
    warnings = []
    symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    for symbol in symbols:
        sql = f"""
            SELECT
                s.symbol,
                s.exchange,
                COALESCE(s.is_active::text, 'missing') AS is_active,
                count(k.*) FILTER (WHERE k.interval = 'day') AS day_kline_count,
                min(k.timestamp::date) FILTER (WHERE k.interval = 'day') AS earliest_kline_date,
                max(k.timestamp::date) FILTER (WHERE k.interval = 'day') AS latest_kline_date,
                count(k.*) FILTER (WHERE k.interval = 'min') AS minute_kline_count,
                max(k.timestamp::date) FILTER (WHERE k.interval = 'min') AS latest_minute_date
            FROM (SELECT '{sql_quote(symbol)}'::text AS wanted_symbol) w
            LEFT JOIN stocks s ON s.symbol = w.wanted_symbol
            LEFT JOIN klines k ON k.symbol = w.wanted_symbol
            GROUP BY s.symbol, s.exchange, s.is_active
        """
        r = psql(sql)
        if r.returncode != 0:
            warnings.append(f"symbol_kline_diagnostic_query_failed:{symbol}:{r.stderr.strip()}")
            diagnostics[symbol] = {"symbol": symbol, "status": "query_failed"}
            continue
        parsed = rows(r.stdout)
        if not parsed:
            diagnostics[symbol] = {"symbol": symbol, "status": "not_found_in_stocks_no_klines"}
            continue
        row = parsed[0]
        stock_symbol = row[0] if len(row) > 0 else ""
        exchange = row[1] if len(row) > 1 else ""
        active = row[2] if len(row) > 2 else "missing"
        kline_count = int(float(row[3])) if len(row) > 3 and row[3] not in ("", None) else 0
        earliest = row[4] if len(row) > 4 and row[4] else None
        latest = row[5] if len(row) > 5 and row[5] else None
        minute_count = int(float(row[6])) if len(row) > 6 and row[6] not in ("", None) else 0
        latest_minute = row[7] if len(row) > 7 and row[7] else None
        if not stock_symbol and not kline_count:
            status = "not_found_in_stocks_no_klines"
        elif not stock_symbol:
            status = "not_found_in_stocks_has_klines"
        elif not kline_count:
            status = "stock_found_no_day_klines"
        else:
            status = "stock_found_has_day_klines_before_signal_date"
        daily_refresh_gap = bool(latest and latest_minute and latest_minute > latest)
        diagnostics[symbol] = {
            "symbol": symbol,
            "status": status,
            "stock_found": bool(stock_symbol),
            "exchange": exchange or None,
            "is_active": active,
            "day_kline_count": kline_count,
            "earliest_kline_date": earliest,
            "latest_kline_date": latest,
            "minute_kline_count": minute_count,
            "latest_minute_date": latest_minute,
            "daily_refresh_gap": daily_refresh_gap,
        }
    return diagnostics, warnings


def dedupe_directional_alerts(alerts):
    seen = set()
    out = []
    duplicates = 0
    for alert in alerts:
        if not is_directional_candidate(alert):
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
        if not is_directional_candidate(alert):
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


def effective_first_hit(outcome):
    first_hit = outcome.get("first_hit")
    if first_hit != "ambiguous_same_day":
        return first_hit or "none"
    sequence = outcome.get("intraday_sequence")
    if isinstance(sequence, dict) and sequence.get("status") == "RESOLVED" and sequence.get("first_hit"):
        return f"intraday_{sequence['first_hit']}"
    if isinstance(sequence, dict) and sequence.get("status") == "AMBIGUOUS":
        return "ambiguous_same_minute"
    if isinstance(sequence, dict) and sequence.get("status") == "MISSING":
        return "ambiguous_intraday_missing"
    if isinstance(sequence, dict) and sequence.get("status") == "UNRESOLVED":
        return "ambiguous_intraday_unresolved"
    if isinstance(sequence, dict) and sequence.get("status") == "LOW_FIDELITY":
        return "ambiguous_intraday_low_fidelity"
    return "ambiguous_same_day"


def apply_sample_scope(alerts, sample_scope_mode="current"):
    scope = infer_current_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    scoped = [alert for alert in alerts if alert_matches_scope(alert, scope)]
    all_directional = [alert for alert in alerts if is_directional_candidate(alert)]
    scoped_directional = [alert for alert in scoped if is_directional_candidate(alert)]
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
    effective_first_hits = Counter(effective_first_hit(outcome) for outcome in resolved)
    effective_target_first = effective_first_hits.get("target", 0) + effective_first_hits.get("intraday_target", 0)
    effective_stop_first = effective_first_hits.get("stop", 0) + effective_first_hits.get("intraday_stop", 0)
    effective_unresolved = sum(
        effective_first_hits.get(key, 0)
        for key in (
            "ambiguous_same_day",
            "ambiguous_same_minute",
            "ambiguous_intraday_missing",
            "ambiguous_intraday_unresolved",
            "ambiguous_intraday_low_fidelity",
        )
    )
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
        "effective_first_hit_counts": dict(effective_first_hits),
        "effective_target_first_rate_pct": pct(effective_target_first, len(resolved)),
        "effective_stop_first_rate_pct": pct(effective_stop_first, len(resolved)),
        "effective_unresolved_first_hit_rate_pct": pct(effective_unresolved, len(resolved)),
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
        "emitted_signal_type_counts": value_counts(items, "emitted_signal_type"),
        "downgraded_directional_count": len([item for item in items if item.get("downgraded_directional")]),
    }


def ambiguous_intraday_targets(evaluations):
    targets = []
    seen = set()
    for item in evaluations or []:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        for outcome in (item.get("outcomes") or {}).values():
            if outcome.get("first_hit") != "ambiguous_same_day":
                continue
            trade_date = str(outcome.get("first_hit_date") or outcome.get("mark_date") or "")[:10]
            key = intraday_sequence_key(symbol, trade_date)
            if trade_date and key not in seen:
                seen.add(key)
                targets.append((symbol, trade_date))
    return targets


def intraday_sequence_summary(evaluations, warnings=None):
    statuses = Counter()
    first_hits = Counter()
    rows = []
    for item in evaluations or []:
        for horizon, outcome in sorted((item.get("outcomes") or {}).items()):
            sequence = outcome.get("intraday_sequence")
            if not isinstance(sequence, dict):
                continue
            status = sequence.get("status") or "UNKNOWN"
            first_hit = sequence.get("first_hit") or "none"
            statuses[status] += 1
            first_hits[first_hit] += 1
            rows.append(
                {
                    "signal_id": item.get("signal_id"),
                    "symbol": item.get("symbol"),
                    "horizon": horizon,
                    "status": status,
                    "first_hit": sequence.get("first_hit"),
                    "first_hit_timestamp": sequence.get("first_hit_timestamp"),
                    "trade_date": sequence.get("trade_date"),
                    "reason": sequence.get("reason"),
                    "sampled_first_hit": sequence.get("sampled_first_hit"),
                    "source_fidelity_status": (sequence.get("source_fidelity") or {}).get("status"),
                    "source_fidelity_reasons": (sequence.get("source_fidelity") or {}).get("reasons") or [],
                }
            )
    return {
        "schema": "intraday_sequence_summary_v1",
        "ambiguous_daily_count": len(rows),
        "resolved_count": statuses.get("RESOLVED", 0),
        "missing_count": statuses.get("MISSING", 0),
        "ambiguous_count": statuses.get("AMBIGUOUS", 0),
        "unresolved_count": statuses.get("UNRESOLVED", 0),
        "low_fidelity_count": statuses.get("LOW_FIDELITY", 0),
        "status_counts": dict(statuses),
        "first_hit_counts": dict(first_hits),
        "examples": rows[:20],
        "warnings": warnings or [],
    }


def intraday_signal_context_alignment(item):
    context = item.get("intraday_signal_context") if isinstance(item.get("intraday_signal_context"), dict) else {}
    return normalize_intraday_signal_alignment(context.get("alignment"))


def intraday_signal_context_summary(evaluations):
    statuses = Counter()
    alignments = Counter()
    examples = []
    for item in evaluations or []:
        context = item.get("intraday_signal_context") if isinstance(item.get("intraday_signal_context"), dict) else {}
        status = str(context.get("status") or "MISSING")
        alignment = normalize_intraday_signal_alignment(context.get("alignment"))
        statuses[status] += 1
        alignments[alignment] += 1
        if len(examples) < 20:
            examples.append(
                {
                    "signal_id": item.get("signal_id"),
                    "symbol": item.get("symbol"),
                    "signal_type": item.get("signal_type"),
                    "status": status,
                    "alignment": alignment,
                    "expected_direction": context.get("expected_direction"),
                    "dominant_direction": context.get("dominant_direction"),
                    "latest_timestamp": context.get("latest_timestamp"),
                    "row_count": context.get("row_count"),
                }
            )
    total = len(evaluations or [])
    usable = total - statuses.get("MISSING", 0)
    return {
        "schema": "intraday_signal_context_summary_v1",
        "description": "No-lookahead minute-bar context at or before each v5 signal timestamp.",
        "signal_count": total,
        "usable_context_count": usable,
        "missing_context_count": statuses.get("MISSING", 0),
        "coverage_pct": pct(usable, total),
        "status_counts": dict(statuses),
        "alignment_counts": dict(alignments),
        "examples": examples,
        "read_only": True,
        "submits_orders": False,
    }


def build_recommendations(payload):
    recs = []
    h1 = payload["overall"]["horizons"].get("1d", {})
    resolved_1d = h1.get("resolved_count", 0)
    maturity = payload.get("outcome_maturity") or {}
    daily_refresh_gap_count = len(
        [
            item
            for item in maturity.get("missing_symbol_kline_diagnostics") or []
            if item.get("daily_refresh_gap")
        ]
    )
    if resolved_1d == 0:
        recs.append("outcome_sample_not_ready_keep_collecting_daily_klines")
    elif resolved_1d < 30:
        recs.append("outcome_sample_below_30_keep_shadow_mode")
    elif h1.get("avg_signed_close_return_pct") is not None and h1["avg_signed_close_return_pct"] <= 0:
        recs.append("one_day_average_return_non_positive_review_v5_filters")
    if h1.get("effective_stop_first_rate_pct", 0) > h1.get("effective_target_first_rate_pct", 0):
        recs.append("effective_stop_first_rate_exceeds_target_first_review_v5_filters")
    if h1.get("effective_unresolved_first_hit_rate_pct", 0) >= 25:
        recs.append("high_unresolved_first_hit_rate_collect_more_intraday_path_evidence")
    repair = payload.get("kline_daily_gap_repair_context") or {}
    has_repair_report = (
        repair.get("source_schema") == "kline_daily_gap_repair_report_v1"
        and str(repair.get("status") or "").upper() != "MISSING"
    )
    if daily_refresh_gap_count and not has_repair_report:
        recs.append(f"repair_daily_kline_refresh_gap_for_minute_fresh_symbols:{daily_refresh_gap_count}")
    intraday = payload.get("intraday_sequence_summary") or {}
    if intraday.get("missing_count"):
        recs.append(f"collect_minute_klines_to_resolve_ambiguous_stop_target:{intraday['missing_count']}")
    if intraday.get("ambiguous_count"):
        recs.append(f"manual_review_same_minute_stop_target_order:{intraday['ambiguous_count']}")
    if intraday.get("unresolved_count"):
        recs.append(f"review_daily_vs_minute_threshold_mismatch:{intraday['unresolved_count']}")
    if intraday.get("low_fidelity_count"):
        recs.append(f"collect_full_ohlcv_minute_path_evidence:{intraday['low_fidelity_count']}")
    intraday_signal = payload.get("intraday_signal_context_summary") or {}
    if intraday_signal.get("missing_context_count"):
        recs.append(f"collect_signal_time_minute_context:{intraday_signal['missing_context_count']}")
    by_intraday_alignment = {
        row.get("key"): row
        for row in payload.get("by_intraday_signal_alignment") or []
    }
    challenged = ((by_intraday_alignment.get("challenges_signal") or {}).get("horizons") or {}).get("1d") or {}
    if challenged.get("resolved_count", 0) >= 5 and challenged.get("avg_signed_close_return_pct") is not None:
        if challenged["avg_signed_close_return_pct"] > 0:
            recs.append("intraday_challenge_signals_profitable_review_threshold_or_labeling")
        else:
            recs.append("intraday_challenge_signals_underperform_consider_hold_or_reduce_rule")
    if repair.get("actionable_missing_symbol_count"):
        recs.append(f"apply_reviewed_daily_gap_plan_for_outcome_symbols:{repair['actionable_missing_symbol_count']}")
    if repair.get("unresolved_missing_symbol_count"):
        recs.append(f"review_unresolved_daily_gap_symbols_for_source_or_mapping:{repair['unresolved_missing_symbol_count']}")
    source_diag = payload.get("kline_gap_source_diagnostic_context") or {}
    if source_diag.get("active_universe_or_mapping_missing_symbol_count"):
        recs.append(
            "review_active_universe_or_mapping_for_outcome_symbols:"
            f"{source_diag['active_universe_or_mapping_missing_symbol_count']}"
        )
    if source_diag.get("provider_lag_missing_symbol_count"):
        recs.append(f"wait_or_refetch_provider_lag_outcome_symbols:{source_diag['provider_lag_missing_symbol_count']}")

    for row in payload["by_trigger"]:
        metric = row["horizons"].get("1d", {})
        if row["count"] >= 5 and metric.get("resolved_count", 0) >= 5:
            avg_return = metric.get("avg_signed_close_return_pct")
            if avg_return is not None and avg_return < 0:
                recs.append(f"negative_1d_trigger:{row['key']}")
            if metric.get("stop_hit_rate_pct", 0) > metric.get("target_hit_rate_pct", 0):
                recs.append(f"stop_hits_exceed_targets:{row['key']}")
            if metric.get("effective_stop_first_rate_pct", 0) > metric.get("effective_target_first_rate_pct", 0):
                recs.append(f"effective_stop_first_exceeds_target_first:{row['key']}")

    if not recs:
        recs.append("continue_shadow_observation_before_enabling_alert_sim")
    return recs


def gap_repair_index(payload):
    payload = payload if isinstance(payload, dict) else {}
    actions = {}
    unresolved = {}
    for action in payload.get("actions") or []:
        if isinstance(action, dict) and action.get("symbol"):
            actions[str(action["symbol"]).upper()] = action
    for issue in payload.get("unresolved") or []:
        if isinstance(issue, dict) and issue.get("symbol"):
            unresolved[str(issue["symbol"]).upper()] = issue
    return actions, unresolved


def enrich_maturity_with_gap_repair(maturity, gap_repair):
    maturity = dict(maturity or {})
    diagnostics = []
    actions, unresolved = gap_repair_index(gap_repair)
    actionable_count = 0
    unresolved_count = 0
    not_in_plan_count = 0
    for row in maturity.get("missing_symbol_kline_diagnostics") or []:
        enriched = dict(row)
        symbol = str(enriched.get("symbol") or "").upper()
        if symbol in actions:
            action = actions[symbol]
            enriched["daily_gap_repair_status"] = "actionable"
            enriched["daily_gap_repair_plan_hash"] = gap_repair.get("plan_hash")
            enriched["daily_gap_repair_action"] = {
                "symbol": action.get("symbol"),
                "row_count": action.get("row_count"),
                "latest_daily_date": action.get("latest_daily_date"),
                "target_end_date": action.get("target_end_date"),
                "source_code": action.get("source_code"),
            }
            actionable_count += 1
        elif symbol in unresolved:
            issue = unresolved[symbol]
            enriched["daily_gap_repair_status"] = "unresolved"
            enriched["daily_gap_repair_plan_hash"] = gap_repair.get("plan_hash")
            enriched["daily_gap_repair_unresolved"] = {
                "symbol": issue.get("symbol"),
                "reason": issue.get("reason"),
                "latest_daily_date": issue.get("latest_daily_date"),
                "target_end_date": issue.get("target_end_date"),
                "latest_source_date": issue.get("latest_source_date"),
                "source_reaches_target_end": issue.get("source_reaches_target_end"),
                "source_after_latest_daily": issue.get("source_after_latest_daily"),
            }
            unresolved_count += 1
        else:
            enriched["daily_gap_repair_status"] = "not_in_repair_plan"
            not_in_plan_count += 1
        diagnostics.append(enriched)
    maturity["missing_symbol_kline_diagnostics"] = diagnostics
    context = {
        "schema": "outcome_daily_gap_repair_context_v1",
        "source_schema": gap_repair.get("schema"),
        "status": gap_repair.get("status") or "MISSING",
        "generated_at": gap_repair.get("generated_at"),
        "plan_hash": gap_repair.get("plan_hash"),
        "summary": gap_repair.get("summary") or {},
        "recommendations": gap_repair.get("recommendations") or [],
        "apply_contract": gap_repair.get("apply_contract") or {},
        "actionable_missing_symbol_count": actionable_count,
        "unresolved_missing_symbol_count": unresolved_count,
        "not_in_repair_plan_missing_symbol_count": not_in_plan_count,
    }
    maturity["daily_gap_repair_context"] = context
    return maturity, context


def gap_source_diagnostic_index(payload):
    payload = payload if isinstance(payload, dict) else {}
    by_symbol = {}
    for item in payload.get("classifications") or []:
        if isinstance(item, dict) and item.get("symbol"):
            by_symbol[str(item["symbol"]).upper()] = item
    return by_symbol


def enrich_maturity_with_gap_source_diagnostic(maturity, gap_source_diagnostic):
    maturity = dict(maturity or {})
    diagnostics = []
    by_symbol = gap_source_diagnostic_index(gap_source_diagnostic)
    classified_count = 0
    unclassified_count = 0
    category_counts = Counter()
    confidence_counts = Counter()
    category_affected_counts = Counter()
    active_universe_or_mapping_count = 0
    provider_lag_count = 0
    for row in maturity.get("missing_symbol_kline_diagnostics") or []:
        enriched = dict(row)
        symbol = str(enriched.get("symbol") or "").upper()
        classification = by_symbol.get(symbol)
        try:
            affected = int(enriched.get("affected_signal_count") or 1)
        except (TypeError, ValueError):
            affected = 1
        if classification:
            category = classification.get("category") or "unclassified_daily_gap_source_issue"
            confidence = classification.get("confidence") or "unknown"
            enriched["daily_gap_source_diagnostic_status"] = "classified"
            enriched["daily_gap_source_category"] = category
            enriched["daily_gap_source_confidence"] = confidence
            enriched["daily_gap_source_recommended_action"] = classification.get("recommended_action")
            enriched["daily_gap_source_hygiene"] = classification.get("hygiene") or {}
            enriched["daily_gap_source_diagnostic"] = {
                "symbol": classification.get("symbol"),
                "market": classification.get("market"),
                "category": category,
                "confidence": confidence,
                "recommended_action": classification.get("recommended_action"),
                "reason": classification.get("reason"),
                "latest_daily_date": classification.get("latest_daily_date"),
                "target_end_date": classification.get("target_end_date"),
                "latest_source_date": classification.get("latest_source_date"),
                "source_lag_days_vs_target": classification.get("source_lag_days_vs_target"),
                "daily_lag_days_vs_target": classification.get("daily_lag_days_vs_target"),
            }
            classified_count += 1
            category_counts[category] += 1
            confidence_counts[confidence] += 1
            category_affected_counts[category] += affected
            if category == "active_universe_or_symbol_mapping_issue":
                active_universe_or_mapping_count += 1
            if category == "provider_lag_or_partial_gap":
                provider_lag_count += 1
        else:
            enriched["daily_gap_source_diagnostic_status"] = "not_classified"
            unclassified_count += 1
        diagnostics.append(enriched)
    maturity["missing_symbol_kline_diagnostics"] = diagnostics
    context = {
        "schema": "outcome_kline_gap_source_diagnostic_context_v1",
        "source_schema": gap_source_diagnostic.get("schema") if isinstance(gap_source_diagnostic, dict) else None,
        "status": gap_source_diagnostic.get("status") if isinstance(gap_source_diagnostic, dict) else "MISSING",
        "generated_at": gap_source_diagnostic.get("generated_at") if isinstance(gap_source_diagnostic, dict) else None,
        "summary": gap_source_diagnostic.get("summary") if isinstance(gap_source_diagnostic, dict) else {},
        "recommendations": gap_source_diagnostic.get("recommendations") if isinstance(gap_source_diagnostic, dict) else [],
        "warnings": gap_source_diagnostic.get("warnings") if isinstance(gap_source_diagnostic, dict) else [],
        "source": gap_source_diagnostic.get("source") if isinstance(gap_source_diagnostic, dict) else {},
        "classified_missing_symbol_count": classified_count,
        "unclassified_missing_symbol_count": unclassified_count,
        "category_counts": dict(category_counts),
        "confidence_counts": dict(confidence_counts),
        "category_affected_signal_counts": dict(category_affected_counts),
        "active_universe_or_mapping_missing_symbol_count": active_universe_or_mapping_count,
        "provider_lag_missing_symbol_count": provider_lag_count,
    }
    maturity["daily_gap_source_diagnostic_context"] = context
    return maturity, context


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


def outcome_maturity_summary(evaluations, primary_horizon="1d", symbol_kline_diagnostics=None):
    try:
        needed_days = int(str(primary_horizon).rstrip("d"))
    except (TypeError, ValueError):
        needed_days = 1
    signal_dates = [item.get("signal_date") for item in evaluations if item.get("signal_date")]
    latest_kline_dates = [item.get("latest_kline_date") for item in evaluations if item.get("latest_kline_date")]
    pending_items = [item for item in evaluations if item.get("status") != "resolved"]
    resolved_items = [item for item in evaluations if item.get("status") == "resolved"]
    days_missing = []
    next_dates = []
    next_trading_dates = []
    calendar_reason_counts = Counter()
    missing_symbol_klines = []
    no_future_klines = []
    insufficient_horizon = []
    for item in pending_items:
        available = item.get("available_future_days")
        try:
            available = int(available)
        except (TypeError, ValueError):
            available = 0
        missing = max(needed_days - available, 0)
        days_missing.append(missing)
        if item.get("reason") == "missing_symbol_klines":
            missing_symbol_klines.append(item)
        if item.get("reason") == "no_future_daily_klines":
            no_future_klines.append(item)
        if missing:
            insufficient_horizon.append(item)
        candidate = add_days_iso(item.get("signal_date"), needed_days)
        if candidate:
            next_dates.append(candidate)
        trading_candidate = add_weekdays_iso(item.get("signal_date"), needed_days)
        if trading_candidate:
            next_trading_dates.append(trading_candidate)
        calendar_reason_counts[calendar_pending_reason(item, needed_days)] += 1
    pending_examples = []
    for item in pending_items[:20]:
        try:
            example_available = int(item.get("available_future_days") or 0)
        except (TypeError, ValueError):
            example_available = 0
        pending_examples.append(
            {
                "signal_id": item.get("signal_id"),
                "symbol": item.get("symbol"),
                "signal_date": item.get("signal_date"),
                "latest_kline_date": item.get("latest_kline_date"),
                "available_future_days": item.get("available_future_days"),
                "needed_future_days": needed_days,
                "missing_future_days": max(needed_days - example_available, 0),
                "reason": item.get("reason"),
                "earliest_primary_horizon_date": add_days_iso(item.get("signal_date"), needed_days),
                "earliest_primary_horizon_trading_date": add_weekdays_iso(item.get("signal_date"), needed_days),
                "calendar_pending_reason": calendar_pending_reason(item, needed_days),
            }
        )
    missing_symbol_rows = []
    for symbol in sorted({item.get("symbol") for item in missing_symbol_klines if item.get("symbol")}):
        symbol_items = [item for item in missing_symbol_klines if item.get("symbol") == symbol]
        diagnostic = dict((symbol_kline_diagnostics or {}).get(symbol) or {"symbol": symbol, "status": "diagnostic_unavailable"})
        signal_dates_for_symbol = [item.get("signal_date") for item in symbol_items if item.get("signal_date")]
        latest_signal_for_symbol = max(signal_dates_for_symbol) if signal_dates_for_symbol else None
        latest_kline_for_symbol = diagnostic.get("latest_kline_date")
        lag_days = None
        if latest_signal_for_symbol and latest_kline_for_symbol:
            try:
                lag_days = (
                    datetime.strptime(latest_signal_for_symbol[:10], "%Y-%m-%d").date()
                    - datetime.strptime(latest_kline_for_symbol[:10], "%Y-%m-%d").date()
                ).days
            except (TypeError, ValueError):
                lag_days = None
        diagnostic.update(
            {
                "affected_signal_count": len(symbol_items),
                "signal_ids": [item.get("signal_id") for item in symbol_items[:20] if item.get("signal_id")],
                "latest_signal_date": latest_signal_for_symbol,
                "lag_days_vs_latest_signal": lag_days,
            }
        )
        missing_symbol_rows.append(diagnostic)

    return {
        "schema": "outcome_maturity_summary_v1",
        "primary_horizon": primary_horizon,
        "needed_future_days": needed_days,
        "evaluated_signal_count": len(evaluations),
        "resolved_count": len(resolved_items),
        "pending_or_invalid_count": len(pending_items),
        "earliest_signal_date": min(signal_dates) if signal_dates else None,
        "latest_signal_date": max(signal_dates) if signal_dates else None,
        "latest_kline_date": max(latest_kline_dates) if latest_kline_dates else None,
        "max_available_future_days": max((item.get("available_future_days") or 0 for item in evaluations), default=0),
        "min_missing_future_days_for_pending": min(days_missing) if days_missing else None,
        "max_missing_future_days_for_pending": max(days_missing) if days_missing else None,
        "earliest_primary_horizon_date_for_pending": min(next_dates) if next_dates else None,
        "earliest_primary_horizon_trading_date_for_pending": min(next_trading_dates) if next_trading_dates else None,
        "calendar_pending_reason_counts": dict(calendar_reason_counts),
        "missing_symbol_kline_count": len(missing_symbol_klines),
        "missing_symbol_kline_symbols": [item.get("symbol") for item in missing_symbol_rows],
        "missing_symbol_kline_unique_symbol_count": len(missing_symbol_rows),
        "missing_symbol_kline_diagnostics": missing_symbol_rows,
        "no_future_daily_kline_count": len(no_future_klines),
        "insufficient_primary_horizon_count": len(insufficient_horizon),
        "pending_examples": pending_examples,
    }


def build_report(
    alerts,
    klines_by_symbol=None,
    intraday_klines_by_symbol_date=None,
    intraday_signal_klines_by_symbol_date=None,
    horizons=DEFAULT_HORIZONS,
    sample_scope_mode="current",
    symbol_kline_diagnostics=None,
    kline_daily_gap_repair=None,
    kline_gap_source_diagnostic=None,
):
    horizons = tuple(sorted(set(int(h) for h in horizons if int(h) > 0))) or DEFAULT_HORIZONS
    scoped_alerts, sample_scope = apply_sample_scope(alerts, sample_scope_mode=sample_scope_mode)
    directional, duplicate_count = dedupe_directional_alerts(scoped_alerts)
    fetch_warnings = []
    intraday_signal_warnings = []
    if klines_by_symbol is None:
        klines_by_symbol, fetch_warnings = fetch_klines(symbol_min_dates(directional))
        if intraday_signal_klines_by_symbol_date is None:
            intraday_signal_klines_by_symbol_date, intraday_signal_warnings = fetch_intraday_signal_context_klines(
                intraday_signal_context_targets(directional)
            )
    evaluations = [
        evaluate_alert(
            alert,
            klines_by_symbol.get(str(alert.get("symbol", "")).upper(), []),
            horizons=horizons,
            intraday_signal_klines_by_symbol_date=intraday_signal_klines_by_symbol_date or {},
        )
        for alert in directional
    ]
    intraday_warnings = []
    intraday_targets = ambiguous_intraday_targets(evaluations)
    if intraday_targets and intraday_klines_by_symbol_date is None:
        intraday_klines_by_symbol_date, intraday_warnings = fetch_intraday_klines(intraday_targets)
    if intraday_targets:
        evaluations = [
            evaluate_alert(
                alert,
                klines_by_symbol.get(str(alert.get("symbol", "")).upper(), []),
                horizons=horizons,
                intraday_klines_by_symbol_date=intraday_klines_by_symbol_date or {},
                intraday_signal_klines_by_symbol_date=intraday_signal_klines_by_symbol_date or {},
            )
            for alert in directional
        ]
    overall_horizons = {f"{horizon}d": horizon_metrics(evaluations, f"{horizon}d") for horizon in horizons}
    pending_reasons = Counter(item.get("reason", "none") for item in evaluations if item.get("status") != "resolved")
    raw_alert_count = len(scoped_alerts)
    directional_alert_count = len([alert for alert in scoped_alerts if is_directional_candidate(alert)])
    downgraded_directional_alert_count = len(
        [
            alert
            for alert in scoped_alerts
            if is_directional_candidate(alert) and signal_side(alert) != candidate_side(alert)
        ]
    )
    evaluated_signal_count = len(evaluations)
    resolved_signal_count = len([item for item in evaluations if item.get("status") == "resolved"])
    pending_or_invalid_count = len([item for item in evaluations if item.get("status") != "resolved"])
    primary_horizon = primary_horizon_key(horizons)
    primary_horizon_metric = overall_horizons.get(primary_horizon, {})
    missing_symbols = {
        item.get("symbol")
        for item in evaluations
        if item.get("reason") == "missing_symbol_klines" and item.get("symbol")
    }
    diagnostic_warnings = []
    if symbol_kline_diagnostics is None and missing_symbols:
        symbol_kline_diagnostics, diagnostic_warnings = fetch_symbol_kline_diagnostics(missing_symbols)
    maturity = outcome_maturity_summary(
        evaluations,
        primary_horizon=primary_horizon,
        symbol_kline_diagnostics=symbol_kline_diagnostics or {},
    )
    if kline_daily_gap_repair is None:
        kline_daily_gap_repair = load_json_file(KLINE_DAILY_GAP_REPAIR_FILE)
    maturity, daily_gap_repair_context = enrich_maturity_with_gap_repair(
        maturity,
        kline_daily_gap_repair or {},
    )
    if kline_gap_source_diagnostic is None:
        kline_gap_source_diagnostic = load_json_file(KLINE_GAP_SOURCE_DIAGNOSTIC_FILE)
    maturity, daily_gap_source_diagnostic_context = enrich_maturity_with_gap_source_diagnostic(
        maturity,
        kline_gap_source_diagnostic or {},
    )
    payload = {
        "schema": "rt_signal_outcome_report_v1",
        "generated_at": now_iso(),
        "sample_scope": sample_scope,
        "status": report_status(evaluated_signal_count, primary_horizon_metric),
        "raw_alert_count": raw_alert_count,
        "directional_alert_count": directional_alert_count,
        "downgraded_directional_alert_count": downgraded_directional_alert_count,
        "evaluated_signal_count": evaluated_signal_count,
        "duplicate_signal_count": duplicate_count,
        "resolved_signal_count": resolved_signal_count,
        "pending_signal_count": pending_or_invalid_count,
        "pending_or_invalid_count": pending_or_invalid_count,
        "pending_reasons": dict(pending_reasons),
        "primary_horizon": primary_horizon,
        "primary_horizon_metric": primary_horizon_metric,
        "outcome_maturity": maturity,
        "intraday_sequence_summary": intraday_sequence_summary(evaluations, warnings=intraday_warnings),
        "intraday_signal_context_summary": intraday_signal_context_summary(evaluations),
        "kline_daily_gap_repair_context": daily_gap_repair_context,
        "kline_gap_source_diagnostic_context": daily_gap_source_diagnostic_context,
        "source": {
            "alert_queue_file": ALERT_QUEUE_FILE,
            "kline_daily_gap_repair_file": KLINE_DAILY_GAP_REPAIR_FILE,
            "kline_gap_source_diagnostic_file": KLINE_GAP_SOURCE_DIAGNOSTIC_FILE,
            "price_source": "klines daily rows after alert quote_date; full-OHLC minute rows only resolve ambiguous_same_day ordering",
            "hit_analysis_note": "daily bars drive outcome returns; ambiguous_same_day is resolved only with full-OHLC interval='min' rows; snapshot-price or unverified minute rows remain LOW_FIDELITY",
            "intraday_signal_context_note": "signal-time minute context uses only interval='min' rows at or before the alert timestamp and is read-only learning evidence",
            "horizons": list(horizons),
        },
        "counts": {
            "raw_alert_count": raw_alert_count,
            "directional_alert_count": directional_alert_count,
            "downgraded_directional_alert_count": downgraded_directional_alert_count,
            "evaluated_signal_count": evaluated_signal_count,
            "duplicate_signal_count": duplicate_count,
            "by_signal_type": dict(Counter(signal_side(alert) or "UNKNOWN" for alert in scoped_alerts)),
            "by_candidate_signal_type": dict(Counter(candidate_side(alert) or "UNKNOWN" for alert in scoped_alerts)),
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
        "by_intraday_signal_alignment": summarize_groups(
            evaluations,
            intraday_signal_context_alignment,
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
        "warnings": fetch_warnings + diagnostic_warnings + intraday_warnings + intraday_signal_warnings,
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
    intraday_summary = payload.get("intraday_sequence_summary") or {}
    if intraday_summary.get("ambiguous_daily_count"):
        lines.append(
            "Intraday sequence: ambiguous_daily={ambiguous} resolved={resolved} "
            "missing={missing} same_minute={same_minute} unresolved={unresolved} low_fidelity={low_fidelity}".format(
                ambiguous=intraday_summary.get("ambiguous_daily_count"),
                resolved=intraday_summary.get("resolved_count"),
                missing=intraday_summary.get("missing_count"),
                same_minute=intraday_summary.get("ambiguous_count"),
                unresolved=intraday_summary.get("unresolved_count"),
                low_fidelity=intraday_summary.get("low_fidelity_count"),
            )
        )
    signal_context = payload.get("intraday_signal_context_summary") or {}
    if signal_context.get("signal_count"):
        lines.append(
            "Intraday signal context: coverage={coverage}% alignments={alignments}".format(
                coverage=signal_context.get("coverage_pct"),
                alignments=json.dumps(signal_context.get("alignment_counts") or {}, ensure_ascii=False, sort_keys=True),
            )
        )
    maturity = payload.get("outcome_maturity") or {}
    if maturity:
        lines.append(
            "Maturity: latest_kline={latest_kline} latest_signal={latest_signal} "
            "pending={pending} missing_future_days={min_missing}-{max_missing} "
            "earliest_primary_horizon={earliest} earliest_trading_horizon={earliest_trading}".format(
                latest_kline=maturity.get("latest_kline_date"),
                latest_signal=maturity.get("latest_signal_date"),
                pending=maturity.get("pending_or_invalid_count"),
                min_missing=maturity.get("min_missing_future_days_for_pending"),
                max_missing=maturity.get("max_missing_future_days_for_pending"),
                earliest=maturity.get("earliest_primary_horizon_date_for_pending"),
                earliest_trading=maturity.get("earliest_primary_horizon_trading_date_for_pending"),
            )
        )
        if maturity.get("calendar_pending_reason_counts"):
            lines.append(
                "Maturity reasons: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(maturity.get("calendar_pending_reason_counts").items())
                )
            )
        if maturity.get("missing_symbol_kline_count"):
            status_counts = Counter()
            daily_refresh_gap_symbols = []
            for item in maturity.get("missing_symbol_kline_diagnostics") or []:
                try:
                    affected = int(item.get("affected_signal_count") or 1)
                except (TypeError, ValueError):
                    affected = 1
                status_counts[item.get("status") or "unknown"] += affected
                if item.get("daily_refresh_gap") and item.get("symbol"):
                    daily_refresh_gap_symbols.append(item["symbol"])
            lines.append(
                "Missing symbol K-lines: "
                + ", ".join(f"{key}={count}" for key, count in sorted(status_counts.items()))
            )
            if daily_refresh_gap_symbols:
                lines.append("Daily refresh gaps: " + ", ".join(sorted(daily_refresh_gap_symbols)))
            gap_status_counts = Counter(
                item.get("daily_gap_repair_status") or "unknown"
                for item in maturity.get("missing_symbol_kline_diagnostics") or []
            )
            if gap_status_counts:
                lines.append(
                    "Daily gap repair mapping: "
                    + ", ".join(f"{key}={count}" for key, count in sorted(gap_status_counts.items()))
                )
            source_category_counts = Counter(
                item.get("daily_gap_source_category") or "unknown"
                for item in maturity.get("missing_symbol_kline_diagnostics") or []
            )
            if source_category_counts:
                lines.append(
                    "Daily gap source diagnostic: "
                    + ", ".join(f"{key}={count}" for key, count in sorted(source_category_counts.items()))
                )
        repair_context = maturity.get("daily_gap_repair_context") or {}
        if repair_context.get("plan_hash"):
            lines.append(
                "Daily gap repair plan: status={status} hash={hash} actionable={actionable} unresolved={unresolved}".format(
                    status=repair_context.get("status"),
                    hash=repair_context.get("plan_hash"),
                    actionable=repair_context.get("actionable_missing_symbol_count"),
                    unresolved=repair_context.get("unresolved_missing_symbol_count"),
                )
            )
        source_context = maturity.get("daily_gap_source_diagnostic_context") or {}
        if source_context.get("classified_missing_symbol_count") or source_context.get("status"):
            lines.append(
                "Daily gap source plan: status={status} classified={classified} active_or_mapping={active}".format(
                    status=source_context.get("status"),
                    classified=source_context.get("classified_missing_symbol_count"),
                    active=source_context.get("active_universe_or_mapping_missing_symbol_count"),
                )
            )
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
    parser.add_argument("--kline-daily-gap-repair-file", default=KLINE_DAILY_GAP_REPAIR_FILE)
    parser.add_argument("--kline-gap-source-diagnostic-file", default=KLINE_GAP_SOURCE_DIAGNOSTIC_FILE)
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
    payload = build_report(
        alerts,
        horizons=parse_horizons(args.horizons),
        sample_scope_mode=args.sample_scope,
        kline_daily_gap_repair=load_json_file(args.kline_daily_gap_repair_file),
        kline_gap_source_diagnostic=load_json_file(args.kline_gap_source_diagnostic_file),
    )
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
