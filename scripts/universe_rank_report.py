#!/usr/bin/env python3
"""Read-only ranked universe report for realtime v5 watchlist review."""
import argparse
import json
import os
import statistics
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
REPORT_FILE = os.environ.get("UNIVERSE_RANK_REPORT_FILE", "/tmp/universe_rank_report.json")
WATCHLIST_CANDIDATE_FILE = os.environ.get(
    "RT_SIGNAL_WATCHLIST_CANDIDATE_FILE",
    "/tmp/rt_signal_watchlist_candidate.json",
)
SIGNAL_MODEL_VERSION = os.environ.get("QM_SIGNAL_MODEL_VERSION", "signal_v4")
SIGNAL_FEATURE_VERSION = os.environ.get("QM_SIGNAL_FEATURE_VERSION", "v4_full")
LOOKBACK_DAYS = int(os.environ.get("UNIVERSE_RANK_LOOKBACK_DAYS", "180"))
DEFAULT_TOP_HK = int(os.environ.get("UNIVERSE_RANK_TOP_HK", "80"))
DEFAULT_TOP_US = int(os.environ.get("UNIVERSE_RANK_TOP_US", "50"))
SIM_EQUITY_HKD = float(os.environ.get("UNIVERSE_RANK_SIM_EQUITY_HKD", str(intake.DEFAULT_EQUITY_HKD)))
SIM_POSITION_SIZE_PCT = float(os.environ.get("UNIVERSE_RANK_SIM_POSITION_SIZE_PCT", str(intake.POSITION_SIZE_PCT)))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def run_cmd(args, timeout=90):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def psql(sql, timeout=120):
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


