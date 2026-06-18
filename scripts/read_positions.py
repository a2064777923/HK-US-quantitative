#!/usr/bin/env python3
"""Read user holdings from the positions table.

This is the small DB-facing helper used by realtime holding checks. The
positions table is the source of truth; callers should not carry their own
hardcoded holding lists.
"""

import argparse
import csv
import io
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal

try:
    import psycopg2
except ImportError:  # pragma: no cover - runtime environment check
    psycopg2 = None


US_EXCHANGES = {"US", "NASDAQ", "NYSE", "AMEX"}
HK_EXCHANGES = {"HK", "HKEX", "SEHK", "SZSE", "SSE"}


def default_portfolio_id():
    for name in ("QM_HOLDINGS_PORTFOLIO_ID", "QM_USER_PORTFOLIO_ID", "QM_USER_PORTFOLIO_IDS"):
        raw = os.environ.get(name, "")
        for item in str(raw).split(","):
            item = item.strip()
            if item.isdigit():
                return int(item)
    return 3


def get_connection():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")
    url = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(url, connect_timeout=5)


def json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def read_positions(portfolio_id=None, statuses=("holding",)):
    portfolio_id = default_portfolio_id() if portfolio_id is None else int(portfolio_id)
    statuses = tuple(statuses or ("holding",))
    placeholders = ", ".join(["%s"] * len(statuses))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT p.symbol, p.symbol_name, p.exchange, p.side,
                   p.quantity, p.avg_cost, p.current_price,
                   p.market_value, p.unrealized_pnl, p.unrealized_pnl_rate,
                   p.weight, p.currency, COALESCE(s.name, p.symbol_name) AS name_cn,
                   p.updated_at
            FROM positions p
            LEFT JOIN stocks s ON p.symbol = s.symbol
            WHERE p.portfolio_id = %s
              AND p.status IN ({placeholders})
              AND COALESCE(p.quantity, 0) > 0
            ORDER BY p.exchange, p.symbol
            """,
            (portfolio_id, *statuses),
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def normalize_symbol(symbol):
    return str(symbol or "").strip().upper()


def is_hk_symbol(symbol):
    symbol = normalize_symbol(symbol)
    return symbol.isdigit() and len(symbol) == 5


def position_market(row):
    exchange = normalize_symbol(row.get("exchange"))
    symbol = normalize_symbol(row.get("symbol"))
    if exchange in HK_EXCHANGES or is_hk_symbol(symbol):
        return "HK"
    if exchange in US_EXCHANGES:
        return "US"
    return "HK" if is_hk_symbol(symbol) else "US"


def filter_positions(rows, market=None):
    market = normalize_symbol(market)
    if market not in {"HK", "US"}:
        return list(rows)
    return [row for row in rows if position_market(row) == market]


def format_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["symbol", "name", "exchange", "qty", "avg_cost", "currency"])
    for row in rows:
        writer.writerow(
            [
                row.get("symbol"),
                row.get("name_cn") or row.get("symbol_name") or row.get("symbol"),
                row.get("exchange"),
                row.get("quantity"),
                row.get("avg_cost"),
                row.get("currency"),
            ]
        )
    return out.getvalue().rstrip()


def format_summary(rows):
    lines = []
    for row in rows:
        try:
            pnl_pct = float(row.get("unrealized_pnl_rate") or 0) * 100
        except (TypeError, ValueError):
            pnl_pct = 0.0
        sign = "+" if pnl_pct >= 0 else ""
        lines.append(
            f"{row.get('symbol')}({row.get('name_cn') or row.get('symbol_name') or ''}) "
            f"{row.get('quantity')}股@{row.get('avg_cost')} | "
            f"現價{row.get('current_price')} | {sign}{pnl_pct:.1f}%"
        )
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description="Read holdings from DB positions")
    parser.add_argument("--us", action="store_true", help="US holdings only")
    parser.add_argument("--hk", action="store_true", help="HK holdings only")
    parser.add_argument("--format", choices=["json", "csv", "summary"], default="json")
    parser.add_argument("--summary", action="store_true", help="Shortcut for --format summary")
    parser.add_argument("--portfolio-id", type=int, default=default_portfolio_id())
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    output_format = "summary" if args.summary else args.format
    try:
        rows = read_positions(args.portfolio_id)
    except Exception as exc:
        print(f"DB Error: {exc}", file=sys.stderr)
        return 2

    if args.us:
        rows = filter_positions(rows, "US")
    elif args.hk:
        rows = filter_positions(rows, "HK")

    if not rows:
        print("[]")
        return 1

    if output_format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=json_default))
    elif output_format == "csv":
        print(format_csv(rows))
    else:
        print(format_summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
