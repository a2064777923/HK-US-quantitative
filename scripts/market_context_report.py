#!/usr/bin/env python3
"""Read-only market regime context for Hermes judgment packets."""
import argparse
import json
import os
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
REPORT_FILE = os.environ.get("MARKET_CONTEXT_REPORT_FILE", "/tmp/market_context_report.json")
MARKET_SENTIMENT_REPORT_FILE = os.environ.get("MARKET_SENTIMENT_REPORT_FILE", "/tmp/market_sentiment_report.json")
MARKET_INDEX_CONTEXT_INPUT_FILE = os.environ.get(
    "MARKET_INDEX_CONTEXT_INPUT_FILE",
    "/tmp/market_index_context_inputs.json",
)
SIGNAL_MODEL_VERSION = os.environ.get("QM_SIGNAL_MODEL_VERSION", "signal_v4")
SIGNAL_FEATURE_VERSION = os.environ.get("QM_SIGNAL_FEATURE_VERSION", "v4_full")


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


def run_cmd(args, timeout=60):
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


def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except Exception as exc:
        return {"status": "missing", "path": path, "error": str(exc)}
    return {"status": "invalid", "path": path}


def sql_quote(value):
    return str(value).replace("'", "''")


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def round_or_none(value, digits=4):
    return round(value, digits) if value is not None else None


def rate(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 4) if values else None


def pct_return(closes, lookback):
    if len(closes) <= lookback:
        return None
    base = closes[-1 - lookback]
    last = closes[-1]
    if base is None or last is None or base <= 0:
        return None
    return (last / base - 1) * 100


def ma(closes, period):
    if len(closes) < period:
        return None
    window = closes[-period:]
    if any(value is None for value in window):
        return None
    return sum(window) / period


def daily_returns(closes):
    result = []
    for prev, cur in zip(closes, closes[1:]):
        if prev and cur and prev > 0:
            result.append((cur / prev - 1) * 100)
    return result


def symbol_metrics(points):
    ordered = sorted(points, key=lambda row: row["date"])
    closes = [as_float(row.get("close")) for row in ordered]
    latest_close = closes[-1] if closes else None
    ma20 = ma(closes, 20)
    ma50 = ma(closes, 50)
    returns = daily_returns(closes)
    vol20 = statistics.pstdev(returns[-20:]) if len(returns) >= 2 else None
    return {
        "symbol": ordered[-1]["symbol"] if ordered else "",
        "latest_date": ordered[-1]["date"] if ordered else "",
        "latest_close": latest_close,
        "history_days": len(ordered),
        "above_ma20": latest_close is not None and ma20 is not None and latest_close > ma20,
        "above_ma50": latest_close is not None and ma50 is not None and latest_close > ma50,
        "return_1d_pct": pct_return(closes, 1),
        "return_5d_pct": pct_return(closes, 5),
        "return_20d_pct": pct_return(closes, 20),
        "volatility_20d_pct": vol20,
    }


def infer_index_market(symbol, name=""):
    symbol_text = str(symbol or "").upper().replace(".", "").replace("-", "")
    name_text = str(name or "").upper()
    if any(token in symbol_text for token in ("HSI", "HSCEI", "HSTECH", "HSHK")):
        return "HK"
    if any(token in name_text for token in ("HANG SENG", "HSI", "HSCEI", "HSTECH")):
        return "HK"
    if symbol_text in ("2800HK", "2828HK", "3033HK"):
        return "HK"
    if any(token in symbol_text for token in ("GSPC", "SPX", "IXIC", "NDX", "DJI", "RUT")):
        return "US"
    if symbol_text in ("SPY", "QQQ", "DIA", "IWM", "VOO", "IVV"):
        return "US"
    if any(token in name_text for token in ("S&P 500", "NASDAQ", "DOW JONES", "RUSSELL 2000")):
        return "US"
    return None


