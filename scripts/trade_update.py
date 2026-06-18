#!/usr/bin/env python3
"""Manual user-holding updates.

This tool is intentionally scoped to the user holdings portfolio by default.
It mutates positions rows only; it does not submit broker orders, write Hermes
judgments, or touch the v5 simulation/paper execution path.
"""

import argparse
import os
import sys
from datetime import datetime

try:
    import psycopg2
except ImportError:  # pragma: no cover - runtime environment check
    psycopg2 = None


HK_EXCHANGES = {"HK", "HKEX", "SEHK", "SZSE", "SSE"}
US_EXCHANGES = {"US", "NASDAQ", "NYSE", "AMEX"}
SUPPORTED_EXCHANGES = sorted(HK_EXCHANGES | US_EXCHANGES)
USD_TO_HKD = float(os.environ.get("USD_TO_HKD", "7.80"))


def default_portfolio_id():
    for name in ("QM_HOLDINGS_PORTFOLIO_ID", "QM_USER_PORTFOLIO_ID", "QM_USER_PORTFOLIO_IDS"):
        raw = os.environ.get(name, "")
        for item in str(raw).split(","):
            item = item.strip()
            if item.isdigit():
                return int(item)
    return 3


def user_portfolio_ids():
    ids = set()
    for name in ("QM_HOLDINGS_PORTFOLIO_ID", "QM_USER_PORTFOLIO_ID", "QM_USER_PORTFOLIO_IDS"):
        raw = os.environ.get(name, "")
        for item in str(raw).split(","):
            item = item.strip()
            if item.isdigit():
                ids.add(int(item))
    return ids or {3}


def portfolio_total_statuses(portfolio_id):
    return ("holding",) if int(portfolio_id) in user_portfolio_ids() else ("active", "holding")


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def positive_float(value):
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def get_connection():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")
    url = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(url, connect_timeout=5)


def normalize_symbol(symbol, exchange=None):
    symbol = str(symbol or "").strip().upper()
    exchange = normalize_exchange(exchange) if exchange else ""
    if symbol.isdigit() and (exchange in HK_EXCHANGES or len(symbol) < 5):
        return symbol.zfill(5)
    return symbol


def normalize_exchange(exchange):
    exchange = str(exchange or "").strip().upper()
    return "HKEX" if exchange in {"HK", "SEHK"} else exchange


def is_hk_symbol(symbol):
    symbol = str(symbol or "").strip().upper()
    return symbol.isdigit() and len(symbol) == 5


def infer_currency(symbol, exchange=None, currency=None):
    if currency:
        return str(currency).strip().upper()
    exchange = normalize_exchange(exchange)
    return "HKD" if exchange in HK_EXCHANGES or is_hk_symbol(symbol) else "USD"


def fx_to_hkd(currency):
    return 1.0 if str(currency or "").upper() == "HKD" else USD_TO_HKD


def trade_notional_hkd(qty, price, currency):
    return round(int(qty) * float(price) * fx_to_hkd(currency), 2)


