#!/usr/bin/env python3
"""Read-only intraday K-line context for Hermes review packets."""
import argparse
import json
import os
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python versions without zoneinfo
    ZoneInfo = None


REPORT_FILE = os.environ.get("INTRADAY_CONTEXT_REPORT_FILE", "/tmp/intraday_context_report.json")
WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
MARKET_SESSION_OVERRIDES_FILE = os.environ.get(
    "INTRADAY_MARKET_SESSION_OVERRIDES_FILE",
    "/root/intraday_market_sessions.json",
)
DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
LOOKBACK_MINUTES = int(os.environ.get("INTRADAY_CONTEXT_LOOKBACK_MINUTES", "390"))
MAX_STALE_MINUTES = int(os.environ.get("INTRADAY_CONTEXT_MAX_STALE_MINUTES", "20"))
MAX_SYMBOLS_PER_MARKET = int(os.environ.get("INTRADAY_CONTEXT_MAX_SYMBOLS_PER_MARKET", "80"))
MAX_GAP_MINUTES = int(os.environ.get("INTRADAY_CONTEXT_MAX_GAP_MINUTES", "5"))
MIN_RECENT_5M_BARS = int(os.environ.get("INTRADAY_CONTEXT_MIN_RECENT_5M_BARS", "2"))
MIN_RECENT_60M_BARS = int(os.environ.get("INTRADAY_CONTEXT_MIN_RECENT_60M_BARS", "1"))
ROLLING_WINDOW_MINUTES = (5, 15, 30, 60)
MIN_ROLLING_WINDOW_COVERAGE_RATIO = float(
    os.environ.get("INTRADAY_CONTEXT_MIN_ROLLING_WINDOW_COVERAGE_RATIO", "0.8")
)
FULL_OHLC_INTRADAY_SOURCES = {
    "broker_minute_ohlcv",
    "vendor_minute_ohlcv",
    "official_exchange_minute_ohlcv",
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
    "tencent_min",
    "tencent_minute_query",
}
LOW_FIDELITY_INTRADAY_GRANULARITIES = {
    "missing",
    "unknown",
    "minute_snapshot_price",
    "snapshot_price",
    "last_price_snapshot",
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


def psql(sql, timeout=45):
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


def table_columns(table):
    sql = f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = '{sql_quote(table)}'
    """
    result = psql(sql, timeout=15)
    if result.returncode != 0:
        return set()
    return {row[0] for row in rows(result.stdout) if row and row[0]}


def rows(stdout):
    return [line.rstrip("\n").split("\t") for line in stdout.splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


def sql_in(values):
    escaped = [f"'{sql_quote(value)}'" for value in values if str(value or "").strip()]
    return ",".join(escaped) or "''"


def normalized_text(value, default="missing"):
    return str(value if value not in (None, "") else default).strip().lower()


def row_has_full_ohlc_fidelity(row):
    source = normalized_text((row or {}).get("data_source"))
    granularity = normalized_text((row or {}).get("source_granularity"))
    return source in FULL_OHLC_INTRADAY_SOURCES or granularity in FULL_OHLC_INTRADAY_GRANULARITIES


def row_is_snapshot_like(row):
    o = as_float((row or {}).get("open"))
    h = as_float((row or {}).get("high"))
    low = as_float((row or {}).get("low"))
    c = as_float((row or {}).get("close"))
    return o == h == low == c if None not in (o, h, low, c) else False


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def nth_weekday_of_month(year, month, weekday, nth):
    day = datetime(year, month, 1)
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + 7 * (nth - 1))


def us_dst_active_for_utc(value):
    if not value:
        return False
    utc_value = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    year = utc_value.year
    start_local = nth_weekday_of_month(year, 3, 6, 2).replace(hour=2, minute=0, second=0, microsecond=0)
    end_local = nth_weekday_of_month(year, 11, 6, 1).replace(hour=2, minute=0, second=0, microsecond=0)
    # DST starts at 02:00 local standard time (UTC-5) and ends at 02:00 local daylight time (UTC-4).
    start_utc = start_local.replace(tzinfo=timezone(timedelta(hours=-5))).astimezone(timezone.utc)
    end_utc = end_local.replace(tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)
    return start_utc <= utc_value < end_utc


def fallback_timezone_for_market(market, reference=None):
    market = str(market or "").upper()
    if market == "US":
        offset = -4 if reference is not None and us_dst_active_for_utc(reference) else -5
        return timezone(timedelta(hours=offset))
    return timezone(timedelta(hours=8))


def timezone_for_market(market, reference=None):
    market = str(market or "").upper()
    if ZoneInfo:
        try:
            return ZoneInfo("America/New_York") if market == "US" else ZoneInfo("Asia/Hong_Kong")
        except Exception:
            pass
    return fallback_timezone_for_market(market, reference=reference)


def local_now_for_market(market, now=None):
    tz = timezone_for_market(market, reference=now)
    if now is None:
        return datetime.now(tz).replace(tzinfo=None)
    if getattr(now, "tzinfo", None):
        return now.astimezone(tz).replace(tzinfo=None)
    return now


def minutes_since_midnight(value):
    return int(value.hour) * 60 + int(value.minute)


def hhmm(total_minutes):
    hour = int(total_minutes) // 60
    minute = int(total_minutes) % 60
    return f"{hour:02d}:{minute:02d}"


def parse_hhmm_minutes(value):
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def normalize_session_windows(value):
    rows = []
    raw_items = value if isinstance(value, list) else []
    for item in raw_items:
        if isinstance(item, dict):
            start = parse_hhmm_minutes(item.get("open") or item.get("start"))
            end = parse_hhmm_minutes(item.get("close") or item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start = parse_hhmm_minutes(item[0])
            end = parse_hhmm_minutes(item[1])
        else:
            continue
        if start is not None and end is not None and start < end:
            rows.append((start, end))
    return sorted(rows)


def normalize_date_reason(value, date_text):
    if isinstance(value, dict):
        reason = value.get(date_text)
        if isinstance(reason, dict):
            return str(reason.get("reason") or reason.get("name") or "session_override")
        if reason:
            return str(reason)
        return None
    if isinstance(value, list) and date_text in {str(item) for item in value}:
        return "configured_closed_date"
    return None


def session_override_for_date(market, date_text, overrides):
    market_payload = {}
    if isinstance(overrides, dict):
        market_payload = overrides.get(market) or overrides.get(str(market).lower()) or {}
    if not isinstance(market_payload, dict):
        return None

    reason = normalize_date_reason(market_payload.get("closed_dates"), date_text)
    if reason:
        return {"type": "closed", "reason": reason, "source_key": "closed_dates"}

    for key in ("session_overrides", "special_sessions", "half_days"):
        parent = market_payload.get(key)
        if not isinstance(parent, dict) or date_text not in parent:
            continue
        item = parent.get(date_text)
        if isinstance(item, dict):
            windows = normalize_session_windows(item.get("session_windows") or item.get("sessions"))
            reason = str(item.get("reason") or item.get("name") or key)
        else:
            windows = normalize_session_windows(item)
            reason = key
        if windows:
            return {
                "type": "sessions",
                "reason": reason,
                "source_key": key,
                "session_windows": windows,
            }
    return None


def market_session_state(market, now=None, session_overrides=None, overrides_source=None):
    market = market_code(market)
    local_now = local_now_for_market(market, now=now)
    minute = minutes_since_midnight(local_now)
    weekday = local_now.weekday()
    if market == "HK":
        sessions = [(570, 720), (780, 960)]
        timezone_name = "Asia/Hong_Kong"
        regular_session = "09:30-12:00,13:00-16:00"
        if weekday >= 5:
            phase = "CLOSED_WEEKEND"
        elif minute < 570:
            phase = "PRE_OPEN"
        elif 570 <= minute < 720:
            phase = "REGULAR_OPEN"
        elif 720 <= minute < 780:
            phase = "LUNCH_BREAK"
        elif 780 <= minute <= 960:
            phase = "REGULAR_OPEN"
        else:
            phase = "AFTER_CLOSE"
    elif market == "US":
        sessions = [(570, 960)]
        timezone_name = "America/New_York"
        regular_session = "09:30-16:00"
        if weekday >= 5:
            phase = "CLOSED_WEEKEND"
        elif minute < 570:
            phase = "PRE_OPEN"
        elif 570 <= minute <= 960:
            phase = "REGULAR_OPEN"
        else:
            phase = "AFTER_CLOSE"
    else:
        sessions = []
        timezone_name = "unknown"
        regular_session = "unknown"
        phase = "UNKNOWN"

    override = session_override_for_date(
        market,
        local_now.date().isoformat(),
        session_overrides or {},
    )
    if override:
        if override.get("type") == "closed":
            sessions = []
            phase = "CLOSED_HOLIDAY"
        elif override.get("type") == "sessions":
            sessions = override.get("session_windows") or []
            regular_session = ",".join(f"{hhmm(start)}-{hhmm(end)}" for start, end in sessions) or regular_session
            if not sessions:
                phase = "CLOSED_HOLIDAY"
            elif minute < sessions[0][0]:
                phase = "PRE_OPEN"
            elif any(start <= minute <= end for start, end in sessions):
                phase = "REGULAR_OPEN"
            elif any(start > minute for start, _end in sessions):
                phase = "SESSION_BREAK"
            else:
                phase = "AFTER_CLOSE"

    is_open = phase == "REGULAR_OPEN"
    minutes_to_next_open = None
    if sessions and not is_open:
        candidates = [start - minute for start, _end in sessions if start > minute]
        if candidates:
            minutes_to_next_open = min(candidates)
        else:
            days = 1
            while (local_now + timedelta(days=days)).weekday() >= 5:
                days += 1
            minutes_to_next_open = days * 1440 - minute + sessions[0][0]
    minutes_since_regular_close = None
    if sessions and not is_open:
        closed_sessions = [end for _start, end in sessions if end < minute]
        if closed_sessions:
            minutes_since_regular_close = minute - max(closed_sessions)

    return {
        "schema": "intraday_market_session_v1",
        "market": market,
        "timezone": timezone_name,
        "local_time": local_now.isoformat(timespec="seconds"),
        "weekday": weekday,
        "phase": phase,
        "is_regular_session_open": is_open,
        "regular_session": regular_session,
        "session_windows": [
            {"open": hhmm(start), "close": hhmm(end)}
            for start, end in sessions
        ],
        "minutes_to_next_regular_open": minutes_to_next_open,
        "minutes_since_regular_close": minutes_since_regular_close,
        "holiday_calendar_applied": bool(override),
        "override_applied": bool(override),
        "override_reason": override.get("reason") if override else None,
        "override_source_key": override.get("source_key") if override else None,
        "overrides_file": overrides_source,
        "note": (
            "configured session override applied"
            if override
            else "weekday regular-hours approximation; exchange holiday and half-day calendars are not applied"
        ),
    }


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pct_change(value, previous):
    if value is None or previous in (None, 0):
        return None
    return (value / previous - 1.0) * 100.0


def valid_ohlc(row):
    o = as_float(row.get("open"))
    h = as_float(row.get("high"))
    low = as_float(row.get("low"))
    c = as_float(row.get("close"))
    values = (o, h, low, c)
    if any(value is None or value <= 0 for value in values):
        return False
    return h >= low and low <= o <= h and low <= c <= h


def minute_gap_stats(points):
    gaps = []
    max_gap = None
    for left, right in zip(points, points[1:]):
        delta = (right["parsed_timestamp"] - left["parsed_timestamp"]).total_seconds() / 60.0
        if delta <= 0:
            continue
        max_gap = delta if max_gap is None else max(max_gap, delta)
        if delta > MAX_GAP_MINUTES:
            gaps.append(
                {
                    "from": left["parsed_timestamp"].isoformat(timespec="seconds"),
                    "to": right["parsed_timestamp"].isoformat(timespec="seconds"),
                    "gap_minutes": round(delta, 2),
                }
            )
    return {
        "max_gap_minutes": round(max_gap, 2) if max_gap is not None else None,
        "large_gap_count": len(gaps),
        "large_gap_examples": gaps[:5],
    }


def symbol_quality(
    symbol,
    raw_point_count,
    points,
    bad_timestamp_count,
    invalid_ohlc_count,
    bars_5m,
    bars_60m,
):
    timestamp_counts = defaultdict(int)
    for point in points:
        timestamp_counts[point["parsed_timestamp"].isoformat(timespec="seconds")] += 1
    duplicate_timestamps = sorted(ts for ts, count in timestamp_counts.items() if count > 1)
    missing_source_count = len(
        [
            point
            for point in points
            if str(point.get("data_source") or "").strip().lower() in ("", "missing")
        ]
    )
    source_counts = Counter(normalized_text(point.get("data_source")) for point in points)
    granularity_counts = Counter(normalized_text(point.get("source_granularity")) for point in points)
    missing_granularity_count = sum(
        count for value, count in granularity_counts.items() if value in ("", "missing", "unknown")
    )
    snapshot_like_count = len([point for point in points if row_is_snapshot_like(point)])
    full_ohlc_count = len([point for point in points if row_has_full_ohlc_fidelity(point)])
    low_fidelity_source_count = sum(
        count for value, count in source_counts.items() if value in LOW_FIDELITY_INTRADAY_SOURCES
    )
    low_fidelity_granularity_count = sum(
        count for value, count in granularity_counts.items() if value in LOW_FIDELITY_INTRADAY_GRANULARITIES
    )
    low_fidelity_point_count = max(
        low_fidelity_source_count,
        low_fidelity_granularity_count,
        len(points) - full_ohlc_count,
    )
    gap_stats = minute_gap_stats(points)
    notes = []
    if bad_timestamp_count:
        notes.append("bad_intraday_timestamps")
    if invalid_ohlc_count:
        notes.append("invalid_intraday_ohlc_rows")
    if duplicate_timestamps:
        notes.append("duplicate_intraday_timestamps")
    if missing_source_count:
        notes.append("missing_intraday_data_source")
    if missing_granularity_count:
        notes.append("missing_intraday_source_granularity")
    if low_fidelity_point_count:
        notes.append("low_fidelity_intraday_source")
    if snapshot_like_count:
        notes.append("snapshot_like_intraday_rows")
    if gap_stats["large_gap_count"]:
        notes.append("intraday_minute_gap_detected")
    if points and len(bars_5m) < MIN_RECENT_5M_BARS:
        notes.append("insufficient_recent_5m_bars")
    if points and len(bars_60m) < MIN_RECENT_60M_BARS:
        notes.append("insufficient_recent_60m_bars")

    if not raw_point_count:
        status = "MISSING"
    elif bad_timestamp_count or invalid_ohlc_count or duplicate_timestamps:
        status = "WARN"
    elif (
        missing_source_count
        or missing_granularity_count
        or low_fidelity_point_count
        or snapshot_like_count
        or gap_stats["large_gap_count"]
        or any("insufficient" in note for note in notes)
    ):
        status = "WARN"
    else:
        status = "OK"

    return {
        "schema": "intraday_symbol_quality_v1",
        "symbol": symbol,
        "status": status,
        "raw_point_count": raw_point_count,
        "valid_point_count": len(points),
        "bad_timestamp_count": bad_timestamp_count,
        "invalid_ohlc_count": invalid_ohlc_count,
        "duplicate_timestamp_count": len(duplicate_timestamps),
        "duplicate_timestamp_examples": duplicate_timestamps[:5],
        "missing_data_source_count": missing_source_count,
        "source_counts": dict(source_counts),
        "source_granularity_counts": dict(granularity_counts),
        "missing_source_granularity_count": missing_granularity_count,
        "snapshot_like_row_count": snapshot_like_count,
        "full_ohlc_row_count": full_ohlc_count,
        "low_fidelity_point_count": low_fidelity_point_count,
        "large_gap_count": gap_stats["large_gap_count"],
        "max_gap_minutes": gap_stats["max_gap_minutes"],
        "large_gap_examples": gap_stats["large_gap_examples"],
        "recent_5m_bar_count": len(bars_5m),
        "recent_60m_bar_count": len(bars_60m),
        "min_recent_5m_bars": MIN_RECENT_5M_BARS,
        "min_recent_60m_bars": MIN_RECENT_60M_BARS,
        "notes": notes,
    }


def market_code(exchange_or_market):
    text = str(exchange_or_market or "").upper()
    if text in ("HK", "HKEX", "HKG"):
        return "HK"
    if text in ("US", "NASDAQ", "NYSE", "AMEX"):
        return "US"
    return text or "UNKNOWN"


def normalize_symbol_list(value):
    if value is None:
        return []
    raw = value if isinstance(value, list) else str(value).replace(";", ",").split(",")
    result = []
    seen = set()
    for item in raw:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def symbols_from_watchlist_payload(payload, market):
    candidates = [
        payload.get(market),
        payload.get(market.lower()),
        payload.get(f"{market}_WATCHLIST"),
        payload.get(f"{market.lower()}_watchlist"),
    ]
    for parent_key in ("markets", "watchlists"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            item = parent.get(market) or parent.get(market.lower())
            if isinstance(item, dict):
                candidates.append(item.get("symbols"))
            else:
                candidates.append(item)
    for candidate in candidates:
        symbols = normalize_symbol_list(candidate)
        if symbols:
            return symbols
    return []


def load_watchlist_symbols(path=WATCHLIST_FILE):
    if not path:
        return {"HK": [], "US": []}, ["watchlist_file_not_configured"]
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return {"HK": [], "US": []}, [f"watchlist_file_unreadable:{path}:{exc}"]
    if not isinstance(payload, dict):
        return {"HK": [], "US": []}, [f"watchlist_file_invalid:{path}"]
    return {
        "HK": symbols_from_watchlist_payload(payload, "HK"),
        "US": symbols_from_watchlist_payload(payload, "US"),
    }, []


def load_market_session_overrides(path=MARKET_SESSION_OVERRIDES_FILE):
    if not path:
        return {}, ["intraday_market_session_overrides_file_not_configured"]
    if not os.path.exists(path):
        return {}, [f"intraday_market_session_overrides_file_missing:{path}"]
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return {}, [f"intraday_market_session_overrides_file_unreadable:{path}:{exc}"]
    if not isinstance(payload, dict):
        return {}, [f"intraday_market_session_overrides_file_invalid:{path}"]
    if isinstance(payload.get("markets"), dict):
        return payload.get("markets") or {}, []
    return payload, []


def fetch_intraday_rows(symbols_by_market, lookback_minutes=LOOKBACK_MINUTES, kline_columns=None):
    symbols = []
    for market in ("HK", "US"):
        symbols.extend(symbols_by_market.get(market) or [])
    symbols = sorted(set(symbols))
    if not symbols:
        return [], ["no_watchlist_symbols_for_intraday_context"]
    kline_columns = table_columns("klines") if kline_columns is None else set(kline_columns or [])
    source_granularity_expr = (
        "COALESCE(k.source_granularity, 'missing')"
        if "source_granularity" in kline_columns
        else "'missing'"
    )
    sql = f"""
        SELECT CASE WHEN s.exchange = 'HKEX' THEN 'HK' ELSE 'US' END AS market,
               k.symbol,
               k.timestamp,
               k.open_price,
               k.high_price,
               k.low_price,
               k.close_price,
               k.volume,
               COALESCE(k.amount, 0),
               COALESCE(k.data_source, 'missing'),
               {source_granularity_expr}
        FROM klines k
        LEFT JOIN stocks s ON s.symbol = k.symbol
        WHERE k.interval = 'min'
          AND k.symbol IN ({sql_in(symbols)})
          AND k.timestamp >= NOW() - INTERVAL '{int(lookback_minutes)} minutes'
        ORDER BY k.symbol, k.timestamp
    """
    result = psql(sql)
    if result.returncode != 0:
        return [], [f"intraday_kline_query_failed:{result.stderr.strip()}"]
    parsed = []
    for row in rows(result.stdout):
        if len(row) < 10:
            continue
        parsed.append(
            {
                "market": market_code(row[0]),
                "symbol": str(row[1] or "").upper(),
                "timestamp": row[2],
                "open": as_float(row[3]),
                "high": as_float(row[4]),
                "low": as_float(row[5]),
                "close": as_float(row[6]),
                "volume": as_float(row[7], 0.0),
                "amount": as_float(row[8], 0.0),
                "data_source": row[9],
                "source_granularity": row[10] if len(row) > 10 and row[10] else "missing",
            }
        )
    return parsed, []


def bucket_start(ts, minutes):
    if not ts:
        return None
    minute = (ts.minute // minutes) * minutes
    return ts.replace(minute=minute, second=0, microsecond=0)


def ohlcv_for_rows(rows_for_bucket):
    if not rows_for_bucket:
        return {}
    ordered = sorted(rows_for_bucket, key=lambda row: row["parsed_timestamp"])
    return {
        "start": ordered[0]["parsed_timestamp"].isoformat(timespec="seconds"),
        "end": ordered[-1]["parsed_timestamp"].isoformat(timespec="seconds"),
        "open": ordered[0]["open"],
        "high": max(row["high"] for row in ordered if row["high"] is not None),
        "low": min(row["low"] for row in ordered if row["low"] is not None),
        "close": ordered[-1]["close"],
        "volume": round(sum(as_float(row.get("volume"), 0.0) or 0.0 for row in ordered), 4),
        "amount": round(sum(as_float(row.get("amount"), 0.0) or 0.0 for row in ordered), 4),
        "row_count": len(ordered),
    }


def aggregate_bars(points, minutes, limit=6):
    buckets = defaultdict(list)
    for point in points:
        start = bucket_start(point.get("parsed_timestamp"), minutes)
        if start:
            buckets[start].append(point)
    bars = [ohlcv_for_rows(bucket_rows) for _start, bucket_rows in sorted(buckets.items())]
    bars = [bar for bar in bars if bar.get("open") is not None and bar.get("close") is not None]
    return bars[-limit:]


def rolling_window_summary(points, minutes, offset_windows=0):
    ordered = sorted(points or [], key=lambda row: row["parsed_timestamp"])
    if not ordered:
        return {
            "schema": "intraday_rolling_window_v1",
            "timeframe": f"{minutes}m",
            "window_minutes": minutes,
            "method": "latest_rolling_window",
            "coverage_status": "MISSING",
            "row_count": 0,
            "expected_minute_count": minutes,
            "coverage_ratio": 0.0,
        }

    latest_ts = ordered[-1]["parsed_timestamp"]
    expected_end = latest_ts - timedelta(minutes=minutes * offset_windows)
    expected_start = expected_end - timedelta(minutes=minutes - 1)
    rows_for_window = [
        row
        for row in ordered
        if expected_start <= row["parsed_timestamp"] <= expected_end
    ]
    bar = ohlcv_for_rows(rows_for_window)
    row_count = len(rows_for_window)
    coverage_ratio = min(row_count / float(minutes), 1.0) if minutes else 0.0
    if not row_count:
        coverage_status = "MISSING"
    elif coverage_ratio < MIN_ROLLING_WINDOW_COVERAGE_RATIO:
        coverage_status = "LIMITED"
    else:
        coverage_status = "OK"
    change = pct_change(bar.get("close"), bar.get("open")) if bar else None
    return {
        "schema": "intraday_rolling_window_v1",
        "timeframe": f"{minutes}m",
        "window_minutes": minutes,
        "method": "latest_rolling_window",
        "expected_start": expected_start.isoformat(timespec="seconds"),
        "expected_end": expected_end.isoformat(timespec="seconds"),
        "coverage_status": coverage_status,
        "row_count": row_count,
        "expected_minute_count": minutes,
        "coverage_ratio": round(coverage_ratio, 4),
        "change_pct": round(change, 4) if change is not None else None,
        "momentum": momentum_label(change),
        "bar": bar,
    }


def rolling_windows_for_points(points):
    return {
        f"{minutes}m": rolling_window_summary(points, minutes)
        for minutes in ROLLING_WINDOW_MINUTES
    }


def window_volume_state(current_window, previous_window):
    current_bar = (current_window or {}).get("bar") or {}
    previous_bar = (previous_window or {}).get("bar") or {}
    return volume_label(current_bar.get("volume"), previous_bar.get("volume"))


def latest_timeframe_payload(window, volume_state):
    window = window or {}
    return {
        "change_pct": window.get("change_pct"),
        "momentum": window.get("momentum") or "unknown",
        "volume_state": volume_state,
        "coverage_status": window.get("coverage_status"),
        "coverage_ratio": window.get("coverage_ratio"),
        "row_count": window.get("row_count"),
        "expected_minute_count": window.get("expected_minute_count"),
        "bar": window.get("bar") or {},
    }


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


def volume_label(value, previous):
    if value is None or previous in (None, 0):
        return "unknown"
    ratio = value / previous
    if ratio >= 1.5:
        return "expanding"
    if ratio <= 0.67:
        return "contracting"
    return "normal"


def direction_from_momentum(momentum):
    if momentum in ("strong_up", "up"):
        return "up"
    if momentum in ("strong_down", "down"):
        return "down"
    if momentum == "flat":
        return "flat"
    return "unknown"


def timeframe_vote(name, change_pct, momentum, volume_state=None):
    vote = {
        "timeframe": name,
        "direction": direction_from_momentum(momentum),
        "momentum": momentum,
    }
    if change_pct is not None:
        vote["change_pct"] = round(change_pct, 4)
    if volume_state:
        vote["volume_state"] = volume_state
    return vote


def multi_timeframe_confirmation(
    session_change,
    last_5m_change,
    last_15m_change,
    last_60m_change,
    latest_volume_state,
    fifteen_volume_state,
    sixty_volume_state,
):
    votes = [
        timeframe_vote("session", session_change, momentum_label(session_change)),
        timeframe_vote("latest_5m", last_5m_change, momentum_label(last_5m_change), latest_volume_state),
        timeframe_vote("latest_15m", last_15m_change, momentum_label(last_15m_change), fifteen_volume_state),
        timeframe_vote("latest_60m", last_60m_change, momentum_label(last_60m_change), sixty_volume_state),
    ]
    directional = [vote for vote in votes if vote["direction"] in ("up", "down")]
    up_count = len([vote for vote in directional if vote["direction"] == "up"])
    down_count = len([vote for vote in directional if vote["direction"] == "down"])
    if not directional:
        alignment = "insufficient"
        dominant_direction = "unknown"
    elif up_count == len(directional):
        alignment = "bullish_aligned"
        dominant_direction = "up"
    elif down_count == len(directional):
        alignment = "bearish_aligned"
        dominant_direction = "down"
    elif up_count > down_count:
        alignment = "mixed_bullish"
        dominant_direction = "up"
    elif down_count > up_count:
        alignment = "mixed_bearish"
        dominant_direction = "down"
    else:
        alignment = "conflicting"
        dominant_direction = "mixed"

    latest_direction = next((vote["direction"] for vote in votes if vote["timeframe"] == "latest_5m"), "unknown")
    fifteen_direction = next((vote["direction"] for vote in votes if vote["timeframe"] == "latest_15m"), "unknown")
    hourly_direction = next((vote["direction"] for vote in votes if vote["timeframe"] == "latest_60m"), "unknown")
    session_direction = next((vote["direction"] for vote in votes if vote["timeframe"] == "session"), "unknown")
    contradictions = []
    if latest_direction in ("up", "down") and session_direction in ("up", "down") and latest_direction != session_direction:
        contradictions.append("latest_5m_contradicts_session")
    if fifteen_direction in ("up", "down") and session_direction in ("up", "down") and fifteen_direction != session_direction:
        contradictions.append("latest_15m_contradicts_session")
    if hourly_direction in ("up", "down") and session_direction in ("up", "down") and hourly_direction != session_direction:
        contradictions.append("latest_60m_contradicts_session")
    if latest_direction in ("up", "down") and fifteen_direction in ("up", "down") and latest_direction != fifteen_direction:
        contradictions.append("latest_5m_contradicts_latest_15m")
    if latest_direction in ("up", "down") and hourly_direction in ("up", "down") and latest_direction != hourly_direction:
        contradictions.append("latest_5m_contradicts_latest_60m")
    if fifteen_direction in ("up", "down") and hourly_direction in ("up", "down") and fifteen_direction != hourly_direction:
        contradictions.append("latest_15m_contradicts_latest_60m")

    return {
        "schema": "intraday_multi_timeframe_confirmation_v1",
        "alignment": alignment,
        "dominant_direction": dominant_direction,
        "directional_vote_count": len(directional),
        "up_vote_count": up_count,
        "down_vote_count": down_count,
        "votes": votes,
        "contradictions": contradictions,
        "buy_confirmation": alignment in ("bullish_aligned", "mixed_bullish") and down_count == 0,
        "sell_confirmation": alignment in ("bearish_aligned", "mixed_bearish") and up_count == 0,
    }


def symbol_summary(market, symbol, raw_points, now=None, session_state=None):
    now = local_now_for_market(market, now=now)
    session_state = session_state or market_session_state(market, now=now)
    points = []
    warnings = []
    bad_timestamp_count = 0
    invalid_ohlc_count = 0
    for row in raw_points:
        parsed = parse_timestamp(row.get("timestamp"))
        if not parsed:
            bad_timestamp_count += 1
            warnings.append("bad_intraday_timestamp")
            continue
        if not valid_ohlc(row):
            invalid_ohlc_count += 1
            warnings.append("invalid_intraday_ohlc")
            continue
        normalized = dict(row)
        normalized["parsed_timestamp"] = parsed
        points.append(normalized)
    points = sorted(points, key=lambda row: row["parsed_timestamp"])
    if not points:
        quality = symbol_quality(
            symbol,
            len(raw_points or []),
            points,
            bad_timestamp_count,
            invalid_ohlc_count,
            [],
            [],
        )
        market_open = session_state.get("is_regular_session_open") is True
        notes = ["intraday_context_missing_for_symbol"]
        if not market_open:
            notes.append("intraday_market_not_open_requires_session_context")
        return {
            "symbol": symbol,
            "market": market,
            "status": "MISSING" if market_open else "CLOSED",
            "market_session": session_state,
            "point_count": 0,
            "quality": quality,
            "warnings": warnings or ["missing_intraday_rows"],
            "hermes_notes": notes,
        }

    first = points[0]
    latest = points[-1]
    latest_ts = latest["parsed_timestamp"]
    age_minutes = round((now - latest_ts).total_seconds() / 60, 2)
    session_change = pct_change(latest.get("close"), first.get("open"))
    high_since_open = max((row.get("high") for row in points if row.get("high") is not None), default=None)
    low_since_open = min((row.get("low") for row in points if row.get("low") is not None), default=None)
    range_pct = pct_change(high_since_open, low_since_open) if high_since_open is not None else None
    bars_5m = aggregate_bars(points, 5, limit=12)
    bars_60m = aggregate_bars(points, 60, limit=8)
    rolling_windows = rolling_windows_for_points(points)
    rolling_5m = rolling_windows.get("5m") or {}
    rolling_15m = rolling_windows.get("15m") or {}
    rolling_30m = rolling_windows.get("30m") or {}
    rolling_60m = rolling_windows.get("60m") or {}
    previous_5m = rolling_window_summary(points, 5, offset_windows=1)
    previous_15m = rolling_window_summary(points, 15, offset_windows=1)
    previous_30m = rolling_window_summary(points, 30, offset_windows=1)
    previous_60m = rolling_window_summary(points, 60, offset_windows=1)
    quality = symbol_quality(
        symbol,
        len(raw_points or []),
        points,
        bad_timestamp_count,
        invalid_ohlc_count,
        bars_5m,
        bars_60m,
    )
    last_5m_change = rolling_5m.get("change_pct")
    last_15m_change = rolling_15m.get("change_pct")
    last_60m_change = rolling_60m.get("change_pct")
    latest_volume_state = window_volume_state(rolling_5m, previous_5m)
    fifteen_volume_state = window_volume_state(rolling_15m, previous_15m)
    thirty_volume_state = window_volume_state(rolling_30m, previous_30m)
    sixty_volume_state = window_volume_state(rolling_60m, previous_60m)
    mtf = multi_timeframe_confirmation(
        session_change,
        last_5m_change,
        last_15m_change,
        last_60m_change,
        latest_volume_state,
        fifteen_volume_state,
        sixty_volume_state,
    )
    data_sources = sorted(set(str(row.get("data_source") or "missing") for row in points))
    source_granularities = sorted(set(str(row.get("source_granularity") or "missing") for row in points))
    market_open = session_state.get("is_regular_session_open") is True
    stale = (market_open and age_minutes > MAX_STALE_MINUTES) or age_minutes < -5
    notes = []
    if stale:
        notes.append("intraday_context_stale_for_symbol")
    if not market_open:
        notes.append("intraday_market_not_open_requires_session_context")
    if session_change is not None and session_change <= -1.0:
        notes.append("intraday_session_down_against_new_buy_review")
    if session_change is not None and session_change >= 1.0:
        notes.append("intraday_session_up_supports_buy_review")
    if latest_volume_state == "expanding":
        notes.append("latest_5m_volume_expanding")
    if sixty_volume_state == "expanding":
        notes.append("latest_60m_volume_expanding")
    if mtf["alignment"] == "bearish_aligned":
        notes.append("intraday_multi_timeframe_bearish_challenges_buy_review")
    if mtf["alignment"] == "bullish_aligned":
        notes.append("intraday_multi_timeframe_bullish_challenges_sell_review")
    if mtf["contradictions"]:
        notes.append("intraday_timeframes_conflicting_requires_disclosure")
    for timeframe in ("5m", "15m", "30m", "60m"):
        window = rolling_windows.get(timeframe) or {}
        if window.get("coverage_status") == "LIMITED":
            notes.append(f"intraday_{timeframe}_window_coverage_limited_requires_disclosure")
    if quality["status"] != "OK":
        notes.append("intraday_context_quality_degraded_requires_disclosure")
    status = "STALE" if stale else "OK"
    if not market_open:
        status = "CLOSED"
    return {
        "symbol": symbol,
        "market": market,
        "status": status,
        "market_session": session_state,
        "point_count": len(points),
        "latest_timestamp": latest_ts.isoformat(timespec="seconds"),
        "latest_age_minutes": age_minutes,
        "latest_price": latest.get("close"),
        "session": {
            "start_timestamp": first["parsed_timestamp"].isoformat(timespec="seconds"),
            "start_price": first.get("open"),
            "change_pct": round(session_change, 4) if session_change is not None else None,
            "high": high_since_open,
            "low": low_since_open,
            "range_pct": round(range_pct, 4) if range_pct is not None else None,
            "momentum": momentum_label(session_change),
        },
        "latest_5m": {
            **latest_timeframe_payload(rolling_5m, latest_volume_state),
        },
        "latest_15m": {
            **latest_timeframe_payload(rolling_15m, fifteen_volume_state),
        },
        "latest_30m": {
            **latest_timeframe_payload(rolling_30m, thirty_volume_state),
        },
        "latest_60m": {
            **latest_timeframe_payload(rolling_60m, sixty_volume_state),
        },
        "multi_timeframe_confirmation": mtf,
        "rolling_windows": rolling_windows,
        "recent_5m_bars": bars_5m,
        "recent_60m_bars": bars_60m,
        "data_sources": data_sources,
        "source_granularities": source_granularities,
        "quality": quality,
        "warnings": warnings,
        "hermes_notes": notes,
    }


def summarize_market(market, symbols, rows_for_market, now=None, session_overrides=None, overrides_source=None):
    now = local_now_for_market(market, now=now)
    session_state = market_session_state(
        market,
        now=now,
        session_overrides=session_overrides,
        overrides_source=overrides_source,
    )
    by_symbol = defaultdict(list)
    for row in rows_for_market:
        by_symbol[str(row.get("symbol") or "").upper()].append(row)
    symbol_summaries = [
        symbol_summary(market, symbol, by_symbol.get(symbol, []), now=now, session_state=session_state)
        for symbol in symbols
    ]
    ok_count = len([item for item in symbol_summaries if item.get("status") == "OK"])
    closed_count = len([item for item in symbol_summaries if item.get("status") == "CLOSED"])
    stale_count = len([item for item in symbol_summaries if item.get("status") == "STALE"])
    missing_count = len([item for item in symbol_summaries if item.get("status") == "MISSING"])
    quality_rows = [
        item.get("quality")
        for item in symbol_summaries
        if isinstance(item.get("quality"), dict)
    ]
    quality_degraded = [row for row in quality_rows if row.get("status") != "OK"]
    quality_summary = {
        "schema": "intraday_market_quality_summary_v1",
        "ok_symbol_count": len([row for row in quality_rows if row.get("status") == "OK"]),
        "degraded_symbol_count": len(quality_degraded),
        "bad_timestamp_symbol_count": len([row for row in quality_rows if row.get("bad_timestamp_count")]),
        "invalid_ohlc_symbol_count": len([row for row in quality_rows if row.get("invalid_ohlc_count")]),
        "duplicate_timestamp_symbol_count": len(
            [row for row in quality_rows if row.get("duplicate_timestamp_count")]
        ),
        "missing_data_source_symbol_count": len(
            [row for row in quality_rows if row.get("missing_data_source_count")]
        ),
        "missing_source_granularity_symbol_count": len(
            [row for row in quality_rows if row.get("missing_source_granularity_count")]
        ),
        "low_fidelity_source_symbol_count": len(
            [row for row in quality_rows if row.get("low_fidelity_point_count")]
        ),
        "snapshot_like_symbol_count": len([row for row in quality_rows if row.get("snapshot_like_row_count")]),
        "full_ohlc_symbol_count": len(
            [
                row
                for row in quality_rows
                if row.get("valid_point_count") and row.get("full_ohlc_row_count") == row.get("valid_point_count")
            ]
        ),
        "large_gap_symbol_count": len([row for row in quality_rows if row.get("large_gap_count")]),
        "insufficient_recent_5m_symbol_count": len(
            [row for row in quality_rows if "insufficient_recent_5m_bars" in (row.get("notes") or [])]
        ),
        "insufficient_recent_60m_symbol_count": len(
            [row for row in quality_rows if "insufficient_recent_60m_bars" in (row.get("notes") or [])]
        ),
        "sample_degraded_symbols": [
            {
                "symbol": row.get("symbol"),
                "status": row.get("status"),
                "notes": row.get("notes") or [],
                "max_gap_minutes": row.get("max_gap_minutes"),
            }
            for row in quality_degraded[:20]
        ],
    }
    up_count = len([item for item in symbol_summaries if (item.get("session") or {}).get("momentum") in ("up", "strong_up")])
    down_count = len([item for item in symbol_summaries if (item.get("session") or {}).get("momentum") in ("down", "strong_down")])
    latest_timestamps = [item.get("latest_timestamp") for item in symbol_summaries if item.get("latest_timestamp")]
    status = "MISSING"
    if ok_count:
        status = "OK"
    elif stale_count:
        status = "STALE"
    elif missing_count:
        status = "MISSING"
    elif closed_count:
        status = "CLOSED"
    return {
        "market": market,
        "status": status,
        "market_session": session_state,
        "symbol_count": len(symbols),
        "ok_symbol_count": ok_count,
        "closed_symbol_count": closed_count,
        "stale_symbol_count": stale_count,
        "missing_symbol_count": missing_count,
        "latest_timestamp": max(latest_timestamps) if latest_timestamps else None,
        "breadth": {
            "session_up_count": up_count,
            "session_down_count": down_count,
            "session_up_pct": round(up_count / len(symbols) * 100.0, 2) if symbols else 0.0,
            "session_down_pct": round(down_count / len(symbols) * 100.0, 2) if symbols else 0.0,
        },
        "quality_summary": quality_summary,
        "symbols": symbol_summaries,
    }


def build_recommendations(markets):
    recs = []
    for market, summary in sorted((markets or {}).items()):
        quality = summary.get("quality_summary") or {}
        session = summary.get("market_session") or {}
        market_open = session.get("is_regular_session_open") is True
        closed_symbols = int(summary.get("closed_symbol_count") or 0)
        if summary.get("missing_symbol_count"):
            recs.append(f"{market}:intraday_missing_for_watchlist_symbols")
        if summary.get("stale_symbol_count"):
            recs.append(f"{market}:refresh_intraday_context_before_trade_judgment")
        if closed_symbols and not market_open:
            recs.append(f"{market}:intraday_market_closed_use_last_session_context_only")
        if quality.get("degraded_symbol_count"):
            recs.append(f"{market}:review_intraday_quality_before_trade_judgment")
        if quality.get("low_fidelity_source_symbol_count") or quality.get("snapshot_like_symbol_count"):
            recs.append(f"{market}:treat_public_snapshot_minute_rows_as_advisory_until_full_ohlcv_source")
        if quality.get("missing_source_granularity_symbol_count"):
            recs.append(f"{market}:persist_intraday_source_granularity_before_claiming_full_ohlcv_context")
        if quality.get("large_gap_symbol_count"):
            recs.append(f"{market}:refresh_or_repair_minute_kline_gap_coverage")
        if quality.get("invalid_ohlc_symbol_count") or quality.get("bad_timestamp_symbol_count"):
            recs.append(f"{market}:fix_invalid_intraday_kline_rows_before_trusting_path_evidence")
        if (summary.get("breadth") or {}).get("session_down_pct", 0) >= 60:
            recs.append(f"{market}:intraday_breadth_weak_tighten_new_buy_review")
        if (summary.get("breadth") or {}).get("session_up_pct", 0) >= 60:
            recs.append(f"{market}:intraday_breadth_strong_require_news_and_risk_confirmation")
    if not recs:
        recs.append("intraday_context_available_for_hermes_review")
    return recs


def intraday_granularity_policy():
    return {
        "schema": "intraday_granularity_usage_policy_v1",
        "read_only": True,
        "submits_orders": False,
        "changes_strategy": False,
        "changes_alert_queue": False,
        "daily_forward_outcomes_remain_authority": True,
        "full_ohlcv_required_for_path_evidence": True,
        "snapshot_minute_rows_are_advisory_only": True,
        "timeframes": {
            "60m": {
                "role": "intraday_regime_confirmation_or_challenge",
                "allowed_uses": [
                    "confirm_or_challenge_daily_signal_direction",
                    "identify_same_session_regime_shift",
                    "adjust_hermes_confidence_or_size_advice",
                ],
                "forbidden_uses": [
                    "standalone_buy_or_sell_signal",
                    "override_execution_readiness_or_daily_strategy_evidence",
                ],
            },
            "30m": {
                "role": "intermediate_confirmation_or_reversal_check",
                "allowed_uses": [
                    "confirm_60m_alignment",
                    "detect_intraday_reversal_against_daily_signal",
                    "support_hold_reduce_or_wait_notes",
                ],
                "forbidden_uses": [
                    "standalone_alpha_source",
                    "increase_confidence_when_coverage_status_is_limited",
                ],
            },
            "15m": {
                "role": "trade_timing_and_confirmation",
                "allowed_uses": [
                    "confirm_or_challenge_latest_signal_momentum",
                    "detect_short_intraday_pullback_or_breakdown",
                    "guide_hermes_timing_notes",
                ],
                "forbidden_uses": [
                    "submit_orders",
                    "replace_news_macro_fundamentals_or_source_reliability_review",
                ],
            },
            "5m": {
                "role": "near_term_timing_and_noise_filter",
                "allowed_uses": [
                    "detect_immediate_contradiction",
                    "flag_chasing_or_weak_entry_timing",
                    "support_watch_or_hold_when_short_term_context_is_unclear",
                ],
                "forbidden_uses": [
                    "raise_confidence_without_15m_or_60m_support",
                    "treat_single_window_move_as_market_regime",
                ],
            },
            "1m": {
                "role": "execution_quality_and_path_diagnostics",
                "allowed_uses": [
                    "resolve_stop_or_target_ordering_only_with_full_ohlcv_rows",
                    "measure_slippage_or_entry_quality",
                    "support_postmortem_learning",
                ],
                "forbidden_uses": [
                    "core_alpha_generation",
                    "confidence_boost_from_public_snapshot_rows",
                    "daily_kline_repair_or_replacement",
                ],
            },
        },
        "hermes_rule": (
            "Use 60m/30m/15m/5m as confirmation, contradiction, timing, and disclosure evidence only. "
            "Use 1m mainly for execution/path/postmortem diagnostics. Do not promote any intraday timeframe "
            "to trading authority without trusted full-OHLCV provenance, adequate coverage, and separate "
            "strategy evidence."
        ),
    }


def build_report(
    intraday_rows=None,
    symbols_by_market=None,
    now=None,
    warnings=None,
    watchlist_file=WATCHLIST_FILE,
    lookback_minutes=LOOKBACK_MINUTES,
    market_session_overrides=None,
    market_session_overrides_file=MARKET_SESSION_OVERRIDES_FILE,
):
    now = now or datetime.now(timezone.utc)
    warnings = list(warnings or [])
    if symbols_by_market is None:
        symbols_by_market, watchlist_warnings = load_watchlist_symbols(watchlist_file)
        warnings.extend(watchlist_warnings)
    symbols_by_market = {
        "HK": normalize_symbol_list((symbols_by_market or {}).get("HK"))[:MAX_SYMBOLS_PER_MARKET],
        "US": normalize_symbol_list((symbols_by_market or {}).get("US"))[:MAX_SYMBOLS_PER_MARKET],
    }
    if intraday_rows is None:
        intraday_rows, fetch_warnings = fetch_intraday_rows(symbols_by_market, lookback_minutes=lookback_minutes)
        warnings.extend(fetch_warnings)
    if market_session_overrides is None:
        market_session_overrides, override_warnings = load_market_session_overrides(market_session_overrides_file)
        warnings.extend(override_warnings)
    rows_by_market = defaultdict(list)
    for row in intraday_rows or []:
        rows_by_market[market_code(row.get("market"))].append(row)
    markets = {
        market: summarize_market(
            market,
            symbols,
            rows_by_market.get(market, []),
            now=now,
            session_overrides=market_session_overrides,
            overrides_source=market_session_overrides_file,
        )
        for market, symbols in sorted(symbols_by_market.items())
        if symbols
    }
    if not markets:
        status = "MISSING"
    elif any(summary.get("status") == "OK" for summary in markets.values()):
        status = "OK"
    elif any(summary.get("status") == "STALE" for summary in markets.values()):
        status = "STALE"
    elif any(summary.get("status") == "MISSING" for summary in markets.values()):
        status = "MISSING"
    elif any(summary.get("status") == "CLOSED" for summary in markets.values()):
        status = "CLOSED"
    else:
        status = "MISSING"
    return {
        "schema": "intraday_context_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "kline_table": "klines",
            "interval": "min",
            "lookback_minutes": lookback_minutes,
            "max_stale_minutes": MAX_STALE_MINUTES,
            "watchlist_file": watchlist_file,
            "market_session_overrides_file": market_session_overrides_file,
            "canonical_daily_dependency": "none; intraday rows are summarized separately and never promoted to day bars",
        },
        "summary": {
            "market_count": len(markets),
            "symbol_count": sum(summary.get("symbol_count", 0) for summary in markets.values()),
            "ok_symbol_count": sum(summary.get("ok_symbol_count", 0) for summary in markets.values()),
            "closed_symbol_count": sum(summary.get("closed_symbol_count", 0) for summary in markets.values()),
            "stale_symbol_count": sum(summary.get("stale_symbol_count", 0) for summary in markets.values()),
            "missing_symbol_count": sum(summary.get("missing_symbol_count", 0) for summary in markets.values()),
            "quality_degraded_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("degraded_symbol_count") or 0)
                for summary in markets.values()
            ),
            "large_gap_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("large_gap_symbol_count") or 0)
                for summary in markets.values()
            ),
            "invalid_ohlc_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("invalid_ohlc_symbol_count") or 0)
                for summary in markets.values()
            ),
            "bad_timestamp_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("bad_timestamp_symbol_count") or 0)
                for summary in markets.values()
            ),
            "duplicate_timestamp_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("duplicate_timestamp_symbol_count") or 0)
                for summary in markets.values()
            ),
            "missing_source_granularity_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("missing_source_granularity_symbol_count") or 0)
                for summary in markets.values()
            ),
            "low_fidelity_source_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("low_fidelity_source_symbol_count") or 0)
                for summary in markets.values()
            ),
            "snapshot_like_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("snapshot_like_symbol_count") or 0)
                for summary in markets.values()
            ),
            "full_ohlc_symbol_count": sum(
                ((summary.get("quality_summary") or {}).get("full_ohlc_symbol_count") or 0)
                for summary in markets.values()
            ),
        },
        "granularity_policy": intraday_granularity_policy(),
        "markets": markets,
        "recommendations": build_recommendations(markets),
        "warnings": warnings,
        "hermes_use": [
            "Use this report as read-only intraday confirmation, contradiction, and timing context.",
            "Do not treat intraday context as a standalone execution signal.",
            "Do not use minute bars to repair or replace daily K-lines.",
            "Daily data_health, strategy_evidence, and execution_readiness gates remain authoritative.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Intraday context report {payload['generated_at']} status={payload['status']}",
        (
        f"symbols={summary.get('symbol_count')} ok={summary.get('ok_symbol_count')} "
            f"closed={summary.get('closed_symbol_count', 0)} "
            f"stale={summary.get('stale_symbol_count')} missing={summary.get('missing_symbol_count')} "
            f"quality_degraded={summary.get('quality_degraded_symbol_count', 0)} "
            f"low_fidelity={summary.get('low_fidelity_source_symbol_count', 0)} "
            f"snapshot_like={summary.get('snapshot_like_symbol_count', 0)} "
            f"large_gap={summary.get('large_gap_symbol_count', 0)} "
            f"invalid_ohlc={summary.get('invalid_ohlc_symbol_count', 0)}"
        ),
    ]
    for market, item in sorted((payload.get("markets") or {}).items()):
        breadth = item.get("breadth") or {}
        quality = item.get("quality_summary") or {}
        session = item.get("market_session") or {}
        lines.append(
            f"{market}: status={item.get('status')} latest={item.get('latest_timestamp')} "
            f"session={session.get('phase', 'UNKNOWN')} "
            f"up={breadth.get('session_up_pct')}% down={breadth.get('session_down_pct')}% "
            f"quality_degraded={quality.get('degraded_symbol_count', 0)}"
        )
    lines.append("Recommendations: " + ", ".join(payload.get("recommendations") or []))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist-file", default=WATCHLIST_FILE)
    parser.add_argument("--lookback-minutes", type=int, default=LOOKBACK_MINUTES)
    parser.add_argument("--market-session-overrides-file", default=MARKET_SESSION_OVERRIDES_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        watchlist_file=args.watchlist_file,
        lookback_minutes=args.lookback_minutes,
        market_session_overrides_file=args.market_session_overrides_file,
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