def index_priority(row):
    symbol_text = str(row.get("symbol") or "").upper().replace(".", "").replace("-", "")
    name_text = str(row.get("name") or "").upper()
    ranking = [
        (0, ("HSI", "HANG SENG INDEX", "HANG SENG")),
        (1, ("SPX", "GSPC", "S&P 500", "SP500")),
        (2, ("IXIC", "NASDAQ COMPOSITE")),
        (3, ("NDX", "QQQ", "NASDAQ 100")),
        (4, ("DJI", "DOW JONES")),
        (5, ("2800HK", "SPY", "VOO", "IVV")),
    ]
    combined = f"{symbol_text} {name_text}"
    for rank, tokens in ranking:
        if any(token in combined for token in tokens):
            return rank
    return 50


def parse_date(value):
    try:
        return datetime.fromisoformat(str(value)).date()
    except Exception:
        return None


def days_between(left, right):
    left_date = parse_date(left)
    right_date = parse_date(right)
    if not left_date or not right_date:
        return None
    return (left_date - right_date).days


def index_symbol_metrics(points):
    metrics = symbol_metrics(points)
    ordered = sorted(points, key=lambda row: row["date"])
    latest = ordered[-1] if ordered else {}
    metrics["name"] = latest.get("name") or metrics.get("symbol")
    metrics["source_table"] = latest.get("source_table")
    metrics["source"] = latest.get("source")
    metrics["provider_grade"] = latest.get("provider_grade")
    return metrics


def index_direction(metrics):
    if not metrics or metrics.get("history_days", 0) < 20:
        return "unknown"
    ret5 = as_float(metrics.get("return_5d_pct"))
    ret20 = as_float(metrics.get("return_20d_pct"))
    above_ma20 = metrics.get("above_ma20")
    if above_ma20 and (ret5 or 0) > 0 and (ret20 is None or ret20 >= 0):
        return "risk_on"
    if above_ma20 and ret20 is not None and ret20 > 2:
        return "risk_on"
    if above_ma20 is False and (ret5 is None or ret5 < 0) and (ret20 is None or ret20 < 0):
        return "risk_off"
    if ret20 is not None and ret20 < -2:
        return "risk_off"
    return "neutral"


def native_index_context(market, summary, index_rows):
    market_rows = [row for row in index_rows or [] if row.get("market") == market]
    points_by_symbol = defaultdict(list)
    for row in market_rows:
        if row.get("symbol") and row.get("date") and row.get("close") is not None:
            points_by_symbol[row["symbol"]].append(row)

    index_metrics = [index_symbol_metrics(points) for points in points_by_symbol.values()]
    index_metrics.sort(key=lambda item: (index_priority(item), item.get("symbol") or ""))
    primary = index_metrics[0] if index_metrics else None
    status = "OK"
    notes = []
    if not primary:
        status = "MISSING"
        notes.append("native_index_series_missing")
    elif primary.get("history_days", 0) < 20:
        status = "INSUFFICIENT_HISTORY"
        notes.append("native_index_history_below_20d")
    latest_lag_days = days_between(summary.get("latest_date"), primary.get("latest_date")) if primary else None
    if primary and latest_lag_days is not None and latest_lag_days > 5:
        status = "STALE"
        notes.append("native_index_latest_date_lags_stock_pool")

    direction = index_direction(primary)
    breadth_direction = regime_direction(summary.get("regime"))
    if status != "OK" or direction == "unknown":
        alignment = "incomplete"
    elif breadth_direction in ("risk_on", "risk_off") and direction == breadth_direction:
        alignment = "confirms_breadth"
    elif breadth_direction in ("risk_on", "risk_off") and direction in ("risk_on", "risk_off"):
        alignment = "conflicts_with_breadth"
        notes.append("native_index_conflicts_with_stock_pool_breadth")
    elif direction in ("risk_on", "risk_off"):
        alignment = "index_direction_without_breadth_confirmation"
    else:
        alignment = "neutral_or_mixed"

    return {
        "schema": "market_context_native_index_v1",
        "status": status,
        "source": "index_ohlcv_daily/index_daily/market_index_context_inputs",
        "breadth_regime": summary.get("regime"),
        "index_direction": direction,
        "alignment": alignment,
        "latest_lag_days_vs_stock_pool": latest_lag_days,
        "available_index_count": len(index_metrics),
        "primary_index": primary,
        "available_indexes": index_metrics[:8],
        "notes": notes,
    }