def avg(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def median(values):
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def rate(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def market_code(exchange):
    return "HK" if exchange == "HKEX" else "US"


def pct_return(closes, lookback):
    if len(closes) <= lookback:
        return None
    base = closes[-1 - lookback]
    last = closes[-1]
    if base is None or last is None or base <= 0:
        return None
    return (last / base - 1) * 100


def daily_returns(closes):
    result = []
    for prev, cur in zip(closes, closes[1:]):
        if prev and cur and prev > 0:
            result.append((cur / prev - 1) * 100)
    return result


def fetch_kline_rows():
    sql = f"""
        WITH latest AS (
            SELECT max(k.timestamp::date) AS latest_date
            FROM klines k
            JOIN stocks s ON s.symbol = k.symbol
            WHERE k.interval = 'day'
              AND s.is_active = true
              AND s.exchange IN ('HKEX','NASDAQ','NYSE')
        )
        SELECT s.exchange, k.symbol, k.timestamp::date,
               k.close_price, k.high_price, k.low_price, k.volume, COALESCE(k.amount, 0)
        FROM klines k
        JOIN stocks s ON s.symbol = k.symbol
        CROSS JOIN latest
        WHERE k.interval = 'day'
          AND s.is_active = true
          AND s.exchange IN ('HKEX','NASDAQ','NYSE')
          AND k.timestamp::date >= latest.latest_date - INTERVAL '{LOOKBACK_DAYS} days'
        ORDER BY s.exchange, k.symbol, k.timestamp
    """
    result = psql(sql)
    if result.returncode != 0:
        return [], [f"kline_query_failed:{result.stderr.strip()}"]
    parsed = []
    for row in rows(result.stdout):
        if len(row) < 8:
            continue
        parsed.append(
            {
                "market": market_code(row[0]),
                "exchange": row[0],
                "symbol": row[1],
                "date": row[2],
                "close": as_float(row[3]),
                "high": as_float(row[4]),
                "low": as_float(row[5]),
                "volume": as_float(row[6]),
                "amount": as_float(row[7]),
            }
        )
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
    result = psql(sql)
    if result.returncode != 0:
        return [], [f"signal_query_failed:{result.stderr.strip()}"]
    parsed = []
    for row in rows(result.stdout):
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


def symbol_metrics(points, market_latest_date, signal=None):
    ordered = sorted(points, key=lambda item: item["date"])
    closes = [as_float(item.get("close")) for item in ordered]
    amounts = [as_float(item.get("amount")) for item in ordered]
    volumes = [as_float(item.get("volume")) for item in ordered]
    returns = daily_returns(closes)
    latest_date = ordered[-1]["date"] if ordered else None
    symbol = ordered[-1]["symbol"] if ordered else ""
    latest_close = closes[-1] if closes else None
    lot = intake.lot_size(symbol) if symbol else None
    fx = intake.fx_to_hkd(symbol) if symbol else None
    min_lot_notional_hkd = latest_close * lot * fx if latest_close and lot and fx else None
    max_alloc_hkd = SIM_EQUITY_HKD * SIM_POSITION_SIZE_PCT
    sim_tradability = "unknown"
    if min_lot_notional_hkd is not None:
        sim_tradability = (
            "allocation_tradable"
            if min_lot_notional_hkd <= max_alloc_hkd
            else "allocation_below_one_lot"
        )
    return {
        "symbol": symbol,
        "market": ordered[-1]["market"] if ordered else "",
        "exchange": ordered[-1]["exchange"] if ordered else "",
        "latest_date": latest_date,
        "is_fresh": latest_date == market_latest_date,
        "history_days": len(ordered),
        "latest_close": latest_close,
        "lot_size": lot,
        "min_lot_notional_hkd": min_lot_notional_hkd,
        "sim_max_alloc_hkd": max_alloc_hkd,
        "sim_tradability": sim_tradability,
        "avg_amount_20d": avg(amounts[-20:]),
        "median_amount_20d": median(amounts[-20:]),
        "avg_volume_20d": avg(volumes[-20:]),
        "zero_volume_days_20d": len([value for value in volumes[-20:] if not value or value <= 0]),
        "return_5d_pct": pct_return(closes, 5),
        "return_20d_pct": pct_return(closes, 20),
        "return_60d_pct": pct_return(closes, 60),
        "volatility_20d_pct": statistics.pstdev(returns[-20:]) if len(returns) >= 2 else None,
        "signal_side": (signal or {}).get("signal_side"),
        "signal_score": as_float((signal or {}).get("fusion_score")),
        "signal_trade_date": (signal or {}).get("trade_date"),
    }


def percentile_map(metrics, key):
    values = sorted({as_float(item.get(key)) for item in metrics if as_float(item.get(key)) is not None})
    if not values:
        return {}
    if len(values) == 1:
        return {values[0]: 1.0}
    denom = max(len(values) - 1, 1)
    return {value: idx / denom for idx, value in enumerate(values)}


def score_metric(item, liquidity_pct):
    reasons = []
    blockers = []
    score = 0.0

    if item["is_fresh"]:
        score += 15
        reasons.append("fresh_latest_kline")
    else:
        blockers.append("stale_latest_kline")

    history_days = item.get("history_days") or 0
    if history_days >= 90:
        score += 15
        reasons.append("history_ge_90d")
    elif history_days >= 60:
        score += 10
        reasons.append("history_ge_60d")
    else:
        score += max(history_days / 60 * 8, 0)
        blockers.append("history_below_60d")

    if item.get("zero_volume_days_20d", 0) > 2:
        blockers.append("zero_volume_days_recent")

    if item.get("sim_tradability") == "allocation_below_one_lot":
        blockers.append("sim_allocation_below_one_lot")
    elif item.get("sim_tradability") == "allocation_tradable":
        reasons.append("sim_allocation_can_buy_one_lot")

    liquidity_score = liquidity_pct * 25
    score += liquidity_score
    if liquidity_pct >= 0.75:
        reasons.append("top_quartile_liquidity")
    elif liquidity_pct < 0.25:
        blockers.append("bottom_quartile_liquidity")

    vol = item.get("volatility_20d_pct")
    if vol is None:
        blockers.append("missing_volatility")
    elif 0.8 <= vol <= 5.5:
        score += 15
        reasons.append("tradable_volatility_band")
    elif 0.4 <= vol <= 8.0:
        score += 8
        reasons.append("acceptable_volatility_band")
    elif vol < 0.4:
        score += 4
        reasons.append("low_volatility")
    else:
        score += 2
        blockers.append("extreme_volatility")

    ret20 = item.get("return_20d_pct")
    ret60 = item.get("return_60d_pct")
    if ret20 is not None:
        if -12 <= ret20 <= 25:
            score += 5
        if ret20 > 0:
            score += 3
            reasons.append("positive_20d_momentum")
        elif ret20 < -15:
            blockers.append("deep_negative_20d_momentum")
    if ret60 is not None and ret60 > 0:
        score += 2

    side = item.get("signal_side")
    signal_score = item.get("signal_score")
    if side == "BUY" and signal_score is not None and signal_score > 0:
        score += min(signal_score * 20, 20)
        reasons.append("latest_v4_buy_support")
    elif side == "HOLD":
        score += 4
    elif side == "SELL":
        score -= 8
        blockers.append("latest_v4_sell_pressure")

    if blockers:
        score -= min(len(blockers) * 4, 16)

    item = dict(item)
    item.update(
        {
            "universe_score": round(max(score, 0), 4),
            "liquidity_percentile": round(liquidity_pct, 4),
            "include_candidate": not blockers and score >= 45,
            "reasons": reasons[:8],
            "blockers": blockers[:8],
            "lot_size": item.get("lot_size"),
            "min_lot_notional_hkd": round_or_none(item.get("min_lot_notional_hkd"), 2),
            "sim_max_alloc_hkd": round_or_none(item.get("sim_max_alloc_hkd"), 2),
            "sim_tradability": item.get("sim_tradability"),
            "avg_amount_20d": round_or_none(item.get("avg_amount_20d"), 2),
            "median_amount_20d": round_or_none(item.get("median_amount_20d"), 2),
            "avg_volume_20d": round_or_none(item.get("avg_volume_20d"), 2),
            "return_5d_pct": round_or_none(item.get("return_5d_pct"), 4),
            "return_20d_pct": round_or_none(item.get("return_20d_pct"), 4),
            "return_60d_pct": round_or_none(item.get("return_60d_pct"), 4),
            "volatility_20d_pct": round_or_none(item.get("volatility_20d_pct"), 4),
            "signal_score": round_or_none(item.get("signal_score"), 4),
        }
    )
    return item


def compact_ranked_item(item):
    return {
        "symbol": item.get("symbol"),
        "universe_score": item.get("universe_score"),
        "include_candidate": item.get("include_candidate"),
        "sim_tradability": item.get("sim_tradability"),
        "min_lot_notional_hkd": item.get("min_lot_notional_hkd"),
        "sim_max_alloc_hkd": item.get("sim_max_alloc_hkd"),
        "blockers": item.get("blockers") or [],
        "reasons": item.get("reasons") or [],
        "signal_side": item.get("signal_side"),
        "signal_score": item.get("signal_score"),
        "liquidity_percentile": item.get("liquidity_percentile"),
        "return_20d_pct": item.get("return_20d_pct"),
        "volatility_20d_pct": item.get("volatility_20d_pct"),
    }


def summarize_market(market, metrics, top_n):
    latest_dates = Counter(item.get("latest_date") for item in metrics if item.get("latest_date"))
    liquidity_pcts = percentile_map(metrics, "avg_amount_20d")
    scored = []
    for item in metrics:
        amount = as_float(item.get("avg_amount_20d"))
        liquidity_pct = liquidity_pcts.get(amount, 0.0)
        scored.append(score_metric(item, liquidity_pct))

    ranked = sorted(scored, key=lambda item: (-item["universe_score"], item["symbol"]))
    selected = [item for item in ranked if item["include_candidate"]][:top_n]
    blocker_counts = Counter()
    for item in ranked:
        for blocker in item.get("blockers") or []:
            blocker_counts[blocker] += 1
    score_values = [item["universe_score"] for item in ranked]
    return {
        "market": market,
        "latest_date": max(latest_dates) if latest_dates else None,
        "symbol_count": len(metrics),
        "candidate_count": len(selected),
        "candidate_limit": top_n,
        "selected_symbols": [item["symbol"] for item in selected],
        "latest_date_distribution": dict(latest_dates),
        "score_summary": {
            "avg": round_or_none(avg(score_values), 4),
            "median": round_or_none(median(score_values), 4),
            "max": round_or_none(max(score_values), 4) if score_values else None,
        },
        "blocker_counts": dict(blocker_counts),
        "sim_tradability_counts": dict(Counter(item.get("sim_tradability") or "missing" for item in ranked)),
        "signal_side_counts": dict(Counter(item.get("signal_side") or "missing" for item in ranked)),
        "ranked_symbol_count": len(ranked),
        "ranked_symbols": [compact_ranked_item(item) for item in ranked],
        "top_ranked": ranked[: min(100, max(top_n, 20))],
    }


def build_recommendations(markets):
    recs = []
    for market, payload in sorted(markets.items()):
        if payload["symbol_count"] and payload["candidate_count"] / payload["symbol_count"] < 0.25:
            recs.append(f"{market}:candidate_coverage_below_25pct_review_data_liquidity_filters")
        if payload["blocker_counts"].get("stale_latest_kline"):
            recs.append(f"{market}:stale_symbols_excluded_from_candidate_watchlist")
        if payload["blocker_counts"].get("latest_v4_sell_pressure"):
            recs.append(f"{market}:sell_pressure_symbols_excluded_or_deprioritized")
        if payload["blocker_counts"].get("sim_allocation_below_one_lot"):
            recs.append(f"{market}:sim_allocation_below_one_lot_review_watchlist_or_position_size")
    if not recs:
        recs.append("ranked_universe_candidate_ready_for_manual_review")
    return recs


def build_watchlist_candidate(markets, generated_at):
    return {
        "schema": "rt_signal_watchlist_v1",
        "generated_at": generated_at,
        "source": {
            "report_schema": "universe_rank_report_v1",
            "selection": "top include_candidate symbols by market after data/liquidity/risk filters",
            "manual_review_required": True,
            "auto_applied": False,
        },
        "markets": {
            market: {"symbols": payload.get("selected_symbols") or []}
            for market, payload in sorted(markets.items())
        },
    }


def build_report(kline_rows=None, signal_rows=None, top_hk=DEFAULT_TOP_HK, top_us=DEFAULT_TOP_US):
    warnings = []
    if kline_rows is None:
        kline_rows, kline_warnings = fetch_kline_rows()
        warnings.extend(kline_warnings)
    if signal_rows is None:
        signal_rows, signal_warnings = fetch_signal_rows()
        warnings.extend(signal_warnings)

    signals = {
        (row.get("market"), row.get("symbol")): row
        for row in signal_rows or []
        if row.get("market") and row.get("symbol")
    }
    points_by_market_symbol = defaultdict(list)
    for row in kline_rows or []:
        market = row.get("market")
        symbol = row.get("symbol")
        if market and symbol and row.get("date"):
            points_by_market_symbol[(market, symbol)].append(row)

    latest_by_market = {}
    for (market, _symbol), points in points_by_market_symbol.items():
        latest = max(item["date"] for item in points if item.get("date"))
        latest_by_market[market] = max(latest, latest_by_market.get(market, latest))

    metrics_by_market = defaultdict(list)
    for (market, symbol), points in points_by_market_symbol.items():
        metrics_by_market[market].append(
            symbol_metrics(points, latest_by_market.get(market), signals.get((market, symbol)))
        )

    limits = {"HK": top_hk, "US": top_us}
    markets = {
        market: summarize_market(market, metrics, limits.get(market, 50))
        for market, metrics in sorted(metrics_by_market.items())
    }
    generated_at = now_iso()
    watchlist_candidate = build_watchlist_candidate(markets, generated_at)
    return {
        "schema": "universe_rank_report_v1",
        "generated_at": generated_at,
        "source": {
            "read_only": True,
            "kline_source": "active HKEX/NASDAQ/NYSE daily klines",
            "lookback_days": LOOKBACK_DAYS,
            "signal_model_version": SIGNAL_MODEL_VERSION,
            "signal_feature_version": SIGNAL_FEATURE_VERSION,
            "sim_equity_hkd": SIM_EQUITY_HKD,
            "sim_position_size_pct": SIM_POSITION_SIZE_PCT,
            "sim_max_alloc_hkd": SIM_EQUITY_HKD * SIM_POSITION_SIZE_PCT,
            "auto_applies_watchlist": False,
        },
        "markets": markets,
        "watchlist_candidate": watchlist_candidate,
        "recommendations": build_recommendations(markets),
        "warnings": warnings,
    }


def build_text_report(payload):
    lines = [f"Universe rank report {payload['generated_at']}"]
    for market, summary in sorted((payload.get("markets") or {}).items()):
        lines.append(
            f"{market}: symbols={summary['symbol_count']} candidates={summary['candidate_count']}/"
            f"{summary['candidate_limit']} latest={summary['latest_date']} score={summary['score_summary']}"
        )
        top = summary.get("top_ranked") or []
        if top:
            lines.append(
                "  top: "
                + ", ".join(
                    f"{item['symbol']}({item['universe_score']:.1f})"
                    for item in top[:10]
                )
            )
    lines.append("Recommendations: " + ", ".join(payload.get("recommendations") or []))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--watchlist-output", default="")
    parser.add_argument("--top-hk", type=int, default=DEFAULT_TOP_HK)
    parser.add_argument("--top-us", type=int, default=DEFAULT_TOP_US)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(top_hk=args.top_hk, top_us=args.top_us)
    if args.output:
        save_json_atomic(args.output, payload)
    if args.watchlist_output:
        save_json_atomic(args.watchlist_output, payload["watchlist_candidate"])

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
