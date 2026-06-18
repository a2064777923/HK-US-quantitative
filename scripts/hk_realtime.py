#!/usr/bin/env python3
"""Realtime HK quote display for DB-sourced holdings."""

import argparse
import os
import re
import sys
import urllib.request
from datetime import datetime

try:
    from read_positions import default_portfolio_id, filter_positions, read_positions
except ImportError:  # pragma: no cover - package import path in tests
    from scripts.read_positions import default_portfolio_id, filter_positions, read_positions


def normalize_hk_symbol(symbol):
    text = str(symbol or "").strip().upper()
    return text.zfill(5) if text.isdigit() and len(text) < 5 else text


def holdings_from_db(portfolio_id):
    rows = filter_positions(read_positions(portfolio_id), "HK")
    holdings = {}
    for row in rows:
        symbol = normalize_hk_symbol(row.get("symbol"))
        holdings[symbol] = {
            "qty": int(row.get("quantity") or 0),
            "cost": float(row.get("avg_cost") or 0),
            "current_price": float(row.get("current_price") or 0),
            "name": row.get("name_cn") or row.get("symbol_name") or symbol,
        }
    return holdings


def get_hk_prices(symbols):
    if not symbols:
        return {}
    codes = ",".join(f"hk{normalize_hk_symbol(symbol)}" for symbol in symbols)
    req = urllib.request.Request(
        f"http://qt.gtimg.cn/q={codes}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"},
    )
    try:
        data = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="ignore")
    except Exception as exc:
        print(f"[ERROR] Tencent API failed: {exc}", file=sys.stderr)
        return {}

    prices = {}
    for line in data.strip().split(";"):
        match = re.search(r'v_hk(\d+)="(.*)"', line.strip())
        if not match:
            continue
        code = match.group(1)
        fields = match.group(2).split("~")
        if len(fields) <= 6:
            continue
        try:
            price = float(fields[3]) if fields[3] else 0
            prev_close = float(fields[4]) if fields[4] else 0
        except (ValueError, IndexError):
            continue
        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
        prices[code] = {"price": price, "change_pct": change_pct, "name": fields[1]}
    return prices


def build_parser():
    parser = argparse.ArgumentParser(description="HK realtime quotes for holdings")
    parser.add_argument("symbols", nargs="*", help="Optional HK symbols")
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
        symbols = [normalize_hk_symbol(symbol) for symbol in args.symbols]
        holdings = {symbol: db_holdings.get(symbol, {"qty": 0, "cost": 0, "current_price": 0, "name": symbol}) for symbol in symbols}
    else:
        holdings = db_holdings
        if not holdings:
            print("[ERROR] No HK holdings found in DB", file=sys.stderr)
            return 1

    prices = get_hk_prices(list(holdings))
    print("=" * 60)
    print(f"  港股即時報價  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"{'股票':<12} {'名稱':<10} {'現價':>8} {'漲跌%':>7} {'數量':>6} {'成本':>8} {'市值':>10} {'盈虧%':>8}")
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
        name = str(quote.get("name") or holding.get("name") or symbol)[:9]
        market_value = price * qty
        pnl_pct = ((price - cost) / cost * 100) if cost and price else 0
        total_value += market_value
        total_cost += cost * qty
        print(
            f"{symbol:<12} {name:<10} {price:>8.3f} "
            f"{'+' if change_pct >= 0 else ''}{change_pct:>6.2f}% "
            f"{qty:>6} {cost:>8.3f} {market_value:>10.0f} "
            f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:>7.1f}%"
        )

    total_pnl_pct = ((total_value - total_cost) / total_cost * 100) if total_cost else 0
    print("-" * 75)
    print(f"{'合計':<12} {'':10} {'':>8} {'':>7} {'':>6} {'':>8} {total_value:>10.0f} {'+' if total_pnl_pct >= 0 else ''}{total_pnl_pct:.1f}%")
    print(f"\n港股總值: HK${total_value:,.0f} (≈ US${total_value / 7.8:,.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
