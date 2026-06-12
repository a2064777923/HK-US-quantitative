#!/usr/bin/env python3
"""Read-only portfolio context and simulation review report for Hermes."""
import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict, deque
from datetime import datetime, date

DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")

SIM_PORTFOLIO_ID = int(os.environ.get("QM_SIM_PORTFOLIO_ID", os.environ.get("QM_PORTFOLIO_ID", "8")))
USER_PORTFOLIO_IDS = [
    int(x.strip())
    for x in os.environ.get("QM_USER_PORTFOLIO_IDS", os.environ.get("QM_USER_PORTFOLIO_ID", "")).split(",")
    if x.strip().isdigit()
]
INITIAL_CAPITAL_HKD = float(os.environ.get("QM_INITIAL_CAPITAL_HKD", "100000"))
USD_TO_HKD = float(os.environ.get("USD_TO_HKD", "7.80"))
SIGNAL_MODEL_VERSION = os.environ.get("QM_SIGNAL_MODEL_VERSION", "signal_v4")
SIGNAL_FEATURE_VERSION = os.environ.get("QM_SIGNAL_FEATURE_VERSION", "v4_full")

_COLUMN_CACHE = {}


def run_cmd(args, timeout=20):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def psql(sql, timeout=30):
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


def sql_in(values):
    return ",".join(f"'{sql_quote(v)}'" for v in values)


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


def first_existing(table, candidates, fallback="NULL"):
    cols = table_columns(table)
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return fallback


def is_hk_symbol(symbol):
    return str(symbol)[:1].isdigit() and len(str(symbol)) == 5


def market_for_position(position):
    exchange = str(position.get("exchange") or "").upper()
    symbol = str(position.get("symbol") or "").upper()
    if exchange == "HKEX" or is_hk_symbol(symbol):
        return "HK"
    return "US"


def quote_currency_for_position(position):
    return "HKD" if market_for_position(position) == "HK" else "USD"


def fx_to_hkd(symbol):
    return 1.0 if is_hk_symbol(symbol) else USD_TO_HKD


def as_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default=0):
    return int(as_float(value, default))