def classify_regime(summary):
    breadth20 = summary["breadth"]["above_ma20_pct"]
    ret5 = summary["returns"]["avg_5d_pct"]
    ret20 = summary["returns"]["avg_20d_pct"]
    vol20 = summary["risk"]["avg_volatility_20d_pct"]
    if breadth20 >= 60 and (ret5 or 0) > 0 and (ret20 or 0) > 0:
        regime = "risk_on"
    elif breadth20 <= 40 or (ret20 is not None and ret20 < -2):
        regime = "risk_off"
    else:
        regime = "mixed"

    if vol20 is not None and vol20 >= 4:
        risk_level = "high"
    elif vol20 is not None and vol20 >= 2.5:
        risk_level = "medium"
    else:
        risk_level = "low"
    return regime, risk_level


def market_code(exchange):
    return "HK" if exchange == "HKEX" else "US"


def summarize_market(market, metrics, signal_rows):
    latest_dates = Counter(item["latest_date"] for item in metrics if item.get("latest_date"))
    latest_date = max(latest_dates) if latest_dates else None
    evaluable_20 = [item for item in metrics if item["history_days"] >= 20]
    evaluable_50 = [item for item in metrics if item["history_days"] >= 50]
    ret1 = [item["return_1d_pct"] for item in metrics]
    ret5 = [item["return_5d_pct"] for item in metrics]
    ret20 = [item["return_20d_pct"] for item in metrics]
    vol20 = [item["volatility_20d_pct"] for item in metrics]
    signal_counts = Counter(row.get("signal_side", "UNKNOWN") for row in signal_rows)
    signal_scores = [as_float(row.get("fusion_score")) for row in signal_rows if row.get("signal_side") == "BUY"]

    summary = {
        "market": market,
        "latest_date": latest_date,
        "symbol_count": len(metrics),
        "latest_date_distribution": dict(latest_dates),
        "coverage": {
            "evaluable_20d": len(evaluable_20),
            "evaluable_50d": len(evaluable_50),
            "evaluable_20d_pct": rate(len(evaluable_20), len(metrics)),
            "evaluable_50d_pct": rate(len(evaluable_50), len(metrics)),
        },
        "breadth": {
            "above_ma20_count": len([item for item in evaluable_20 if item["above_ma20"]]),
            "above_ma20_pct": rate(len([item for item in evaluable_20 if item["above_ma20"]]), len(evaluable_20)),
            "above_ma50_count": len([item for item in evaluable_50 if item["above_ma50"]]),
            "above_ma50_pct": rate(len([item for item in evaluable_50 if item["above_ma50"]]), len(evaluable_50)),
            "up_1d_count": len([value for value in ret1 if value is not None and value > 0]),
            "up_1d_pct": rate(len([value for value in ret1 if value is not None and value > 0]), len([value for value in ret1 if value is not None])),
        },
        "returns": {
            "avg_1d_pct": avg(ret1),
            "median_1d_pct": median(ret1),
            "avg_5d_pct": avg(ret5),
            "median_5d_pct": median(ret5),
            "avg_20d_pct": avg(ret20),
            "median_20d_pct": median(ret20),
        },
        "risk": {
            "avg_volatility_20d_pct": avg(vol20),
            "median_volatility_20d_pct": median(vol20),
        },
        "v4_signal_summary": {
            "trade_date": max((row.get("trade_date") for row in signal_rows if row.get("trade_date")), default=None),
            "count": len(signal_rows),
            "by_side": dict(signal_counts),
            "buy_avg_score": avg(signal_scores),
        },
    }
    regime, risk_level = classify_regime(summary)
    summary["regime"] = regime
    summary["risk_level"] = risk_level
    summary["notes"] = build_market_notes(summary)
    return summary


