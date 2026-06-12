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
SIGNAL_MODEL_VERSION = os.environ.get("QM_SIGNAL_MODEL_VERSION", "signal_v4")
SIGNAL_FEATURE_VERSION = os.environ.get("QM_SIGNAL_FEATURE_VERSION", "v4_full")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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


def fetch_kline_rows():
    sql = """
        WITH latest AS (
            SELECT max(k.timestamp::date) AS latest_date
            FROM klines k
            JOIN stocks s ON s.symbol = k.symbol
            WHERE k.interval = 'day'
              AND s.is_active = true
              AND s.exchange IN ('HKEX','NASDAQ','NYSE')
        )
        SELECT s.exchange, k.symbol, k.timestamp::date, k.close_price
        FROM klines k
        JOIN stocks s ON s.symbol = k.symbol
        CROSS JOIN latest
        WHERE k.interval = 'day'
          AND s.is_active = true
          AND s.exchange IN ('HKEX','NASDAQ','NYSE')
          AND k.timestamp::date >= latest.latest_date - INTERVAL '120 days'
        ORDER BY s.exchange, k.symbol, k.timestamp
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


def build_report(kline_rows=None, signal_rows=None):
    warnings = []
    if kline_rows is None:
        kline_rows, kline_warnings = fetch_kline_rows()
        warnings.extend(kline_warnings)
    if signal_rows is None:
        signal_rows, signal_warnings = fetch_signal_rows()
        warnings.extend(signal_warnings)

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
        market_summaries[market] = summarize_market(market, metrics, signal_by_market.get(market, []))

    payload = {
        "schema": "market_context_report_v1",
        "generated_at": now_iso(),
        "source": {
            "price_source": "active HKEX/NASDAQ/NYSE stock-pool daily klines",
            "index_source": "none_available; breadth is stock-pool proxy",
            "lookback_calendar_days": 120,
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
            f"v4={summary['v4_signal_summary']['by_side']}"
        )
    lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