def position_values(qty, avg_cost, current_price, currency):
    qty = int(qty)
    avg_cost = float(avg_cost)
    current_price = float(current_price)
    fx = fx_to_hkd(currency)
    total_cost = qty * avg_cost
    market_value = qty * current_price * fx
    unrealized_pnl = qty * (current_price - avg_cost) * fx
    unrealized_pnl_rate = (current_price / avg_cost - 1) if avg_cost > 0 and current_price > 0 else 0.0
    return {
        "total_cost": round(total_cost, 2),
        "market_value": round(market_value, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "unrealized_pnl_rate": round(unrealized_pnl_rate, 6),
    }


def adjust_available_cash(cur, portfolio_id, delta_hkd):
    cur.execute(
        """
        UPDATE portfolios
        SET available_cash = COALESCE(available_cash, 0) + %s,
            updated_at = %s
        WHERE id = %s
        """,
        (round(float(delta_hkd), 2), datetime.utcnow(), portfolio_id),
    )


def refresh_portfolio_totals(cur, portfolio_id):
    statuses = portfolio_total_statuses(portfolio_id)
    placeholders = ", ".join(["%s"] * len(statuses))
    cur.execute(
        f"""
        UPDATE portfolios p
        SET current_capital = COALESCE(p.available_cash, 0) + COALESCE(x.positions_value, 0),
            total_value = COALESCE(p.available_cash, 0) + COALESCE(x.positions_value, 0),
            updated_at = %s
        FROM (
            SELECT COALESCE(SUM(market_value), 0) AS positions_value
            FROM positions
            WHERE portfolio_id = %s
              AND status IN ({placeholders})
              AND COALESCE(quantity, 0) > 0
        ) x
        WHERE p.id = %s
        """,
        (datetime.utcnow(), portfolio_id, *statuses, portfolio_id),
    )


def fetch_open_position(cur, portfolio_id, symbol):
    cur.execute(
        """
        SELECT id, quantity, avg_cost, total_cost, current_price, currency
        FROM positions
        WHERE portfolio_id = %s AND symbol = %s AND status = 'holding'
        ORDER BY id DESC
        LIMIT 1
        """,
        (portfolio_id, symbol),
    )
    return cur.fetchone()


def cmd_buy(args):
    portfolio_id = int(args.portfolio_id)
    exchange = normalize_exchange(args.exchange)
    symbol = normalize_symbol(args.symbol, exchange)
    currency = infer_currency(symbol, exchange, args.currency)
    values = position_values(args.qty, args.cost, args.cost, currency)
    conn = get_connection()
    try:
        cur = conn.cursor()
        if fetch_open_position(cur, portfolio_id, symbol):
            print(f"[ERROR] {symbol} already exists. Use 'add' to add more.", file=sys.stderr)
            return 1
        now = datetime.utcnow()
        cur.execute(
            """
            INSERT INTO positions (
                portfolio_id, symbol, symbol_name, exchange, side, quantity,
                available_quantity, avg_cost, total_cost, current_price, market_value,
                unrealized_pnl, unrealized_pnl_rate, realized_pnl, weight, status,
                opened_at, updated_at, currency
            )
            VALUES (
                %s, %s, %s, %s, 'long', %s,
                %s, %s, %s, %s, %s,
                %s, %s, 0, 0, 'holding',
                %s, %s, %s
            )
            RETURNING id
            """,
            (
                portfolio_id,
                symbol,
                args.name or symbol,
                exchange,
                args.qty,
                args.qty,
                args.cost,
                values["total_cost"],
                args.cost,
                values["market_value"],
                values["unrealized_pnl"],
                values["unrealized_pnl_rate"],
                now,
                now,
                currency,
            ),
        )
        position_id = cur.fetchone()[0]
        if getattr(args, "cash_adjust", True):
            adjust_available_cash(cur, portfolio_id, -trade_notional_hkd(args.qty, args.cost, currency))
        refresh_portfolio_totals(cur, portfolio_id)
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] Bought {args.qty} x {symbol} @{args.cost} {currency} (position #{position_id})")
    return 0


def cmd_add(args):
    portfolio_id = int(args.portfolio_id)
    symbol = normalize_symbol(args.symbol)
    conn = get_connection()
    try:
        cur = conn.cursor()
        row = fetch_open_position(cur, portfolio_id, symbol)
        if not row:
            print(f"[ERROR] {symbol} not found. Use 'buy' first.", file=sys.stderr)
            return 1
        position_id, old_qty, old_avg, _old_total, current_price, currency = row
        old_qty = int(old_qty)
        new_qty = old_qty + args.qty
        new_total_cost = old_qty * float(old_avg) + args.qty * args.cost
        new_avg = new_total_cost / new_qty
        current_price = float(current_price or args.cost)
        currency = infer_currency(symbol, None, currency)
        values = position_values(new_qty, new_avg, current_price, currency)
        cur.execute(
            """
            UPDATE positions
            SET quantity = %s,
                available_quantity = %s,
                avg_cost = %s,
                total_cost = %s,
                market_value = %s,
                unrealized_pnl = %s,
                unrealized_pnl_rate = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (
                new_qty,
                new_qty,
                round(new_avg, 4),
                values["total_cost"],
                values["market_value"],
                values["unrealized_pnl"],
                values["unrealized_pnl_rate"],
                datetime.utcnow(),
                position_id,
            ),
        )
        if getattr(args, "cash_adjust", True):
            adjust_available_cash(cur, portfolio_id, -trade_notional_hkd(args.qty, args.cost, currency))
        refresh_portfolio_totals(cur, portfolio_id)
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] Added {args.qty} x {symbol} @{args.cost}. Total: {new_qty} @ avg {new_avg:.4f}")
    return 0


def cmd_sell(args):
    portfolio_id = int(args.portfolio_id)
    symbol = normalize_symbol(args.symbol)
    conn = get_connection()
    try:
        cur = conn.cursor()
        row = fetch_open_position(cur, portfolio_id, symbol)
        if not row:
            print(f"[ERROR] {symbol} not found or already closed.", file=sys.stderr)
            return 1
        position_id, qty, avg_cost, _total_cost, current_price, currency = row
        qty = int(qty)
        sell_qty = args.qty or qty
        if sell_qty > qty:
            print(f"[ERROR] sell qty {sell_qty} exceeds holding qty {qty}.", file=sys.stderr)
            return 1
        now = datetime.utcnow()
        currency = infer_currency(symbol, None, currency)
        sell_price = float(getattr(args, "price", None) or current_price or avg_cost)
        realized_delta = trade_notional_hkd(sell_qty, sell_price - float(avg_cost), currency)
        if sell_qty == qty:
            cur.execute(
                """
                UPDATE positions
                SET status = 'closed',
                    quantity = 0,
                    available_quantity = 0,
                    frozen_quantity = 0,
                    total_cost = 0,
                    market_value = 0,
                    unrealized_pnl = 0,
                    unrealized_pnl_rate = 0,
                    realized_pnl = COALESCE(realized_pnl, 0) + %s,
                    weight = 0,
                    closed_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (realized_delta, now, now, position_id),
            )
            print(f"[OK] Sold ALL {symbol} ({qty} shares). Position CLOSED.")
        else:
            new_qty = qty - sell_qty
            values = position_values(new_qty, float(avg_cost), sell_price, currency)
            cur.execute(
                """
                UPDATE positions
                SET quantity = %s,
                    available_quantity = %s,
                    current_price = %s,
                    total_cost = %s,
                    market_value = %s,
                    unrealized_pnl = %s,
                    unrealized_pnl_rate = %s,
                    realized_pnl = COALESCE(realized_pnl, 0) + %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    new_qty,
                    new_qty,
                    sell_price,
                    values["total_cost"],
                    values["market_value"],
                    values["unrealized_pnl"],
                    values["unrealized_pnl_rate"],
                    realized_delta,
                    now,
                    position_id,
                ),
            )
            print(f"[OK] Sold {sell_qty} x {symbol}. Remaining: {new_qty}")
        if getattr(args, "cash_adjust", True):
            adjust_available_cash(cur, portfolio_id, trade_notional_hkd(sell_qty, sell_price, currency))
        refresh_portfolio_totals(cur, portfolio_id)
        conn.commit()
    finally:
        conn.close()
    return 0


def cmd_delete(args):
    portfolio_id = int(args.portfolio_id)
    symbol = normalize_symbol(args.symbol)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM positions WHERE portfolio_id = %s AND symbol = %s", (portfolio_id, symbol))
        deleted = cur.rowcount
        refresh_portfolio_totals(cur, portfolio_id)
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] Deleted {symbol} ({deleted} rows)")
    return 0


def cmd_list(args):
    portfolio_id = int(args.portfolio_id)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT symbol, quantity, avg_cost, current_price, unrealized_pnl_rate, exchange, status, currency
            FROM positions
            WHERE portfolio_id = %s AND status = 'holding'
            ORDER BY exchange, symbol
            """,
            (portfolio_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    print(f"{'Symbol':<10} {'Qty':>7} {'AvgCost':>10} {'CurPrice':>10} {'PnL%':>8} {'Exch':<7} {'Ccy':<4} {'Status':<8}")
    print("-" * 76)
    for row in rows:
        pnl = float(row[4]) * 100 if row[4] is not None else 0
        print(
            f"{row[0]:<10} {int(row[1]):>7} {float(row[2]):>10.4f} "
            f"{float(row[3] or 0):>10.2f} {'+' if pnl >= 0 else ''}{pnl:>7.1f}% "
            f"{row[5]:<7} {row[7] or '':<4} {row[6]:<8}"
        )
    return 0


def add_portfolio_arg(parser):
    parser.add_argument("--portfolio-id", type=int, default=default_portfolio_id())


def add_cash_adjust_arg(parser):
    parser.add_argument(
        "--no-cash-adjust",
        dest="cash_adjust",
        action="store_false",
        default=True,
        help="Do not change portfolio available_cash; use only when syncing an already-settled broker position.",
    )


def build_parser():
    parser = argparse.ArgumentParser(description="Manual user position manager")
    sub = parser.add_subparsers(dest="command")

    p_buy = sub.add_parser("buy", help="Buy new stock")
    add_portfolio_arg(p_buy)
    p_buy.add_argument("--symbol", required=True)
    p_buy.add_argument("--exchange", required=True, choices=SUPPORTED_EXCHANGES)
    p_buy.add_argument("--qty", required=True, type=positive_int)
    p_buy.add_argument("--cost", required=True, type=positive_float)
    p_buy.add_argument("--currency", default=None)
    p_buy.add_argument("--name", default=None)
    add_cash_adjust_arg(p_buy)

    p_add = sub.add_parser("add", help="Add to existing position")
    add_portfolio_arg(p_add)
    p_add.add_argument("--symbol", required=True)
    p_add.add_argument("--qty", required=True, type=positive_int)
    p_add.add_argument("--cost", required=True, type=positive_float)
    add_cash_adjust_arg(p_add)

    p_sell = sub.add_parser("sell", help="Sell position, partial or full")
    add_portfolio_arg(p_sell)
    p_sell.add_argument("--symbol", required=True)
    p_sell.add_argument("--qty", type=positive_int, default=None, help="Sell qty; omit for all")
    p_sell.add_argument("--price", type=positive_float, default=None, help="Execution price; defaults to current_price")
    add_cash_adjust_arg(p_sell)

    p_delete = sub.add_parser("delete", help="Force delete records")
    add_portfolio_arg(p_delete)
    p_delete.add_argument("--symbol", required=True)

    p_list = sub.add_parser("list", help="List open positions")
    add_portfolio_arg(p_list)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    try:
        return {
            "buy": cmd_buy,
            "add": cmd_add,
            "sell": cmd_sell,
            "delete": cmd_delete,
            "list": cmd_list,
        }[args.command](args)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