def build_market_notes(summary):
    notes = []
    if summary["coverage"]["evaluable_20d_pct"] < 80:
        notes.append("low_20d_coverage")
    if summary["regime"] == "risk_off":
        notes.append("tighten_new_buy_approval_or_reduce_size")
    elif summary["regime"] == "risk_on":
        notes.append("normal_buy_review_allowed_if_signal_and_risk_pass")
    else:
        notes.append("mixed_regime_require_stronger_signal_confluence")
    if summary["breadth"]["above_ma20_pct"] < 45 and summary["v4_signal_summary"]["by_side"].get("BUY", 0) > 0:
        notes.append("buy_signals_against_weak_breadth")
    if summary["risk_level"] == "high":
        notes.append("high_volatility_reduce_position_size")
    return notes


def sentiment_market_score(sentiment_payload, market):
    summary = sentiment_payload.get("summary") if isinstance(sentiment_payload.get("summary"), dict) else {}
    market_scores = summary.get("market_scores") if isinstance(summary.get("market_scores"), dict) else {}
    score = as_float(market_scores.get(market))
    if score is None:
        score = as_float(market_scores.get("GLOBAL"))
    return score


def sentiment_direction_from_score(score):
    if score is None:
        return "unknown"
    if score >= 0.25:
        return "risk_on"
    if score <= -0.25:
        return "risk_off"
    return "neutral"


def regime_direction(regime):
    if regime in ("risk_on", "risk_off"):
        return regime
    return "neutral"


def sentiment_indicators_for_market(sentiment_payload, market):
    rows_in = sentiment_payload.get("indicators") if isinstance(sentiment_payload.get("indicators"), list) else []
    result = []
    for item in rows_in:
        if not isinstance(item, dict) or item.get("stale") is True:
            continue
        markets = item.get("markets") if isinstance(item.get("markets"), list) else []
        normalized_markets = [str(value).upper() for value in markets]
        if market not in normalized_markets and "GLOBAL" not in normalized_markets:
            continue
        result.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "indicator_type": item.get("indicator_type"),
                "source": item.get("source"),
                "direction": item.get("direction"),
                "score": item.get("score"),
                "value": item.get("value"),
                "change": item.get("change"),
                "unit": item.get("unit"),
                "summary": item.get("summary"),
            }
        )
    return result[:12]


def cross_market_context(market, summary, sentiment_payload):
    if not isinstance(sentiment_payload, dict) or sentiment_payload.get("schema") != "market_sentiment_report_v1":
        return {
            "schema": "market_context_cross_market_v1",
            "status": "incomplete",
            "source": "market_sentiment_report_missing_or_invalid",
            "sentiment_status": sentiment_payload.get("status") if isinstance(sentiment_payload, dict) else None,
            "breadth_regime": summary.get("regime"),
            "sentiment_direction": "unknown",
            "sentiment_score": None,
            "alignment": "incomplete",
            "indicators": [],
            "notes": ["market_sentiment_missing_for_cross_market_confirmation"],
        }
    sentiment_status = sentiment_payload.get("status")
    score = sentiment_market_score(sentiment_payload, market)
    sentiment_direction = sentiment_direction_from_score(score)
    breadth_direction = regime_direction(summary.get("regime"))
    if sentiment_status in ("MISSING", "STALE", "FAIL") or sentiment_direction == "unknown":
        alignment = "incomplete"
    elif breadth_direction in ("risk_on", "risk_off") and sentiment_direction == breadth_direction:
        alignment = "confirms_breadth"
    elif breadth_direction in ("risk_on", "risk_off") and sentiment_direction in ("risk_on", "risk_off"):
        alignment = "conflicts_with_breadth"
    elif sentiment_direction in ("risk_on", "risk_off"):
        alignment = "sentiment_direction_without_breadth_confirmation"
    else:
        alignment = "neutral_or_mixed"
    notes = []
    if alignment == "conflicts_with_breadth":
        notes.append("real_index_or_volatility_sentiment_conflicts_with_breadth_proxy")
    elif alignment == "confirms_breadth":
        notes.append("real_index_or_volatility_sentiment_confirms_breadth_proxy")
    elif alignment == "incomplete":
        notes.append("cross_market_sentiment_unavailable_or_stale")
    return {
        "schema": "market_context_cross_market_v1",
        "status": "OK" if alignment != "incomplete" else "incomplete",
        "source": "market_sentiment_report",
        "sentiment_status": sentiment_status,
        "breadth_regime": summary.get("regime"),
        "sentiment_direction": sentiment_direction,
        "sentiment_score": score,
        "alignment": alignment,
        "indicators": sentiment_indicators_for_market(sentiment_payload, market),
        "notes": notes,
    }


