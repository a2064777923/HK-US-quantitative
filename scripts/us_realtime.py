#!/usr/bin/env python3
"""Realtime US quote display for DB-sourced holdings."""

import argparse
import re
import sys
import urllib.request
from datetime import datetime

try:
    from read_positions import default_portfolio_id, filter_positions, read_positions
except ImportError:  # pragma: no cover - package import path in tests
    from scripts.read_positions import default_portfolio_id, filter_positions, read_positions


def normalize_us_symbol(symbol):
    return str(symbol or "").strip().upper()


def holdings_from_db(portfolio_id):
    rows = filter_positions(read_positions(portfolio_id), "US")
    holdings = {}
    for row in rows:
        symbol = normalize_us_symbol(row.get("symbol"))
        holdings[symbol] = {
            "qty": int(row.get("quantity") or 0),
            "cost": float(row.get("avg_cost") or 0),
            "current_price": float(row.get("current_price") or 0),
            "name": row.get("name_cn") or row.get("symbol_name") or symbol,
        }
    return holdings


def sina_code(symbol):
    return "gb_" + normalize_us_symbol(symbol).lower().replace(".", "_").replace("-", "_")


def get_us_prices(symbols):
    if not symbols:
        return {}
    code_to_symbol = {sina_code(symbol)[3:]: normalize_us_symbol(symbol) for symbol in symbols}
    codes = ",".join(f"gb_{code}" for code in code_to_symbol)
    req = urllib.request.Request(
        f"http://hq.sinajs.cn/list={codes}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
    )
    try:
        data = urllib.request.urlopen(req, timeout=10).read().decode("gb18030", errors="ignore")
    except Exception as exc:
        print(f"[ERROR] Sina API failed: {exc}", file=sys.stderr)
        return {}

    prices = {}
    for line in data.strip().splitlines():
        match = re.match(r'var hq_str_gb_([a-z0-9_]+)="(.*)"', line.strip(), re.IGNORECASE)
        if not match:
            continue
        raw_code = match.group(1).lower()
        symbol = code_to_symbol.get(raw_code, raw_code.upper().replace("_", "."))
        fields = match.group(2).split(",")
        if len(fields) < 3:
            continue
        try:
            price = float(fields[1]) if fields[1] else 0
            change_pct = float(fields[2]) if fields[2] else 0
        except (ValueError, IndexError):
            continue
        prices[symbol] = {"price": price, "change_pct": change_pct, "name": fields[0]}
    return prices


def build_parser():
    parser = argparse.ArgumentParser(description="US realtime quotes for holdings")
    parser.add_argument("symbols", nargs="*", help="Optional US symbols")
    parser.add_argument("--portfolio-id", type=int, default=default_portfolio_id())
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    db_holdings = {}
    try:
        db_holdings = holdings_from_db(args.portfolio_id)
    except Exception as exc:
        if not args.symbols:
            print(f"[ERROR] DB read failed: {exc}", file=sys.stderr)
            return 2
        print(f"[WARN] DB read failed: {exc}", file=sys.stderr)

    if args.symbols:
        symbols = [normalize_us_symbol(symbol) for symbol in args.symbols]
        holdings = {symbol: db_holdings.get(symbol, {"qty": 0, "cost": 0, "current_price": 0, "name": symbol}) for symbol in symbols}
    else:
        holdings = db_holdings
        if not holdings:
            print("[ERROR] No US holdings found in DB", file=sys.stderr)
            return 1

    prices = get_us_prices(list(holdings))
    print("=" * 60)
    print(f"  美股即時報價  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"{'股票':<10} {'名稱':<15} {'現價':>8} {'漲跌%':>7} {'數量':>5} {'成本':>8} {'市值':>10} {'盈虧%':>8}")
    print("-" * 75)

    total_value = 0.0
    total_cost = 0.0
    for symbol in sorted(holdings):
        holding = holdings[symbol]
        quote = prices.get(symbol, {})
        price = float(quote.get("price") or holding.get("current_price") or 0)
        change_pct = float(quote.get("change_pct") or 0)
        qty = int(holding.get("qty") or 0)
        cost = float(holding.get("cost") or 0)
        name = str(quote.get("name") or holding.get("name") or symbol)[:14]
        market_value = price * qty
        pnl_pct = ((price - cost) / cost * 100) if cost and price else 0
        total_value += market_value
        total_cost += cost * qty
        print(
            f"{symbol:<10} {name:<15} {price:>8.2f} "
            f"{'+' if change_pct >= 0 else ''}{change_pct:>6.2f}% "
            f"{qty:>5} {cost:>8.2f} {market_value:>10.2f} "
            f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:>7.1f}%"
        )

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
    print("-" * 75)
    print(f"{'合計':<10} {'':15} {'':>8} {'':>7} {'':>5} {'':>8} {total_value:>10.2f} {'+' if total_pnl_pct >= 0 else ''}{total_pnl_pct:.1f}%")
    print(f"\n總市值: ${total_value:,.2f} USD (≈ HK${total_value * 7.8:,.0f})")
    print(f"總成本: ${total_cost:,.2f} USD | 盈虧: ${total_pnl:+,.2f} ({'+' if total_pnl_pct >= 0 else ''}{total_pnl_pct:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