def parse_json(value):
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def parse_date(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 10:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None


def days_between(later, earlier):
    if not later or not earlier:
        return None
    if isinstance(later, str):
        later = parse_date(later)
    if isinstance(earlier, str):
        earlier = parse_date(earlier)
    if isinstance(later, date) and isinstance(earlier, date):
        return (later - earlier).days
    return None


def pct(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def round_or_none(value, digits=2):
    return round(value, digits) if value is not None else None


def priority_rank(priority):
    return {"watch": 0, "normal": 1, "medium": 2, "high": 3}.get(priority, 1)


def raise_priority(current, candidate):
    return candidate if priority_rank(candidate) > priority_rank(current) else current


def get_portfolio_row(portfolio_id):
    cols = table_columns("portfolios")
    if not cols:
        return {"id": portfolio_id, "missing": True}
    name_expr = first_existing("portfolios", ("name", "portfolio_name"), "''")
    cash_expr = first_existing("portfolios", ("available_cash", "cash", "cash_balance"), "0")
    capital_expr = first_existing("portfolios", ("current_capital", "total_value", "total_asset"), "0")
    total_value_expr = first_existing("portfolios", ("total_value", "current_capital", "total_asset"), "0")
    initial_capital_expr = first_existing("portfolios", ("initial_capital",), "0")
    updated_expr = first_existing("portfolios", ("updated_at", "created_at"), "NULL")
    r = psql(
        f"""
        SELECT id, {name_expr}, {cash_expr}, {capital_expr}, {total_value_expr},
               {initial_capital_expr}, {updated_expr}
        FROM portfolios
        WHERE id = {int(portfolio_id)}
        """
    )
    parsed = rows(r.stdout) if r.returncode == 0 else []
    if not parsed:
        return {"id": portfolio_id, "missing": True}
    row = parsed[0]
    return {
        "id": as_int(row[0], portfolio_id),
        "name": row[1] if len(row) > 1 else "",
        "cash_hkd": as_float(row[2] if len(row) > 2 else 0),
        "current_capital": as_float(row[3] if len(row) > 3 else 0),
        "reported_total_value_hkd": as_float(row[4] if len(row) > 4 else 0),
        "initial_capital_hkd": as_float(row[5] if len(row) > 5 else 0),
        "updated_at": row[6] if len(row) > 6 else "",
    }


def get_positions(portfolio_id):
    cols = table_columns("positions")
    if not cols:
        return []
    qty_expr = first_existing("positions", ("quantity", "volume", "qty"), "0")
    cost_expr = first_existing("positions", ("avg_cost", "cost_price", "average_price"), "0")
    price_expr = first_existing("positions", ("current_price", "last_price", "price"), "0")
    status_expr = first_existing("positions", ("status",), "'holding'")
    exchange_expr = first_existing("positions", ("exchange", "market"), "''")
    name_expr = first_existing("positions", ("symbol_name", "name", "stock_name"), "''")
    updated_expr = first_existing("positions", ("updated_at", "created_at"), "NULL")
    r = psql(
        f"""
        SELECT symbol, {name_expr}, {qty_expr}, {cost_expr}, {price_expr},
               {status_expr}, {exchange_expr}, {updated_expr}
        FROM positions
        WHERE portfolio_id = {int(portfolio_id)}
        AND COALESCE(({qty_expr})::numeric, 0) > 0
        AND {status_expr} IN ('active','holding')
        ORDER BY symbol
        """
    )
    parsed = rows(r.stdout) if r.returncode == 0 else []
    positions = []
    for row in parsed:
        symbol = row[0]
        qty = as_float(row[2] if len(row) > 2 else 0)
        cost = as_float(row[3] if len(row) > 3 else 0)
        price = as_float(row[4] if len(row) > 4 else 0)
        positions.append(
            {
                "symbol": symbol,
                "name": row[1] if len(row) > 1 else "",
                "quantity": qty,
                "avg_cost": cost,
                "current_price": price,
                "status": row[5] if len(row) > 5 else "holding",
                "exchange": row[6] if len(row) > 6 else "",
                "updated_at": row[7] if len(row) > 7 else "",
            }
        )
    return positions


def get_latest_klines(symbols):
    if not symbols:
        return {}
    r = psql(
        f"""
        SELECT DISTINCT ON (symbol) symbol, close_price, timestamp::date
        FROM klines
        WHERE interval = 'day'
        AND symbol IN ({sql_in(symbols)})
        ORDER BY symbol, timestamp DESC
        """
    )
    result = {}
    for row in rows(r.stdout) if r.returncode == 0 else []:
        if len(row) >= 3:
            result[row[0]] = {"close": as_float(row[1]), "date": row[2]}
    return result


def get_latest_signals(symbols):
    if not symbols:
        return {}
    r = psql(
        f"""
        SELECT DISTINCT ON (symbol)
               symbol, trade_date, signal_side, fusion_score, expected_price, quality::text
        FROM engine_signal_scores
        WHERE model_version = '{sql_quote(SIGNAL_MODEL_VERSION)}'
        AND feature_version = '{sql_quote(SIGNAL_FEATURE_VERSION)}'
        AND symbol IN ({sql_in(symbols)})
        ORDER BY symbol, trade_date DESC
        """
    )
    result = {}
    for row in rows(r.stdout) if r.returncode == 0 else []:
        if len(row) >= 6:
            result[row[0]] = {
                "trade_date": row[1],
                "side": row[2],
                "score": as_float(row[3]),
                "expected_price": as_float(row[4]),
                "quality": parse_json(row[5]),
            }
    return result


def get_top_buy_opportunities(held_symbols, limit=10):
    exclude = f"AND symbol NOT IN ({sql_in(held_symbols)})" if held_symbols else ""
    r = psql(
        f"""
        SELECT symbol, trade_date, fusion_score, expected_price, quality::text
        FROM engine_signal_scores
        WHERE trade_date = (
            SELECT max(trade_date)
            FROM engine_signal_scores
            WHERE model_version = '{sql_quote(SIGNAL_MODEL_VERSION)}'
            AND feature_version = '{sql_quote(SIGNAL_FEATURE_VERSION)}'
        )
        AND model_version = '{sql_quote(SIGNAL_MODEL_VERSION)}'
        AND feature_version = '{sql_quote(SIGNAL_FEATURE_VERSION)}'
        AND signal_side = 'BUY'
        {exclude}
        ORDER BY fusion_score DESC
        LIMIT {int(limit)}
        """
    )
    items = []
    for row in rows(r.stdout) if r.returncode == 0 else []:
        if len(row) >= 5:
            quality = parse_json(row[4])
            items.append(
                {
                    "symbol": row[0],
                    "trade_date": row[1],
                    "score": as_float(row[2]),
                    "expected_price": as_float(row[3]),
                    "reasons": quality.get("reasons", [])[:4],
                    "risk_flags": quality.get("risk_flags", [])[:4],
                    "order_prices": quality.get("order_prices") or {},
                }
            )
    return items


def enrich_position(position, signal, kline):
    symbol = position["symbol"]
    db_price = as_float(position.get("current_price"))
    kline_close = as_float(kline.get("close"))
    signal_price = as_float(signal.get("expected_price"))
    price_source = "missing"
    if db_price and db_price > 0:
        price = db_price
        price_source = "db_current_price"
    elif kline_close and kline_close > 0:
        price = kline_close
        price_source = "latest_kline_close"
    elif signal_price and signal_price > 0:
        price = signal_price
        price_source = "signal_expected_price"
    else:
        price = 0
    qty = position["quantity"]
    cost = position["avg_cost"]
    fx = fx_to_hkd(symbol)
    value_hkd = qty * price * fx
    cost_hkd = qty * cost * fx
    pnl_hkd = value_hkd - cost_hkd if cost > 0 else 0
    pnl_pct = (price / cost - 1) * 100 if cost > 0 and price > 0 else 0
    quality = signal.get("quality") or {}
    order_prices = quality.get("order_prices") or {}
    risk_flags = list(quality.get("risk_flags") or [])
    side = signal.get("side", "UNKNOWN")
    score = signal.get("score", 0)

    recommendation = "hold"
    priority = "normal"
    reasons = []
    price_flags = []
    stop_loss = as_float(order_prices.get("stop_loss"))
    take_profit = as_float(order_prices.get("take_profit"))

    if not db_price or db_price <= 0:
        price_flags.append("db_current_price_missing_or_zero")
    if not kline_close or kline_close <= 0:
        price_flags.append("latest_kline_missing")
    if price_source != "db_current_price":
        price_flags.append(f"fallback_valuation_used:{price_source}")

    if side == "SELL":
        recommendation = "review_reduce_or_exit"
        priority = raise_priority(priority, "high")
        reasons.append("latest_signal_sell")
    elif side == "BUY":
        recommendation = "hold_or_add_only_after_risk_review"
        reasons.append("latest_signal_buy")
    elif side == "HOLD":
        reasons.append("latest_signal_hold")
    else:
        priority = raise_priority(priority, "watch")
        reasons.append("missing_signal")

    if stop_loss > 0 and price > 0 and price <= stop_loss:
        recommendation = "stop_loss_review"
        priority = raise_priority(priority, "high")
        reasons.append("price_below_signal_stop_loss")
    if take_profit > 0 and price > 0 and price >= take_profit:
        priority = raise_priority(priority, "medium")
        reasons.append("price_reached_signal_take_profit")
    if pnl_pct <= -8:
        priority = raise_priority(priority, "high")
        reasons.append("position_loss_below_minus_8pct")
    if pnl_pct >= 15:
        priority = raise_priority(priority, "medium")
        reasons.append("position_gain_above_15pct")
    if risk_flags:
        priority = raise_priority(priority, "high" if priority == "normal" else priority)
        reasons.extend([f"risk:{flag}" for flag in risk_flags[:3]])

    stop_distance_pct = None
    if stop_loss > 0 and price > 0:
        stop_distance_pct = (price - stop_loss) / price * 100

    return {
        **position,
        "current_price": price,
        "db_current_price": db_price,
        "valuation_price_source": price_source,
        "price_data_flags": price_flags,
        "kline_date": kline.get("date", ""),
        "market": market_for_position(position),
        "quote_currency": quote_currency_for_position(position),
        "market_value_hkd": round(value_hkd, 2),
        "unrealized_pnl_hkd": round(pnl_hkd, 2),
        "unrealized_pnl_pct": round(pnl_pct, 2),
        "stop_distance_pct": round_or_none(stop_distance_pct),
        "signal": {
            "trade_date": signal.get("trade_date", ""),
            "side": side,
            "score": round(score, 4),
            "reasons": quality.get("reasons", [])[:4],
            "risk_flags": risk_flags[:4],
            "order_prices": order_prices,
        },
        "recommendation": recommendation,
        "priority": priority,
        "recommendation_reasons": reasons[:8],
    }


def top_positions_by_weight(positions, denominator, limit=5):
    if denominator <= 0:
        return []
    ranked = sorted(positions, key=lambda p: p.get("market_value_hkd", 0), reverse=True)
    return [
        {
            "symbol": pos.get("symbol"),
            "name": pos.get("name", ""),
            "market": pos.get("market"),
            "market_value_hkd": pos.get("market_value_hkd", 0),
            "weight_pct": pct(pos.get("market_value_hkd", 0), denominator),
            "signal_side": (pos.get("signal") or {}).get("side"),
            "priority": pos.get("priority"),
        }
        for pos in ranked[:limit]
    ]


def sum_by_key(positions, key):
    grouped = defaultdict(float)
    for pos in positions:
        grouped[pos.get(key) or "UNKNOWN"] += pos.get("market_value_hkd", 0)
    return {k: round(v, 2) for k, v in sorted(grouped.items())}


def pct_by_value(values, denominator):
    return {key: pct(value, denominator) for key, value in values.items()}


def effective_position_count(positions):
    total = sum(pos.get("market_value_hkd", 0) for pos in positions)
    if total <= 0:
        return 0
    weights = [pos.get("market_value_hkd", 0) / total for pos in positions if pos.get("market_value_hkd", 0) > 0]
    denom = sum(w * w for w in weights)
    return round(1 / denom, 2) if denom > 0 else 0


def latest_dates_by_market(positions):
    latest = {}
    for pos in positions:
        market = pos.get("market") or "UNKNOWN"
        parsed = parse_date(pos.get("kline_date"))
        if parsed and (market not in latest or parsed > latest[market]):
            latest[market] = parsed
    return {market: value.isoformat() for market, value in sorted(latest.items())}


def stale_kline_symbols(positions, latest_by_market):
    stale = []
    for pos in positions:
        symbol = pos.get("symbol")
        market = pos.get("market") or "UNKNOWN"
        pos_date = parse_date(pos.get("kline_date"))
        market_date = parse_date(latest_by_market.get(market))
        if not pos_date:
            stale.append({"symbol": symbol, "reason": "missing_kline_date"})
            continue
        lag = days_between(market_date, pos_date)
        if lag is not None and lag > 0:
            stale.append({"symbol": symbol, "kline_date": pos.get("kline_date"), "market_latest_date": latest_by_market.get(market), "lag_days": lag})
    return stale


def portfolio_risk_level(flags):
    critical = {
        "portfolio_row_value_disagrees_with_computed_value",
        "all_position_prices_missing_or_zero_in_db",
        "no_valid_position_valuation",
    }
    high = {
        "single_position_weight_above_25pct",
        "top3_weight_above_60pct",
        "exit_pressure_above_30pct",
        "stale_or_missing_kline_data",
        "positions_below_stop_loss",
        "cash_below_5pct",
    }
    if any(flag in critical for flag in flags):
        return "critical"
    if any(flag in high for flag in flags):
        return "high"
    if flags:
        return "medium"
    return "low"


def has_position_exit_pressure(position):
    side = (position.get("signal") or {}).get("side")
    reasons = set(position.get("recommendation_reasons") or [])
    return (
        position.get("recommendation") in ("stop_loss_review", "review_reduce_or_exit")
        or side == "SELL"
        or (position.get("stop_distance_pct") is not None and position.get("stop_distance_pct") <= 0)
        or position.get("unrealized_pnl_pct", 0) <= -8
        or "position_loss_below_minus_8pct" in reasons
        or "price_below_signal_stop_loss" in reasons
    )


def build_portfolio_risk(report, portfolio):
    positions = report.get("positions") or []
    cash_hkd = report.get("cash_hkd", 0)
    positions_value = report.get("positions_value_hkd", 0)
    total_value = report.get("total_value_hkd", 0)
    invested_denominator = positions_value if positions_value > 0 else 0
    total_denominator = total_value if total_value > 0 else positions_value
    by_market = sum_by_key(positions, "market")
    by_currency = sum_by_key(positions, "quote_currency")
    side_counts = defaultdict(int)
    for pos in positions:
        side_counts[(pos.get("signal") or {}).get("side", "UNKNOWN")] += 1

    high_priority = [pos for pos in positions if pos.get("priority") == "high"]
    exit_pressure = [pos for pos in positions if has_position_exit_pressure(pos)]
    db_invalid = [pos for pos in positions if not pos.get("db_current_price") or pos.get("db_current_price") <= 0]
    fallback_used = [pos for pos in positions if pos.get("valuation_price_source") != "db_current_price"]
    missing_kline = [pos for pos in positions if not pos.get("kline_date")]
    latest_by_market = latest_dates_by_market(positions)
    stale_symbols = stale_kline_symbols(positions, latest_by_market)

    top_total = top_positions_by_weight(positions, total_denominator, limit=5)
    top_invested = top_positions_by_weight(positions, invested_denominator, limit=5)
    max_total_weight = top_total[0]["weight_pct"] if top_total else 0
    top3_weight = round(sum(item["weight_pct"] for item in top_total[:3]), 2) if top_total else 0
    cost_basis = sum(pos.get("quantity", 0) * pos.get("avg_cost", 0) * fx_to_hkd(pos.get("symbol", "")) for pos in positions)
    unrealized_pnl = sum(pos.get("unrealized_pnl_hkd", 0) for pos in positions)
    stop_positions = [pos for pos in positions if pos.get("stop_distance_pct") is not None]
    below_stop = [pos for pos in stop_positions if pos.get("stop_distance_pct") <= 0]
    nearest_stop = min(stop_positions, key=lambda pos: pos.get("stop_distance_pct")) if stop_positions else None
    reported_total = portfolio.get("reported_total_value_hkd") or portfolio.get("current_capital") or 0
    discrepancy_pct = None
    if reported_total and total_value:
        discrepancy_pct = abs(total_value - reported_total) / reported_total * 100

    flags = []
    if not positions:
        flags.append("no_open_positions")
    if positions and positions_value <= 0:
        flags.append("no_valid_position_valuation")
    if positions and len(db_invalid) == len(positions):
        flags.append("all_position_prices_missing_or_zero_in_db")
    elif db_invalid:
        flags.append("some_position_prices_missing_or_zero_in_db")
    if fallback_used:
        flags.append("fallback_valuation_used")
    if missing_kline or stale_symbols:
        flags.append("stale_or_missing_kline_data")
    if max_total_weight > 25:
        flags.append("single_position_weight_above_25pct")
    if top3_weight > 60:
        flags.append("top3_weight_above_60pct")
    if report.get("position_count", 0) > 0 and len(exit_pressure) / report.get("position_count", 1) > 0.3:
        flags.append("exit_pressure_above_30pct")
    if below_stop:
        flags.append("positions_below_stop_loss")
    if total_value > 0 and cash_hkd / total_value < 0.05:
        flags.append("cash_below_5pct")
    if discrepancy_pct is not None and discrepancy_pct > 5:
        flags.append("portfolio_row_value_disagrees_with_computed_value")

    risk = {
        "portfolio_id": report.get("portfolio_id"),
        "role": report.get("role"),
        "execution_policy": {
            "advice_only": report.get("role") == "user",
            "submits_orders": False,
            "simulation_execution_path": "rt_order_intake.py --mode execute with health, evidence, market, and Hermes judgment gates"
            if report.get("role") == "simulation"
            else None,
        },
        "valuation": {
            "cash_hkd": round(cash_hkd, 2),
            "positions_value_hkd": round(positions_value, 2),
            "computed_total_value_hkd": round(total_value, 2),
            "portfolio_row_total_value_hkd": round(reported_total, 2),
            "portfolio_row_discrepancy_pct": round_or_none(discrepancy_pct),
            "fallback_valuation_count": len(fallback_used),
        },
        "cash_and_exposure": {
            "cash_pct": pct(cash_hkd, total_value),
            "gross_exposure_hkd": round(positions_value, 2),
            "gross_exposure_pct": pct(positions_value, total_value),
            "by_market_hkd": by_market,
            "by_market_pct": pct_by_value(by_market, total_denominator),
            "by_quote_currency_hkd": by_currency,
            "by_quote_currency_pct": pct_by_value(by_currency, total_denominator),
        },
        "concentration": {
            "position_count": report.get("position_count", 0),
            "effective_position_count": effective_position_count(positions),
            "max_position_weight_pct": max_total_weight,
            "max_position_symbol": top_total[0]["symbol"] if top_total else None,
            "top3_weight_pct": top3_weight,
            "top_positions_by_total": top_total,
            "top_positions_by_invested": top_invested,
        },
        "unrealized_pnl": {
            "unrealized_pnl_hkd": round(unrealized_pnl, 2),
            "unrealized_pnl_pct_of_cost": pct(unrealized_pnl, cost_basis),
            "winning_position_count": len([pos for pos in positions if pos.get("unrealized_pnl_hkd", 0) > 0]),
            "losing_position_count": len([pos for pos in positions if pos.get("unrealized_pnl_hkd", 0) < 0]),
            "largest_unrealized_loss": min(positions, key=lambda pos: pos.get("unrealized_pnl_hkd", 0), default=None),
            "largest_unrealized_gain": max(positions, key=lambda pos: pos.get("unrealized_pnl_hkd", 0), default=None),
        },
        "signal_pressure": {
            "by_latest_v4_side": dict(sorted(side_counts.items())),
            "sell_signal_count": side_counts.get("SELL", 0),
            "buy_signal_count": side_counts.get("BUY", 0),
            "missing_signal_count": side_counts.get("UNKNOWN", 0),
            "high_priority_count": len(high_priority),
            "exit_pressure_count": len(exit_pressure),
            "exit_pressure_pct": pct(len(exit_pressure), report.get("position_count", 0)),
            "exit_pressure_symbols": [pos.get("symbol") for pos in exit_pressure[:20]],
        },
        "stop_loss": {
            "positions_with_stop_count": len(stop_positions),
            "below_or_at_stop_count": len(below_stop),
            "nearest_stop_symbol": nearest_stop.get("symbol") if nearest_stop else None,
            "nearest_stop_distance_pct": nearest_stop.get("stop_distance_pct") if nearest_stop else None,
            "below_or_at_stop_symbols": [pos.get("symbol") for pos in below_stop],
        },
        "price_quality": {
            "latest_kline_by_market": latest_by_market,
            "db_invalid_price_count": len(db_invalid),
            "db_invalid_price_symbols": [pos.get("symbol") for pos in db_invalid[:30]],
            "fallback_valuation_count": len(fallback_used),
            "fallback_valuation_symbols": [pos.get("symbol") for pos in fallback_used[:30]],
            "missing_kline_count": len(missing_kline),
            "missing_kline_symbols": [pos.get("symbol") for pos in missing_kline[:30]],
            "stale_kline_count": len(stale_symbols),
            "stale_kline_symbols": stale_symbols[:30],
        },
        "risk_flags": flags,
        "risk_level": portfolio_risk_level(flags),
    }
    risk["recommendations"] = build_risk_recommendations(risk)
    return risk


def build_risk_recommendations(risk):
    flags = set(risk.get("risk_flags") or [])
    role = risk.get("role")
    recs = []
    if role == "user":
        recs.append("user_portfolio_advice_only_do_not_submit_orders")
    elif role == "simulation":
        recs.append("simulation_orders_must_use_intake_and_hermes_execute_gates")
    if "portfolio_row_value_disagrees_with_computed_value" in flags:
        recs.append("reconcile_portfolio_total_value_before_using_cash_or_return_metrics")
    if "all_position_prices_missing_or_zero_in_db" in flags or "some_position_prices_missing_or_zero_in_db" in flags:
        recs.append("repair_position_price_refresh_before_trusting_backend_portfolio_totals")
    if "fallback_valuation_used" in flags:
        recs.append("treat_report_values_as_fallback_estimates_until_position_prices_update")
    if "stale_or_missing_kline_data" in flags:
        recs.append("block_new_trades_for_symbols_with_missing_or_stale_kline_context")
    if "exit_pressure_above_30pct" in flags:
        recs.append("prioritize_reduce_or_exit_review_before_new_openings")
    if "positions_below_stop_loss" in flags:
        recs.append("review_stop_loss_breaches_immediately")
    if "single_position_weight_above_25pct" in flags or "top3_weight_above_60pct" in flags:
        recs.append("reduce_concentration_or_cap_new_size")
    if not recs:
        recs.append("portfolio_risk_allows_normal_review_discipline")
    return recs


def build_portfolio_risk_payload(reports):
    risks = [report.get("risk_summary") for report in reports if report.get("risk_summary")]
    counts = defaultdict(int)
    for risk in risks:
        counts[risk.get("risk_level", "unknown")] += 1
    return {
        "schema": "portfolio_risk_report_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reports": risks,
        "risk_level_counts": dict(sorted(counts.items())),
        "hard_gates": [
            "User portfolios are advice-only and must not submit orders.",
            "Simulation execution must go through rt_order_intake.py execute mode and its health/evidence/market/Hermes gates.",
            "Do not approve new trades from portfolio context alone.",
            "Treat fallback valuation or stale price flags as reasons to reduce confidence until data is repaired.",
        ],
    }


def position_review_urgency(position):
    side = (position.get("signal") or {}).get("side")
    reasons = set(position.get("recommendation_reasons") or [])
    if (
        position.get("recommendation") == "stop_loss_review"
        or position.get("stop_distance_pct") is not None
        and position.get("stop_distance_pct") <= 0
        or position.get("unrealized_pnl_pct", 0) <= -8
    ):
        return "high"
    if position.get("priority") == "high" or side == "SELL":
        return "medium"
    if position.get("priority") == "medium" or "price_reached_signal_take_profit" in reasons:
        return "medium"
    return "low"


def position_review_action(position):
    side = (position.get("signal") or {}).get("side")
    reasons = set(position.get("recommendation_reasons") or [])
    pnl_pct = position.get("unrealized_pnl_pct", 0)
    if position.get("recommendation") == "stop_loss_review" or (
        position.get("stop_distance_pct") is not None and position.get("stop_distance_pct") <= 0
    ):
        return "exit_review"
    if side == "SELL" and pnl_pct <= 0:
        return "exit_review"
    if side == "SELL":
        return "reduce_or_exit_review"
    if pnl_pct <= -8 or "position_loss_below_minus_8pct" in reasons:
        return "reduce_or_exit_review"
    if "price_reached_signal_take_profit" in reasons or pnl_pct >= 15:
        return "take_profit_or_trailing_stop_review"
    if position.get("priority") == "high" or (position.get("signal") or {}).get("risk_flags"):
        return "risk_review"
    return "hold_watch_review"


def should_create_position_review(position):
    urgency = position_review_urgency(position)
    action = position_review_action(position)
    return urgency in ("high", "medium") or action != "hold_watch_review"


def build_position_review_item(report, position):
    side = (position.get("signal") or {}).get("side")
    action = position_review_action(position)
    urgency = position_review_urgency(position)
    role = report.get("role")
    symbol = position.get("symbol")
    signal_date = (position.get("signal") or {}).get("trade_date") or "no_signal_date"
    review_id = f"{role}:{report.get('portfolio_id')}:{symbol}:{signal_date}:{action}"
    return {
        "review_id": review_id,
        "portfolio_id": report.get("portfolio_id"),
        "role": role,
        "symbol": symbol,
        "name": position.get("name", ""),
        "market": position.get("market"),
        "urgency": urgency,
        "recommended_action": action,
        "execution_policy": {
            "advice_only": role == "user",
            "review_only": True,
            "submits_orders": False,
            "requires_separate_order_path": True,
        },
        "position": {
            "quantity": position.get("quantity"),
            "current_price": position.get("current_price"),
            "market_value_hkd": position.get("market_value_hkd"),
            "unrealized_pnl_hkd": position.get("unrealized_pnl_hkd"),
            "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
            "stop_distance_pct": position.get("stop_distance_pct"),
            "valuation_price_source": position.get("valuation_price_source"),
            "kline_date": position.get("kline_date"),
        },
        "latest_signal": {
            "side": side,
            "score": (position.get("signal") or {}).get("score"),
            "trade_date": signal_date,
            "risk_flags": (position.get("signal") or {}).get("risk_flags", []),
            "order_prices": (position.get("signal") or {}).get("order_prices", {}),
        },
        "recommendation": position.get("recommendation"),
        "review_reasons": position.get("recommendation_reasons", []),
        "llm_review_questions": [
            "Should this position be exited, reduced, held, or watched?",
            "Is the latest SELL/stop-loss evidence strong enough after considering market context and portfolio exposure?",
            "Would reducing this position improve risk before approving new BUY signals?",
        ],
    }


def build_position_review_payload(reports):
    items = []
    for report in reports:
        for position in report.get("positions") or []:
            if should_create_position_review(position):
                items.append(build_position_review_item(report, position))
    urgency_rank = {"high": 0, "medium": 1, "low": 2}
    items = sorted(
        items,
        key=lambda item: (
            urgency_rank.get(item.get("urgency"), 9),
            -as_float((item.get("position") or {}).get("market_value_hkd")),
            item.get("symbol") or "",
        ),
    )
    counts_by_urgency = defaultdict(int)
    counts_by_action = defaultdict(int)
    counts_by_role = defaultdict(int)
    for item in items:
        counts_by_urgency[item.get("urgency", "unknown")] += 1
        counts_by_action[item.get("recommended_action", "unknown")] += 1
        counts_by_role[item.get("role", "unknown")] += 1
    return {
        "schema": "portfolio_position_review_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "review_only": True,
        "submits_orders": False,
        "item_count": len(items),
        "counts_by_urgency": dict(sorted(counts_by_urgency.items())),
        "counts_by_action": dict(sorted(counts_by_action.items())),
        "counts_by_role": dict(sorted(counts_by_role.items())),
        "items": items,
        "hard_rules": [
            "These items are advisory review tasks, not order commands.",
            "User portfolio items are advice-only.",
            "Simulation exit execution still requires a separate gated order path.",
            "Review high-urgency reduce/exit items before approving new BUY exposure.",
        ],
    }


def derive_open_positions_from_trades(trades):
    positions = {}
    for trade in trades:
        symbol = str(trade.get("symbol") or "").upper()
        side = str(trade.get("side") or "").lower()
        qty = as_float(trade.get("quantity"))
        price = as_float(trade.get("price"))
        if not symbol or qty <= 0 or price <= 0:
            continue
        current = positions.setdefault(symbol, {"symbol": symbol, "quantity": 0.0, "avg_cost": 0.0})
        if side == "buy":
            old_cost = current["avg_cost"] * current["quantity"]
            new_qty = current["quantity"] + qty
            current["avg_cost"] = (old_cost + price * qty) / new_qty if new_qty > 0 else 0
            current["quantity"] = new_qty
        elif side == "sell":
            current["quantity"] = max(current["quantity"] - qty, 0.0)
            if current["quantity"] <= 0:
                current["avg_cost"] = 0.0
    return {
        symbol: {
            "symbol": symbol,
            "quantity": round(item["quantity"], 6),
            "avg_cost": round(item["avg_cost"], 6),
        }
        for symbol, item in sorted(positions.items())
        if item["quantity"] > 0
    }


def reconcile_report_positions_with_trade_review(report, trade_review):
    open_from_trades = trade_review.get("open_positions_from_trades") or {}
    position_qty = {
        str(pos.get("symbol") or "").upper(): as_float(pos.get("quantity"))
        for pos in report.get("positions") or []
        if str(pos.get("symbol") or "").upper()
    }
    missing_from_positions = [
        {
            "symbol": symbol,
            "trade_quantity": item.get("quantity", 0),
            "trade_avg_cost": item.get("avg_cost", 0),
        }
        for symbol, item in sorted(open_from_trades.items())
        if position_qty.get(symbol, 0) <= 0
    ]
    closed_but_open_in_positions = [
        {
            "symbol": symbol,
            "position_quantity": qty,
        }
        for symbol, qty in sorted(position_qty.items())
        if symbol not in open_from_trades and qty > 0
    ]
    quantity_mismatches = []
    for symbol, item in sorted(open_from_trades.items()):
        expected = as_float(item.get("quantity"))
        actual = position_qty.get(symbol, 0)
        tolerance = max(1.0, expected * 0.001)
        if actual > 0 and abs(actual - expected) > tolerance:
            quantity_mismatches.append(
                {
                    "symbol": symbol,
                    "position_quantity": actual,
                    "trade_quantity": expected,
                    "difference": round(actual - expected, 6),
                }
            )

    status = "PASS"
    notes = []
    if not trade_review.get("trade_count"):
        status = "UNKNOWN"
        notes.append("no_recent_trades_for_position_reconciliation")
    if missing_from_positions or closed_but_open_in_positions or quantity_mismatches:
        status = "FAIL"
        notes.append("positions_table_differs_from_recent_trade_ledger")

    return {
        "status": status,
        "lookback_days": trade_review.get("lookback_days"),
        "trade_count": trade_review.get("trade_count", 0),
        "positions_table_symbol_count": len(position_qty),
        "trade_ledger_open_symbol_count": len(open_from_trades),
        "missing_from_positions": missing_from_positions,
        "closed_but_open_in_positions": closed_but_open_in_positions,
        "quantity_mismatches": quantity_mismatches,
        "notes": notes,
    }


def apply_trade_reconciliation_to_report(report, reconciliation):
    report["trade_position_reconciliation"] = reconciliation
    if reconciliation.get("status") != "FAIL":
        return report
    risk = report.get("risk_summary")
    if not risk:
        return report
    flags = risk.setdefault("risk_flags", [])
    if "positions_table_conflicts_with_trade_ledger" not in flags:
        flags.append("positions_table_conflicts_with_trade_ledger")
    risk["risk_level"] = "critical"
    recommendations = risk.setdefault("recommendations", [])
    rec = "reconcile_positions_table_with_sim_trades_before_using_simulation_state"
    if rec not in recommendations:
        recommendations.insert(0, rec)
    risk["trade_position_reconciliation_status"] = reconciliation.get("status")
    return report


def build_portfolio_report(portfolio_id, role):
    portfolio = get_portfolio_row(portfolio_id)
    positions = get_positions(portfolio_id)
    symbols = [p["symbol"] for p in positions]
    klines = get_latest_klines(symbols)
    signals = get_latest_signals(symbols)
    enriched = [enrich_position(p, signals.get(p["symbol"], {}), klines.get(p["symbol"], {})) for p in positions]

    positions_value = sum(p["market_value_hkd"] for p in enriched)
    cash_hkd = portfolio.get("cash_hkd", 0)
    total_value = cash_hkd + positions_value
    warnings = []
    if portfolio.get("missing"):
        warnings.append("portfolio_row_missing")
    if not positions:
        warnings.append("no_open_positions")
    high_priority = [p for p in enriched if p["priority"] == "high"]
    held = {p["symbol"] for p in enriched}
    opportunities = get_top_buy_opportunities(held, limit=8) if role == "simulation" else []

    initial_capital = portfolio.get("initial_capital_hkd") or INITIAL_CAPITAL_HKD
    report = {
        "portfolio_id": portfolio_id,
        "role": role,
        "name": portfolio.get("name", ""),
        "cash_hkd": round(cash_hkd, 2),
        "positions_value_hkd": round(positions_value, 2),
        "total_value_hkd": round(total_value, 2),
        "portfolio_row_total_value_hkd": round(portfolio.get("reported_total_value_hkd", 0), 2),
        "portfolio_row_updated_at": portfolio.get("updated_at", ""),
        "return_pct_vs_initial": round((total_value / initial_capital - 1) * 100, 2)
        if initial_capital > 0 and total_value > 0
        else 0,
        "position_count": len(enriched),
        "high_priority_count": len(high_priority),
        "warnings": warnings,
        "positions": enriched,
        "top_opportunities": opportunities,
    }
    report["risk_summary"] = build_portfolio_risk(report, portfolio)
    report["position_review_items"] = [
        build_position_review_item(report, position)
        for position in report["positions"]
        if should_create_position_review(position)
    ]
    return report


def get_recent_trades(portfolio_id, days=30):
    cols = table_columns("sim_trades")
    if not cols:
        return []
    fee_expr = first_existing("sim_trades", ("total_fee", "fee", "commission"), "0")
    value_expr = first_existing("sim_trades", ("trade_value", "amount"), "price * quantity")
    created_expr = first_existing("sim_trades", ("created_at", "executed_at", "trade_time"), "NOW()")
    date_filter = f"AND {created_expr} >= NOW() - INTERVAL '{int(days)} days'" if days and days > 0 else ""
    r = psql(
        f"""
        SELECT symbol, side, price, quantity, {fee_expr}, {value_expr}, {created_expr}
        FROM sim_trades
        WHERE portfolio_id = {int(portfolio_id)}
        {date_filter}
        ORDER BY {created_expr} ASC
        """
    )
    trades = []
    for row in rows(r.stdout) if r.returncode == 0 else []:
        if len(row) >= 7:
            trades.append(
                {
                    "symbol": row[0],
                    "side": row[1].lower(),
                    "price": as_float(row[2]),
                    "quantity": as_float(row[3]),
                    "fee": as_float(row[4]),
                    "trade_value": as_float(row[5]),
                    "created_at": row[6],
                }
            )
    return trades


def fifo_trade_review(trades):
    lots = defaultdict(deque)
    closed = []
    for trade in trades:
        symbol = trade["symbol"]
        side = trade["side"]
        qty = trade["quantity"]
        price = trade["price"]
        fee = trade["fee"]
        if qty <= 0 or price <= 0:
            continue
        if side == "buy":
            lots[symbol].append({"quantity": qty, "price": price, "fee": fee})
            continue
        if side != "sell":
            continue
        remaining = qty
        cost = 0.0
        buy_fee = 0.0
        while remaining > 0 and lots[symbol]:
            lot = lots[symbol][0]
            used = min(remaining, lot["quantity"])
            cost += used * lot["price"]
            if lot["quantity"] > 0:
                buy_fee += lot["fee"] * (used / lot["quantity"])
            lot["quantity"] -= used
            remaining -= used
            if lot["quantity"] <= 0:
                lots[symbol].popleft()
        matched = qty - remaining
        if matched <= 0:
            continue
        proceeds = matched * price
        pnl = proceeds - cost - buy_fee - fee * (matched / qty)
        closed.append(
            {
                "symbol": symbol,
                "quantity": matched,
                "exit_price": price,
                "pnl_hkd_est": round(pnl * fx_to_hkd(symbol), 2),
                "pnl_pct_est": round((proceeds / cost - 1) * 100, 2) if cost > 0 else 0,
                "closed_at": trade["created_at"],
            }
        )
    return closed


def build_trade_review(portfolio_id, days=30):
    trades = get_recent_trades(portfolio_id, days=days)
    ledger_trades = trades if not days or days <= 0 else get_recent_trades(portfolio_id, days=0)
    closed = fifo_trade_review(trades)
    open_positions = derive_open_positions_from_trades(ledger_trades)
    wins = [t for t in closed if t["pnl_hkd_est"] > 0]
    losses = [t for t in closed if t["pnl_hkd_est"] <= 0]
    gross_buys = sum(t["trade_value"] * fx_to_hkd(t["symbol"]) for t in trades if t["side"] == "buy")
    gross_sells = sum(t["trade_value"] * fx_to_hkd(t["symbol"]) for t in trades if t["side"] == "sell")
    return {
        "portfolio_id": portfolio_id,
        "lookback_days": days,
        "trade_count": len(trades),
        "buy_count": len([t for t in trades if t["side"] == "buy"]),
        "sell_count": len([t for t in trades if t["side"] == "sell"]),
        "trade_ledger_count": len(ledger_trades),
        "trade_ledger_scope": "all_available_sim_trades",
        "gross_buys_hkd": round(gross_buys, 2),
        "gross_sells_hkd": round(gross_sells, 2),
        "closed_trade_count": len(closed),
        "closed_win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0,
        "closed_pnl_hkd_est": round(sum(t["pnl_hkd_est"] for t in closed), 2),
        "largest_loss": min(closed, key=lambda t: t["pnl_hkd_est"]) if closed else None,
        "largest_win": max(closed, key=lambda t: t["pnl_hkd_est"]) if closed else None,
        "open_positions_from_trades": open_positions,
        "recent_closed": closed[-10:],
        "review_notes": build_review_notes(trades, closed),
    }


def build_review_notes(trades, closed):
    notes = []
    if not trades:
        notes.append("no_recent_trades")
    if closed:
        pnl = sum(t["pnl_hkd_est"] for t in closed)
        if pnl < 0:
            notes.append("recent_closed_trades_negative")
        losses = [t for t in closed if t["pnl_hkd_est"] <= 0]
        if len(losses) >= 3 and len(losses) / len(closed) > 0.6:
            notes.append("loss_rate_above_60pct")
    else:
        notes.append("no_closed_trades_for_fifo_review")
    return notes


def build_text_report(payload):
    lines = [f"Portfolio context report {payload['generated_at']}"]
    for report in payload["portfolio_reports"]:
        risk = report.get("risk_summary") or {}
        lines.append(
            f"{report['role']} P{report['portfolio_id']}: total={report['total_value_hkd']:,.0f} "
            f"cash={report['cash_hkd']:,.0f} positions={report['position_count']} "
            f"return={report['return_pct_vs_initial']:+.1f}% high_priority={report['high_priority_count']} "
            f"risk={risk.get('risk_level', 'unknown')}"
        )
        if risk.get("risk_flags"):
            lines.append("  Risk flags: " + ", ".join(risk["risk_flags"][:8]))
        if report.get("position_review_items"):
            top_reviews = report["position_review_items"][:3]
            lines.append(
                "  Position reviews: "
                + ", ".join(
                    f"{item['symbol']}:{item['recommended_action']}:{item['urgency']}" for item in top_reviews
                )
            )
        reconciliation = report.get("trade_position_reconciliation")
        if reconciliation and reconciliation.get("status") == "FAIL":
            missing = [x["symbol"] for x in reconciliation.get("missing_from_positions", [])[:5]]
            closed = [x["symbol"] for x in reconciliation.get("closed_but_open_in_positions", [])[:5]]
            lines.append(
                f"  Position ledger mismatch: missing={missing} closed_but_open={closed}"
            )
        for pos in report["positions"]:
            if pos["priority"] == "high":
                lines.append(
                    f"  HIGH {pos['symbol']} pnl={pos['unrealized_pnl_pct']:+.1f}% "
                    f"signal={pos['signal']['side']} score={pos['signal']['score']} "
                    f"action={pos['recommendation']}"
                )
        if report["role"] == "simulation" and report["top_opportunities"]:
            top = report["top_opportunities"][:3]
            lines.append("  Top opportunities: " + ", ".join(f"{x['symbol']}({x['score']:.3f})" for x in top))
    review = payload.get("simulation_trade_review")
    if review:
        lines.append(
            f"Sim review {review['lookback_days']}d: trades={review['trade_count']} "
            f"closed={review['closed_trade_count']} win_rate={review['closed_win_rate_pct']:.1f}% "
            f"pnl_est={review['closed_pnl_hkd_est']:,.0f} notes={','.join(review['review_notes'])}"
        )
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--output", help="write JSON payload to this file atomically")
    parser.add_argument("--send-feishu", action="store_true", help="send text report to Feishu")
    parser.add_argument("--sim-portfolio-id", type=int, default=SIM_PORTFOLIO_ID)
    parser.add_argument("--user-portfolio-id", action="append", type=int, default=[])
    parser.add_argument("--review-days", type=int, default=30)
    return parser.parse_args()


def build_payload(sim_portfolio_id=SIM_PORTFOLIO_ID, user_portfolio_ids=None, review_days=30):
    user_ids = user_portfolio_ids if user_portfolio_ids is not None else USER_PORTFOLIO_IDS
    reports = []
    for portfolio_id in user_ids:
        reports.append(build_portfolio_report(portfolio_id, "user"))
    sim_report = build_portfolio_report(sim_portfolio_id, "simulation")
    trade_review = build_trade_review(sim_portfolio_id, days=review_days)
    apply_trade_reconciliation_to_report(
        sim_report,
        reconcile_report_positions_with_trade_review(sim_report, trade_review),
    )
    reports.append(sim_report)
    return {
        "schema": "portfolio_context_report_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "portfolio_reports": reports,
        "portfolio_risk": build_portfolio_risk_payload(reports),
        "position_review": build_position_review_payload(reports),
        "simulation_trade_review": trade_review,
    }


def main():
    args = parse_args()
    payload = build_payload(
        sim_portfolio_id=args.sim_portfolio_id,
        user_portfolio_ids=args.user_portfolio_id or USER_PORTFOLIO_IDS,
        review_days=args.review_days,
    )
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