def fetch_kline_rows():
    sql = """
        WITH latest AS (
            SELECT max(d.trade_date) AS latest_date
            FROM (
                SELECT DISTINCT ON (k.symbol, k.timestamp::date)
                       k.symbol, k.timestamp::date AS trade_date
                FROM klines k
                WHERE k.interval = 'day'
                ORDER BY k.symbol, k.timestamp::date, k.timestamp DESC
            ) d
            JOIN stocks s ON s.symbol = d.symbol
            WHERE s.is_active = true
              AND s.exchange IN ('HKEX','NASDAQ','NYSE')
        ),
        daily_bar AS (
            SELECT DISTINCT ON (k.symbol, k.timestamp::date)
                   k.symbol, k.timestamp::date AS trade_date, k.close_price
            FROM klines k
            WHERE k.interval = 'day'
            ORDER BY k.symbol, k.timestamp::date, k.timestamp DESC
        )
        SELECT s.exchange, d.symbol, d.trade_date, d.close_price
        FROM daily_bar d
        JOIN stocks s ON s.symbol = d.symbol
        CROSS JOIN latest
        WHERE s.is_active = true
          AND s.exchange IN ('HKEX','NASDAQ','NYSE')
          AND d.trade_date >= latest.latest_date - INTERVAL '120 days'
        ORDER BY s.exchange, d.symbol, d.trade_date
    """
    r = psql(sql)
    if r.returncode != 0:
        return [], [f"kline_query_failed:{r.stderr.strip()}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 4:
            continue
        parsed.append({"market": market_code(row[0]), "exchange": row[0], "symbol": row[1], "date": row[2], "close": as_float(row[3])})
    return parsed, []


def fetch_signal_rows():
    sql = f"""
        SELECT CASE WHEN s.exchange = 'HKEX' THEN 'HK' ELSE 'US' END AS market,
               e.symbol, e.trade_date, e.signal_side, e.fusion_score
        FROM engine_signal_scores e
        JOIN stocks s ON s.symbol = e.symbol
        WHERE e.model_version = '{sql_quote(SIGNAL_MODEL_VERSION)}'
          AND e.feature_version = '{sql_quote(SIGNAL_FEATURE_VERSION)}'
          AND e.trade_date = (
              SELECT max(trade_date)
              FROM engine_signal_scores
              WHERE model_version = '{sql_quote(SIGNAL_MODEL_VERSION)}'
                AND feature_version = '{sql_quote(SIGNAL_FEATURE_VERSION)}'
          )
          AND s.exchange IN ('HKEX','NASDAQ','NYSE')
        ORDER BY market, e.symbol
    """
    r = psql(sql)
    if r.returncode != 0:
        return [], [f"signal_query_failed:{r.stderr.strip()}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 5:
            continue
        parsed.append(
            {
                "market": row[0],
                "symbol": row[1],
                "trade_date": row[2],
                "signal_side": row[3],
                "fusion_score": as_float(row[4]),
            }
        )
    return parsed, []


def fetch_index_rows():
    sql = """
        WITH max_date AS (
            SELECT max(trade_date) AS latest_date
            FROM (
                SELECT max(trade_date) AS trade_date FROM index_ohlcv_daily
                UNION ALL
                SELECT max(trade_date) AS trade_date FROM index_daily
            ) dates
        )
        SELECT 'index_ohlcv_daily' AS source_table,
               symbol,
               COALESCE(index_name, symbol) AS name,
               trade_date::date,
               close,
               COALESCE(source, 'index_ohlcv_daily') AS source
        FROM index_ohlcv_daily, max_date
        WHERE max_date.latest_date IS NOT NULL
          AND trade_date >= max_date.latest_date - INTERVAL '220 days'
        UNION ALL
        SELECT 'index_daily' AS source_table,
               symbol,
               symbol AS name,
               trade_date::date,
               close,
               'index_daily' AS source
        FROM index_daily, max_date
        WHERE max_date.latest_date IS NOT NULL
          AND trade_date >= max_date.latest_date - INTERVAL '220 days'
        ORDER BY source_table, symbol, trade_date
    """
    r = psql(sql)
    if r.returncode != 0:
        return [], [f"native_index_query_failed:{r.stderr.strip()}"]
    parsed = []
    unmapped = 0
    for row in rows(r.stdout):
        if len(row) < 6:
            continue
        source_table, symbol, name, trade_date, close, source = row[:6]
        market = infer_index_market(symbol, name)
        if market is None:
            unmapped += 1
            continue
        parsed.append(
            {
                "market": market,
                "source_table": source_table,
                "symbol": symbol,
                "name": name,
                "date": trade_date,
                "close": as_float(close),
                "source": source,
            }
        )
    warnings = []
    if unmapped:
        warnings.append(f"native_index_unmapped_row_count:{unmapped}")
    return parsed, warnings


def index_rows_from_snapshot(payload):
    if not isinstance(payload, dict):
        return [], ["market_index_context_input_invalid"]
    if payload.get("schema") != "market_index_context_producer_v1":
        return [], ["market_index_context_input_schema_invalid"]
    parsed = []
    warnings = []
    producer_warnings = payload.get("warnings")
    if isinstance(producer_warnings, list):
        for warning in producer_warnings:
            warning_text = str(warning).strip()
            if warning_text:
                warnings.append(f"market_index_context_producer_warning:{warning_text}")
    for item in payload.get("indexes") or []:
        if not isinstance(item, dict):
            continue
        market = str(item.get("market") or "").upper()
        symbol = item.get("symbol") or item.get("provider_symbol")
        name = item.get("name") or symbol
        series = item.get("series") if isinstance(item.get("series"), list) else []
        if market not in ("HK", "US"):
            market = infer_index_market(symbol, name) or market
        if market not in ("HK", "US") or not symbol:
            warnings.append(f"market_index_context_unmapped_index:{symbol or name}")
            continue
        for point in series:
            if not isinstance(point, dict):
                continue
            parsed.append(
                {
                    "market": market,
                    "source_table": "market_index_context_inputs",
                    "symbol": symbol,
                    "name": name,
                    "date": point.get("date"),
                    "close": as_float(point.get("close")),
                    "source": item.get("source") or "market_index_context_input",
                    "provider_grade": (payload.get("source") or {}).get("provider_grade")
                    or item.get("provider_grade")
                    or "public_fallback",
                }
            )
    return parsed, warnings


def fetch_index_snapshot_rows(path=MARKET_INDEX_CONTEXT_INPUT_FILE):
    loaded = load_json_file(path)
    if loaded.get("status") in ("missing", "invalid"):
        return [], [f"market_index_context_input_missing_or_invalid:{path}"]
    return index_rows_from_snapshot(loaded)


def build_report(kline_rows=None, signal_rows=None, sentiment_payload=None, index_rows=None, index_snapshot_payload=None):
    warnings = []
    caller_supplied_kline_rows = kline_rows is not None
    if kline_rows is None:
        kline_rows, kline_warnings = fetch_kline_rows()
        warnings.extend(kline_warnings)
    if signal_rows is None:
        signal_rows, signal_warnings = fetch_signal_rows()
        warnings.extend(signal_warnings)
    if sentiment_payload is None:
        sentiment_payload = load_json_file(MARKET_SENTIMENT_REPORT_FILE)
    if index_rows is None:
        if caller_supplied_kline_rows:
            index_rows = []
        else:
            index_rows, index_warnings = fetch_index_rows()
            warnings.extend(index_warnings)
            snapshot_rows, snapshot_warnings = fetch_index_snapshot_rows()
            index_rows.extend(snapshot_rows)
            warnings.extend(snapshot_warnings)
    if index_snapshot_payload is not None:
        snapshot_rows, snapshot_warnings = index_rows_from_snapshot(index_snapshot_payload)
        index_rows = list(index_rows or []) + snapshot_rows
        warnings.extend(snapshot_warnings)

    points_by_market_symbol = defaultdict(list)
    for row in kline_rows:
        market = row.get("market")
        symbol = row.get("symbol")
        if market and symbol and row.get("date"):
            points_by_market_symbol[(market, symbol)].append(row)

    signal_by_market = defaultdict(list)
    for row in signal_rows or []:
        signal_by_market[row.get("market")].append(row)

    market_summaries = {}
    for market in sorted({market for market, _ in points_by_market_symbol} | set(signal_by_market)):
        metrics = [
            symbol_metrics(points)
            for (item_market, _symbol), points in points_by_market_symbol.items()
            if item_market == market
        ]
        summary = summarize_market(market, metrics, signal_by_market.get(market, []))
        summary["native_index_context"] = native_index_context(market, summary, index_rows)
        summary["cross_market"] = cross_market_context(market, summary, sentiment_payload)
        market_summaries[market] = summary

    for market, summary in sorted(market_summaries.items()):
        native_status = (summary.get("native_index_context") or {}).get("status")
        if native_status != "OK":
            warnings.append(f"native_index_context_{str(native_status or 'missing').lower()}:{market}")

    payload = {
        "schema": "market_context_report_v1",
        "generated_at": now_iso(),
        "source": {
            "price_source": "active HKEX/NASDAQ/NYSE stock-pool daily klines",
            "index_source": "native index_ohlcv_daily/index_daily when populated; market_sentiment_report as cross-market confirmation",
            "market_index_context_input_file": MARKET_INDEX_CONTEXT_INPUT_FILE,
            "market_sentiment_file": MARKET_SENTIMENT_REPORT_FILE,
            "lookback_calendar_days": 120,
            "native_index_lookback_calendar_days": 220,
            "signal_model_version": SIGNAL_MODEL_VERSION,
            "signal_feature_version": SIGNAL_FEATURE_VERSION,
        },
        "markets": market_summaries,
        "recommendations": build_recommendations(market_summaries),
        "warnings": warnings,
    }
    return payload


def build_recommendations(markets):
    recs = []
    for market, summary in sorted(markets.items()):
        if summary["regime"] == "risk_off":
            recs.append(f"{market}:risk_off_require_reduced_or_rejected_new_buys")
        elif summary["regime"] == "mixed":
            recs.append(f"{market}:mixed_regime_require_signal_confluence")
        if summary["risk_level"] == "high":
            recs.append(f"{market}:high_volatility_reduce_size")
        if "buy_signals_against_weak_breadth" in summary["notes"]:
            recs.append(f"{market}:buy_signals_against_weak_breadth")
    if not recs:
        recs.append("market_context_supports_normal_review_discipline")
    return recs


def build_text_report(payload):
    lines = [f"Market context report {payload['generated_at']}"]
    for market, summary in sorted(payload["markets"].items()):
        lines.append(
            f"{market}: regime={summary['regime']} risk={summary['risk_level']} "
            f"aboveMA20={summary['breadth']['above_ma20_pct']:.1f}% "
            f"avg5d={summary['returns']['avg_5d_pct']} avg20d={summary['returns']['avg_20d_pct']} "
            f"cross={summary.get('cross_market', {}).get('alignment')} "
            f"sentiment={summary.get('cross_market', {}).get('sentiment_direction')} "
            f"v4={summary['v4_signal_summary']['by_side']}"
        )
    lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--market-sentiment-file", default=MARKET_SENTIMENT_REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(sentiment_payload=load_json_file(args.market_sentiment_file))
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
